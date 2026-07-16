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

from iotdb.Session import Session
from iotdb.table_session import TableSession
from iotdb.utils.SessionDataSet import SessionDataSet
from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.services.json_response import error_response, payload_response
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    select_target_config,
    table_session_pool,
    tree_session_pool,
)
from iotdb_mcp_server.services.tree_sql_guardrails import assert_tree_query_shape


def _collect_result(
    res: SessionDataSet, session_or_table_session: Session | TableSession
) -> tuple[list[str], list[str]]:
    columns = res.get_column_names()
    rows: list[str] = []
    while res.has_next():
        row = res.next().get_fields()
        rows.append(",".join(map(str, row)))
    session_or_table_session.close()
    return columns, rows


def _normalize_and_validate_sql(
    sql_dialect: str, sql: str, analyze: bool
) -> tuple[str, str]:
    raw_sql = sql.strip()
    if not raw_sql:
        raise ValueError("SQL cannot be empty")

    explain_sql = raw_sql
    upper = raw_sql.upper()

    if not upper.startswith("EXPLAIN"):
        explain_prefix = "EXPLAIN ANALYZE " if analyze else "EXPLAIN "
        explain_sql = explain_prefix + raw_sql
        target_sql = raw_sql.strip()
    else:
        # Accept user-provided EXPLAIN; do not override existing options.
        target_sql = raw_sql[len("EXPLAIN") :].strip()
        if target_sql.upper().startswith("ANALYZE"):
            target_sql = target_sql[len("ANALYZE") :].strip()

    target_upper = target_sql.upper()
    allowed_prefixes = (
        ("SELECT", "SHOW", "COUNT", "WITH")
        if sql_dialect == "tree"
        else ("SELECT", "SHOW", "DESC", "DESCRIBE", "WITH")
    )

    if not target_upper.startswith(allowed_prefixes):
        raise ValueError(
            f"explain_query only supports query-like SQL for {sql_dialect} dialect. "
            f"Allowed prefixes: {', '.join(allowed_prefixes)}"
        )
    if sql_dialect == "tree" and target_upper.startswith("SELECT"):
        assert_tree_query_shape(target_sql)

    return explain_sql, target_sql


def register_explain_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register EXPLAIN tool with per-call IoTDB target selection."""
    max_pool_size = 100

    @mcp.tool()
    async def explain_query(
        query_sql: str,
        analyze: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute EXPLAIN for one statement against the selected IoTDB target."""
        selected_config = select_target_config(config, target_id=target_id, target=target)
        with iotdb_target_response_context(selected_config):
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="explain_query",
                )
                session = None
                try:
                    explain_sql, _ = _normalize_and_validate_sql(
                        selected_config.sql_dialect, query_sql, analyze
                    )
                    session = session_pool.get_session()
                    res = session.execute_query_statement(explain_sql)
                    columns, rows = _collect_result(res, session)
                    return payload_response(
                        "explain_query",
                        {
                            "explain_sql": explain_sql,
                            "plan": {
                                "format": "csv",
                                "columns": columns,
                                "rows": rows,
                                "text": "\n".join([",".join(columns)] + rows),
                            },
                        },
                        message="Explain executed.",
                    )
                except Exception as e:
                    if session:
                        session.close()
                    logger.error(f"Failed to execute explain query: {str(e)}")
                    return error_response("explain_query", str(e))

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="explain_query",
            )
            table_session = None
            try:
                explain_sql, _ = _normalize_and_validate_sql(
                    selected_config.sql_dialect, query_sql, analyze
                )
                table_session = session_pool.get_session()
                res = table_session.execute_query_statement(explain_sql)
                columns, rows = _collect_result(res, table_session)
                return payload_response(
                    "explain_query",
                    {
                        "explain_sql": explain_sql,
                        "plan": {
                            "format": "csv",
                            "columns": columns,
                            "rows": rows,
                            "text": "\n".join([",".join(columns)] + rows),
                        },
                    },
                    message="Explain executed.",
                )
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to execute explain query: {str(e)}")
                return error_response("explain_query", str(e))
