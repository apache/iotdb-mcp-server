# IoTDB MCP Services

This directory contains tool modules registered by `src/iotdb_mcp_server/server.py`.

## Registration Overview

Current registration order:

1. `register_target_tools`
2. `register_runtime_policy_tools`
3. `register_result_store_tools`
4. `register_query_tools`
5. `register_sql_driver_tools`
6. `register_metadata_tools`
7. `register_explain_tools`
8. `register_database_tools`
9. `register_timeseries_tools`
10. `register_ttl_tools`
11. `register_table_tools`
12. `register_udf_tools`
13. `register_write_tools`
14. `register_model_tools`

## Tool Matrix

### `query.py`

- Tree dialect:
  - `select_query(query_sql, max_inline_rows=None, page_size_rows=None)`
  - `export_query(query_sql, format="csv", filename=None)`
- Table dialect:
  - `read_query(query_sql, max_inline_rows=None, page_size_rows=None)`
  - `export_table_query(query_sql, format="csv", filename=None)`

Read-query tools stream rows into ResultStore and return only a preview by
default. Use the returned `result_id` and `next_cursor` with
`read_result_page`.

### `results.py`

- `read_result_page(result_id, cursor=None, offset=None, limit=None, owner_session_id=None, target_id=None)`
- `read_result_pages(pages, owner_session_id=None, default_limit=None, max_pages=None, max_total_rows=None, continue_on_error=True, target_id=None)`
- `list_result_store(owner_session_id=None, all_sessions=False, limit=50, target_id=None)`
- `cleanup_result_store(owner_session_id=None, all_sessions=False, ttl_seconds=None, target_id=None)`
- `delete_result(result_id, owner_session_id=None, all_sessions=False, target_id=None)`

Loads a bounded page from a file-backed ResultStore result. Pass the same
`target_id` as the original query when multiple IoTDB targets are registered.
When the TimeSeek Codex plugin hook is active, `owner_session_id` is injected
from the Codex session. Standalone MCP usage can pass `owner_session_id`
explicitly or set `TIMESEEK_MCP_SESSION_ID`; otherwise the ResultStore owner is
`standalone`.

ResultStore cleanup is finite and session-aware. Each write runs lightweight
cleanup for the current owner and then enforces global limits. Entries expire by
`last_accessed_at`, and quota pressure evicts least-recently-accessed results.
If a single new result is larger than the byte quota, it is retained so the
caller can still page through the result that was just returned.

`read_result_pages` batches multiple page requests in one MCP call. Each item
accepts `result_id` plus optional `cursor`, `offset`, `limit`, and
`owner_session_id`. The default per-call limits are 32 pages and 10000 returned
rows; override them with tool arguments or `TIMESEEK_RESULT_MAX_BATCH_PAGES`
and `TIMESEEK_RESULT_MAX_BATCH_READ_ROWS`.

### `sql_driver.py`

- Both dialects:
  - `sql_driver_policy()`
  - `inspect_sql_permission(sql)`
  - `sql_executor_batch(sqls=None, sql_template=None, param_sets=None, max_concurrency=4, worker_pool_size=None, per_item_timeout_ms=None, batch_timeout_ms=None, max_result_rows_per_item=None, max_result_bytes_per_item=None, max_batch_result_rows=None, max_batch_result_bytes=None, continue_on_error=True, max_inline_rows=5, page_size_rows=None)`
  - `sql_execute(sql, confirm_destructive=False, max_inline_rows=None, page_size_rows=None)`

Features:

- Statement whitelist by category: `readonly` / `ddl` / `full`
- Advisory permission metadata via `IOTDB_SQL_DRIVER_MODE`
- SQL inspection returns `required_permission`, `destructive`, `risk_level`,
  `approval_required`, and the confirmation parameter without executing SQL
- Destructive SQL confirmation policy
- `sql_executor_batch` executes only readonly single statements. It accepts
  either explicit `sqls=[...]` or a repeated `sql_template` plus `param_sets`.
  Template placeholders are `{{name}}` for SQL literals, `{{name:path}}` for
  IoTDB paths, and `{{name:identifier}}` for SQL identifiers.
- Batch results are per-statement ResultStore entries. The batch response
  returns status, `result_id`, row counts, a small preview, and paging metadata;
  use `read_result_page` to fetch full result pages.
