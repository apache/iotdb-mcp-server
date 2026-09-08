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
import time
from typing import Any

from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.runtime_policy import dynamic_env_bool
from iotdb_mcp_server.runtime_policy import dynamic_getenv
from iotdb_mcp_server.runtime_policy import strict_permission_enforcement
from iotdb_mcp_server.services.json_response import payload_response, sql_success_response
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    tree_session_pool,
)


def _env_bool(name: str, default: bool) -> bool:
    return dynamic_env_bool(name, default)


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _assert_timeseries_ddl_permission(
    config: Config, action: str, confirm: bool = False
) -> None:
    if strict_permission_enforcement():
        if not _env_bool("IOTDB_ENABLE_TIMESERIES_DDL", False):
            raise PermissionError(
                "Timeseries DDL tools are disabled by strict MCP policy. "
                "Set IOTDB_ENABLE_TIMESERIES_DDL=true to enable."
            )

        allowed_users = _csv_set(dynamic_getenv("IOTDB_TIMESERIES_DDL_ALLOWED_USERS", "root") or "root")
        if "*" not in allowed_users and config.user not in allowed_users:
            raise PermissionError(
                f"Current MCP user '{config.user}' is not allowed by IOTDB_TIMESERIES_DDL_ALLOWED_USERS."
            )

    if (
        action == "DROP"
        and _env_bool("IOTDB_REQUIRE_TIMESERIES_DROP_CONFIRM", True)
        and not confirm
    ):
        raise PermissionError(
            "DROP/DELETE TIMESERIES requires confirm=True by server policy."
        )


