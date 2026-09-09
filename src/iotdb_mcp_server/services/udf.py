from __future__ import annotations

import datetime
import logging
import re
import uuid
from typing import Any

from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.result_store import result_store_from_export_path
from iotdb_mcp_server.services.json_response import (
    csv_result_payload_response,
    payload_response,
    text_payload_response,
)
from iotdb_mcp_server.services.query import (
    _ensure_export_directory,
    _prepare_table_res,
    _prepare_tree_res,
    sanitize_filename,
)
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    select_target_config,
    table_session_pool,
    tree_session_pool,
)
from iotdb_mcp_server.services.tree_sql_guardrails import assert_tree_query_shape

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TREE_PATH_RE = re.compile(r"^root(?:\.[A-Za-z_][A-Za-z0-9_]*|\.\*|\.\*\*)*$")
_TREE_EXPR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_DANGEROUS_SQL_RE = re.compile(
    r";|--|/\*|\*/|\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|LOAD|SET|GRANT|REVOKE|TRUNCATE)\b",
    re.IGNORECASE,
)


def _validate_identifier(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid {label}. Use letters, numbers, and underscore only.")
    return cleaned


def _validate_function_name(function_name: str) -> str:
    return _validate_identifier(function_name, "function_name")


def _validate_tree_path(path: str) -> str:
    cleaned = str(path or "").strip()
    if not _TREE_PATH_RE.fullmatch(cleaned):
        raise ValueError("Invalid tree from_path. Use path like root.sg.d1 or root.sg.*.")
    return cleaned


def _validate_expression(expression: str, *, dialect: str) -> str:
    cleaned = str(expression or "").strip()
    if not cleaned:
        raise ValueError("UDF expression cannot be empty.")
    if _DANGEROUS_SQL_RE.search(cleaned):
        raise ValueError("UDF expression contains unsupported SQL syntax.")
    if dialect == "table":
        return _validate_identifier(cleaned, "table expression")
    if not _TREE_EXPR_RE.fullmatch(cleaned):
        raise ValueError("Invalid tree expression. Use measurement names or relative paths.")
    return cleaned


def _validate_where_clause(where_clause: str | None) -> str | None:
    if where_clause is None:
        return None
    cleaned = str(where_clause).strip()
    if not cleaned:
        return None
    if _DANGEROUS_SQL_RE.search(cleaned):
        raise ValueError("where_clause contains unsupported or non-readonly SQL syntax.")
    if cleaned.upper().startswith("WHERE "):
        cleaned = cleaned[6:].strip()
    if not cleaned:
        return None
    return cleaned


def _render_attribute_value(value: Any) -> str:
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_attributes(attributes: dict[str, Any] | None) -> list[str]:
    if not attributes:
        return []
    rendered = []
    for key, value in attributes.items():
        safe_key = _validate_identifier(str(key), "attribute key")
        rendered.append(f'"{safe_key}"={_render_attribute_value(value)}')
    return rendered


def _normalize_expressions(expressions: list[str] | str | None, *, dialect: str) -> list[str]:
    if expressions is None:
        raise ValueError("expressions is required.")
    raw_values = [expressions] if isinstance(expressions, str) else list(expressions)
    if not raw_values:
        raise ValueError("expressions cannot be empty.")
    return [_validate_expression(str(value), dialect=dialect) for value in raw_values]


def _build_udf_query_plan(
    *,
    dialect: str,
    function_name: str,
    expressions: list[str] | str,
    table_name: str | None = None,
    from_path: str | None = None,
    attributes: dict[str, Any] | None = None,
    where_clause: str | None = None,
    limit: int | None = None,
    alias: str | None = None,
    align_by_device: bool = False,
) -> dict[str, Any]:
    safe_dialect = str(dialect or "").strip().lower()
    if safe_dialect not in {"tree", "table"}:
        raise ValueError("dialect must be tree or table.")
    safe_function = _validate_function_name(function_name)
    safe_expressions = _normalize_expressions(expressions, dialect=safe_dialect)
    function_args = safe_expressions + _render_attributes(attributes)
    projection = f"{safe_function}({', '.join(function_args)})"
    if alias:
        projection += f" AS {_validate_identifier(alias, 'alias')}"

    where = _validate_where_clause(where_clause)
    clauses: list[str]
    if safe_dialect == "table":
        source = _validate_identifier(str(table_name or ""), "table_name")
        clauses = [f"SELECT {projection}", f"FROM {source}"]
    else:
        source = _validate_tree_path(str(from_path or ""))
        clauses = [f"SELECT {projection}", f"FROM {source}"]

    if where:
        clauses.append(f"WHERE {where}")
    if limit is not None:
        safe_limit = int(limit)
        if safe_limit <= 0:
            raise ValueError("limit must be positive.")
        clauses.append(f"LIMIT {safe_limit}")
    if safe_dialect == "tree" and align_by_device:
        clauses.append("ALIGN BY DEVICE")

    sql = " ".join(clauses)
    return {
        "version": "v0",
        "kind": "iotdb_udf_query_plan_ir",
        "sql": sql,
        "dialect": safe_dialect,
        "function_name": safe_function,
        "expressions": safe_expressions,
        "source": {"table_name": source} if safe_dialect == "table" else {"from_path": source},
        "attributes": attributes or {},
        "where_clause": where,
        "limit": limit,
        "alias": alias,
        "align_by_device": bool(align_by_device),
        "readonly": True,
    }


def _export_dataset(
    *,
    res: Any,
    session: Any,
    selected_config: Config,
    tool_name: str,
    query_sql: str,
    filename: str | None,
    fmt: str,
    logger: logging.Logger,
) -> list[TextContent]:
    try:
        df = res.todf()
    finally:
        session.close()

    timestamp = int(datetime.datetime.now().timestamp())
    if filename is None:
        filename = f"udf_{uuid.uuid4().hex[:4]}_{timestamp}"

    fmt_lower = fmt.lower()
    if fmt_lower == "csv":
        if filename.lower().endswith(".csv"):
            filename = filename[:-4]
        filepath = sanitize_filename(f"{filename}.csv", selected_config.export_path)
        df.to_csv(filepath, index=False)
    elif fmt_lower == "excel":
        if filename.lower().endswith(".xlsx"):
            filename = filename[:-5]
        filepath = sanitize_filename(f"{filename}.xlsx", selected_config.export_path)
        df.to_excel(filepath, index=False)
    else:
        raise ValueError("format must be either 'csv' or 'excel'.")

    preview_rows = min(10, len(df))
    preview_data = [",".join(df.columns)]
    for index in range(preview_rows):
        preview_data.append(",".join(map(str, df.iloc[index])))
    logger.info("Exported UDF query result to %s", filepath)
    return text_payload_response(
        tool_name,
        f"Query results exported to {filepath}\n\nSQL:\n{query_sql}\n\nPreview (first {preview_rows} rows):\n"
        + "\n".join(preview_data),
    )


def _prepare_udf_res(
    res: Any,
    session: Any,
    tool_name: str,
    export_path: str,
    plan: dict[str, Any],
    owner_session_id: str | None = None,
    max_inline_rows: int | None = None,
    page_size_rows: int | None = None,
) -> list[TextContent]:
    columns = res.get_column_names()

    def rows():
        while res.has_next():
            record = res.next()
            fields = record.get_fields()
            if plan["dialect"] == "tree" and columns and columns[0] == "Time":
                yield str(record.get_timestamp()) + "," + ",".join(
                    map(str, fields)
                )
            else:
                yield ",".join(map(str, fields))

    try:
        return csv_result_payload_response(
            tool_name,
            columns,
            rows(),
            result_store=result_store_from_export_path(export_path),
            source={"query_sql": plan["sql"], "udf_query_plan": plan},
            diagnostics=[
                {
                    "code": "iotdb_udf_query_plan",
                    "query_sql": plan["sql"],
                    "function_name": plan["function_name"],
                    "dialect": plan["dialect"],
                    "readonly": True,
                }
            ],
            owner_session_id=owner_session_id,
            max_inline_rows=max_inline_rows,
            page_size_rows=page_size_rows,
        )
    finally:
        session.close()


def register_udf_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register read-only IoTDB UDF discovery and execution tools."""
    _ensure_export_directory(config.export_path, logger)
    max_pool_size = 100

    @mcp.tool()
    async def list_udf_functions(
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """List IoTDB functions/UDFs visible to the selected target via SHOW FUNCTIONS."""
        selected_config = select_target_config(config, target_id=target_id, target=target, tool_name="list_udf_functions")
        with iotdb_target_response_context(selected_config):
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="list_udf_functions",
                )
                session = None
                try:
                    session = session_pool.get_session()
                    res = session.execute_query_statement("SHOW FUNCTIONS")
                    return _prepare_tree_res(
                        res,
                        session,
                        "list_udf_functions",
                        selected_config.export_path,
                        "SHOW FUNCTIONS",
                        owner_session_id=owner_session_id,
                        max_inline_rows=max_inline_rows,
                        page_size_rows=page_size_rows,
                    )
                except Exception:
                    if session:
                        session.close()
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="list_udf_functions",
            )
            table_session = None
            try:
                table_session = session_pool.get_session()
                res = table_session.execute_query_statement("SHOW FUNCTIONS")
                return _prepare_table_res(
                    res,
                    table_session,
                    "list_udf_functions",
                    selected_config.export_path,
                    "SHOW FUNCTIONS",
                    owner_session_id=owner_session_id,
                    max_inline_rows=max_inline_rows,
                    page_size_rows=page_size_rows,
                )
            except Exception:
                if table_session:
                    table_session.close()
                raise

    @mcp.tool()
    async def prepare_udf_query(
        function_name: str,
        expressions: list[str] | str,
        table_name: str | None = None,
        from_path: str | None = None,
        attributes: dict[str, object] | None = None,
        where_clause: str | None = None,
        limit: int | None = None,
        alias: str | None = None,
        align_by_device: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Build a conservative read-only SQL plan for invoking an IoTDB UDF."""
        selected_config = select_target_config(config, target_id=target_id, target=target, tool_name="prepare_udf_query")
        with iotdb_target_response_context(selected_config):
            payload = _build_udf_query_plan(
                dialect=selected_config.sql_dialect,
                function_name=function_name,
                expressions=expressions,
                table_name=table_name,
                from_path=from_path,
                attributes=attributes,
                where_clause=where_clause,
                limit=limit,
                alias=alias,
                align_by_device=align_by_device,
            )
            return payload_response("prepare_udf_query", payload, message="UDF query plan prepared.")

    @mcp.tool()
    async def execute_udf_query(
        function_name: str,
        expressions: list[str] | str,
        table_name: str | None = None,
        from_path: str | None = None,
        attributes: dict[str, object] | None = None,
        where_clause: str | None = None,
        limit: int | None = None,
        alias: str | None = None,
        align_by_device: bool = False,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute one read-only IoTDB UDF SELECT built from validated inputs."""
        selected_config = select_target_config(config, target_id=target_id, target=target, tool_name="execute_udf_query")
        with iotdb_target_response_context(selected_config):
            plan = _build_udf_query_plan(
                dialect=selected_config.sql_dialect,
                function_name=function_name,
                expressions=expressions,
                table_name=table_name,
                from_path=from_path,
                attributes=attributes,
                where_clause=where_clause,
                limit=limit,
                alias=alias,
                align_by_device=align_by_device,
            )
            sql = plan["sql"]
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="execute_udf_query",
                )
                session = None
                try:
                    assert_tree_query_shape(sql)
                    session = session_pool.get_session()
                    res = session.execute_query_statement(sql)
                    return _prepare_udf_res(
                        res,
                        session,
                        "execute_udf_query",
                        selected_config.export_path,
                        plan,
                        owner_session_id=owner_session_id,
                        max_inline_rows=max_inline_rows,
                        page_size_rows=page_size_rows,
                    )
                except Exception:
                    if session:
                        session.close()
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="execute_udf_query",
            )
            table_session = None
            try:
                table_session = session_pool.get_session()
                res = table_session.execute_query_statement(sql)
                return _prepare_udf_res(
                    res,
                    table_session,
                    "execute_udf_query",
                    selected_config.export_path,
                    plan,
                    owner_session_id=owner_session_id,
                    max_inline_rows=max_inline_rows,
                    page_size_rows=page_size_rows,
                )
            except Exception:
                if table_session:
                    table_session.close()
                raise

    @mcp.tool()
    async def export_udf_query(
        function_name: str,
        expressions: list[str] | str,
        table_name: str | None = None,
        from_path: str | None = None,
        attributes: dict[str, object] | None = None,
        where_clause: str | None = None,
        limit: int | None = None,
        alias: str | None = None,
        align_by_device: bool = False,
        format: str = "csv",
        filename: str | None = None,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute one read-only IoTDB UDF SELECT and export the result set."""
        selected_config = select_target_config(config, target_id=target_id, target=target, tool_name="export_udf_query")
        with iotdb_target_response_context(selected_config):
            _ensure_export_directory(selected_config.export_path, logger)
            plan = _build_udf_query_plan(
                dialect=selected_config.sql_dialect,
                function_name=function_name,
                expressions=expressions,
                table_name=table_name,
                from_path=from_path,
                attributes=attributes,
                where_clause=where_clause,
                limit=limit,
                alias=alias,
                align_by_device=align_by_device,
            )
            sql = plan["sql"]
            if selected_config.sql_dialect == "tree":
                _, session_pool = tree_session_pool(
                    config,
                    target_id=selected_config.target_id,
                    max_pool_size=max_pool_size,
                    wait_timeout_in_ms=5000,
                    tool_name="export_udf_query",
                )
                session = None
                try:
                    assert_tree_query_shape(sql)
                    session = session_pool.get_session()
                    res = session.execute_query_statement(sql)
                    return _export_dataset(
                        res=res,
                        session=session,
                        selected_config=selected_config,
                        tool_name="export_udf_query",
                        query_sql=sql,
                        filename=filename,
                        fmt=format,
                        logger=logger,
                    )
                except Exception:
                    if session:
                        session.close()
                    raise

            _, session_pool = table_session_pool(
                config,
                target_id=selected_config.target_id,
                max_pool_size=max_pool_size,
                tool_name="export_udf_query",
            )
            table_session = None
            try:
                table_session = session_pool.get_session()
                res = table_session.execute_query_statement(sql)
                return _export_dataset(
                    res=res,
                    session=table_session,
                    selected_config=selected_config,
                    tool_name="export_udf_query",
                    query_sql=sql,
                    filename=filename,
                    fmt=format,
                    logger=logger,
                )
            except Exception:
                if table_session:
                    table_session.close()
                raise