- Batch execution controls include a bounded thread worker pool, per-item and
  whole-batch wait timeouts, and per-item / whole-batch result row and byte
  quotas. Defaults: 4 concurrency, 60000 ms per item, 300000 ms per batch,
  10000 rows and 16 MiB per item, 100000 rows and 64 MiB per batch.
- Tree CQ enabled in whitelist:
  - Readonly: `SHOW CONTINUOUS QUERIES`, `SHOW CQS`
  - DDL: `CREATE CONTINUOUS QUERY` / `CREATE CQ`, `DROP CONTINUOUS QUERY` / `DROP CQ`
- Downsampling execution path:
  - ad-hoc: `SELECT ... GROUP BY ([start, end), interval[, slidingStep])`
  - scheduled: CQ with `INTO` + optional `RESAMPLE` clause

### `runtime_policy.py`

- Session scoped permission controls:
  - `get_iotdb_session_policy()`
  - `set_iotdb_session_policy(policy=None, preset=None, replace=False)`
  - `reset_iotdb_session_policy(keys=None)`
- Presets:
  - `full`: advisory signal for read, DDL, and DML-capable workflows
  - `ddl`: advisory signal for read and DDL-capable workflows
  - `readonly`: advisory signal for read-only workflows
  - In `strict` mode only, these presets become hard execution gates
- Effective policy priority:
  - session overlay set by MCP tool
  - dynamically read `.mcp.json`
  - process environment
  - built-in full-permission defaults

### `metadata.py`

- Tree dialect:
  - `metadata_query(query_sql)`
  - `list_timeseries(path="root.**")`
  - `list_devices(path="root.**")`
  - `list_child_paths(path)`
  - `list_child_nodes(path)`
  - `count_timeseries(path="root.**")`
  - `count_devices(path="root.**")`
  - `count_nodes(path="root")`
- Table dialect:
  - `metadata_query(query_sql)`
  - `list_tables()`
  - `describe_table(table_name, details=True)`

Metadata result sets also use ResultStore, which keeps large `SHOW TIMESERIES`
or `SHOW DEVICES` responses bounded.

### `explain.py`

- Both dialects:
  - `explain_query(query_sql, analyze=False)`

### `database.py`

- Tree dialect:
  - `list_databases(details=False)`
  - `create_database(database)`
  - `drop_database(database, confirm=False)`
- Table dialect:
  - `list_databases(details=False)`
  - `create_database(database, if_not_exists=True)`
  - `drop_database(database, if_exists=True, confirm=False)`
  - `use_database(database)`

### `timeseries.py` (tree only)

- `create_timeseries_ddl(ddl_sql)`
- `create_timeseries_batch_ddl(ddl_list, continue_on_error=True, skip_if_exists=True, max_statements=1000)`
- `alter_timeseries_ddl(ddl_sql)`
- `drop_timeseries_ddl(ddl_sql, confirm=False)`

### `ttl.py` (tree only)

- `ttl_command(ttl_sql, confirm_unset=False)`
- `ttl_query(ttl_sql)`

### `table.py` (table only)

- `create_table_ddl(ddl_sql)`
- `alter_table_ddl(ddl_sql)`
- `drop_table_ddl(ddl_sql, confirm=False)`

### `udf.py`

- Both dialects:
  - `list_udf_functions()`
  - `prepare_udf_query(function_name, expressions, table_name=None, from_path=None, attributes=None, where_clause=None, limit=None, alias=None, align_by_device=False)`
  - `execute_udf_query(function_name, expressions, table_name=None, from_path=None, attributes=None, where_clause=None, limit=None, alias=None, align_by_device=False, max_inline_rows=None, page_size_rows=None)`
  - `export_udf_query(function_name, expressions, table_name=None, from_path=None, attributes=None, where_clause=None, limit=None, alias=None, align_by_device=False, format="csv", filename=None)`

Features:

- `SHOW FUNCTIONS` discovery through `list_udf_functions`
- Conservative SQL generation for UDF calls instead of accepting raw SQL
- Table dialect shape: `SELECT UDF(column, "k"="v") FROM table ...`
- Tree dialect shape: `SELECT UDF(measurement, "k"="v") FROM root.sg.d1 ...`
- Read-only execution only; DDL/DML keywords, semicolons, and comments are rejected in expressions and `where_clause`
- ResultStore-backed previews for inline execution and export support for large UDF outputs

