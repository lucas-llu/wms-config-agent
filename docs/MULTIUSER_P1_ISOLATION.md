# P1：账号接入、用户归属与接口隔离

状态：P1 实现和隔离门禁已完成，功能 PR #121 待最终合并；尚未对真实用户开放。
P0 前版本单独经 PR #120 发布到 `main`；
P0/P1 继续在 `dev` 推进。原有未提交的回答增强和前端修改保留在原工作目录，不属于该发布。

## 交付边界

- OIDC 固定 issuer / API audience 验签，再通过机密客户端 introspection 检查身份服务会话。
- 首次认证建立 issuer + subject 到用户 UUID 的映射，不自动授予 Workspace/知识库权限。
- 对话所有者由服务器取得，客户端不能设置身份、角色、检查点 thread 或 owner。
- 私人对话需要“所有者 + 当前有效成员”两项条件；平台/Workspace 管理员不自动读取其他人的正文。
- PostgreSQL 存储复用已有 SessionService、验证、审批、导出和多轮 Supervisor；检查点独立 schema。
- HTTP 和 `/mcp` 白名单工具共用 OwnedApplication，不回退到旧 `host_process` 工具注册表。
- 数据库运行角色非 owner、非超级用户、无 BYPASSRLS；所有私有业务/检查点表启用 FORCE RLS。
- 连接池身份用事务级变量，事务退出后不残留；账号停用、撤销会话、token 到期和成员撤销持续检查。
- 检索前应用 Workspace 过滤，检索结果再次过滤；权限策略变动使缓存的执行上下文失效。
- 不共享聊天答案/摘要缓存。滚动摘要、近期消息、草稿、证据随私有版本/检查点保存。
- 文件经资源归属和路径边界检查后以 attachment/octet-stream 下载，并设置 nosniff，
  不内联执行任意附件；不使用公开静态目录；响应禁止缓存，不返回导出绝对路径。

## 接口与归属矩阵

所有下列入口均要求 Bearer access token；知道资源 ID 不等于拥有权限。

| 入口 | 验证规则 |
|---|---|
| `/v1/me` 读取/昵称修改 | 当前有效身份；不能修改账号状态、管理员字段 |
| `/v1/logout` | 撤销该 OIDC sid，旧 token 不再访问业务接口 |
| `/v1/conversations` 创建、列表、搜索、归档列表 | 有效 Workspace 成员；只查当前用户 |
| `/v1/conversations/{id}`、历史版本、继续、命名、删除、恢复、归档 | 父对话所有者 + 成员；已删除对话禁止普通读取/写入 |
| 草稿验证、反馈、审批、导出 | 父对话归属；审批/生成导出另需 reviewer/workspace_admin；保留原批准门禁 |
| 导出下载、附件/图片、证据、run/event/usage 资源、事件流 | 父对话归属；文件在该用户的私有存储根内；每条事件重查权限 |
| `/v1/trash`、批量永久删除 | 只列自己回收站；全部 ID 和版本验证后同事务删除，混入他人 ID 全部失败 |
| `/v1/tools/call`、`/mcp` | 同一认证/业务服务；严格白名单与参数 schema；无主机身份回退 |
| LangGraph 检查点 | 服务器生成 thread 与父对话映射；PG 行策略阻止跨用户读取/写入和已删对话迟到写入 |

`/mcp` 为无状态、JSON 响应的 Streamable HTTP 工具适配器，支持 2025-03-26 / 2025-11-25。
未知工具/非法请求参数返回 JSON-RPC 协议错误；业务失败返回无私人详情的 `isError` 工具结果，
身份失败仍用 HTTP 认证/不可用状态。
不提供服务器主动通知流；认证 GET 返回 405。不接受任意 Origin，不输出认证令牌。
完整 OAuth 登录 UI/发现与账号设备管理在 P2 接入，不能将本适配器当作已完成的公共身份门户。

## 启动及迁移边界

新增 ASGI 工厂：`api.bootstrap:from_environment`。由 ASGI 服务加载 `--factory`。
例如隔离的本地联调启动（不是公开生产部署）：

```text
python -m uvicorn api.bootstrap:from_environment --factory --host 127.0.0.1 --port 8510
```