def _normalize_sql(sql: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("DDL SQL cannot be empty.")
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if ";" in cleaned:
        raise ValueError("Only a single SQL statement is allowed.")
    return cleaned


def _ensure_prefix(sql: str, allowed_prefixes: tuple[str, ...], action_name: str) -> None:
    upper = sql.upper()
    if not upper.startswith(allowed_prefixes):
        raise ValueError(
            f"{action_name} only supports SQL starting with: {', '.join(allowed_prefixes)}"
        )


def _is_already_exists_error(message: str) -> bool:
    lower = message.lower()
    patterns = (
        "already exist",
        "already_exists",
        "path already exists",
        "timeseries already exists",
    )
    return any(p in lower for p in patterns)


def register_timeseries_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register tree-model timeseries-level DDL tools."""

    @mcp.tool()
    async def create_timeseries_ddl(
        ddl_sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute tree-model CREATE TIMESERIES DDL.

        Supported prefixes:
        - CREATE TIMESERIES
        - CREATE ALIGNED TIMESERIES
        """
        selected_config, session_pool = tree_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=100,
            wait_timeout_in_ms=5000,
            tool_name="create_timeseries_ddl",
        )
        with iotdb_target_response_context(selected_config):
            session = None
            try:
                _assert_timeseries_ddl_permission(selected_config, action="CREATE")
                sql = _normalize_sql(ddl_sql)
                _ensure_prefix(
                    sql,
                    ("CREATE TIMESERIES", "CREATE ALIGNED TIMESERIES"),
                    "create_timeseries_ddl",
                )
                session = session_pool.get_session()
                session.execute_non_query_statement(sql)
                session.close()
                return sql_success_response("create_timeseries_ddl", sql)
            except Exception as e:
                if session:
                    session.close()
                logger.error(f"Failed to execute create_timeseries_ddl: {str(e)}")
                raise

    @mcp.tool()
    async def create_timeseries_batch_ddl(
        ddl_list: list[str],
        continue_on_error: bool = True,
        skip_if_exists: bool = True,
        max_statements: int = 1000,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute a batch of tree-model CREATE TIMESERIES DDL in one session pipeline.

        Supported prefixes in each statement:
        - CREATE TIMESERIES
        - CREATE ALIGNED TIMESERIES
        """
        selected_config, session_pool = tree_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=100,
            wait_timeout_in_ms=5000,
            tool_name="create_timeseries_batch_ddl",
        )
        with iotdb_target_response_context(selected_config):
            session = None
            started_at = time.perf_counter()
            try:
                _assert_timeseries_ddl_permission(selected_config, action="CREATE")

                if not isinstance(ddl_list, list) or len(ddl_list) == 0:
                    raise ValueError("ddl_list must be a non-empty list of SQL statements.")
                if max_statements <= 0:
                    raise ValueError("max_statements must be greater than 0.")
                if len(ddl_list) > max_statements:
                    raise ValueError(
                        f"ddl_list length {len(ddl_list)} exceeds max_statements={max_statements}."
                    )

                validated_sqls: list[dict[str, Any]] = []
                per_statement: list[dict[str, Any]] = []
                failed_count = 0

                for idx, raw_sql in enumerate(ddl_list, start=1):
                    statement_started = time.perf_counter()
                    try:
                        sql = _normalize_sql(str(raw_sql))
                        _ensure_prefix(
                            sql,
                            ("CREATE TIMESERIES", "CREATE ALIGNED TIMESERIES"),
                            "create_timeseries_batch_ddl",
                        )
                        validated_sqls.append({"index": idx, "sql": sql})
                    except Exception as e:
                        failed_count += 1
                        per_statement.append(
                            {
                                "index": idx,
                                "sql": str(raw_sql),
                                "status": "invalid_sql",
                                "elapsed_ms": round(
                                    (time.perf_counter() - statement_started) * 1000, 3
                                ),
                                "error": str(e),
                            }
                        )
                        if not continue_on_error:
                            break

                applied_count = 0
                skipped_count = 0
                execution_aborted = False

                if validated_sqls and (failed_count == 0 or continue_on_error):
                    session = session_pool.get_session()
                    for item in validated_sqls:
                        idx = int(item["index"])
                        sql = str(item["sql"])
                        statement_started = time.perf_counter()
                        status = "applied"
                        error_msg = ""
                        try:
                            session.execute_non_query_statement(sql)
                            applied_count += 1
                        except Exception as e:
                            if skip_if_exists and _is_already_exists_error(str(e)):
                                status = "skipped_existing"
                                skipped_count += 1
                            else:
                                status = "error"
                                error_msg = str(e)
                                failed_count += 1
                                if not continue_on_error:
                                    execution_aborted = True
                        per_statement.append(
                            {
                                "index": idx,
                                "sql": sql,
                                "status": status,
                                "elapsed_ms": round(
                                    (time.perf_counter() - statement_started) * 1000, 3
                                ),
                                "error": error_msg,
                            }
                        )
                        if execution_aborted:
                            break

                if session:
                    session.close()

                per_statement.sort(key=lambda x: x.get("index", 0))
                total_elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
                executed_count = applied_count + skipped_count + (
                    len([x for x in per_statement if x.get("status") == "error"])
                )

                return payload_response(
                    "create_timeseries_batch_ddl",
                    {
                        "result": "success" if failed_count == 0 else "partial_success",
                        "execution_mode": "single_session_pipeline",
                        "continue_on_error": continue_on_error,
                        "skip_if_exists": skip_if_exists,
                        "total_input": len(ddl_list),
                        "validated_count": len(validated_sqls),
                        "executed_count": executed_count,
                        "applied_count": applied_count,
                        "skipped_count": skipped_count,
                        "failed_count": failed_count,
                        "total_elapsed_ms": total_elapsed_ms,
                        "statements": per_statement,
                    },
                    message="Batch CREATE TIMESERIES execution completed.",
                )
            except Exception as e:
                if session:
                    session.close()
                logger.error(f"Failed to execute create_timeseries_batch_ddl: {str(e)}")
                raise

    @mcp.tool()
    async def alter_timeseries_ddl(
        ddl_sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute tree-model ALTER TIMESERIES DDL."""
        selected_config, session_pool = tree_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=100,
            wait_timeout_in_ms=5000,
            tool_name="alter_timeseries_ddl",
        )
        with iotdb_target_response_context(selected_config):
            session = None
            try:
                _assert_timeseries_ddl_permission(selected_config, action="ALTER")
                sql = _normalize_sql(ddl_sql)
                _ensure_prefix(sql, ("ALTER TIMESERIES",), "alter_timeseries_ddl")
                session = session_pool.get_session()
                session.execute_non_query_statement(sql)
                session.close()
                return sql_success_response("alter_timeseries_ddl", sql)
            except Exception as e:
                if session:
                    session.close()
                logger.error(f"Failed to execute alter_timeseries_ddl: {str(e)}")
                raise

    @mcp.tool()
    async def drop_timeseries_ddl(
        ddl_sql: str,
        confirm: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute tree-model DROP/DELETE TIMESERIES DDL (destructive)."""
        selected_config, session_pool = tree_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=100,
            wait_timeout_in_ms=5000,
            tool_name="drop_timeseries_ddl",
        )
        with iotdb_target_response_context(selected_config):
            session = None
            try:
                _assert_timeseries_ddl_permission(
                    selected_config, action="DROP", confirm=confirm
                )
                sql = _normalize_sql(ddl_sql)
                _ensure_prefix(
                    sql,
                    ("DROP TIMESERIES", "DELETE TIMESERIES"),
                    "drop_timeseries_ddl",
                )
                session = session_pool.get_session()
                session.execute_non_query_statement(sql)
                session.close()
                return sql_success_response("drop_timeseries_ddl", sql)
            except Exception as e:
                if session:
                    session.close()
                logger.error(f"Failed to execute drop_timeseries_ddl: {str(e)}")
                raise