### `write.py`

- Tree dialect:
  - `write_query(write_sql, confirm_delete=False)` (INSERT/DELETE)
- Table dialect:
  - `write_query(write_sql, confirm_delete=False)` (INSERT/UPDATE/DELETE)

### `model.py`

- Both dialects:
  - `model_query(model_sql)`
  - `model_command(model_sql, confirm_destructive=False)`
- Tree dialect:
  - `prepare_model_inference_request(model_id, input_sql, output_length=96, generate_time=False)`
  - `model_inference(inference_sql)`

### `json_response.py`

- Shared response wrapper and parser:
  - `JsonParser.check_format(obj)`
  - `JsonParser.parse(text)`
  - `payload_response(tool, payload, message=None)`
  - `text_payload_response(tool, text, message=None)`
  - `csv_payload_response(tool, columns, rows, message=None)`
  - `sql_success_response(tool, sql)`

## Tool Response JSON Envelope

All tools now return `TextContent.text` as a JSON envelope instead of plain text.

Envelope format:

```json
{
  "version": "1.0",
  "tool": "tool_name",
  "ok": true,
  "timestamp": "2026-02-26T00:00:00+00:00",
  "payload": {},
  "message": "optional"
}
```

Payload conventions:

- Query-like tools: `payload.format="csv"` with `columns`, preview `rows`,
  preview `text`, `row_count`, `inline_row_count`, `inline_truncated`, and
  ResultStore fields when persisted
- Text-like tools: `payload.format="text"` with `text`
- SQL success tools: `payload.sql` + `payload.result="success"`

For ResultStore-backed responses:

```json
{
  "format": "csv",
  "columns": ["Time", "root.sg.d.s0"],
  "rows": ["0,1.0"],
  "text": "Time,root.sg.d.s0\n0,1.0",
  "row_count": 100000,
  "inline_row_count": 1,
  "inline_truncated": true,
  "result_id": "res_20260704T000000Z_0123456789abcdef",
  "owner_session_id": "ses_abc123",
  "next_cursor": "1",
  "result_store": {
    "type": "file",
    "page_tool": "read_result_page",
    "batch_page_tool": "read_result_pages",
    "owner_session_id": "ses_abc123",
    "page_size_rows": 500,
    "shard_count": 10
  }
}
```

Environment knobs:

- `TIMESEEK_RESULT_PREVIEW_ROWS` default `50`
- `TIMESEEK_RESULT_PAGE_SIZE_ROWS` default `500`
- `TIMESEEK_RESULT_MAX_PAGE_ROWS` default `5000`
- `TIMESEEK_RESULT_SHARD_ROWS` default `10000`
- `TIMESEEK_RESULT_SHARD_BYTES` default `8388608`
- `TIMESEEK_RESULT_TTL_SECONDS` default `86400`; set `0` to disable TTL expiry
- `TIMESEEK_RESULT_MAX_SESSION_RESULTS` default `100`; set `0` to disable
- `TIMESEEK_RESULT_MAX_SESSION_BYTES` default `536870912`; set `0` to disable
- `TIMESEEK_RESULT_MAX_GLOBAL_RESULTS` default `1000`; set `0` to disable
- `TIMESEEK_RESULT_MAX_GLOBAL_BYTES` default `2147483648`; set `0` to disable
- `TIMESEEK_MCP_SESSION_ID` optional standalone ResultStore owner fallback
- `IOTDB_SQL_EXECUTOR_BATCH_MAX_STATEMENTS` default `64`
- `IOTDB_SQL_EXECUTOR_BATCH_MAX_CONCURRENCY` default `16`

Parser check:

- Wrapper output is validated by `JsonParser.check_format(...)`
- Serialized text is parsed again by `JsonParser.parse(...)` before returning
- Invalid envelope shape raises `ValueError`

## Security and Permission Model

IoTDB MCP permissions default to host-agent approval flow. The MCP server reports
required permission and risk; Codex, Claude Code, or OpenCode owns user approval
through its normal question/approval mechanism. This keeps MCP policy aligned
with the host agent system instead of turning `.mcp.json` into a hard blocker.

Default mode is `advisory`. In advisory mode:

