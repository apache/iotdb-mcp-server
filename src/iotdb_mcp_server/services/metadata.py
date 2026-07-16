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

from iotdb.Session import Session
from iotdb.table_session import TableSession
from iotdb.utils.SessionDataSet import SessionDataSet
from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.result_store import result_store_from_export_path
from iotdb_mcp_server.runtime_policy import dynamic_env_bool
from iotdb_mcp_server.runtime_policy import dynamic_getenv
from iotdb_mcp_server.runtime_policy import strict_permission_enforcement
from iotdb_mcp_server.services.json_response import csv_result_payload_response
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    select_target_config,
    table_session_pool,
    tree_session_pool,
)

_TREE_PATH_PATTERN = re.compile(r"^root(?:\.[A-Za-z_][A-Za-z0-9_]*|\.\*|\.\*\*)*$")
_TABLE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_bool(name: str, default: bool) -> bool:
    return dynamic_env_bool(name, default)


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _assert_metadata_permission(config: Config) -> None:
    if strict_permission_enforcement():
        if not _env_bool("IOTDB_ENABLE_METADATA_QUERY", True):
            raise PermissionError(
                "Metadata tools are disabled by strict MCP policy. "
                "Set IOTDB_ENABLE_METADATA_QUERY=true to enable."
            )

        allowed_users = _csv_set(
            dynamic_getenv("IOTDB_METADATA_ALLOWED_USERS", "*") or "*"
        )
        if "*" not in allowed_users and config.user not in allowed_users:
            raise PermissionError(
                f"Current MCP user '{config.user}' is not allowed by IOTDB_METADATA_ALLOWED_USERS."
            )


