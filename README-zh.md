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

## 权限模型

IoTDB MCP 权限默认是提示层。服务器为 SQL 操作返回所需权限、风险级别和确认参数，
由宿主 agent system 负责用户审批。执行 DDL/DML 或破坏性 SQL 前，如果可用，应先使用
`inspect_sql_permission`。

长期托管 agent 可以通过环境变量提供 full permission 默认值，例如
`IOTDB_SQL_DRIVER_MODE=full` 和 `TIMESEEK_MCP_PERMISSION_ENFORCEMENT=advisory`。
只有需要 MCP server 自身硬阻断时，才设置
`TIMESEEK_MCP_PERMISSION_ENFORCEMENT=strict`。

### 工具

服务器为 IoTDB 的树模型（Tree Model）和表模型（Table Model）提供了不同的工具。您可以通过设置 "IOTDB_SQL_DIALECT" 配置为 "tree" 或 "table" 来选择使用哪种模型。

方言相关的标识符语义：

- 树模型：
  - `FROM` 中使用显式的 `root...` 路径。
  - `SELECT` 投影通常应使用测点名，而不是完整 `root...` 路径。
  - `IOTDB_DATABASE` 只作为连接/会话范围提示，树模型 SQL 仍应显式写出 `root...` 路径。
- 表模型：
  - `FROM` 中使用当前数据库下的表名。
  - `SELECT` 投影使用列名。

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

- `sql_executor_batch`

  - 并行执行多条只读 SQL，并将每个子结果写入 ResultStore
  - 输入:
    - `sqls` (数组): 显式单语句 SQL，或
    - `sql_template` + `param_sets`: SQL 模板和参数对象列表
    - `max_concurrency` (整数): 并发语句上限，默认 4
    - `worker_pool_size` (整数): 线程 worker pool 大小，默认跟随并发度，并受 `IOTDB_SQL_EXECUTOR_BATCH_MAX_WORKER_POOL_SIZE` 限制（默认 16）
    - `per_item_timeout_ms` (整数): 单条语句等待超时，默认 60000
    - `batch_timeout_ms` (整数): 整个 batch 等待超时，默认 300000
    - `max_result_rows_per_item` / `max_result_bytes_per_item`: 单条语句结果 quota，默认 10000 行和 16 MiB
    - `max_batch_result_rows` / `max_batch_result_bytes`: 整个 batch 结果 quota，默认 100000 行和 64 MiB
  - 模板占位符:
    - `{{name}}` 表示 SQL 字面量，`{{name:path}}` 表示 IoTDB 路径，`{{name:identifier}}` 表示 SQL 标识符
  - 返回: batch 汇总，以及每条 SQL 的 `result_id`、行数、预览行和分页信息

- `read_result_pages`

  - 一次 MCP 调用读取多个 ResultStore page
  - 输入:
    - `pages` (数组): page 请求对象，包含 `result_id`，可选 `cursor`、`offset`、`limit`、`owner_session_id`
    - `default_limit` (整数): 未设置 `limit` 时的默认 page 大小
    - `max_pages` / `max_total_rows`: 单次调用 quota，默认 32 个 page 和 10000 行
    - `continue_on_error` (布尔): 是否以 per-item error 返回，默认 true
  - 返回: 批量读页汇总，以及每个 page 的 rows、cursor 和错误信息

- `export_query`
  - 执行查询并将结果导出为 CSV 或 Excel 文件
  - 输入:
    - `query_sql` (字符串): 要执行的 SQL 查询（使用树模型方言）
    - `format` (字符串): 导出格式，可以是 "csv" 或 "excel"（默认: "csv"）
    - `filename` (字符串): 导出文件的文件名（可选，如果未提供，将生成唯一文件名）
  - 返回: 有关导出文件的信息和数据预览（最多 10 行）