- `IOTDB_SQL_DRIVER_MODE` is a prompt-layer signal, not an execution blocker.
- `IOTDB_ENABLE_*` and `*_ALLOWED_USERS` are prompt-layer/session settings, not
  hard gates.
- Destructive operations still require explicit tool confirmation flags, such
  as `confirm_destructive=true`, after the host agent has obtained approval.

Strict hard gating is available only when explicitly enabled:

```bash
TIMESEEK_MCP_PERMISSION_ENFORCEMENT=strict
# or
IOTDB_STRICT_PERMISSION_ENFORCEMENT=true
```

Policy can be changed at runtime without restarting the MCP server:

```json
{
  "preset": "readonly",
  "replace": true
}
```

or:

```json
{
  "policy": {
    "IOTDB_SQL_DRIVER_MODE": "full",
    "IOTDB_ENABLE_WRITE_DML": "true"
  }
}
```

### Per-module advisory keys

- `database.py`
  - `IOTDB_ENABLE_DATABASE_DDL`
  - `IOTDB_DATABASE_DDL_ALLOWED_USERS`
  - `IOTDB_REQUIRE_DROP_CONFIRM`
- `timeseries.py`
  - `IOTDB_ENABLE_TIMESERIES_DDL`
  - `IOTDB_TIMESERIES_DDL_ALLOWED_USERS`
  - `IOTDB_REQUIRE_TIMESERIES_DROP_CONFIRM`
- `table.py`
  - `IOTDB_ENABLE_TABLE_DDL`
  - `IOTDB_TABLE_DDL_ALLOWED_USERS`
  - `IOTDB_REQUIRE_TABLE_DROP_CONFIRM`
- `ttl.py`
  - `IOTDB_ENABLE_TTL_SQL`
  - `IOTDB_TTL_ALLOWED_USERS`
  - `IOTDB_REQUIRE_TTL_UNSET_CONFIRM`
- `write.py`
  - `IOTDB_ENABLE_WRITE_DML`
  - `IOTDB_WRITE_ALLOWED_USERS`
  - `IOTDB_REQUIRE_DELETE_CONFIRM`
- `model.py`
  - `IOTDB_ENABLE_MODEL_MANAGEMENT`
  - `IOTDB_MODEL_ALLOWED_USERS`
  - `IOTDB_REQUIRE_MODEL_DESTRUCTIVE_CONFIRM`
  - `model_inference` is registered only for tree dialect and accepts
    `CALL INFERENCE(...)` SQL.
- `metadata.py`
  - `IOTDB_ENABLE_METADATA_QUERY`
  - `IOTDB_METADATA_ALLOWED_USERS`
- `sql_driver.py`
  - `IOTDB_ENABLE_SQL_DRIVER`
  - `IOTDB_SQL_DRIVER_ALLOWED_USERS`
  - `IOTDB_SQL_DRIVER_MODE` (`readonly` / `ddl` / `full`)
  - `IOTDB_SQL_DRIVER_REQUIRE_DESTRUCTIVE_CONFIRM`
  - `IOTDB_SQL_DRIVER_EXTRA_READONLY_PREFIXES`
  - `IOTDB_SQL_DRIVER_EXTRA_DDL_PREFIXES`
  - `IOTDB_SQL_DRIVER_EXTRA_FULL_PREFIXES`

## Minimal Enablement Example

```bash
# Dialect
IOTDB_SQL_DIALECT=tree

# Keep generic driver available in full mode
IOTDB_ENABLE_SQL_DRIVER=true
IOTDB_SQL_DRIVER_MODE=full
IOTDB_SQL_DRIVER_ALLOWED_USERS=*

# Enable specialized tools
IOTDB_ENABLE_METADATA_QUERY=true
IOTDB_ENABLE_DATABASE_DDL=true
IOTDB_ENABLE_TIMESERIES_DDL=true
IOTDB_ENABLE_TTL_SQL=true
IOTDB_ENABLE_WRITE_DML=true
IOTDB_ENABLE_MODEL_MANAGEMENT=true
```

## Notes

- All tools enforce single-statement execution (`;` separated multi-statement is rejected).
- Tool availability depends on `sql_dialect` (`tree` or `table`).
- For production hard-block behavior, set `TIMESEEK_MCP_PERMISSION_ENFORCEMENT=strict`.
- Keep destructive confirm flags enabled unless an external approval layer
  provides equivalent per-action confirmation.
