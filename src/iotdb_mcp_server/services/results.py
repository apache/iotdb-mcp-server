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

from __future__ import annotations

import logging

from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.result_store import result_store_from_export_path
from iotdb_mcp_server.services.json_response import payload_response
from iotdb_mcp_server.services.target_selection import select_target_config


def _cursor_to_offset(cursor: str | None, offset: int | None) -> int:
    if offset is not None:
        return max(0, int(offset))
    if cursor is None or str(cursor).strip() == "":
        return 0
    cleaned = str(cursor).strip()
    if not cleaned.isdigit():
        raise ValueError("cursor must be a non-negative integer row offset.")
    return int(cleaned)


def _csv_text(columns: list[str], rows: list[str]) -> str:
    return "\n".join([",".join(columns)] + rows)


def register_result_store_tools(
    mcp,
    config: Config,
    logger: logging.Logger,
) -> None:
    """Register tools for paging file-backed MCP result sets."""

    @mcp.tool()
    async def read_result_page(
        result_id: str,
        cursor: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        owner_session_id: str | None = None,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Read one page from a previously returned ResultStore result_id.

        Pass the same target_id used by the original query when multiple IoTDB
        targets are registered. cursor is the next_cursor returned by a prior
        page; offset can be used for random access and takes precedence.
        """
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        store = result_store_from_export_path(selected_config.export_path)
        try:
            payload = store.read_page(
                result_id,
                offset=_cursor_to_offset(cursor, offset),
                limit=limit,
                owner_session_id=owner_session_id,
            )
            payload["target_id"] = selected_config.target_id
            payload["text"] = _csv_text(payload["columns"], payload["rows"])
            payload["inline_row_count"] = payload["returned_rows"]
            payload["inline_truncated"] = payload["has_more"]
            return payload_response(
                "read_result_page",
                payload,
                message="Result page loaded from ResultStore.",
            )
        except Exception as e:
            logger.error(f"Failed to read ResultStore page: {str(e)}")
            raise

    @mcp.tool()
    async def read_result_pages(
        pages: list[dict[str, object]],
        owner_session_id: str | None = None,
        default_limit: int | None = None,
        max_pages: int | None = None,
        max_total_rows: int | None = None,
        continue_on_error: bool = True,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Read multiple ResultStore pages in one MCP call.

        Each page request is an object with result_id plus optional cursor,
        offset, limit, and owner_session_id. offset takes precedence over
        cursor. owner_session_id on an item takes precedence over the shared
        owner_session_id argument.
        """
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        store = result_store_from_export_path(selected_config.export_path)
        try:
            payload = store.read_pages(
                pages,
                owner_session_id=owner_session_id,
                default_limit=default_limit,
                max_pages=max_pages,
                max_total_rows=max_total_rows,
                continue_on_error=continue_on_error,
            )
            payload["target_id"] = selected_config.target_id
            for page in payload["pages"]:
                if page.get("status") != "success":
                    continue
                page["text"] = _csv_text(page["columns"], page["rows"])
                page["inline_row_count"] = page["returned_rows"]
                page["inline_truncated"] = page["has_more"]
            return payload_response(
                "read_result_pages",
                payload,
                message="Result pages loaded from ResultStore.",
            )
        except Exception as e:
            logger.error(f"Failed to read ResultStore pages: {str(e)}")
            raise

    @mcp.tool()
    async def list_result_store(
        owner_session_id: str | None = None,
        all_sessions: bool = False,
        limit: int = 50,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """List ResultStore entries for the current session by default."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        store = result_store_from_export_path(selected_config.export_path)
        try:
            payload = store.list_results(
                owner_session_id=owner_session_id,
                all_sessions=all_sessions,
                limit=limit,
            )
            payload["target_id"] = selected_config.target_id
            return payload_response(
                "list_result_store",
                payload,
                message="ResultStore entries listed.",
            )
        except Exception as e:
            logger.error(f"Failed to list ResultStore entries: {str(e)}")
            raise

    @mcp.tool()
    async def cleanup_result_store(
        owner_session_id: str | None = None,
        all_sessions: bool = False,
        ttl_seconds: int | None = None,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Run TTL and quota cleanup for ResultStore entries."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        store = result_store_from_export_path(selected_config.export_path)
        try:
            payload = store.cleanup(
                owner_session_id=owner_session_id,
                all_sessions=all_sessions,
                ttl_seconds=ttl_seconds,
            )
            payload["target_id"] = selected_config.target_id
            return payload_response(
                "cleanup_result_store",
                payload,
                message="ResultStore cleanup completed.",
            )
        except Exception as e:
            logger.error(f"Failed to cleanup ResultStore: {str(e)}")
            raise

    @mcp.tool()
    async def delete_result(
        result_id: str,
        owner_session_id: str | None = None,
        all_sessions: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Delete one cached ResultStore result."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        store = result_store_from_export_path(selected_config.export_path)
        try:
            payload = store.delete_result(
                result_id,
                owner_session_id=owner_session_id,
                all_sessions=all_sessions,
            )
            payload["target_id"] = selected_config.target_id
            return payload_response(
                "delete_result",
                payload,
                message="ResultStore result deleted.",
            )
        except Exception as e:
            logger.error(f"Failed to delete ResultStore result: {str(e)}")
            raise
