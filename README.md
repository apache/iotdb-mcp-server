# IoTDB MCP Server

[![smithery badge](https://smithery.ai/badge/@apache/iotdb-mcp-server)](https://smithery.ai/server/@apache/iotdb-mcp-server)

## Overview

A Model Context Protocol (MCP) server implementation that provides database interaction and business intelligence capabilities through IoTDB. This server enables running SQL queries.

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
  - Returns: Query results as array of objects
- `select_query`
  - Execute SELECT queries to read data from the database
  - Input:
    - `query_sql` (string): The SELECT SQL query to execute
  - Returns: Query results as array of objects
- `export_query`
  - Execute a query and export the results to a CSV or Excel file
  - Input:
    - `query_sql` (string): The SQL query to execute (using TREE dialect)
    - `format` (string): Export format, either "csv" or "excel" (default: "csv")
  - Returns: Information about the exported file and a preview of the data

#### Table Model

##### Query Tools

- `read_query`
  - Execute SELECT queries to read data from the database
  - Input:
    - `query` (string): The SELECT SQL query to execute
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
  - Returns: Information about the exported file and a preview of the data

## Claude Desktop Integration

## Prerequisites

- Python with `uv` package manager
- IoTDB installation
- MCP server dependencies

## Development

```
# Clone the repository
git clone https://github.com/apache/iotdb-mcp-server.git
cd iotdb_mcp_server

# Create virtual environment
uv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install development dependencies
uv sync
```

Configure the MCP server in Claude Desktop's configuration file:

#### MacOS

Location: `~/Library/Application Support/Claude/claude_desktop_config.json`

#### Windows

Location: `%APPDATA%/Claude/claude_desktop_config.json`

**You may need to put the full path to the uv executable in the command field. You can get this by running `which uv` on MacOS/Linux or `where uv` on Windows.**

### Configuration Options

The server can be configured using the following environment variables or command-line arguments:

| Option      | Environment Variable | Default Value | Description                                  |
| ----------- | -------------------- | ------------- | -------------------------------------------- |
| Host        | `IOTDB_HOST`         | 192.168.99.19 | IoTDB server host                            |
| Port        | `IOTDB_PORT`         | 6667          | IoTDB server port                            |
| User        | `IOTDB_USER`         | root          | IoTDB username                               |
| Password    | `IOTDB_PASSWORD`     | root          | IoTDB password                               |
| Database    | `IOTDB_DATABASE`     | czxk          | IoTDB database name                          |
| SQL Dialect | `IOTDB_SQL_DIALECT`  | table         | SQL dialect to use (tree or table)           |
| Export Path | `IOTDB_EXPORT_PATH`  | /tmp          | Path to directory for exported query results |

```json
{
  "mcpServers": {
    "iotdb": {
      "command": "uv",
      "args": [
        "--directory",
        "YOUR_REPO_PATH/src/iotdb_mcp_server",
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
