[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/apache-iotdb-mcp-server-badge.png)](https://mseep.ai/app/apache-iotdb-mcp-server)

# IoTDB MCP Server

English | [中文](README-zh.md)

## Overview

A Model Context Protocol (MCP) server implementation that provides database interaction and business intelligence capabilities through IoTDB. This server enables running SQL queries and interacting with IoTDB using different SQL dialects (Tree Model and Table Model).

## Components

### Resources

The server doesn't expose any resources.

### Prompts

The server doesn't provide any prompts.

## Permission Model

IoTDB MCP permissions are advisory by default. The server reports the required
permission, risk level, and confirmation parameter for SQL actions, while the
host agent system owns user approval. Use `inspect_sql_permission` before
executing DDL/DML or destructive SQL when the tool is available.

Long-running hosted agents can provide full-permission defaults with
environment variables such as `IOTDB_SQL_DRIVER_MODE=full` and
`TIMESEEK_MCP_PERMISSION_ENFORCEMENT=advisory`. Set
`TIMESEEK_MCP_PERMISSION_ENFORCEMENT=strict` only when the MCP server itself
should hard-block disallowed operations.

### Tools

The server offers different tools for IoTDB Tree Model and Table Model. You can choose between them by setting the "IOTDB_SQL_DIALECT" configuration to either "tree" or "table".

Dialect-specific identifier semantics:

- Tree dialect:
  - `FROM` targets use explicit `root...` paths.
  - Projection expressions should usually use measurement names instead of full `root...` paths.
  - `IOTDB_DATABASE` is only a connection/session hint; tree SQL still needs explicit root paths.
- Table dialect:
  - `FROM` targets use table names in the current database.
  - Projection expressions use column names.

#### Tree Model

- `metadata_query`
  - Execute SHOW/COUNT queries to read metadata from the database
  - Input:
    - `query_sql` (string): The SHOW/COUNT SQL query to execute
  - Supported query types:
    - SHOW DATABASES [path]
    - SHOW TIMESERIES [path]
    - SHOW CHILD PATHS [path]
    - SHOW CHILD NODES [path]
    - SHOW DEVICES [path]
    - COUNT TIMESERIES [path]
    - COUNT NODES [path]
    - COUNT DEVICES [path]
  - Returns: Query results as array of objects
- `select_query`
  - Execute SELECT queries to read data from the database
  - Input:
    - `query_sql` (string): The SELECT SQL query to execute (using TREE dialect, time using ISO 8601 format, e.g. 2017-11-01T00:08:00.000)
  - Supported functions:
    - SUM, COUNT, MAX_VALUE, MIN_VALUE, AVG, VARIANCE, MAX_TIME, MIN_TIME, etc.
  - Returns: Query results as array of objects
- `sql_executor_batch`
  - Execute multiple readonly SQL statements in parallel and store each result in ResultStore
  - Input:
    - `sqls` (array): Explicit single SQL statements, or
    - `sql_template` + `param_sets`: Repeated SQL template with parameter objects
    - `max_concurrency` (integer): Concurrent statement limit, default 4
    - `worker_pool_size` (integer): Thread worker pool size, default follows concurrency and is capped by `IOTDB_SQL_EXECUTOR_BATCH_MAX_WORKER_POOL_SIZE` (default 16)
    - `per_item_timeout_ms` (integer): Per-statement wait timeout, default 60000
    - `batch_timeout_ms` (integer): Whole-batch wait timeout, default 300000
    - `max_result_rows_per_item` / `max_result_bytes_per_item`: Per-statement result quota, defaults 10000 rows and 16 MiB
    - `max_batch_result_rows` / `max_batch_result_bytes`: Whole-batch result quota, defaults 100000 rows and 64 MiB
  - Template placeholders:
    - `{{name}}` for SQL literals, `{{name:path}}` for IoTDB paths, `{{name:identifier}}` for SQL identifiers
  - Returns: Batch summary plus per-statement `result_id`, row count, preview rows, and paging metadata
- `read_result_pages`
  - Read multiple ResultStore pages in one MCP call
  - Input:
    - `pages` (array): Page request objects with `result_id` plus optional `cursor`, `offset`, `limit`, and `owner_session_id`
    - `default_limit` (integer): Default page size for items without `limit`
    - `max_pages` / `max_total_rows`: Per-call quotas, defaults 32 pages and 10000 rows
    - `continue_on_error` (boolean): Return per-item errors instead of aborting, default true
  - Returns: Batch page summary plus per-page rows, cursors, and errors
- `export_query`
  - Execute a query and export the results to a CSV or Excel file
  - Input:
    - `query_sql` (string): The SQL query to execute (using TREE dialect)
    - `format` (string): Export format, either "csv" or "excel" (default: "csv")
    - `filename` (string): Optional filename for the exported file. If not provided, a unique filename will be generated.
  - Returns: Information about the exported file and a preview of the data (max 10 rows)
- `model_inference`
  - Execute AINode `CALL INFERENCE(...)` SQL and return the result set
  - Input:
    - `inference_sql` (string): A single Tree-dialect SQL statement starting with `CALL INFERENCE`
  - Validates model id, quoted input SELECT SQL, explicit non-wildcard columns,
    and supported parameters (`generateTime`, `outputLength`) before execution
  - Permission metadata: model management is reported through the advisory MCP
    policy layer. In strict mode, `IOTDB_ENABLE_MODEL_MANAGEMENT=true` and
    `IOTDB_MODEL_ALLOWED_USERS` are enforced.
- `prepare_model_inference_request`
  - Build and validate AINode `CALL INFERENCE(...)` SQL from structured fields
  - Input:
    - `model_id` (string): Registered AINode model id
    - `input_sql` (string): Bounded Tree-dialect SELECT query used as model input
    - `output_length` (int): Forecast output length (default: 96)
    - `generate_time` (bool): Whether to request a Time column (default: false)

#### UDF Tools

- `list_udf_functions`
  - Execute `SHOW FUNCTIONS` for the selected IoTDB target.
- `prepare_udf_query`
  - Build a read-only UDF `SELECT` from structured inputs.
  - Tree form: `SELECT UDF(measurement, "k"="v") FROM root.sg.d1 ...`
  - Table form: `SELECT UDF(column, "k"="v") FROM table ...`
- `execute_udf_query`
  - Execute the validated UDF query and return a ResultStore-backed preview.
- `export_udf_query`
  - Execute the validated UDF query and export the result set to CSV or Excel.

UDF tools reject semicolons, SQL comments, and DDL/DML keywords in expressions
and filter clauses. They are intended for read-only UDF calls such as data
quality, profiling, repair planning, and anomaly scoring.

#### Table Model

##### Query Tools

- `read_query`
  - Execute SELECT queries to read data from the database
  - Input:
    - `query_sql` (string): The SELECT SQL query to execute (using TABLE dialect, time using ISO 8601 format, e.g. 2017-11-01T00:08:00.000)
  - Returns: Query results as array of objects

##### Schema Tools

- `list_tables`

  - Get a list of all tables in the database
  - No input required
  - Returns: Array of table names

- `describe_table`

  - View schema information for a specific table
  - Input:
    - `table_name` (string): Name of table to describe
  - Returns: Array of column definitions with names and types

- `export_table_query`
  - Execute a query and export the results to a CSV or Excel file
  - Input:
    - `query_sql` (string): The SQL query to execute (using TABLE dialect)
    - `format` (string): Export format, either "csv" or "excel" (default: "csv")
    - `filename` (string): Optional filename for the exported file. If not provided, a unique filename will be generated.
  - Returns: Information about the exported file and a preview of the data (max 10 rows)

## Configuration Options

IoTDB MCP Server supports the following configuration options, which can be set via environment variables or command-line arguments:

| Option        | Environment Variable | Default Value | Description                      |
| ------------- | -------------------- | ------------- | -------------------------------- |
| --host        | IOTDB_HOST           | 127.0.0.1     | IoTDB host address               |
| --port        | IOTDB_PORT           | 6667          | IoTDB port                       |
| --user        | IOTDB_USER           | root          | IoTDB username                   |
| --password    | IOTDB_PASSWORD       | empty         | IoTDB password                   |
| --database    | IOTDB_DATABASE       | test          | Table dialect: current database name. Tree dialect: optional session/root scope hint; queries still use explicit `root...` paths. |
| --sql-dialect | IOTDB_SQL_DIALECT    | table         | SQL dialect: tree or table       |
| --export-path | IOTDB_EXPORT_PATH    | /tmp          | Path for exporting query results |

The target registry contains only connections that have completed a successful
login. Call `prepare_iotdb_target` with non-secret fields, then pass explicitly
user-supplied credentials to `connect_iotdb_target` for one authentication
attempt. If credentials are absent, ask the user; never probe empty or default
passwords. An explicitly supplied empty password remains valid input.

Successful login atomically publishes the target and records its per-target
`last_known_good_credential`. Any connection-layer failure consumes the
candidate or evicts the published target. A retry requires a new candidate and
`user_confirmed_retry=true` after explicit user instruction. Public target
responses redact both the active password and last-known-good password.

When `TIMESEEK_IOTDB_TARGETS_FILE` is configured, successful connections are
persisted by default. The local Java CLI can reuse exactly that target through
`iotdb-target-cli`:

```bash
iotdb-target-cli --target-id cloud \
  --cli /opt/iotdb/sbin/start-cli.sh -- -e "SHOW VERSION"
```

The wrapper reloads the verified target on every invocation and supplies its
host, port, dialect, username, and last-known-good password. It does not pass
`-db` to `start-cli.sh`, because the Java CLI does not support that option;
select a table database with SQL `USE <database>`. For `import-data.sh` and
`import-data.bat`, which do support `-db`, a table target's database is injected
automatically. A verified empty password is represented by omitting `-pw`, and
command previews redact non-empty passwords. Calling `start-cli.sh` directly
does not read the target registry.

## Performance Optimizations

IoTDB MCP Server includes the following performance optimization features:

1. **Session Pool Management**: Uses optimized session pool configurations, supporting up to 100 concurrent sessions
2. **Optimized Fetch Size**: For queries, a fetch size of 1024 is set
3. **Connection Retry**: Configured automatic retry mechanism for connection failures
4. **Timeout Management**: Session wait timeout set to 5000 milliseconds for improved reliability
5. **Export Functionality**: Support for exporting query results to CSV or Excel formats

## Prerequisites

- Python environment
- `uv` package manager
- IoTDB installation
- MCP server dependencies

## Development

```bash
# Clone the repository
git clone https://github.com/apache/iotdb-mcp-server.git
cd iotdb-mcp-server

# Create virtual environment
uv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install development dependencies
uv sync
```

## Claude Desktop Integration

Configure the MCP server in Claude Desktop's configuration file:

#### macOS

Location: `~/Library/Application Support/Claude/claude_desktop_config.json`

#### Windows

Location: `%APPDATA%/Claude/claude_desktop_config.json`

**You may need to put the full path to the uv executable in the command field. You can get this by running `which uv` on MacOS/Linux or `where uv` on Windows.**

### Claude Desktop Configuration Example

Add the following configuration to Claude Desktop's configuration file:

```json
{
  "mcpServers": {
    "iotdb": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/your_username/iotdb-mcp-server/src/iotdb_mcp_server",
        "run",
        "server.py"
      ],
      "env": {
        "IOTDB_HOST": "127.0.0.1",
        "IOTDB_PORT": "6667",
        "IOTDB_USER": "root",
        "IOTDB_PASSWORD": "",
        "IOTDB_DATABASE": "test",
        "IOTDB_SQL_DIALECT": "table",
        "IOTDB_EXPORT_PATH": "/path/to/export/folder"
      }
    }
  }
}
```

> **Note**: Make sure to replace the `--directory` parameter's path with your actual repository clone path.

## Error Handling and Logging

IoTDB MCP Server includes comprehensive error handling and logging capabilities:

1. **Log Level**: Logging level is set to INFO, allowing you to view server status in the console
2. **Exception Handling**: All database operations include exception handling to ensure graceful handling and meaningful error messages when errors occur
3. **Session Management**: Automatic closure of used sessions to prevent resource leaks
4. **Parameter Validation**: Basic validation of user-input SQL queries to ensure only allowed query types are executed

## Docker Support

You can build a container image for the IoTDB MCP Server using the `Dockerfile` in the project root:

```bash
# Build Docker image
docker build -t iotdb-mcp-server .

# Run container
docker run -e IOTDB_HOST=<your-iotdb-host> -e IOTDB_PORT=<your-iotdb-port> -e IOTDB_USER=<your-iotdb-user> -e IOTDB_PASSWORD=<your-iotdb-password> iotdb-mcp-server
```
