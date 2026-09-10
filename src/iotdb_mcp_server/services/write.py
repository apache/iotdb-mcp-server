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

from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.runtime_policy import dynamic_env_bool
from iotdb_mcp_server.runtime_policy import dynamic_getenv
from iotdb_mcp_server.runtime_policy import strict_permission_enforcement
from iotdb_mcp_server.services.json_response import sql_success_response
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    select_target_config,
    table_session_pool,
    tree_session_pool,
)


def _env_bool(name: str, default: bool) -> bool:
    return dynamic_env_bool(name, default)


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _assert_write_permission(
    config: Config, action: str, confirm_delete: bool = False
) -> None:
    if strict_permission_enforcement():
        if not _env_bool("IOTDB_ENABLE_WRITE_DML", False):
            raise PermissionError(
                "Write DML tools are disabled by strict MCP policy. "
                "Set IOTDB_ENABLE_WRITE_DML=true to enable."
            )

        allowed_users = _csv_set(dynamic_getenv("IOTDB_WRITE_ALLOWED_USERS", "root") or "root")
        if "*" not in allowed_users and config.user not in allowed_users:
            raise PermissionError(
                f"Current MCP user '{config.user}' is not allowed by IOTDB_WRITE_ALLOWED_USERS."
            )

    if (
        action == "DELETE"
        and _env_bool("IOTDB_REQUIRE_DELETE_CONFIRM", True)
        and not confirm_delete
    ):
        raise PermissionError(
            "DELETE operation requires confirm_delete=True by server policy."
        )


def _normalize_sql(sql: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("Write SQL cannot be empty.")
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if ";" in cleaned:
        raise ValueError("Only a single SQL statement is allowed.")
    return cleaned


def register_write_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register write-related DML tools with per-call IoTDB target selection."""
    max_pool_size = 100

    @mcp.tool()
    async def write_query(
        write_sql: str,
        confirm_delete: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute exactly one write statement on the selected IoTDB target."""
        selected_config = select_target_config(config, target_id=target_id, target=target)
        with iotdb_target_response_context(selected_config):
            sql = _normalize_sql(write_sql)
            upper = sql.upper()

            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    selected_config,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="write_query",
                )
                session = None
                try:
                    if upper.startswith("INSERT INTO"):
                        _assert_write_permission(selected_config, action="INSERT")
                    elif upper.startswith("DELETE FROM"):
                        _assert_write_permission(
                            selected_config,
                            action="DELETE",
                            confirm_delete=confirm_delete,
                        )
                    else:
                        raise ValueError(
                            "tree write_query only supports SQL starting with: "
                            "INSERT INTO, DELETE FROM"
                        )

                    session = session_pool.get_session()
                    session.execute_non_query_statement(sql)
                    session.close()
                    return sql_success_response("write_query", sql)
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to execute tree write_query: {str(e)}")
                    raise

            _, session_pool = table_session_pool(
                selected_config,
                max_pool_size=max_pool_size,
                tool_name="write_query",
            )
            table_session = None
            try:
                if upper.startswith("INSERT INTO"):
                    _assert_write_permission(selected_config, action="INSERT")
                elif upper.startswith("UPDATE"):
                    _assert_write_permission(selected_config, action="UPDATE")
                elif upper.startswith("DELETE FROM") or upper.startswith("DELETE DEVICES"):
                    _assert_write_permission(
                        selected_config,
                        action="DELETE",
                        confirm_delete=confirm_delete,
                    )
                else:
                    raise ValueError(
                        "table write_query only supports SQL starting with: "
                        "INSERT INTO, UPDATE, DELETE FROM, DELETE DEVICES"
                    )

                table_session = session_pool.get_session()
                table_session.execute_non_query_statement(sql)
                table_session.close()
                return sql_success_response("write_query", sql)
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to execute table write_query: {str(e)}")
                raise
