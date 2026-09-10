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

import logging
import re
from typing import Any

from iotdb.Session import Session
from iotdb.table_session import TableSession
from iotdb.utils.SessionDataSet import SessionDataSet
from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.result_store import result_store_from_export_path
from iotdb_mcp_server.runtime_policy import dynamic_env_bool
from iotdb_mcp_server.runtime_policy import dynamic_getenv
from iotdb_mcp_server.runtime_policy import strict_permission_enforcement
from iotdb_mcp_server.services.json_response import (
    csv_result_payload_response,
    payload_response,
    sql_success_response,
)
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    select_target_config,
    table_session_pool,
    tree_session_pool,
)

_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")
_INFERENCE_PATTERN = re.compile(
    r"^\s*CALL\s+INFERENCE\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL
)
_ALLOWED_INFERENCE_PARAMS = {"generateTime", "outputLength"}


def _env_bool(name: str, default: bool) -> bool:
    return dynamic_env_bool(name, default)


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _assert_model_permission(
    config: Config, action: str, confirm_destructive: bool = False
) -> None:
    if strict_permission_enforcement():
        if not _env_bool("IOTDB_ENABLE_MODEL_MANAGEMENT", False):
            raise PermissionError(
                "AINode model tools are disabled by strict MCP policy. "
                "Set IOTDB_ENABLE_MODEL_MANAGEMENT=true to enable."
            )

        allowed_users = _csv_set(
            dynamic_getenv("IOTDB_MODEL_ALLOWED_USERS", "root") or "root"
        )
        if "*" not in allowed_users and config.user not in allowed_users:
            raise PermissionError(
                f"Current MCP user '{config.user}' is not allowed by IOTDB_MODEL_ALLOWED_USERS."
            )

    if (
        action == "DESTRUCTIVE"
        and _env_bool("IOTDB_REQUIRE_MODEL_DESTRUCTIVE_CONFIRM", True)
        and not confirm_destructive
    ):
        raise PermissionError(
            "Destructive model command requires confirm_destructive=True by server policy."
        )


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
    return _strip_single_trailing_semicolon(sql, "Model SQL cannot be empty.")


