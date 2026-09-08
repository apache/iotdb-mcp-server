#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from iotdb.Session import Session
from iotdb.table_session import TableSession
from iotdb.utils.SessionDataSet import SessionDataSet
from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.result_store import result_store_from_export_path
from iotdb_mcp_server.result_store import StoredCsvResult
from iotdb_mcp_server.runtime_policy import dynamic_env_bool
from iotdb_mcp_server.runtime_policy import dynamic_getenv
from iotdb_mcp_server.runtime_policy import dynamic_policy_snapshot
from iotdb_mcp_server.runtime_policy import permission_enforcement_mode
from iotdb_mcp_server.runtime_policy import strict_permission_enforcement
from iotdb_mcp_server.services.json_response import (
    csv_result_payload_response,
    payload_response,
)
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    select_target_config,
    table_session_pool,
    tree_session_pool,
)
from iotdb_mcp_server.services.tree_sql_guardrails import assert_tree_query_shape
from iotdb_mcp_server.services.tree_sql_guardrails import (
    tree_from_wildcard_runtime_hint,
)

_READONLY_PREFIXES_COMMON = (
    "SELECT",
    "SHOW",
    "EXPLAIN",
    "DESC",
    "DESCRIBE",
)
_READONLY_PREFIXES_TREE = (
    "COUNT TIMESERIES",
    "COUNT NODES",
    "COUNT DEVICES",
    "CALL INFERENCE",
    "SHOW CONTINUOUS QUERIES",
    "SHOW CQS",
)
_READONLY_PREFIXES_TABLE: tuple[str, ...] = ()

_DDL_PREFIXES_COMMON = (
    "CREATE DATABASE",
    "ALTER DATABASE",
    "DROP DATABASE",
    "CREATE TABLE",
    "ALTER TABLE",
    "DROP TABLE",
    "USE",
    "SET TTL",
    "UNSET TTL",
    "CREATE MODEL",
    "DROP MODEL",
    "LOAD MODEL",
    "UNLOAD MODEL",
    "REMOVE AINODE",
)
_DDL_PREFIXES_TREE = (
    "CREATE TIMESERIES",
    "CREATE ALIGNED TIMESERIES",
    "ALTER TIMESERIES",
    "DROP TIMESERIES",
    "DELETE TIMESERIES",
    "CREATE CONTINUOUS QUERY",
    "CREATE CQ",
    "DROP CONTINUOUS QUERY",
    "DROP CQ",
)
_DDL_PREFIXES_TABLE: tuple[str, ...] = ()

_FULL_PREFIXES_COMMON = (
    "INSERT INTO",
    "UPDATE",
    "DELETE FROM",
    "DELETE DEVICES",
)
_FULL_PREFIXES_TREE: tuple[str, ...] = ()
_FULL_PREFIXES_TABLE: tuple[str, ...] = ()

_DESTRUCTIVE_PREFIXES = (
    "DROP ",
    "DELETE ",
    "UNSET TTL",
    "REMOVE AINODE",
    "UNLOAD MODEL",
)

