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
from iotdb_mcp_server.runtime_policy import dynamic_env_bool
from iotdb_mcp_server.runtime_policy import dynamic_getenv
from iotdb_mcp_server.runtime_policy import strict_permission_enforcement
from iotdb_mcp_server.services.json_response import (
    csv_payload_response,
    sql_success_response,
)
from iotdb_mcp_server.services.target_selection import (
    apply_target_registry,
    iotdb_target_response_context,
    select_target_config,
    table_session_pool,
    tree_session_pool,
)
from iotdb_mcp_server.target_registry import merge_target

_TABLE_DB_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TREE_DB_PATTERN = re.compile(r"^root(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _env_bool(name: str, default: bool) -> bool:
    return dynamic_env_bool(name, default)


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _assert_database_ddl_permission(
    config: Config, action: str, confirm: bool = False
) -> None:
    if strict_permission_enforcement():
        if not _env_bool("IOTDB_ENABLE_DATABASE_DDL", False):
            raise PermissionError(
                "Database DDL tool is disabled by strict MCP policy. "
                "Set IOTDB_ENABLE_DATABASE_DDL=true to enable."
            )

        allowed_users = _csv_set(dynamic_getenv("IOTDB_DATABASE_DDL_ALLOWED_USERS", "root") or "root")
        if "*" not in allowed_users and config.user not in allowed_users:
            raise PermissionError(
                f"Current MCP user '{config.user}' is not allowed by IOTDB_DATABASE_DDL_ALLOWED_USERS."
            )

    if action == "DROP" and _env_bool("IOTDB_REQUIRE_DROP_CONFIRM", True) and not confirm:
        raise PermissionError(
            "DROP DATABASE requires confirm=True by server policy."
        )


def _validate_database_name(sql_dialect: str, database: str) -> str:
    raw = database.strip()
    if not raw:
        raise ValueError("Database name/path cannot be empty.")

    if sql_dialect == "tree":
        if not _TREE_DB_PATTERN.fullmatch(raw):
            raise ValueError(
                "Invalid tree database path. Expected pattern like root.sg or root.sg1.dev."
            )
    elif sql_dialect == "table":
        if not _TABLE_DB_PATTERN.fullmatch(raw):
            raise ValueError(
                "Invalid table database name. Use letters, numbers, and underscore only."
            )
    else:
        raise ValueError(
            f"Unsupported sql_dialect '{sql_dialect}'. Expected 'tree' or 'table'."
        )
    return raw


def _format_result(
    res: SessionDataSet,
    session_or_table_session: Session | TableSession,
    tool_name: str,
) -> list[TextContent]:
    columns = res.get_column_names()
    rows: list[str] = []
    while res.has_next():
        row = res.next().get_fields()
        rows.append(",".join(map(str, row)))
    session_or_table_session.close()
    return csv_payload_response(tool_name, columns, rows)


def register_database_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register database management tools with per-call IoTDB target selection."""
    max_pool_size = 100

    @mcp.tool()
    async def list_databases(
        details: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """List databases in the selected IoTDB target."""
        selected_config = select_target_config(config, target_id=target_id, target=target)
        with iotdb_target_response_context(selected_config):
            sql = "SHOW DATABASES DETAILS" if details else "SHOW DATABASES"
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="list_databases",
                )
                session = None
                try:
                    session = session_pool.get_session()
                    res = session.execute_query_statement(sql)
                    return _format_result(res, session, "list_databases")
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to list databases: {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="list_databases",
            )
            table_session = None
            try:
                table_session = session_pool.get_session()
                res = table_session.execute_query_statement(sql)
                return _format_result(res, table_session, "list_databases")
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to list databases: {str(e)}")
                raise

    @mcp.tool()
    async def create_database(
        database: str,
        if_not_exists: bool = True,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Create a database/storage group in the selected IoTDB target."""
        selected_config = select_target_config(config, target_id=target_id, target=target)
        with iotdb_target_response_context(selected_config):
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="create_database",
                )
                session = None
                try:
                    _assert_database_ddl_permission(selected_config, action="CREATE")
                    database_path = _validate_database_name(
                        selected_config.sql_dialect, database
                    )
                    sql = f"CREATE DATABASE {database_path}"
                    session = session_pool.get_session()
                    session.execute_non_query_statement(sql)
                    session.close()
                    return sql_success_response("create_database", sql)
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to create database: {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="create_database",
            )
            table_session = None
            try:
                _assert_database_ddl_permission(selected_config, action="CREATE")
                database_name = _validate_database_name(
                    selected_config.sql_dialect, database
                )
                sql = (
                    f"CREATE DATABASE IF NOT EXISTS {database_name}"
                    if if_not_exists
                    else f"CREATE DATABASE {database_name}"
                )
                table_session = session_pool.get_session()
                table_session.execute_non_query_statement(sql)
                table_session.close()
                return sql_success_response("create_database", sql)
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to create database: {str(e)}")
                raise

    @mcp.tool()
    async def drop_database(
        database: str,
        if_exists: bool = True,
        confirm: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Drop a database/storage group in the selected IoTDB target."""
        selected_config = select_target_config(config, target_id=target_id, target=target)
        with iotdb_target_response_context(selected_config):
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    selected_config,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="drop_database",
                )
                session = None
                try:
                    _assert_database_ddl_permission(
                        selected_config, action="DROP", confirm=confirm
                    )
                    database_path = _validate_database_name(
                        selected_config.sql_dialect, database
                    )
                    sql = f"DROP DATABASE {database_path}"
                    session = session_pool.get_session()
                    session.execute_non_query_statement(sql)
                    session.close()
                    return sql_success_response("drop_database", sql)
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to drop database: {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                selected_config,
                max_pool_size=max_pool_size,
                tool_name="drop_database",
            )
            table_session = None
            try:
                _assert_database_ddl_permission(
                    selected_config, action="DROP", confirm=confirm
                )
                database_name = _validate_database_name(
                    selected_config.sql_dialect, database
                )
                sql = (
                    f"DROP DATABASE IF EXISTS {database_name}"
                    if if_exists
                    else f"DROP DATABASE {database_name}"
                )
                table_session = session_pool.get_session()
                table_session.execute_non_query_statement(sql)
                table_session.close()
                return sql_success_response("drop_database", sql)
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to drop database: {str(e)}")
                raise

    @mcp.tool()
    async def use_database(
        database: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Switch current database in a table-model IoTDB target."""
        selected_config, session_pool = table_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=max_pool_size,
            tool_name="use_database",
        )
        table_session = None
        try:
            database_name = _validate_database_name(
                selected_config.sql_dialect, database
            )
            sql = f"USE {database_name}"
            table_session = session_pool.get_session()
            table_session.execute_non_query_statement(sql)
            table_session.close()
            table_session = None

            updated_target = merge_target(
                selected_config.to_target(),
                {"database": database_name},
            )
            registry = config.target_registry or selected_config.target_registry
            if registry is not None:
                updated_registry = registry.with_target(updated_target)
                apply_target_registry(
                    config,
                    updated_registry,
                    active_target_id=config.target_id or updated_target.target_id,
                    close_pools=False,
                )
                if config.session_manager is not None:
                    config.session_manager.close_target_pools(updated_target.target_id)
                updated_config = select_target_config(
                    config,
                    target_id=updated_target.target_id,
                )
            else:
                selected_config.database = database_name
                if config.target_id == selected_config.target_id:
                    config.database = database_name
                updated_config = selected_config

            with iotdb_target_response_context(updated_config):
                return sql_success_response("use_database", sql)
        except Exception as e:
            if table_session:
                table_session.close()
            logger.error(f"Failed to use database: {str(e)}")
            raise