def _normalize_for_prefix(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().upper()


def _matched_prefix(sql: str, prefixes: tuple[str, ...]) -> str | None:
    upper = _normalize_for_prefix(sql)
    for prefix in prefixes:
        if not upper.startswith(prefix):
            continue
        if (
            len(upper) == len(prefix)
            or upper[len(prefix)].isspace()
            or upper[len(prefix)] == "("
        ):
            return prefix
    return None


def _assert_prefix(sql: str, prefixes: tuple[str, ...], tool_name: str) -> None:
    if _matched_prefix(sql, prefixes):
        return
    raise ValueError(
        f"{tool_name} only supports SQL starting with: {', '.join(prefixes)}"
    )


def _split_top_level_args(value: str) -> list[str]:
    args: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    paren_depth = 0

    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            continue
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    continue
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            paren_depth += 1
        elif char == ")":
            if paren_depth > 0:
                paren_depth -= 1
        elif char == "," and paren_depth == 0:
            args.append(value[start:index].strip())
            start = index + 1

    if quote:
        raise ValueError("CALL INFERENCE input has an unterminated string literal.")
    if paren_depth:
        raise ValueError("CALL INFERENCE input has unbalanced parentheses.")

    args.append(value[start:].strip())
    return args


def _decode_sql_string(value: str) -> str:
    if len(value) < 2 or value[0] not in {"'", '"'} or value[-1] != value[0]:
        raise ValueError(
            "CALL INFERENCE second argument must be a quoted SELECT SQL string, "
            'for example: "SELECT s0 FROM root.AI LIMIT 256".'
        )
    quote = value[0]
    body = value[1:-1]
    return body.replace(quote * 2, quote)


def _quote_sql_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _assert_model_id(model_id: str) -> None:
    if not _MODEL_ID_PATTERN.match(model_id):
        raise ValueError(
            "model_id must be 2-64 characters, start with a letter, and contain "
            "only letters, numbers, or underscores."
        )


def _validate_inference_input_sql(input_sql: str) -> list[dict[str, Any]]:
    cleaned = _strip_single_trailing_semicolon(
        input_sql, "CALL INFERENCE input SELECT SQL cannot be empty."
    )
    normalized = _normalize_for_prefix(cleaned)
    diagnostics: list[dict[str, Any]] = []

    if not normalized.startswith("SELECT "):
        raise ValueError("CALL INFERENCE input SQL must be a SELECT query.")
    if re.match(r"^\s*SELECT\s+\*", cleaned, re.IGNORECASE):
        raise ValueError(
            "CALL INFERENCE input SQL must use an explicit ordered column list; "
            "do not use SELECT * because wildcard column order is undefined."
        )
    if " LIMIT " not in f" {normalized} " and " WHERE " not in f" {normalized} ":
        diagnostics.append(
            {
                "severity": "warning",
                "code": "unbounded_input_sql",
                "message": "Prefer LIMIT or a concrete time predicate for inference input SQL.",
            }
        )
    return diagnostics


def _parse_inference_params(raw_params: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for raw in raw_params:
        if "=" not in raw:
            raise ValueError(
                "CALL INFERENCE optional arguments must use name=value syntax. "
                "Supported names: generateTime, outputLength."
            )
        name, raw_value = [part.strip() for part in raw.split("=", 1)]
        if name not in _ALLOWED_INFERENCE_PARAMS:
            raise ValueError(
                f"Unsupported CALL INFERENCE parameter '{name}'. "
                "Supported parameters: generateTime, outputLength."
            )
        if name in params:
            raise ValueError(f"Duplicate CALL INFERENCE parameter '{name}'.")

        if name == "generateTime":
            lowered = raw_value.lower()
            if lowered not in {"true", "false"}:
                raise ValueError("generateTime must be a boolean: true or false.")
            params[name] = lowered == "true"
        elif name == "outputLength":
            if not re.match(r"^[1-9][0-9]*$", raw_value):
                raise ValueError("outputLength must be a positive integer.")
            params[name] = int(raw_value)
    return params


def _validate_inference_sql(sql: str) -> dict[str, Any]:
    match = _INFERENCE_PATTERN.match(sql)
    if not match:
        raise ValueError(
            "model_inference expects CALL INFERENCE(model_id, "
            '"SELECT ...", generateTime=true|false, outputLength=<positive int>).'
        )

    args = _split_top_level_args(match.group(1))
    if len(args) < 2:
        raise ValueError(
            "CALL INFERENCE requires at least model_id and input SELECT SQL."
        )

    model_id = args[0].strip()
    _assert_model_id(model_id)

    input_sql = _decode_sql_string(args[1])
    diagnostics = _validate_inference_input_sql(input_sql)
    params = _parse_inference_params(args[2:])

    return {
        "model_id": model_id,
        "input_sql": input_sql,
        "parameters": params,
        "diagnostics": diagnostics,
    }


def _build_inference_sql(
    model_id: str,
    input_sql: str,
    output_length: int = 96,
    generate_time: bool = False,
) -> tuple[str, dict[str, Any]]:
    _assert_model_id(model_id)
    diagnostics = _validate_inference_input_sql(input_sql)
    if output_length <= 0:
        raise ValueError("output_length must be a positive integer.")
    inference_sql = (
        f"CALL INFERENCE({model_id}, {_quote_sql_string(input_sql)}, "
        f"generateTime={str(generate_time).lower()}, outputLength={output_length})"
    )
    return inference_sql, {
        "model_id": model_id,
        "input_sql": input_sql,
        "parameters": {
            "generateTime": generate_time,
            "outputLength": output_length,
        },
        "diagnostics": diagnostics,
    }


def _format_result(
    res: SessionDataSet,
    session_or_table_session: Session | TableSession,
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
            row = res.next().get_fields()
            yield ",".join(map(str, row))

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
        session_or_table_session.close()


def register_model_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register AINode model-management SQL tools with target selection."""
    max_pool_size = 100

    @mcp.tool()
    async def model_query(
        model_sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute AINode model-query SQL against the selected IoTDB target."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        with iotdb_target_response_context(selected_config):
            sql = _normalize_sql(model_sql)
            _assert_prefix(
                sql,
                (
                    "SHOW MODELS",
                    "SHOW LOADED MODELS",
                    "SHOW AI_DEVICES",
                    "SHOW AINODES",
                ),
                "model_query",
            )
            _assert_model_permission(selected_config, action="QUERY")

            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="model_query",
                )
                session = None
                try:
                    session = session_pool.get_session()
                    res = session.execute_query_statement(sql)
                    return _format_result(
                        res,
                        session,
                        "model_query",
                        selected_config.export_path,
                        sql,
                        owner_session_id=owner_session_id,
                        max_inline_rows=max_inline_rows,
                        page_size_rows=page_size_rows,
                    )
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to execute model_query: {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="model_query",
            )
            table_session = None
            try:
                table_session = session_pool.get_session()
                res = table_session.execute_query_statement(sql)
                return _format_result(
                    res,
                    table_session,
                    "model_query",
                    selected_config.export_path,
                    sql,
                    owner_session_id=owner_session_id,
                    max_inline_rows=max_inline_rows,
                    page_size_rows=page_size_rows,
                )
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to execute model_query: {str(e)}")
                raise

    @mcp.tool()
    async def prepare_model_inference_request(
        model_id: str,
        input_sql: str,
        output_length: int = 96,
        generate_time: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Build and validate Tree-dialect AINode CALL INFERENCE SQL."""
        selected_config = select_target_config(
            config,
            target_id=target_id,
            target=target,
            required_sql_dialect="tree",
            tool_name="prepare_model_inference_request",
        )
        with iotdb_target_response_context(selected_config):
            inference_sql, validation = _build_inference_sql(
                model_id=model_id,
                input_sql=input_sql,
                output_length=output_length,
                generate_time=generate_time,
            )
            return payload_response(
                "prepare_model_inference_request",
                {
                    "inference_sql": inference_sql,
                    "validation": validation,
                    "recommended_next_tool": "model_inference",
                },
            )

    @mcp.tool()
    async def model_inference(
        inference_sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute Tree-dialect AINode inference SQL."""
        selected_config, session_pool = tree_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=max_pool_size,
            wait_timeout_in_ms=5000,
            tool_name="model_inference",
        )
        with iotdb_target_response_context(selected_config):
            session = None
            try:
                _assert_model_permission(selected_config, action="QUERY")
                sql = _normalize_sql(inference_sql)
                _assert_prefix(sql, ("CALL INFERENCE",), "model_inference")
                _validate_inference_sql(sql)
                session = session_pool.get_session()
                res = session.execute_query_statement(sql)
                return _format_result(
                    res,
                    session,
                    "model_inference",
                    selected_config.export_path,
                    sql,
                    owner_session_id=owner_session_id,
                    max_inline_rows=max_inline_rows,
                    page_size_rows=page_size_rows,
                )
            except Exception as e:
                if session:
                    session.close()
                logger.error(f"Failed to execute model_inference: {str(e)}")
                raise

    @mcp.tool()
    async def model_command(
        model_sql: str,
        confirm_destructive: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute AINode model-management command SQL."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        with iotdb_target_response_context(selected_config):
            sql = _normalize_sql(model_sql)

            if _matched_prefix(sql, ("DROP MODEL", "UNLOAD MODEL", "REMOVE AINODE")):
                _assert_model_permission(
                    selected_config,
                    action="DESTRUCTIVE",
                    confirm_destructive=confirm_destructive,
                )
            elif _matched_prefix(sql, ("CREATE MODEL", "LOAD MODEL")):
                _assert_model_permission(selected_config, action="MANAGE")
            else:
                raise ValueError(
                    "model_command only supports SQL starting with: "
                    "CREATE MODEL, DROP MODEL, LOAD MODEL, UNLOAD MODEL, REMOVE AINODE"
                )

            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    selected_config,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="model_command",
                )
                session = None
                try:
                    session = session_pool.get_session()
                    session.execute_non_query_statement(sql)
                    session.close()
                    return sql_success_response("model_command", sql)
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to execute model_command: {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                selected_config,
                max_pool_size=max_pool_size,
                tool_name="model_command",
            )
            table_session = None
            try:
                table_session = session_pool.get_session()
                table_session.execute_non_query_statement(sql)
                table_session.close()
                return sql_success_response("model_command", sql)
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to execute model_command: {str(e)}")
                raise
