# Session 权限管理

## 权限边界

服务启动时冻结部署策略：进程环境变量优先，其次为启动时发现的 MCP 配置，最后为默认值。
运行期间修改环境变量或 `.mcp.json` 不会改变该策略。修改部署策略后须由管理员重启服务。
部署上限不是启动 session 的只读默认值；需要“部署允许写、session 暂时只读”时，
先配置可写部署上限，再通过 session 工具收紧。

策略比较规则：

| 类型 | 收紧 | 扩大 |
| --- | --- | --- |
| `IOTDB_ENABLE_*` | true → false | false → true |
| `*_ALLOWED_USERS` | 删除用户，`*` → 指定集合 | 添加用户，指定集合 → `*` |
| `IOTDB_SQL_DRIVER_MODE` | full → ddl → readonly | readonly → ddl → full |
| `*CONFIRM*` | false → true | true → false |

空用户白名单表示拒绝所有用户。`readonly` 预设也关闭模型管理。
预设与部署上限取交集，例如部署只允许 alice 时，`full` 不会把用户白名单改成 `*`。
显式 `policy` 中任何一项超过部署上限，整个请求都会被拒绝；不会应用其余项。
混合收紧和扩大的请求作为整体审批，在批准之前不应用其中任何一项。

以下内容仅由管理员配置，普通 MCP 工具不能设置或 reset：

- `IOTDB_STRICT_PERMISSION_ENFORCEMENT`
- `TIMESEEK_MCP_PERMISSION_ENFORCEMENT`
- `IOTDB_SQL_DRIVER_EXTRA_READONLY_PREFIXES`
- `IOTDB_SQL_DRIVER_EXTRA_DDL_PREFIXES`
- `IOTDB_SQL_DRIVER_EXTRA_FULL_PREFIXES`
- 审批模式和审批目录

SQL 分类扩展项可能把写语句划为只读，因此也不能交给 agent 修改。
关闭严格检查、扩大部署上限或修改 SQL 分类规则的管理员通道是：
在受保护的宿主配置中修改相应项，然后重启 MCP。session 审批命令不具备这些能力。

## 上限内提权审批

`IOTDB_SESSION_POLICY_APPROVAL_MODE` 由管理员在启动时设置：

- `require`（默认）：收紧立即生效；扩大需由独立管理员通道批准。
- `allow`：允许上限内扩大权限，无需逐次审批。仍不能越过部署上限或修改管理员专属项。

`set_iotdb_session_policy`、`replace=true`、`reset_iotdb_session_policy` 均遵循此规则。
重置只读 session 通常会恢复更宽权限，所以也需要审批。
未配置审批目录时，扩大请求返回 `approval_unavailable`、`applied=false`，原权限保持不变。
无变化的请求不需要审批。

管理员在 agent 沙箱之外创建目录，并在 MCP 启动环境中配置：

```sh
mkdir -m 700 /absolute/protected/iotdb-policy-approvals
export IOTDB_SESSION_POLICY_APPROVAL_DIR=/absolute/protected/iotdb-policy-approvals
export IOTDB_SESSION_POLICY_APPROVAL_MODE=require
export TIMESEEK_MCP_PERMISSION_ENFORCEMENT=strict
```

目录需由服务运行用户拥有；POSIX 上禁止 group/other 权限。管理员命令在可信终端中
以能访问该目录的服务用户身份运行。Windows 部署需用 ACL 和宿主沙箱落实等效隔离。
不要把目录设在 agent 可写工作区、SQL 结果导出目录或其他工具可写路径中。

1. agent 请求扩大 session 权限。返回 `status=approval_required`、`applied=false`、
   `request_id`、变更前后值和过期时间；此时策略尚未改变。
2. 管理员从可信界面查看请求：

   ```sh
   iotdb-policy-admin show REQUEST_ID --directory /absolute/protected/iotdb-policy-approvals
   ```

3. 核对具体变更后，从独立管理终端批准或拒绝：

   ```sh
   iotdb-policy-admin approve REQUEST_ID --directory /absolute/protected/iotdb-policy-approvals
   # 或：iotdb-policy-admin deny REQUEST_ID --directory /absolute/protected/iotdb-policy-approvals
   ```

4. agent 重试原始 MCP 调用。服务校验对应审批后才应用变更，返回 `applied=true`。

源码运行可用 `PYTHONPATH=src python3 -m iotdb_mcp_server.policy_approval` 替代命令名。
管理命令不会注册为 MCP tool；MCP 调用没有 `approved` 或 `confirm` 提权参数。
批准绑定具体变更、部署指纹、策略修订号和运行实例；有效期 5 分钟，仅可消费一次。
策略发生变化、服务重启或请求过期后，旧审批不能用于新的变更。最多保留 32 个待审批请求；
在新申请时清理本实例已过期或旧修订请求。重启遗留文件不会被新实例采纳，可由管理员清理。
审批文件是授权凭据，不是不可篡改审计日志；生产环境可由管理平台另行记录审批者身份。

## 与 agent full permission 的关系

宿主的 full permission 不会自动改变服务端审批模式。人工审批流程应由宿主在独立权限域
执行，不应把上述审批命令直接交给 agent 自动运行。

**同一 OS 用户下，若 agent 具有未受限制的 shell/文件访问，0700 不能隔离它与 MCP。**
部署者必须通过沙箱、容器、独立服务身份或可信管理平台，阻止 agent 读写审批目录、
修改部署配置、替换服务代码或自行重启为更宽权限。若这些能力已授权给 agent，
它拥有管理员通道的能力，服务端不能再保证人工审批。普通工具自动批准不能代替此授权。

## 保证范围与兼容性

- 默认 SQL 检查仍为 `advisory`；要让只读、工具开关和用户白名单成为硬限制，管理员必须启用
  `TIMESEEK_MCP_PERMISSION_ENFORCEMENT=strict` 或 `IOTDB_STRICT_PERMISSION_ENFORCEMENT=true`。
  在 advisory 模式下，session 配置收紧不会提供 SQL 执行隔离保证。
- SQL 执行工具的 `confirm_destructive` 等布尔值仍是调用者声明，不是可验证的人工审批。
  本变更的独立审批针对 session 权限扩大，不是每条危险 SQL 的审批服务。
- 通用 SQL driver 的模式/白名单与专用 DDL/DML 工具开关分别控制各自入口。
  部署只读时应同时设置 driver 为 readonly 并关闭相关专用写工具；可用 readonly 预设收紧
  session，但其部署上限仍以管理员配置为准。IoTDB 账号权限继续独立生效，建议最小授权。
- 策略是单个 stdio MCP 进程级别的，不是多租户或多目标独立授权域。
  不应把此进程在不同信任级别的客户端间共享；正在执行的 SQL 不承诺被策略收紧立即取消。
- 相较旧版，配置不再热加载，进程环境变量优先，非法策略值会使初始化失败；
  reset/replace 可能返回待审批而非立即完成。调用方应检查 `payload.applied` 和 `payload.status`。

IoTDB 主仓库的威胁模型补充草案见 [iotdb-threat-model-proposal.md](iotdb-threat-model-proposal.md)。
