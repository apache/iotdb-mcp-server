import datetime
import logging
import os
import re
import uuid

from iotdb.Session import Session
from iotdb.table_session import TableSession
from iotdb.utils.SessionDataSet import SessionDataSet
from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.result_store import result_store_from_export_path
from iotdb_mcp_server.services.json_response import (
    csv_result_payload_response,
    text_payload_response,
)
from iotdb_mcp_server.services.target_selection import (
    iotdb_target_response_context,
    table_session_pool,
    tree_session_pool,
)
from iotdb_mcp_server.services.tree_sql_guardrails import assert_tree_query_shape
from iotdb_mcp_server.services.tree_sql_guardrails import (
    tree_from_wildcard_runtime_hint,
)


def sanitize_filename(filename: str, base_dir: str) -> str:
    """
    Sanitize and validate filename to prevent path traversal attacks.

    Security patch for CVE-2026-XXXXX
    Author: Mohammed Tanveer (threatpointer)
    Date: 2026-01-12

    Args:
        filename: The user-provided filename
        base_dir: The base directory for exports (must be absolute path)

    Returns:
        The sanitized absolute filepath

    Raises:
        ValueError: If the filename contains invalid characters or attempts path traversal

    Security measures:
    - Accepts path-like inputs but strips directories and uses only the basename
      under base_dir
    - Rejects directory traversal segments before processing
    - Validates allowed characters (alphanumeric, underscore, hyphen, dot)
    - Resolves absolute path and verifies it stays within base_dir boundary
    - Prevents directory traversal, symlink attacks, and path manipulation
    """
    if not filename:
        raise ValueError("Filename cannot be empty")

    normalized = filename.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part]
    if any(part == ".." for part in parts):
        raise ValueError(
            "Invalid filename: directory traversal sequences are not allowed"
        )

    filename = os.path.basename(parts[-1] if parts else normalized)

    if not re.match(r"^[a-zA-Z0-9_\-\.]+$", filename):
        raise ValueError(
            "Invalid filename: only alphanumeric characters, underscore, hyphen, and dot are allowed"
        )

    if not filename or filename in (".", ".."):
        raise ValueError("Invalid filename")

    if filename.startswith(".."):
        raise ValueError("Invalid filename: cannot start with '..'")

    filepath = os.path.join(base_dir, filename)
    filepath_real = os.path.realpath(filepath)
    basedir_real = os.path.realpath(base_dir)

    if (
        not filepath_real.startswith(basedir_real + os.sep)
        and filepath_real != basedir_real
    ):
        raise ValueError(
            "Path traversal detected: file must be within export directory"
        )

    return filepath_real


def _ensure_export_directory(export_path: str, logger: logging.Logger) -> None:
    if os.path.exists(export_path):
        return
    try:
        os.makedirs(export_path)
        logger.info(f"Created export directory: {export_path}")
    except Exception as e:
        logger.warning(f"Failed to create export directory {export_path}: {str(e)}")


def _prepare_tree_res(
    _res: SessionDataSet,
    _session: Session,
    tool_name: str,
    export_path: str,
    query_sql: str | None = None,
    owner_session_id: str | None = None,
    max_inline_rows: int | None = None,
    page_size_rows: int | None = None,
) -> list[TextContent]:
    columns = _res.get_column_names()

    def rows():
        while _res.has_next():
            record = _res.next()
            if columns and columns[0] == "Time":
                timestamp = record.get_timestamp()
                row = record.get_fields()
                yield str(timestamp) + "," + ",".join(map(str, row))
            else:
                row = record.get_fields()
                yield ",".join(map(str, row))

    def diagnostics_factory(row_count: int):
        diagnostics = []
        if query_sql:
            hint = tree_from_wildcard_runtime_hint(query_sql, row_count=row_count)
            if hint:
                diagnostics.append(hint)
        return diagnostics

    try:
        return csv_result_payload_response(
            tool_name,
            columns,
            rows(),
            result_store=result_store_from_export_path(export_path),
            source={"query_sql": query_sql} if query_sql else None,
            diagnostics_factory=diagnostics_factory,
            owner_session_id=owner_session_id,
            max_inline_rows=max_inline_rows,
            page_size_rows=page_size_rows,
        )
    finally:
        _session.close()


