# IoTDB 威胁模型补充建议：MCP Server

本文件是提交 PMC 审阅的建议，尚不是已批准的安全承诺。
按本地 IoTDB 主仓库 `THREAT_MODEL.md` 的 v0 章节组织；合入前需与目标版本核对。
当前 MCP 的运行约束见 [Session 权限管理](session-policy-security.md)。

## §2：明确组件与参与者

在组件表中显式增加 `apache/iotdb-mcp-server`，由 PMC 确认纳入主模型还是维护单独模型并互相引用。
不要因其位于独立仓库，就默认套用客户端 SDK 的范围排除。

建议组件表条目：

| Family | Entry point | Touches outside process | In model? |
| --- | --- | --- | --- |
| IoTDB MCP Server | MCP tools over stdio | IoTDB session RPC, local configuration, result files, policy approval files | In the MCP-specific boundary described below; database-side RBAC remains independently enforced |

明确四种参与者：部署管理员、负责交互与沙箱的宿主、调用 MCP 工具的 agent/MCP 客户端、
数据库账号。宿主可以管理 agent 的执行权限，但 agent 不因此自动获得管理员身份。
数据库凭据由 MCP 使用时，数据库账号权限也不等于 agent 对 MCP 管理策略的权限。

## §3、§4、§7、§11a：修正 root/admin 排除规则

现有模型把“需要 root/admin 的操作”视为可信输入。对 MCP 应补充例外：

> An MCP client does not become a trusted deployment administrator merely because
> the MCP server uses a database account with administrative privileges. An MCP
> client bypassing an operator-enforced MCP policy is an in-scope adversary even
> when the underlying database would authorize the SQL for the server's account.

在威胁主体中增加：已连接且能调用普通 MCP 工具的恶意客户端、被提示注入影响的 agent、
超出用户意图自行执行操作的 agent。攻击者可选择任意工具名、参数、调用顺序和并发请求，
也可自行填写危险操作确认布尔值。不能假设工具调用来自自然语言用户的真实授权。

## §4：增加两条边界

1. **agent → MCP 数据操作与 session 策略工具**：不可信输入；部署策略上限在服务启动时冻结。
   显式越权请求不可修改策略；收紧可直接生效；上限内扩大按部署者选定的审批模式执行。
2. **管理员 → 配置与审批通道**：独立的可信管理操作；修改上限、enforcement 和 SQL 分类规则
   需要受保护的配置及重启。普通 MCP 工具不能批准自己的请求。

数据操作还要跨越 **MCP → IoTDB RBAC** 边界。MCP 限制与数据库授权应同时满足，
MCP 的部署上限不能扩大数据库账号本身的权限。

## §5、§5a、§6：部署假设与输入分类

- stdio 仅说明传输方式，不说明客户端可信；一个进程是一个策略域，不提供多租户隔离。
- 管理配置、审批目录和服务代码必须不在 agent 可读写或可替换的权限范围内；
  受信管理端可以访问它们。不得将审批目录放在可由查询导出等工具写入的位置。
- 同用户不受限 shell 能运行管理员命令或伪造审批文件。0700 不是同 UID 的安全隔离；
  若需要约束该 agent，宿主必须提供额外隔离。管理员自动批准服务也属于可信计算基。
- 审批模式 `require`（默认）和 `allow`（管理员显式选择）必须列为安全配置变体。
  require 无可用通道时禁止扩大，不可自动降级为 allow。
- 明确 `advisory` 不提供 SQL 权限强制执行保证；`strict` 下才承诺相关执行检查。
  当前默认 advisory 应清楚记录，PMC 可另行决定未来版本是否切换默认 strict。
- `confirm_destructive=true` 是攻击者可控输入，不等于人类身份、签名或独立审批凭据。
- 审批请求内容与决定是不同信任级别的输入。审批需绑定确切变更及有效期，防止替换与重放。

## §8：建议承诺的 MCP 安全属性

建议 PMC 明确承诺并逐个核验调用入口：

1. **部署上限不可由工具扩大**：set、preset、replace、reset、工具别名、并发与重启恢复
   均不得绕过。enforcement 和 SQL 分类规则不能由普通工具改写。
2. **先审批后生效**：require 模式下，未批准、拒绝、过期、不匹配或已消费的审批均不能扩大权限。
   对混合权限变更，拒绝或待批时不得部分生效。
3. **审批绑定**：批准仅用于特定运行实例、策略修订和具体变更，不可挪用到另一提权请求。
4. **严格执行一致性**：strict 模式的各 SQL/DDL/DML 工具必须按其声明的权限规则检查。
   主模型应说明通用 SQL driver 与专用工具开关之间的关系，避免部署者误认为关闭单个入口
   等于关闭所有等价 SQL。实现完整入口审计后再扩大此项承诺。
5. **凭据保密与资源生命周期**：建议分别定义快照/日志不泄露认证凭据、结果文件路径约束、
   查询与连接池资源释放的保证及边界，并以独立测试核验；本次策略修改不代表已完成这些审计。

## §9、§10、§11：非保证与部署者职责

- 不防御已控制 MCP 进程、服务代码、管理配置或审批文件的攻击者；但这种控制不能从
  “能够调用 MCP 工具”或“服务器使用 root 数据库凭据”直接推定。
- 不把自然语言“用户已同意”、普通确认布尔值或 agent full permission 当成人工审批证明。
- 不宣称 advisory 模式强制只读，也不宣称 session 策略收紧会撤销已经开始执行的 SQL。
- 部署者负责 strict 配置、IoTDB 最小权限账号、管理通道隔离、审批目录保护及实例隔离。
  如果显式选择 allow，仅表示同意部署上限内自动调整，不表示同意修改部署上限或关闭 strict。

## §12、§13：模型变更与分流示例

| 场景 | 建议分流 |
| --- | --- |
| 普通 MCP 客户端能关闭已启用的 strict 或突破部署上限 | VALID：权限边界绕过 |
| require 模式下可伪造、替换或重放审批而扩大权限 | VALID：审批边界绕过 |
| 使用部署者明确开启的 allow 在部署上限内扩大 session 权限 | BY-DESIGN，前提是配置契约清楚 |
| 仅证明显式 advisory 不强制执行 SQL 权限 | 按已声明非保证判断；误导文档另行评估 |
| 已掌握管理员 shell 后编辑配置重启 | OUT-OF-MODEL: trusted-input，须证明已有管理权限 |
| 服务端持有高权限数据库账号，而 agent 仅有普通 MCP 工具权限 | 不得仅凭账号级别判为 trusted-input |

新增 HTTP/共享服务传输、多客户端共享实例、在线管理 API、宿主自动审批集成，或修改默认
enforcement/审批模式时，应触发模型复审。最终范围和承诺由 PMC 确认，并随发布版本绑定。
