# IoTDB MCP Server

[![smithery badge](https://smithery.ai/badge/@apache/iotdb-mcp-server)](https://smithery.ai/server/@apache/iotdb-mcp-server)

English | [中文](README-zh.md)

## Overview

A Model Context Protocol (MCP) server implementation that provides database interaction and business intelligence capabilities through IoTDB. This server enables running SQL queries and interacting with IoTDB using different SQL dialects (Tree Model and Table Model).

## Components

### Resources

The server doesn't expose any resources.

### Prompts

The server doesn't provide any prompts.

### Tools

The server offers different tools for IoTDB Tree Model and Table Model. You can choose between them by setting the "IOTDB_SQL_DIALECT" configuration to either "tree" or "table".

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
- `export_query`
  - Execute a query and export the results to a CSV or Excel file
  - Input:
    - `query_sql` (string): The SQL query to execute (using TREE dialect)
    - `format` (string): Export format, either "csv" or "excel" (default: "csv")
    - `filename` (string): Optional filename for the exported file. If not provided, a unique filename will be generated.
  - Returns: Information about the exported file and a preview of the data (max 10 rows)

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
| --password    | IOTDB_PASSWORD       | root          | IoTDB password                   |
| --database    | IOTDB_DATABASE       | test          | IoTDB database name              |
| --sql-dialect | IOTDB_SQL_DIALECT    | table         | SQL dialect: tree or table       |
| --export-path | IOTDB_EXPORT_PATH    | /tmp          | Path for exporting query results |

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
        "IOTDB_PASSWORD": "root",
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