def _normalize_sql(sql: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("Metadata SQL cannot be empty.")
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if ";" in cleaned:
        raise ValueError("Only a single SQL statement is allowed.")
    return cleaned


def _validate_tree_path(path: str) -> str:
    cleaned = path.strip()
    if not cleaned:
        raise ValueError("Tree path cannot be empty.")
    if not _TREE_PATH_PATTERN.fullmatch(cleaned):
        raise ValueError(
            "Invalid tree path. Use path like root.sg, root.sg.dev, root.**, root.sg.*"
        )
    return cleaned


def _validate_table_identifier(identifier: str) -> str:
    cleaned = identifier.strip()
    if not _TABLE_IDENTIFIER_PATTERN.fullmatch(cleaned):
        raise ValueError(
            "Invalid table identifier. Use letters, numbers, and underscore only."
        )
    return cleaned


def _format_result(
    res: SessionDataSet,
    session_or_table_session: Session | TableSession,
    tool_name: str,
    export_path: str,
    query_sql: str | None = None,
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
            source={"query_sql": query_sql} if query_sql else None,
            owner_session_id=owner_session_id,
            max_inline_rows=max_inline_rows,
            page_size_rows=page_size_rows,
        )
    finally:
        session_or_table_session.close()


def _ensure_prefix(
    sql: str, allowed_prefixes: tuple[str, ...], action_name: str
) -> None:
    upper = sql.upper()
    if not upper.startswith(allowed_prefixes):
        raise ValueError(
            f"{action_name} only supports SQL starting with: {', '.join(allowed_prefixes)}"
        )


def register_metadata_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register metadata tools with per-call IoTDB target selection."""
    max_pool_size = 100
    tree_prefixes = (
        "SHOW DATABASES",
        "SHOW TIMESERIES",
        "SHOW DEVICES",
        "SHOW CHILD PATHS",
        "SHOW CHILD NODES",
        "SHOW FUNCTIONS",
        "COUNT TIMESERIES",
        "COUNT NODES",
        "COUNT DEVICES",
    )
    table_prefixes = ("SHOW", "DESC", "DESCRIBE")

    def _require_target_dialect(
        required_sql_dialect: str,
        tool_name: str,
        target_id: str | None,
        target: dict[str, object] | None,
    ) -> None:
        select_target_config(
            config,
            target_id=target_id,
            target=target,
            required_sql_dialect=required_sql_dialect,
            tool_name=tool_name,
        )

    @mcp.tool()
    async def metadata_query(
        query_sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute metadata SQL against the selected IoTDB target."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        with iotdb_target_response_context(selected_config):
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="metadata_query",
                )
                session = None
                try:
                    _assert_metadata_permission(selected_config)
                    sql = _normalize_sql(query_sql)
                    _ensure_prefix(sql, tree_prefixes, "metadata_query")
                    session = session_pool.get_session()
                    res = session.execute_query_statement(sql)
                    return _format_result(
                        res,
                        session,
                        "metadata_query",
                        selected_config.export_path,
                        sql,
                        owner_session_id=owner_session_id,
                        max_inline_rows=max_inline_rows,
                        page_size_rows=page_size_rows,
                    )
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to execute metadata_query: {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="metadata_query",
            )
            table_session = None
            try:
                _assert_metadata_permission(selected_config)
                sql = _normalize_sql(query_sql)
                _ensure_prefix(sql, table_prefixes, "metadata_query")
                table_session = session_pool.get_session()
                res = table_session.execute_query_statement(sql)
                return _format_result(
                    res,
                    table_session,
                    "metadata_query",
                    selected_config.export_path,
                    sql,
                    owner_session_id=owner_session_id,
                    max_inline_rows=max_inline_rows,
                    page_size_rows=page_size_rows,
                )
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to execute metadata_query: {str(e)}")
                raise

    @mcp.tool()
    async def list_timeseries(
        path: str = "root.**",
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """List timeseries under a tree path pattern."""
        _require_target_dialect("tree", "list_timeseries", target_id, target)
        query_sql = f"SHOW TIMESERIES {_validate_tree_path(path)}"
        return await metadata_query(
            query_sql,
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def list_devices(
        path: str = "root.**",
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """List devices under a tree path pattern."""
        _require_target_dialect("tree", "list_devices", target_id, target)
        query_sql = f"SHOW DEVICES {_validate_tree_path(path)}"
        return await metadata_query(
            query_sql,
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def list_child_paths(
        path: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """List child paths under a tree path."""
        _require_target_dialect("tree", "list_child_paths", target_id, target)
        query_sql = f"SHOW CHILD PATHS {_validate_tree_path(path)}"
        return await metadata_query(
            query_sql,
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def list_child_nodes(
        path: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """List child nodes under a tree path."""
        _require_target_dialect("tree", "list_child_nodes", target_id, target)
        query_sql = f"SHOW CHILD NODES {_validate_tree_path(path)}"
        return await metadata_query(
            query_sql,
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def count_timeseries(
        path: str = "root.**",
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """Count timeseries under a tree path pattern."""
        _require_target_dialect("tree", "count_timeseries", target_id, target)
        query_sql = f"COUNT TIMESERIES {_validate_tree_path(path)}"
        return await metadata_query(
            query_sql,
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def count_devices(
        path: str = "root.**",
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """Count devices under a tree path pattern."""
        _require_target_dialect("tree", "count_devices", target_id, target)
        query_sql = f"COUNT DEVICES {_validate_tree_path(path)}"
        return await metadata_query(
            query_sql,
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def count_nodes(
        path: str = "root",
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """Count nodes under a tree path."""
        _require_target_dialect("tree", "count_nodes", target_id, target)
        query_sql = f"COUNT NODES {_validate_tree_path(path)}"
        return await metadata_query(
            query_sql,
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def list_tables(
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """List all tables in current table-model database."""
        _require_target_dialect("table", "list_tables", target_id, target)
        return await metadata_query(
            "SHOW TABLES",
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )

    @mcp.tool()
    async def describe_table(
        table_name: str,
        details: bool = True,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
    ) -> list[TextContent]:
        """Describe schema for a table in current table-model database."""
        _require_target_dialect("table", "describe_table", target_id, target)
        safe_table = _validate_table_identifier(table_name)
        details_suffix = " details" if details else ""
        return await metadata_query(
            f"DESC {safe_table}{details_suffix}",
            target_id=target_id,
            target=target,
            owner_session_id=owner_session_id,
        )