_PERMISSION_RANK = {"readonly": 0, "ddl": 1, "full": 2}
_TEMPLATE_FIELD_PATTERN = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)(?::(literal|path|identifier))?\s*\}\}"
)
_PATH_SEGMENT_PATTERN = re.compile(r"^([A-Za-z0-9_]+|\*|\*\*)$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_BATCH_PER_ITEM_TIMEOUT_MS = 60_000
_DEFAULT_BATCH_TIMEOUT_MS = 300_000
_DEFAULT_BATCH_MAX_ROWS_PER_ITEM = 10_000
_DEFAULT_BATCH_MAX_BYTES_PER_ITEM = 16 * 1024 * 1024
_DEFAULT_BATCH_MAX_TOTAL_ROWS = 100_000
_DEFAULT_BATCH_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_DEFAULT_BATCH_WORKER_POOL_SIZE = 4


def _env_bool(name: str, default: bool) -> bool:
    return dynamic_env_bool(name, default)


def _env_int(
    name: str, default: int, minimum: int = 1, maximum: int | None = None
) -> int:
    raw = (dynamic_getenv(name, "") or os.getenv(name, "") or "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _positive_int(
    value: int | None,
    *,
    env_name: str,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    if value is None:
        return _env_int(env_name, default, minimum=minimum, maximum=maximum)
    resolved = max(minimum, int(value))
    if maximum is not None:
        resolved = min(maximum, resolved)
    return resolved


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _parse_mode() -> str:
    mode = (
        (dynamic_getenv("IOTDB_SQL_DRIVER_MODE", "readonly") or "readonly")
        .strip()
        .lower()
    )
    if mode not in ("readonly", "ddl", "full"):
        raise ValueError(
            "Invalid IOTDB_SQL_DRIVER_MODE. Expected one of: readonly, ddl, full."
        )
    return mode


def _strip_single_trailing_semicolon(sql: str, empty_message: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError(empty_message)

    quote: str | None = None
    escaped = False
    semicolon_indexes: list[int] = []
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote:
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                if index + 1 < len(cleaned) and cleaned[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == ";":
            semicolon_indexes.append(index)
        index += 1

    if not semicolon_indexes:
        return cleaned
    if semicolon_indexes == [len(cleaned) - 1]:
        return cleaned[:-1].strip()
    raise ValueError("Only a single SQL statement is allowed.")


def _normalize_sql(sql: str) -> str:
    return _strip_single_trailing_semicolon(sql, "SQL cannot be empty.")


def _normalize_for_prefix(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().upper()


def _top_level_sql_keywords(sql: str) -> list[str]:
    """Return unquoted SQL words outside parentheses and comments."""
    keywords: list[str] = []
    token: list[str] = []
    quote: str | None = None
    depth = 0
    index = 0

    def flush_token() -> None:
        if token:
            keywords.append("".join(token).upper())
            token.clear()

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if quote is not None:
            if char == quote:
                if next_char == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\":
                index += 2
                continue
            index += 1
            continue

        if char in {"'", '"', "`"}:
            flush_token()
            quote = char
            index += 1
            continue
        if char == "-" and next_char == "-":
            flush_token()
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if char == "/" and next_char == "*":
            flush_token()
            comment_end = sql.find("*/", index + 2)
            index = len(sql) if comment_end < 0 else comment_end + 2
            continue
        if char == "(":
            flush_token()
            depth += 1
            index += 1
            continue
        if char == ")":
            flush_token()
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and (char.isalnum() or char == "_"):
            token.append(char)
        else:
            flush_token()
        index += 1

    flush_token()
    return keywords


def _select_writes_results(sql: str) -> bool:
    keywords = _top_level_sql_keywords(sql)
    return bool(keywords and keywords[0] == "SELECT" and "INTO" in keywords[1:])


def _matches_prefix(normalized_upper: str, prefix: str) -> bool:
    if not normalized_upper.startswith(prefix):
        return False
    return (
        len(normalized_upper) == len(prefix)
        or normalized_upper[len(prefix)].isspace()
        or normalized_upper[len(prefix)] == "("
    )


def _merge_prefixes(base: tuple[str, ...], env_var: str) -> tuple[str, ...]:
    extra = tuple(
        item.strip().upper()
        for item in (dynamic_getenv(env_var, "") or "").split(",")
        if item.strip()
    )
    all_prefixes = tuple(dict.fromkeys([*base, *extra]))
    return tuple(sorted(all_prefixes, key=len, reverse=True))


def _resolve_whitelists(sql_dialect: str) -> dict[str, tuple[str, ...]]:
    readonly = _READONLY_PREFIXES_COMMON + (
        _READONLY_PREFIXES_TREE if sql_dialect == "tree" else _READONLY_PREFIXES_TABLE
    )
    ddl = _DDL_PREFIXES_COMMON + (
        _DDL_PREFIXES_TREE if sql_dialect == "tree" else _DDL_PREFIXES_TABLE
    )
    full = _FULL_PREFIXES_COMMON + (
        _FULL_PREFIXES_TREE if sql_dialect == "tree" else _FULL_PREFIXES_TABLE
    )

    return {
        "readonly": _merge_prefixes(
            readonly, "IOTDB_SQL_DRIVER_EXTRA_READONLY_PREFIXES"
        ),
        "ddl": _merge_prefixes(ddl, "IOTDB_SQL_DRIVER_EXTRA_DDL_PREFIXES"),
        "full": _merge_prefixes(full, "IOTDB_SQL_DRIVER_EXTRA_FULL_PREFIXES"),
    }


def _classify_sql(sql: str, whitelists: dict[str, tuple[str, ...]]) -> tuple[str, str]:
    normalized_upper = _normalize_for_prefix(sql)

    if _select_writes_results(sql):
        return "full", "SELECT INTO"

    for category in ("readonly", "ddl", "full"):
        for prefix in whitelists[category]:
            if _matches_prefix(normalized_upper, prefix):
                return category, prefix

    raise ValueError(
        "SQL is not in whitelist. Allowed categories and prefixes can be inspected "
        "with sql_driver_policy tool."
    )


def _is_destructive_prefix(prefix: str) -> bool:
    upper_prefix = prefix.upper()
    return upper_prefix.startswith(_DESTRUCTIVE_PREFIXES)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Non-finite float values are not valid SQL literals.")
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _validate_iotdb_path(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("Path placeholder value cannot be empty.")
    if any(char in text for char in (";", "'", '"', "`", "\\", "\n", "\r", "\t", " ")):
        raise ValueError(f"Unsafe IoTDB path placeholder value: {text!r}")
    parts = text.split(".")
    if not parts or any(not _PATH_SEGMENT_PATTERN.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid IoTDB path placeholder value: {text!r}")
    return text


def _validate_identifier(value: Any) -> str:
    text = str(value).strip()
    if not _IDENTIFIER_PATTERN.fullmatch(text):
        raise ValueError(f"Invalid SQL identifier placeholder value: {text!r}")
    return text


def _render_sql_template(sql_template: str, params: dict[str, Any]) -> str:
    template = str(sql_template or "").strip()
    if not template:
        raise ValueError("sql_template cannot be empty.")
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        kind = match.group(2) or "literal"
        if name not in params:
            raise ValueError(f"Missing SQL template parameter: {name}")
        seen.add(name)
        value = params[name]
        if kind == "literal":
            return _sql_literal(value)
        if kind == "path":
            return _validate_iotdb_path(value)
        if kind == "identifier":
            return _validate_identifier(value)
        raise ValueError(f"Unsupported SQL template placeholder kind: {kind}")

    rendered = _TEMPLATE_FIELD_PATTERN.sub(replace, template)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError(
            "Invalid sql_template placeholder. Use {{name}}, {{name:path}}, or {{name:identifier}}."
        )
    if not seen:
        raise ValueError(
            "sql_template must contain at least one placeholder such as {{device:path}}."
        )
    return _normalize_sql(rendered)


def _resolve_batch_sqls(
    sqls: list[str] | None,
    sql_template: str | None,
    param_sets: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    direct_sqls = [str(sql) for sql in (sqls or []) if str(sql).strip()]
    has_template = bool(sql_template and str(sql_template).strip())
    has_param_sets = bool(param_sets)

    if direct_sqls and (has_template or has_param_sets):
        raise ValueError("Use either sqls or sql_template+param_sets, not both.")
    if direct_sqls:
        return [
            {"index": index, "sql": _normalize_sql(sql), "params": None}
            for index, sql in enumerate(direct_sqls)
        ]

    if has_template or has_param_sets:
        if not has_template:
            raise ValueError("sql_template is required when param_sets is provided.")
        if not has_param_sets:
            raise ValueError("param_sets must contain at least one parameter set.")
        statements: list[dict[str, Any]] = []
        for index, params in enumerate(param_sets or []):
            if not isinstance(params, dict):
                raise ValueError("Each param_sets item must be an object.")
            statements.append(
                {
                    "index": index,
                    "sql": _render_sql_template(str(sql_template), params),
                    "params": dict(params),
                }
            )
        return statements

    raise ValueError("Provide either sqls or sql_template+param_sets.")


def _mode_satisfies(required_category: str, mode: str) -> bool:
    return _PERMISSION_RANK[mode] >= _PERMISSION_RANK[required_category]


def _sql_permission_payload(
    sql: str,
    selected_config: Config,
    whitelists: dict[str, tuple[str, ...]],
    mode: str,
) -> dict[str, object]:
    normalized_sql = _normalize_sql(sql)
    category, matched_prefix = _classify_sql(normalized_sql, whitelists)
    destructive = _is_destructive_prefix(matched_prefix)
    destructive_confirmation_required = destructive and _env_bool(
        "IOTDB_SQL_DRIVER_REQUIRE_DESTRUCTIVE_CONFIRM", True
    )
    current_satisfies = _mode_satisfies(category, mode)
    approval_required = destructive_confirmation_required or not current_satisfies
    approval_reasons: list[str] = []
    if not current_satisfies:
        approval_reasons.append(
            f"current advisory permission is '{mode}', SQL requires '{category}'"
        )
    if destructive_confirmation_required:
        approval_reasons.append("SQL is destructive and requires explicit confirmation")

    return {
        "sql": normalized_sql,
        "sql_dialect": selected_config.sql_dialect,
        "matched_prefix": matched_prefix,
        "required_permission": category,
        "current_advisory_permission": mode,
        "current_permission_satisfies": current_satisfies,
        "permission_enforcement": permission_enforcement_mode(),
        "strict_enforcement": strict_permission_enforcement(),
        "destructive": destructive,
        "risk_level": (
            "high" if destructive else ("medium" if category != "readonly" else "low")
        ),
        "confirmation_parameter": (
            "confirm_destructive" if destructive_confirmation_required else None
        ),
        "approval_required": approval_required,
        "approval_reasons": approval_reasons,
        "agent_approval_protocol": {
            "owner": "host agent system",
            "before_execution": (
                "If approval_required is true, ask the user through the host agent "
                "question/approval mechanism before executing."
            ),
            "after_user_approval": (
                "Call sql_execute with confirm_destructive=true when the "
                "confirmation_parameter is confirm_destructive."
            ),
            "do_not_use_environment_as_primary_gate": True,
        },
    }


def _assert_sql_driver_permission(
    config: Config,
    required_category: str,
    mode: str,
    confirm_destructive: bool,
    matched_prefix: str,
) -> None:
    if strict_permission_enforcement():
        if not _env_bool("IOTDB_ENABLE_SQL_DRIVER", False):
            raise PermissionError(
                "SQL driver tool is disabled by strict MCP policy. "
                "Set IOTDB_ENABLE_SQL_DRIVER=true to enable."
            )

        allowed_users = _csv_set(
            dynamic_getenv("IOTDB_SQL_DRIVER_ALLOWED_USERS", "root") or "root"
        )
        if "*" not in allowed_users and config.user not in allowed_users:
            raise PermissionError(
                f"Current MCP user '{config.user}' is not allowed by IOTDB_SQL_DRIVER_ALLOWED_USERS."
            )

        if not _mode_satisfies(required_category, mode):
            raise PermissionError(
                f"Current sql driver mode '{mode}' does not allow SQL requiring "
                f"'{required_category}' permission ('{matched_prefix}')."
            )

    if (
        _is_destructive_prefix(matched_prefix)
        and _env_bool("IOTDB_SQL_DRIVER_REQUIRE_DESTRUCTIVE_CONFIRM", True)
        and not confirm_destructive
    ):
        raise PermissionError(
            "Destructive SQL requires confirm_destructive=True by server policy."
        )


def _format_tree_result(
    res: SessionDataSet,
    session: Session,
    tool_name: str,
    export_path: str,
    sql: str | None = None,
    owner_session_id: str | None = None,
    max_inline_rows: int | None = None,
    page_size_rows: int | None = None,
) -> list[TextContent]:
    columns = res.get_column_names()

    def rows():
        while res.has_next():
            record = res.next()
            if columns and columns[0] == "Time":
                timestamp = record.get_timestamp()
                row = record.get_fields()
                yield str(timestamp) + "," + ",".join(map(str, row))
            else:
                yield ",".join(map(str, record.get_fields()))

    def diagnostics_factory(row_count: int):
        diagnostics = []
        if sql:
            hint = tree_from_wildcard_runtime_hint(sql, row_count=row_count)
            if hint:
                diagnostics.append(hint)
        return diagnostics

    try:
        return csv_result_payload_response(
            tool_name,
            columns,
            rows(),
            result_store=result_store_from_export_path(export_path),
            source={"sql": sql} if sql else None,
            diagnostics_factory=diagnostics_factory,
            owner_session_id=owner_session_id,
            max_inline_rows=max_inline_rows,
            page_size_rows=page_size_rows,
        )
    finally:
        session.close()


def _format_table_result(
    res: SessionDataSet,
    table_session: TableSession,
    tool_name: str,
    export_path: str,
    sql: str | None = None,
    owner_session_id: str | None = None,
    max_inline_rows: int | None = None,
    page_size_rows: int | None = None,
) -> list[TextContent]:
    columns = res.get_column_names()

    def rows():
        while res.has_next():
            yield ",".join(map(str, res.next().get_fields()))

    try:
        return csv_result_payload_response(
            tool_name,
            columns,
            rows(),
            result_store=result_store_from_export_path(export_path),
            source={"sql": sql} if sql else None,
            owner_session_id=owner_session_id,
            max_inline_rows=max_inline_rows,
            page_size_rows=page_size_rows,
        )
    finally:
        table_session.close()


def _tree_csv_rows(res: SessionDataSet, columns: list[str]):
    while res.has_next():
        record = res.next()
        if columns and columns[0] == "Time":
            timestamp = record.get_timestamp()
            row = record.get_fields()
            yield str(timestamp) + "," + ",".join(map(str, row))
        else:
            yield ",".join(map(str, record.get_fields()))


def _table_csv_rows(res: SessionDataSet):
    while res.has_next():
        yield ",".join(map(str, res.next().get_fields()))


class BatchQuotaExceeded(RuntimeError):
    pass


class BatchItemTimeout(TimeoutError):
    pass


class BatchTimeout(TimeoutError):
    pass


@dataclass
class _ResultQuota:
    label: str
    max_rows: int
    max_bytes: int
    rows: int = 0
    bytes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def consume(self, row: str) -> None:
        row_bytes = len(row.encode("utf-8")) + 1
        with self.lock:
            next_rows = self.rows + 1
            next_bytes = self.bytes + row_bytes
            if next_rows > self.max_rows:
                raise BatchQuotaExceeded(
                    f"{self.label} row quota exceeded: {next_rows}>{self.max_rows}"
                )
            if next_bytes > self.max_bytes:
                raise BatchQuotaExceeded(
                    f"{self.label} byte quota exceeded: {next_bytes}>{self.max_bytes}"
                )
            self.rows = next_rows
            self.bytes = next_bytes


def _quota_csv_rows(rows, *quotas: _ResultQuota):
    for row in rows:
        for quota in quotas:
            quota.consume(row)
        yield row


def _stored_result_payload(
    *,
    index: int,
    sql: str,
    stored: StoredCsvResult,
    columns: list[str],
    duration_ms: int,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    preview_rows = stored.preview_rows
    inline_truncated = stored.row_count > len(preview_rows)
    payload: dict[str, Any] = {
        "index": index,
        "ok": True,
        "status": "success",
        "sql": sql,
        "format": "csv",
        "columns": columns,
        "rows": preview_rows,
        "row_count": stored.row_count,
        "inline_row_count": len(preview_rows),
        "inline_truncated": inline_truncated,
        "result_id": stored.result_id,
        "owner_session_id": stored.owner_session_id,
        "next_cursor": str(len(preview_rows)) if inline_truncated else None,
        "duration_ms": duration_ms,
        "result_store": {
            "type": "file",
            "result_id": stored.result_id,
            "owner_session_id": stored.owner_session_id,
            "page_tool": "read_result_page",
            "batch_page_tool": "read_result_pages",
            "page_size_rows": stored.page_size_rows,
            "manifest_path": stored.manifest_path,
            "shard_count": stored.shard_count,
            "byte_count": stored.byte_count,
        },
    }
    if diagnostics:
        payload["diagnostics"] = diagnostics
    return payload


def _batch_error_payload(index: int, sql: str, exc: BaseException) -> dict[str, Any]:
    return {
        "index": index,
        "ok": False,
        "status": "error",
        "sql": sql,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _batch_skipped_payload(index: int, sql: str) -> dict[str, Any]:
    return {
        "index": index,
        "ok": False,
        "status": "skipped",
        "sql": sql,
        "error_type": "BatchAborted",
        "error": "Skipped because continue_on_error is false and an earlier item failed.",
    }


def _store_tree_batch_item(
    *,
    session_pool: Any,
    selected_config: Config,
    index: int,
    sql: str,
    batch_id: str,
    owner_session_id: str | None,
    max_inline_rows: int | None,
    page_size_rows: int | None,
    item_quota: _ResultQuota,
    batch_quota: _ResultQuota,
) -> dict[str, Any]:
    started = time.monotonic()
    session = None
    try:
        session = session_pool.get_session()
        res = session.execute_query_statement(sql)
        columns = res.get_column_names()
        store = result_store_from_export_path(selected_config.export_path)
        stored = store.write_csv_result(
            tool="sql_executor_batch",
            columns=columns,
            rows=_quota_csv_rows(_tree_csv_rows(res, columns), item_quota, batch_quota),
            source={"sql": sql, "batch_id": batch_id, "index": index},
            owner_session_id=owner_session_id,
            preview_rows=max_inline_rows,
            page_size_rows=page_size_rows,
        )
        diagnostics = []
        hint = tree_from_wildcard_runtime_hint(sql, row_count=stored.row_count)
        if hint:
            diagnostics.append(hint)
        return _stored_result_payload(
            index=index,
            sql=sql,
            stored=stored,
            columns=columns,
            duration_ms=int((time.monotonic() - started) * 1000),
            diagnostics=diagnostics,
        )
    finally:
        if session is not None:
            session.close()


def _store_table_batch_item(
    *,
    session_pool: Any,
    selected_config: Config,
    index: int,
    sql: str,
    batch_id: str,
    owner_session_id: str | None,
    max_inline_rows: int | None,
    page_size_rows: int | None,
    item_quota: _ResultQuota,
    batch_quota: _ResultQuota,
) -> dict[str, Any]:
    started = time.monotonic()
    table_session = None
    try:
        table_session = session_pool.get_session()
        res = table_session.execute_query_statement(sql)
        columns = res.get_column_names()
        store = result_store_from_export_path(selected_config.export_path)
        stored = store.write_csv_result(
            tool="sql_executor_batch",
            columns=columns,
            rows=_quota_csv_rows(_table_csv_rows(res), item_quota, batch_quota),
            source={"sql": sql, "batch_id": batch_id, "index": index},
            owner_session_id=owner_session_id,
            preview_rows=max_inline_rows,
            page_size_rows=page_size_rows,
        )
        return _stored_result_payload(
            index=index,
            sql=sql,
            stored=stored,
            columns=columns,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if table_session is not None:
            table_session.close()


def register_sql_driver_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register generic SQL execution tools with advisory permission metadata."""
    logger.info("SQL driver registered; mode is resolved dynamically per tool call")

    @mcp.tool()
    async def sql_driver_policy(
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Show current sql_driver policy and statement whitelists."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        mode = _parse_mode()
        whitelists = _resolve_whitelists(selected_config.sql_dialect)
        with iotdb_target_response_context(selected_config):
            return payload_response(
                "sql_driver_policy",
                {
                    "sql_dialect": selected_config.sql_dialect,
                    "mode": mode,
                    "advisory_permission": mode,
                    "enable_sql_driver": _env_bool("IOTDB_ENABLE_SQL_DRIVER", True),
                    "permission_enforcement": permission_enforcement_mode(),
                    "strict_enforcement": strict_permission_enforcement(),
                    "permission_model": {
                        "readonly": "read-only SQL such as SELECT, SHOW, DESC, EXPLAIN",
                        "ddl": "schema/model/TTL management SQL",
                        "full": "DML or data-changing SQL such as INSERT, UPDATE, DELETE",
                        "host_agent_approval": (
                            "MCP reports required_permission and risk. The host agent "
                            "asks the user through its question/approval mechanism before "
                            "executing risky SQL."
                        ),
                    },
                    "require_destructive_confirm": _env_bool(
                        "IOTDB_SQL_DRIVER_REQUIRE_DESTRUCTIVE_CONFIRM", True
                    ),
                    "runtime_policy": dynamic_policy_snapshot(),
                    "whitelists": whitelists,
                },
                message="SQL driver policy snapshot.",
            )

    @mcp.tool()
    async def inspect_sql_permission(
        sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Inspect SQL permission, risk, and approval requirements without executing it."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        mode = _parse_mode()
        whitelists = _resolve_whitelists(selected_config.sql_dialect)
        with iotdb_target_response_context(selected_config):
            return payload_response(
                "inspect_sql_permission",
                _sql_permission_payload(sql, selected_config, whitelists, mode),
                message="SQL permission inspection completed.",
            )

    @mcp.tool()
    async def sql_executor_batch(
        sqls: list[str] | None = None,
        sql_template: str | None = None,
        param_sets: list[dict[str, object]] | None = None,
        max_concurrency: int = 4,
        worker_pool_size: int | None = None,
        per_item_timeout_ms: int | None = None,
        batch_timeout_ms: int | None = None,
        max_result_rows_per_item: int | None = None,
        max_result_bytes_per_item: int | None = None,
        max_batch_result_rows: int | None = None,
        max_batch_result_bytes: int | None = None,
        continue_on_error: bool = True,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = 5,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute many readonly SQL statements in parallel and store each result.

        Use either `sqls=[...]` for explicit single statements or
        `sql_template` with `param_sets` to generate repeated SELECT/SHOW
        statements without asking the model to spell out each SQL. Template
        placeholders are `{{name}}` for SQL literals, `{{name:path}}` for IoTDB
        paths, and `{{name:identifier}}` for SQL identifiers.
        """
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        mode = _parse_mode()
        whitelists = _resolve_whitelists(selected_config.sql_dialect)
        max_statements = _env_int(
            "IOTDB_SQL_EXECUTOR_BATCH_MAX_STATEMENTS", 64, minimum=1, maximum=1000
        )
        max_allowed_concurrency = _env_int(
            "IOTDB_SQL_EXECUTOR_BATCH_MAX_CONCURRENCY", 16, minimum=1, maximum=128
        )
        max_allowed_workers = _env_int(
            "IOTDB_SQL_EXECUTOR_BATCH_MAX_WORKER_POOL_SIZE",
            16,
            minimum=1,
            maximum=128,
        )
        per_item_timeout_ms = _positive_int(
            per_item_timeout_ms,
            env_name="IOTDB_SQL_EXECUTOR_BATCH_PER_ITEM_TIMEOUT_MS",
            default=_DEFAULT_BATCH_PER_ITEM_TIMEOUT_MS,
            maximum=3_600_000,
        )
        batch_timeout_ms = _positive_int(
            batch_timeout_ms,
            env_name="IOTDB_SQL_EXECUTOR_BATCH_TIMEOUT_MS",
            default=_DEFAULT_BATCH_TIMEOUT_MS,
            maximum=24 * 3_600_000,
        )
        max_result_rows_per_item = _positive_int(
            max_result_rows_per_item,
            env_name="IOTDB_SQL_EXECUTOR_BATCH_MAX_RESULT_ROWS_PER_ITEM",
            default=_DEFAULT_BATCH_MAX_ROWS_PER_ITEM,
            maximum=10_000_000,
        )
        max_result_bytes_per_item = _positive_int(
            max_result_bytes_per_item,
            env_name="IOTDB_SQL_EXECUTOR_BATCH_MAX_RESULT_BYTES_PER_ITEM",
            default=_DEFAULT_BATCH_MAX_BYTES_PER_ITEM,
            maximum=10 * 1024 * 1024 * 1024,
        )
        max_batch_result_rows = _positive_int(
            max_batch_result_rows,
            env_name="IOTDB_SQL_EXECUTOR_BATCH_MAX_TOTAL_RESULT_ROWS",
            default=_DEFAULT_BATCH_MAX_TOTAL_ROWS,
            maximum=100_000_000,
        )
        max_batch_result_bytes = _positive_int(
            max_batch_result_bytes,
            env_name="IOTDB_SQL_EXECUTOR_BATCH_MAX_TOTAL_RESULT_BYTES",
            default=_DEFAULT_BATCH_MAX_TOTAL_BYTES,
            maximum=100 * 1024 * 1024 * 1024,
        )
        statements = _resolve_batch_sqls(
            sqls,
            sql_template,
            param_sets,
        )
        if len(statements) > max_statements:
            raise ValueError(
                f"sql_executor_batch accepts at most {max_statements} statements."
            )

        for item in statements:
            normalized_sql = item["sql"]
            category, matched_prefix = _classify_sql(normalized_sql, whitelists)
            if category != "readonly":
                raise PermissionError(
                    "sql_executor_batch only supports readonly SQL. "
                    f"Statement {item['index']} requires '{category}' permission."
                )
            _assert_sql_driver_permission(
                selected_config,
                required_category=category,
                mode=mode,
                confirm_destructive=False,
                matched_prefix=matched_prefix,
            )
            if (
                selected_config.sql_dialect == "tree"
                and normalized_sql.upper().startswith("SELECT")
            ):
                assert_tree_query_shape(normalized_sql)
            item["matched_prefix"] = matched_prefix

        batch_id = (
            "batch_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:12]
        )
        concurrency = min(
            max(1, int(max_concurrency or 1)),
            max_allowed_concurrency,
            len(statements),
        )
        workers = min(
            _positive_int(
                worker_pool_size,
                env_name="IOTDB_SQL_EXECUTOR_BATCH_WORKER_POOL_SIZE",
                default=max(concurrency, _DEFAULT_BATCH_WORKER_POOL_SIZE),
                maximum=max_allowed_workers,
            ),
            concurrency,
            len(statements),
        )
        started = time.monotonic()
        batch_deadline = started + (batch_timeout_ms / 1000.0)
        stop_event = asyncio.Event()
        semaphore = asyncio.Semaphore(concurrency)
        batch_quota = _ResultQuota(
            "batch",
            max_rows=max_batch_result_rows,
            max_bytes=max_batch_result_bytes,
        )

        with iotdb_target_response_context(selected_config):
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=concurrency,
                    wait_timeout_in_ms=selected_config.tree_wait_timeout_in_ms,
                    tool_name="sql_executor_batch",
                )
                store_item = _store_tree_batch_item
            else:
                _, session_pool = table_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=concurrency,
                    wait_timeout_in_ms=selected_config.table_wait_timeout_in_ms,
                    tool_name="sql_executor_batch",
                )
                store_item = _store_table_batch_item

            loop = asyncio.get_running_loop()
            executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="iotdb-sql-batch",
            )

            async def execute_item(item: dict[str, Any]) -> dict[str, Any]:
                if stop_event.is_set():
                    return _batch_skipped_payload(item["index"], item["sql"])
                async with semaphore:
                    if stop_event.is_set():
                        return _batch_skipped_payload(item["index"], item["sql"])
                    try:
                        remaining_seconds = batch_deadline - time.monotonic()
                        if remaining_seconds <= 0:
                            raise BatchTimeout(
                                f"Batch timeout exceeded after {batch_timeout_ms} ms."
                            )
                        timeout_seconds = min(
                            per_item_timeout_ms / 1000.0,
                            remaining_seconds,
                        )
                        item_quota = _ResultQuota(
                            f"statement {item['index']}",
                            max_rows=max_result_rows_per_item,
                            max_bytes=max_result_bytes_per_item,
                        )
                        call = partial(
                            store_item,
                            session_pool=session_pool,
                            selected_config=selected_config,
                            index=item["index"],
                            sql=item["sql"],
                            batch_id=batch_id,
                            owner_session_id=owner_session_id,
                            max_inline_rows=max_inline_rows,
                            page_size_rows=page_size_rows,
                            item_quota=item_quota,
                            batch_quota=batch_quota,
                        )
                        return await asyncio.wait_for(
                            loop.run_in_executor(executor, call),
                            timeout=timeout_seconds,
                        )
                    except asyncio.TimeoutError as exc:
                        if not continue_on_error:
                            stop_event.set()
                        return _batch_error_payload(
                            item["index"],
                            item["sql"],
                            BatchItemTimeout(
                                f"Statement timed out after {per_item_timeout_ms} ms."
                            ),
                        )
                    except Exception as exc:
                        if not continue_on_error:
                            stop_event.set()
                        logger.error(
                            "sql_executor_batch item %s failed: %s",
                            item["index"],
                            str(exc),
                        )
                        return _batch_error_payload(item["index"], item["sql"], exc)

            tasks = {
                asyncio.create_task(execute_item(item)): item for item in statements
            }
            done, pending = await asyncio.wait(
                tasks,
                timeout=batch_timeout_ms / 1000.0,
            )
            results = []
            for task in done:
                try:
                    results.append(task.result())
                except asyncio.CancelledError:
                    item = tasks[task]
                    results.append(
                        _batch_error_payload(
                            item["index"],
                            item["sql"],
                            BatchTimeout(
                                f"Batch timeout exceeded after {batch_timeout_ms} ms."
                            ),
                        )
                    )
            if pending:
                stop_event.set()
                for task in pending:
                    item = tasks[task]
                    task.cancel()
                    results.append(
                        _batch_error_payload(
                            item["index"],
                            item["sql"],
                            BatchTimeout(
                                f"Batch timeout exceeded after {batch_timeout_ms} ms."
                            ),
                        )
                    )
            executor.shutdown(wait=False, cancel_futures=True)
            results.sort(key=lambda item: int(item.get("index", 0)))
            succeeded = sum(1 for item in results if item.get("status") == "success")
            failed = sum(1 for item in results if item.get("status") == "error")
            skipped = sum(1 for item in results if item.get("status") == "skipped")
            payload = {
                "batch_id": batch_id,
                "sql_dialect": selected_config.sql_dialect,
                "target_id": selected_config.target_id,
                "statement_count": len(statements),
                "succeeded": succeeded,
                "failed": failed,
                "skipped": skipped,
                "continue_on_error": continue_on_error,
                "max_concurrency": concurrency,
                "worker_pool_size": workers,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "generated_from_template": bool(sql_template),
                "execution_controls": {
                    "per_item_timeout_ms": per_item_timeout_ms,
                    "batch_timeout_ms": batch_timeout_ms,
                    "timeout_cancellation": "best_effort",
                    "max_result_rows_per_item": max_result_rows_per_item,
                    "max_result_bytes_per_item": max_result_bytes_per_item,
                    "max_batch_result_rows": max_batch_result_rows,
                    "max_batch_result_bytes": max_batch_result_bytes,
                    "batch_rows_consumed": batch_quota.rows,
                    "batch_bytes_consumed": batch_quota.bytes,
                },
                "result_store": {
                    "type": "file",
                    "page_tool": "read_result_page",
                    "batch_page_tool": "read_result_pages",
                    "owner_session_id": owner_session_id,
                },
                "results": results,
            }
            return payload_response(
                "sql_executor_batch",
                payload,
                message="Batch SQL execution completed.",
            )

    @mcp.tool()
    async def sql_execute(
        sql: str,
        confirm_destructive: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute exactly one SQL statement on the selected IoTDB target.

        The selected target's dialect determines the whitelist and session pool.
        """
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        mode = _parse_mode()
        whitelists = _resolve_whitelists(selected_config.sql_dialect)
        with iotdb_target_response_context(selected_config):
            normalized_sql = _normalize_sql(sql)
            category, matched_prefix = _classify_sql(normalized_sql, whitelists)
            _assert_sql_driver_permission(
                selected_config,
                required_category=category,
                mode=mode,
                confirm_destructive=confirm_destructive,
                matched_prefix=matched_prefix,
            )

            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=100,
                    wait_timeout_in_ms=5000,
                    tool_name="sql_execute",
                )
                session = None
                try:
                    if category == "readonly":
                        if normalized_sql.upper().startswith("SELECT"):
                            assert_tree_query_shape(normalized_sql)
                        session = session_pool.get_session()
                        res = session.execute_query_statement(normalized_sql)
                        return _format_tree_result(
                            res,
                            session,
                            "sql_execute",
                            selected_config.export_path,
                            normalized_sql,
                            owner_session_id=owner_session_id,
                            max_inline_rows=max_inline_rows,
                            page_size_rows=page_size_rows,
                        )

                    session = session_pool.get_session()
                    session.execute_non_query_statement(normalized_sql)
                    session.close()
                    return payload_response(
                        "sql_execute",
                        {
                            "sql": normalized_sql,
                            "category": category,
                            "mode": mode,
                            "matched_prefix": matched_prefix,
                            "result": "success",
                        },
                        message="SQL executed successfully.",
                    )
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to execute sql_execute (tree): {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=100,
                tool_name="sql_execute",
            )
            table_session = None
            try:
                table_session = session_pool.get_session()
                if category == "readonly":
                    res = table_session.execute_query_statement(normalized_sql)
                    return _format_table_result(
                        res,
                        table_session,
                        "sql_execute",
                        selected_config.export_path,
                        normalized_sql,
                        owner_session_id=owner_session_id,
                        max_inline_rows=max_inline_rows,
                        page_size_rows=page_size_rows,
                    )

                table_session.execute_non_query_statement(normalized_sql)
                table_session.close()
                return payload_response(
                    "sql_execute",
                    {
                        "sql": normalized_sql,
                        "category": category,
                        "mode": mode,
                        "matched_prefix": matched_prefix,
                        "result": "success",
                    },
                    message="SQL executed successfully.",
                )
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to execute sql_execute (table): {str(e)}")
                raise