def _prepare_table_res(
    _res: SessionDataSet,
    _table_session: TableSession,
    tool_name: str,
    export_path: str,
    query_sql: str | None = None,
    owner_session_id: str | None = None,
    max_inline_rows: int | None = None,
    page_size_rows: int | None = None,
) -> list[TextContent]:
    columns = _res.get_column_names()

    def rows():
        while _res.has_next():
            row = _res.next().get_fields()
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
        _table_session.close()


def register_query_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register query tools with per-call IoTDB target selection."""
    _ensure_export_directory(config.export_path, logger)
    max_pool_size = 100

    @mcp.tool()
    async def select_query(
        query_sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute one tree-dialect read query and return rows.

        Use target_id or target selector fields to choose a registered IoTDB target.
        """
        selected_config, session_pool = tree_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=max_pool_size,
            wait_timeout_in_ms=5000,
            tool_name="select_query",
        )
        with iotdb_target_response_context(selected_config):
            session = None
            try:
                stmt = query_sql.strip().upper()
                if stmt.startswith("SELECT"):
                    assert_tree_query_shape(query_sql)
                    session = session_pool.get_session()
                    res = session.execute_query_statement(query_sql)
                    return _prepare_tree_res(
                        res,
                        session,
                        "select_query",
                        selected_config.export_path,
                        query_sql,
                        owner_session_id=owner_session_id,
                        max_inline_rows=max_inline_rows,
                        page_size_rows=page_size_rows,
                    )
                raise ValueError("Only SELECT queries are allowed for select_query")
            except Exception as e:
                if session:
                    session.close()
                logger.error(f"Failed to execute select query: {str(e)}")
                raise

    @mcp.tool()
    async def export_query(
        query_sql: str,
        format: str = "csv",
        filename: str = None,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute one tree-dialect read query and export its result set."""
        selected_config, session_pool = tree_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=max_pool_size,
            wait_timeout_in_ms=5000,
            tool_name="export_query",
        )
        with iotdb_target_response_context(selected_config):
            _ensure_export_directory(selected_config.export_path, logger)
            session = None
            try:
                stmt = query_sql.strip().upper()
                if not (stmt.startswith("SELECT") or stmt.startswith("SHOW")):
                    raise ValueError(
                        "Only SELECT or SHOW queries are allowed for export"
                    )
                if stmt.startswith("SELECT"):
                    assert_tree_query_shape(query_sql)

                session = session_pool.get_session()
                res = session.execute_query_statement(query_sql)
                df = res.todf()
                session.close()

                timestamp = int(datetime.datetime.now().timestamp())
                if filename is None:
                    filename = f"dump_{uuid.uuid4().hex[:4]}_{timestamp}"

                if format.lower() == "csv":
                    if filename.lower().endswith(".csv"):
                        filename = filename[:-4]
                    filepath = sanitize_filename(
                        f"{filename}.csv", selected_config.export_path
                    )
                    df.to_csv(filepath, index=False)
                elif format.lower() == "excel":
                    if filename.lower().endswith(".xlsx"):
                        filename = filename[:-5]
                    filepath = sanitize_filename(
                        f"{filename}.xlsx", selected_config.export_path
                    )
                    df.to_excel(filepath, index=False)
                else:
                    raise ValueError("Format must be either 'csv' or 'excel'")

                preview_rows = min(10, len(df))
                preview_data = [",".join(df.columns)]
                for i in range(preview_rows):
                    preview_data.append(",".join(map(str, df.iloc[i])))

                wildcard_hint = tree_from_wildcard_runtime_hint(
                    query_sql, row_count=len(df)
                )
                diagnostic_text = ""
                if wildcard_hint:
                    diagnostic_text = (
                        "\n\nDiagnostic: "
                        + wildcard_hint["message"]
                        + " "
                        + wildcard_hint["rewrite_hint"]
                    )

                return text_payload_response(
                    "export_query",
                    f"Query results exported to {filepath}\n\nPreview (first {preview_rows} rows):\n"
                    + "\n".join(preview_data)
                    + diagnostic_text,
                )
            except Exception as e:
                if session:
                    session.close()
                logger.error(f"Failed to export query: {str(e)}")
                raise

    @mcp.tool()
    async def read_query(
        query_sql: str,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        owner_session_id: str | None = None,
        max_inline_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> list[TextContent]:
        """Execute one table-dialect read query and return rows."""
        selected_config, session_pool = table_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=max_pool_size,
            tool_name="read_query",
        )
        with iotdb_target_response_context(selected_config):
            table_session = None
            try:
                table_session = session_pool.get_session()
                stmt = query_sql.strip().upper()
                if (
                    stmt.startswith("SELECT")
                    or stmt.startswith("DESCRIBE")
                    or stmt.startswith("SHOW")
                ):
                    res = table_session.execute_query_statement(query_sql)
                    return _prepare_table_res(
                        res,
                        table_session,
                        "read_query",
                        selected_config.export_path,
                        query_sql,
                        owner_session_id=owner_session_id,
                        max_inline_rows=max_inline_rows,
                        page_size_rows=page_size_rows,
                    )
                table_session.close()
                raise ValueError("Only SELECT queries are allowed for read_query")
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to execute query: {str(e)}")
                raise

    @mcp.tool()
    async def export_table_query(
        query_sql: str,
        format: str = "csv",
        filename: str = None,
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Execute one table-dialect read query and export its result set."""
        selected_config, session_pool = table_session_pool(
            config,
            target_id=target_id,
            target=target,
            max_pool_size=max_pool_size,
            tool_name="export_table_query",
        )
        with iotdb_target_response_context(selected_config):
            _ensure_export_directory(selected_config.export_path, logger)
            table_session = None
            try:
                table_session = session_pool.get_session()
                stmt = query_sql.strip().upper()
                if not (
                    stmt.startswith("SELECT")
                    or stmt.startswith("SHOW")
                    or stmt.startswith("DESCRIBE")
                    or stmt.startswith("DESC")
                ):
                    raise ValueError(
                        "Only SELECT, SHOW or DESCRIBE queries are allowed for export"
                    )

                res = table_session.execute_query_statement(query_sql)
                df = res.todf()
                table_session.close()

                timestamp = int(datetime.datetime.now().timestamp())
                if filename is None:
                    filename = f"dump_{uuid.uuid4().hex[:4]}_{timestamp}"

                if format.lower() == "csv":
                    if filename.lower().endswith(".csv"):
                        filename = filename[:-4]
                    filepath = sanitize_filename(
                        f"{filename}.csv", selected_config.export_path
                    )
                    df.to_csv(filepath, index=False)
                elif format.lower() == "excel":
                    if filename.lower().endswith(".xlsx"):
                        filename = filename[:-5]
                    filepath = sanitize_filename(
                        f"{filename}.xlsx", selected_config.export_path
                    )
                    df.to_excel(filepath, index=False)
                else:
                    raise ValueError("Format must be either 'csv' or 'excel'")

                preview_rows = min(10, len(df))
                preview_data = [",".join(df.columns)]
                for i in range(preview_rows):
                    preview_data.append(",".join(map(str, df.iloc[i])))

                return text_payload_response(
                    "export_table_query",
                    f"Query results exported to {filepath}\n\nPreview (first {preview_rows} rows):\n"
                    + "\n".join(preview_data),
                )
            except Exception as e:
                if table_session:
                    table_session.close()
                logger.error(f"Failed to export table query: {str(e)}")
                raise
