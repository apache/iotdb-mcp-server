# IoTDB MCP Server

[![smithery badge](https://smithery.ai/badge/@apache/iotdb-mcp-server)](https://smithery.ai/server/@apache/iotdb-mcp-server)

[English](README.md) | 中文

## 概述

IoTDB MCP Server 是一个基于模型上下文协议（Model Context Protocol, MCP）的服务器实现，通过 IoTDB 提供数据库交互和商业智能能力。该服务器支持执行 SQL 查询，并可以通过不同的 SQL 方言（树模型和表模型）与 IoTDB 进行交互。

## 组件

### 资源

此服务器不暴露任何资源。

### 提示

此服务器不提供任何提示。

### 工具

服务器为 IoTDB 的树模型（Tree Model）和表模型（Table Model）提供了不同的工具。您可以通过设置 "IOTDB_SQL_DIALECT" 配置为 "tree" 或 "table" 来选择使用哪种模型。

#### 树模型 (Tree Model)

- `metadata_query`

  - 执行 SHOW/COUNT 查询以从数据库读取元数据
  - 输入:
    - `query_sql` (字符串): 要执行的 SHOW/COUNT SQL 查询
  - 支持的查询类型:
    - SHOW DATABASES [path]
    - SHOW TIMESERIES [path]
    - SHOW CHILD PATHS [path]
    - SHOW CHILD NODES [path]
    - SHOW DEVICES [path]
    - COUNT TIMESERIES [path]
    - COUNT NODES [path]
    - COUNT DEVICES [path]
  - 返回: 查询结果作为对象数组

- `select_query`

  - 执行 SELECT 查询以从数据库读取数据
  - 输入:
    - `query_sql` (字符串): 要执行的 SELECT SQL 查询（使用树模型方言，时间使用 ISO 8601 格式，例如 2017-11-01T00:08:00.000）
  - 支持的函数:
    - SUM, COUNT, MAX_VALUE, MIN_VALUE, AVG, VARIANCE, MAX_TIME, MIN_TIME 等
  - 返回: 查询结果作为对象数组

- `export_query`
  - 执行查询并将结果导出为 CSV 或 Excel 文件
  - 输入:
    - `query_sql` (字符串): 要执行的 SQL 查询（使用树模型方言）
    - `format` (字符串): 导出格式，可以是 "csv" 或 "excel"（默认: "csv"）
    - `filename` (字符串): 导出文件的文件名（可选，如果未提供，将生成唯一文件名）
  - 返回: 有关导出文件的信息和数据预览（最多 10 行）

#### 表模型 (Table Model)

- `read_query`

  - 执行 SELECT 查询以从数据库读取数据
  - 输入:
    - `query_sql` (字符串): 要执行的 SELECT SQL 查询（使用表模型方言，时间使用 ISO 8601 格式，例如 `2017-11-01T00:08:00.000`）
  - 返回: 查询结果作为对象数组

- `list_tables`

  - 获取数据库中所有表的列表
  - 无需输入参数
  - 返回: 表名数组

- `describe_table`

  - 查看特定表的模式信息
  - 输入:
    - `table_name` (字符串): 要描述的表名
  - 返回: 包含列名和类型的列定义数组

- `export_table_query`
  - 执行查询并将结果导出为 CSV 或 Excel 文件
  - 输入:
    - `query_sql` (字符串): 要执行的 SQL 查询（使用表模型方言）
    - `format` (字符串): 导出格式，可以是 "csv" 或 "excel"（默认: "csv"）
    - `filename` (字符串): 导出文件的文件名（可选，如果未提供，将生成唯一文件名）
  - 返回: 有关导出文件的信息和数据预览（最多 10 行）

## 配置选项

IoTDB MCP Server 支持以下配置选项，可以通过环境变量或命令行参数进行设置：

| 选项          | 环境变量          | 默认值    | 描述                    |
| ------------- | ----------------- | --------- | ----------------------- |
| --host        | IOTDB_HOST        | 127.0.0.1 | IoTDB 主机地址          |
| --port        | IOTDB_PORT        | 6667      | IoTDB 端口              |
| --user        | IOTDB_USER        | root      | IoTDB 用户名            |
| --password    | IOTDB_PASSWORD    | root      | IoTDB 密码              |
| --database    | IOTDB_DATABASE    | test      | IoTDB 数据库名称        |
| --sql-dialect | IOTDB_SQL_DIALECT | table     | SQL 方言: tree 或 table |
| --export-path | IOTDB_EXPORT_PATH | /tmp      | 查询结果导出路径        |

## 性能优化

IoTDB MCP Server 包含以下性能优化特性：

1. **会话池管理**：使用优化的会话池配置，可支持最多 100 个并发会话
2. **优化的获取大小**：对于查询，设置了 1024 的获取大小
3. **连接重试**：配置了连接失败时的自动重试机制
4. **超时管理**：会话等待超时设置为 5000 毫秒，提高可靠性
5. **导出功能**：支持将查询结果导出为 CSV 或 Excel 格式

## 前提条件

- Python 环境
- `uv` 包管理器
- IoTDB 安装
- MCP 服务器依赖项

## 开发

```bash
# 克隆仓库
git clone https://github.com/apache/iotdb-mcp-server.git
cd iotdb-mcp-server

# 创建虚拟环境
uv venv
source venv/bin/activate  # 或在 Windows 上使用 `venv\Scripts\activate`

# 安装开发依赖
uv sync
```

## 在 Claude Desktop 中配置

在 Claude Desktop 的配置文件中设置 MCP 服务器：

#### macOS

位置: `~/Library/Application Support/Claude/claude_desktop_config.json`

#### Windows

位置: `%APPDATA%/Claude/claude_desktop_config.json`

**你可能需要在命令字段中放入 uv 可执行文件的完整路径。你可以通过在 macOS/Linux 上运行 `which uv` 或在 Windows 上运行 `where uv` 来获取这个路径。**

### Claude Desktop 配置示例

将以下配置添加到 Claude Desktop 的配置文件中：

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

> **注意**：请确保将 `--directory` 参数后的路径替换为你实际克隆仓库的路径。

## 错误处理与日志

IoTDB MCP Server 包含全面的错误处理和日志记录功能：

1. **日志级别**：日志记录级别设置为 INFO，可以在控制台查看服务器的运行状态
2. **异常处理**：所有的数据库操作都包含了异常处理，确保在出现错误时能够优雅地处理并返回有意义的错误消息
3. **会话管理**：自动关闭已使用的会话，防止资源泄露
4. **参数验证**：对用户输入的 SQL 查询进行基本验证，确保只有允许的查询类型被执行

## Docker 支持

您可以使用项目根目录下的 `Dockerfile` 构建 IoTDB MCP Server 的容器镜像：

```bash
# 构建 Docker 镜像
docker build -t iotdb-mcp-server .

# 运行容器
docker run -e IOTDB_HOST=<your-iotdb-host> -e IOTDB_PORT=<your-iotdb-port> -e IOTDB_USER=<your-iotdb-user> -e IOTDB_PASSWORD=<your-iotdb-password> iotdb-mcp-server
```
