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

import json
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from mcp.types import TextContent

from iotdb_mcp_server.result_store import FileResultStore

_RESPONSE_VERSION = "1.0"
_target_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "iotdb_target_context", default=None
)

_TRUE_VALUES = {"1", "true", "yes", "on", "available", "running", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "unavailable", "stopped", "disabled"}


def set_iotdb_target_context(target: dict[str, Any] | None):
    return _target_context.set(target)


def reset_iotdb_target_context(token) -> None:
    _target_context.reset(token)


def _coerce_availability(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return "yes"
    if normalized in _FALSE_VALUES:
        return "no"
    if normalized in {"unknown", "probe_failed", "not_probed"}:
        return "unknown"
    return None


def _ainode_availability(target: dict[str, Any]) -> tuple[str, Any]:
    availability = _coerce_availability(target.get("ainode_available")) or "unknown"
    source = target.get("ainode_availability_source")
    if source is None:
        source = {
            "method": "SHOW AINODES",
            "ok": False,
            "error": "not_probed",
        }
    return availability, source


def _cli_endpoint(target: dict[str, Any]) -> str:
    node_urls = target.get("node_urls")
    if isinstance(node_urls, list) and node_urls:
        first = str(node_urls[0]).strip()
        if first:
            return first
    host = str(target.get("host") or "127.0.0.1").strip()
    port = str(target.get("port") or "6667").strip()
    return f"{host}:{port}"


def _row_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    rows = payload.get("rows")
    if isinstance(rows, list):
        return len(rows)
    for key in ("row_count", "rows_count", "count"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def _iotdb_context(tool: str, payload: Any, target: dict[str, Any]) -> dict[str, Any]:
    ainode_available, ainode_source = _ainode_availability(target)
    return {
        "target_id": target.get("target_id"),
        "cli_endpoint": _cli_endpoint(target),
        "ainode_available": ainode_available,
        "ainode_availability_source": ainode_source,
        "dialect": target.get("sql_dialect"),
        "database": target.get("database"),
        "tool": tool,
        "rows": _row_count(payload),
    }


def _render_iotdb_context(context: dict[str, Any]) -> str:
    lines = [
        "TimeSeek IoTDB context:",
        f"- target_id: {context.get('target_id') or ''}",
        f"- cli_endpoint: {context.get('cli_endpoint') or ''}",
        f"- ainode_available: {context.get('ainode_available') or 'unknown'}",
        f"- dialect: {context.get('dialect') or ''}",
        f"- database: {context.get('database') or ''}",
        f"- tool: {context.get('tool') or ''}",
        f"- rows: {context.get('rows', 0)}",
    ]
    return "\n".join(lines)


class JsonParser:
    """Simple parser/checker for MCP tool JSON envelopes."""

    _required_keys = ("version", "tool", "ok", "timestamp", "payload")

    @staticmethod
    def check_format(obj: Any) -> bool:
        if not isinstance(obj, dict):
            return False
        for key in JsonParser._required_keys:
            if key not in obj:
                return False
        if not isinstance(obj["version"], str):
            return False
        if not isinstance(obj["tool"], str) or not obj["tool"].strip():
            return False
        if not isinstance(obj["ok"], bool):
            return False
        if not isinstance(obj["timestamp"], str) or not obj["timestamp"].strip():
            return False
        return True

    @staticmethod
    def parse(text: str) -> dict[str, Any]:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON text: {e.msg}") from e

        if not JsonParser.check_format(obj):
            raise ValueError(
                "Invalid wrapped response format. Expected keys: "
                "version, tool, ok, timestamp, payload."
            )
        return obj


def _envelope(
    tool: str, payload: Any, ok: bool = True, message: str | None = None
) -> dict[str, Any]:
    target = _target_context.get()
    context = _iotdb_context(tool, payload, target) if target else None
    if target:
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("iotdb_target", target)
            if context:
                payload.setdefault("iotdb_context", context)
        else:
            payload = {
                "value": payload,
                "iotdb_target": target,
            }
            if context:
                payload["iotdb_context"] = context
    obj: dict[str, Any] = {
        "version": _RESPONSE_VERSION,
        "tool": tool,
        "ok": ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    if context:
        obj["context"] = {"iotdb": context}
    if message is not None:
        obj["message"] = message
    return obj


def _to_text_content(obj: dict[str, Any]) -> list[TextContent]:
    if not JsonParser.check_format(obj):
        raise ValueError("JsonParser format check failed before output.")
    text = json.dumps(obj, ensure_ascii=False)
    JsonParser.parse(text)
    context = obj.get("context")
    iotdb_context = context.get("iotdb") if isinstance(context, dict) else None
    if isinstance(iotdb_context, dict):
        return [
            TextContent(type="text", text=_render_iotdb_context(iotdb_context)),
            TextContent(type="text", text=text),
        ]
    return [TextContent(type="text", text=text)]


def payload_response(
    tool: str, payload: Any, message: str | None = None
) -> list[TextContent]:
    return _to_text_content(_envelope(tool=tool, payload=payload, message=message))


def error_response(tool: str, error: str) -> list[TextContent]:
    return _to_text_content(
        _envelope(
            tool=tool,
            payload={"error": error},
            ok=False,
            message="Tool execution failed.",
        )
    )


def text_payload_response(
    tool: str, text: str, message: str | None = None
) -> list[TextContent]:
    return payload_response(
        tool=tool,
        payload={"format": "text", "text": text},
        message=message,
    )


def _csv_text(columns: list[str], rows: list[str]) -> str:
    return "\n".join([",".join(columns)] + rows)


def csv_payload_response(
    tool: str,
    columns: list[str],
    rows: list[str],
    message: str | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[TextContent]:
    csv_text = _csv_text(columns, rows)
    payload: dict[str, Any] = {
        "format": "csv",
        "columns": columns,
        "rows": rows,
        "text": csv_text,
        "row_count": len(rows),
        "inline_row_count": len(rows),
        "inline_truncated": False,
    }
    if diagnostics:
        payload["diagnostics"] = diagnostics
    return payload_response(
        tool=tool,
        payload=payload,
        message=message,
    )


def csv_result_payload_response(
    tool: str,
    columns: list[str],
    rows: Iterable[str],
    *,
    result_store: FileResultStore,
    source: dict[str, Any] | None = None,
    message: str | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    diagnostics_factory: Callable[[int], list[dict[str, Any]] | None] | None = None,
    owner_session_id: str | None = None,
    max_inline_rows: int | None = None,
    page_size_rows: int | None = None,
) -> list[TextContent]:
    stored = result_store.write_csv_result(
        tool=tool,
        columns=columns,
        rows=rows,
        source=source,
        owner_session_id=owner_session_id,
        preview_rows=max_inline_rows,
        page_size_rows=page_size_rows,
    )
    preview_rows = stored.preview_rows
    inline_truncated = stored.row_count > len(preview_rows)
    payload: dict[str, Any] = {
        "format": "csv",
        "columns": columns,
        "rows": preview_rows,
        "text": _csv_text(columns, preview_rows),
        "row_count": stored.row_count,
        "inline_row_count": len(preview_rows),
        "inline_truncated": inline_truncated,
        "result_id": stored.result_id,
        "owner_session_id": stored.owner_session_id,
        "next_cursor": str(len(preview_rows)) if inline_truncated else None,
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
    combined_diagnostics = list(diagnostics or [])
    if diagnostics_factory is not None:
        combined_diagnostics.extend(diagnostics_factory(stored.row_count) or [])
    if combined_diagnostics:
        payload["diagnostics"] = combined_diagnostics
    return payload_response(tool=tool, payload=payload, message=message)


def sql_success_response(tool: str, sql: str) -> list[TextContent]:
    return payload_response(
        tool=tool,
        payload={"sql": sql, "result": "success"},
        message="SQL executed successfully.",
    )