- `model_inference`
  - 执行 AINode `CALL INFERENCE(...)` SQL 并返回结果集
  - 输入:
    - `inference_sql` (字符串): 以 `CALL INFERENCE` 开头的单条树模型 SQL
  - 执行前校验模型 ID、带引号的输入 SELECT SQL、显式非通配列，以及支持的参数
    (`generateTime`, `outputLength`)
  - 权限信息：模型管理能力通过 MCP advisory policy 层返回。仅在 strict 模式下才硬性执行
    `IOTDB_ENABLE_MODEL_MANAGEMENT=true` 和 `IOTDB_MODEL_ALLOWED_USERS`。
- `prepare_model_inference_request`
  - 通过结构化字段构造并校验 AINode `CALL INFERENCE(...)` SQL
  - 输入:
    - `model_id` (字符串): 已注册的 AINode 模型 ID
    - `input_sql` (字符串): 作为模型输入的有界树模型 SELECT 查询
    - `output_length` (整数): 预测输出长度（默认: 96）
    - `generate_time` (布尔): 是否请求 Time 列（默认: false）

#### UDF 工具

- `list_udf_functions`
  - 对所选 IoTDB target 执行 `SHOW FUNCTIONS`。
- `prepare_udf_query`
  - 使用结构化参数生成只读 UDF `SELECT`。
  - 树模型形式：`SELECT UDF(measurement, "k"="v") FROM root.sg.d1 ...`
  - 表模型形式：`SELECT UDF(column, "k"="v") FROM table ...`
- `execute_udf_query`
  - 执行校验后的 UDF 查询，并返回 ResultStore 支持的预览结果。
- `export_udf_query`
  - 执行校验后的 UDF 查询，并导出为 CSV 或 Excel。

UDF 工具会拒绝表达式和过滤条件中的分号、SQL 注释以及 DDL/DML 关键字。
它们用于只读 UDF 调用，例如数据质量、画像、修复规划和异常评分。

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
| --password    | IOTDB_PASSWORD    | 空        | IoTDB 密码              |
| --database    | IOTDB_DATABASE    | test      | 表模型: 当前数据库名；树模型: 可选的连接/范围提示，查询仍需显式使用 `root...` 路径 |
| --sql-dialect | IOTDB_SQL_DIALECT | table     | SQL 方言: tree 或 table |
| --export-path | IOTDB_EXPORT_PATH | /tmp      | 查询结果导出路径        |

target registry 只保存已经成功登录的连接。先用 `prepare_iotdb_target` 创建
不含凭据、不可执行 SQL 的临时 candidate，再把用户明确提供的账号密码传给
`connect_iotdb_target`，并且只认证一次。缺少凭据时必须询问用户，禁止探测空
密码或默认密码；用户明确说明空密码时，空字符串才是有效凭据。

登录成功后，MCP 会原子发布 target，并自动记录 per-target
`last_known_good_credential`。任何连接层错误都会消费 candidate 或驱逐已发布
target。只有用户明确要求重连后，才能创建新 candidate，并传入
`user_confirmed_retry=true`。公开 target 响应会脱敏当前密码和
last-known-good 密码。

配置 `TIMESEEK_IOTDB_TARGETS_FILE` 后，成功连接默认会持久化。通过
`iotdb-target-cli` 启动本地 Java CLI，即可复用完全相同的 target：

```bash
iotdb-target-cli --target-id cloud \
  --cli /opt/iotdb/sbin/start-cli.sh -- -e "SHOW VERSION"
```

wrapper 每次启动都会重新加载已验证 target，并注入 host、port、方言、用户名和
last-known-good 密码。Java CLI 不支持 `-db`，因此 wrapper 不会向
`start-cli.sh` 传该参数；Table 数据库应通过 SQL `USE <database>` 选择。对于支持
`-db` 的 `import-data.sh` 和 `import-data.bat`，wrapper 仍会自动注入 Table
target 的数据库。已验证的空密码会省略 `-pw`，命令预览会脱敏非空密码。直接调用
`start-cli.sh` 不会读取 target registry。

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
        "IOTDB_PASSWORD": "",
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