需要环境配置：`WMS_OIDC_ISSUER`（HTTPS）、`WMS_USER_DB_DSN`（非 owner 运行身份）、
`WMS_API_CLIENT_SECRET`；可选 `WMS_API_CLIENT_ID`、`WMS_WEB_ORIGINS`、`WMS_SETTINGS_PATH`。
不能把 P0 超级用户 DSN 配置给服务。所有启动配置由运维设置，不接受请求中的 DB/issuer 参数。
首次建库通过 `migrations/001_user_isolation.sql` 和检查点官方迁移，用独立迁移身份完成，
不能在用户请求中运行 setup。`scripts/prepare_p1_database.py` 仅供隔离的 P0/P1 CI 测试库初始化，
不是线上自动迁移/密码轮换脚本；已存在的完整 P1 schema 不应重复运行此初始建库步骤。

本阶段不搬迁旧 SQLite，不把旧对话交给第一个新用户。旧 Streamlit/stdio MCP 保持本地受限管理入口，
新多用户 API 不挂载旧工具/公开日志/静态图片目录。公开部署必须仅开放新 API，不同时暴露旧入口。

## 验收方法

- 本地：Ruff、全量离线回归、身份/工具/检索授权单元测试。
- 独立 CI：两个真实 Keycloak 浏览器 PKCE 登录账号；真实 PostgreSQL 17，使用非 owner 运行账号。
- 双向 A/B 越权矩阵：读取、版本/记忆、继续、搜索、命名、删除/恢复/永久删除、归档、验证/审批、
  反馈、导出/附件/图片、证据、后台资源、SSE、HTTP 工具、MCP 和检查点。
- 额外验证管理员不继承私人正文、池复用无身份残留、所有者不可替换、有效 token 的会话撤销、
  账号停用/密码重置撤销水位/到期/权限修改后缓存上下文停止、两轮真实 Supervisor 图 PG 持久恢复。
- 全量覆盖率维持 >=90%，真实服务门禁不得以跳过代替通过。运行结果在 PR 审查后补录。

### 已完成的验收证据

独立修复 PR #123 处理 Issues #122（身份异常/MCP 错误）、#124（持锁后版本重查）、
#125（私有下载活动内容隔离），再合入 #121。初始版本检查和执行锁之间的竞争有确定性红→绿回归。
不是直接在功能分支修复发现的问题；每个 PR 都记录创建及最终代码审查 comment。

| 门禁 | 结果 | 证据 |
|---|---|---|
| 本地授权与安全单元测试 | 43 passed | 修复提交 `810e10a` |
| 本地全量离线回归 | 639 passed / 23 条 opt-in 跳过 | 真实服务在 CI 补齐，未以本地跳过宣布隔离通过 |
| 真实 OIDC/PG/RQ/检查点门禁 | 37 passed / 0 failed / 0 skipped | [运行 37297704707](https://github.com/lucas-llu/wms-config-agent/actions/runs/37297704707) |
| 包含真实服务的全量测试 | 659 passed / 3 opt-in 模型测试跳过 | [运行 37297704611](https://github.com/lucas-llu/wms-config-agent/actions/runs/37297704611) |
| 全量覆盖率 | 91.57%，门槛 90% | 同上；未降低门槛或排除新增模块 |
| Ruff、格式、密钥扫描、检索基准、产品发布门禁 | 全部通过 | 同上 |

3 条全量 opt-in 跳过是现有真实供应商/模型测试，P1 不要求重新调用收费模型；
真实账号登录/退出、非 owner RLS、A/B 双向越权、管理员私人正文隔离、权限失效、
检查点恢复与现有两轮 Supervisor 均已实际执行。修复后的边界未发现跨用户内容泄露。
本记录是该提交的实测快照，功能 PR 最终合并仍须检查最新 head 门禁。

## 下一阶段仍需完成

P2 注册/验证邮箱/登录/密码找回与设备页、新用户工作台；真实身份服务的重置流程撤销旧会话联调。
P3 持久运行任务、幂等、分布式会话租约、公平队列、实时 SSE 重连与取消。
当前 event/run/usage 是已授权资源契约，不是已上线的任务调度/计费实现；事件流暂为有限持久事件读取。
当前 Agent 同一对话进程内串行及版本冲突保护，不宣称跨进程执行权或多实例生产并发安全。
P4 用量/额度账本和账号管理；P5 历史归属迁移、回退与 10/20 用户压力/故障验收。
因此 P1 通过隔离门禁也不代表平台已满足多人公开上线条件。

参考：[PostgreSQL 行级安全](https://www.postgresql.org/docs/17/ddl-rowsecurity.html)、
[Keycloak OIDC](https://www.keycloak.org/securing-apps/oidc-layers)、
[MCP HTTP 传输](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。
