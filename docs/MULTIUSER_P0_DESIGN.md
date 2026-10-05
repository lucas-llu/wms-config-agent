# P0：身份、队列、检查点原型与设计确认

日期：2026-10-05。范围来自 `MULTIUSER_DEVELOPMENT_PLAN.md`。
P0 是独立原型与架构决策，不代表现有 Streamlit 已可安全对外提供多用户服务。
原型使用合成账号、隔离数据库及独立端口；不连接现有对话、知识库或真实模型。

## 1. 固定组件与验证方式

| 组件 | P0 固定版本 | 验证 |
|---|---|---|
| Python / LangGraph / checkpoint | 3.12 / 1.2.11 / 4.2.0 | 保持现有图运行语义 |
| PostgreSQL / checkpoint-postgres | 17.6 / 3.1.2 | 暂停、关闭连接、重新打开、恢复；不同身份命名空间 |
| psycopg / psycopg-pool | 3.3.6 / 3.3.3 | 短事务、受限连接池 |
| Redis / redis-py / RQ | 7.4.5 / 8.1.0 / 2.12.0 | outbox、独立 Worker、重复投递、硬中断和租约恢复 |
| Keycloak / PyJWT | 26.7.4 / 2.15.1 | 真实 Authorization Code + S256 PKCE → access token → API |
| FastAPI / httpx | 0.142.2 / 0.28.1 | `/health` 与受认证保护的 `/v1/me` |
| Playwright | 1.63.0 | 真实 Chromium 登录表单、会话 Cookie 与授权码回调 |

版本是本次兼容基线，升级必须重新运行门禁。完整 Python 依赖由 `uv.lock` 管理。
本机未安装 Docker/可用 WSL，真实容器测试在 GitHub Actions 的隔离 Linux Runner 执行；
离线 JWT 单测不替代真实 Keycloak、Redis 或 PostgreSQL 结果。
只有 `multiuser-p0` 工作流所有原型测试通过且无跳过，才能认定兼容性退出条件通过。

官方依据：[Keycloak OIDC](https://www.keycloak.org/securing-apps/oidc-layers)、
[Keycloak 版本](https://www.keycloak.org/downloads)、
[PostgresSaver 配置](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/README.md)、
[RQ Workers / JSONSerializer](https://python-rq.org/docs/workers/)。

## 2. 身份和权限边界

首版前端采用 React + TypeScript；登录采用授权码 + PKCE，不使用密码直传到应用 API 的授权方式。
生产 access token 保持在内存，应用不把 refresh/access token 长期写入浏览器 localStorage。
API 从固定 issuer 的可信 JWKS 验证 RS256 签名、iss/aud/exp/iat/sub 及 access-token 类型。
不从 token 自带 issuer 选择验证地址，不接受请求里的 owner/user/role 作为授权依据。
JWKS 使用共享 HTTP 客户端、缓存和有间隔的未知 kid 刷新，避免逐请求新建连接。
P0 的 identity_key 只是 issuer + subject 的诊断标识；生产用户主键为应用生成 UUID。

Keycloak 负责账号凭据、邮箱验证、密码恢复和身份会话；应用数据库负责账号状态、Workspace 成员、
私人资源归属、业务角色、额度和审计。新用户默认没有知识库授权。
P1 必须加入停用/成员撤销/会话撤销检查；仅校验 JWT 签名不表示密码重置或退出后的旧 token 已失效。
生产将结合 Keycloak 会话/内省与应用会话撤销记录验证当前有效性，敏感操作和 Worker 下一步重新检查。
P0 没有注册、密码恢复、邮件投递和完整会话撤销接口；这些仍属于 P1/P2。

| API 资源族（P1/P2 合同） | 普通用户 | Workspace 管理员 / 平台管理员 |
|---|---|---|
| `/v1/me`、资料、设备会话 | 本人 | 不通过该接口读取他人 |
| conversations / turns / revisions / memory / feedback | 本人拥有且当前仍是 Workspace 成员 | 管理角色不自动取得私人正文权限 |
| drafts / approvals / exports / attachments | 同父对话归属；审批另需业务权限 | 管理角色不跳过审批与下载授权 |
| runs / events（SSE）/ cancel | 同父对话归属，重连事件序号绑定 run | 运维聚合状态，不默认读取私人内容 |
| usage / quota | 个人明细 | 聚合用量及额度管理，不需要私人聊天正文 |
| memberships / corpus grants | 查询自身范围 | 对授权范围管理；平台账号管理需平台角色 |
| legacy MCP / Streamlit 运维 | 不是多用户公共入口 | 仅隔离的主机管理环境 |

P1 Repository 必须接收可信用户上下文；所有子资源沿父对话校验 owner 与成员关系。
不提供不带归属条件的普通请求查询，找不到和无权访问私人资源统一返回 404。
RLS 是第二道保护，运行角色不得是表 owner、superuser 或 BYPASSRLS；使用事务级 SET LOCAL 身份。
身份校验通过不等于业务授权通过；P0 `/v1/me` 不公开旧 Agent 主机工具。

## 3. 数据与接口合同

生产 schema 分为 `identity_business`（用户/成员/会话/撤销）、`agent_business`（对话/版本/审批/运行/账本），
`agent_checkpoints`（LangGraph 管理表）。迁移账号与运行账号分开；schema/table 所有权不授予请求运行角色。
users 使用 UNIQUE(identity_issuer, identity_subject)，conversations.owner_user_id 不可通过普通更新更换。
runs 的 idempotency UNIQUE(owner_user_id, conversation_id, idempotency_key)，相同键不同内容返回 409。
同一 conversation 通过部分唯一约束限制 queued/running 活跃任务；Worker 写入须同时匹配 run 与 lease_epoch。
检查点标识由服务端生成，先做归属校验；执行代次的写入命名空间与旧代次隔离，恢复从可信快照承接。
P0 只验证哈希身份/会话命名空间和保存器兼容；生产执行代次检查点承接仍需 P3 实施和故障测试。

POST `/v1/conversations/{id}/runs` 请求字段：message、answer_strategy、idempotency_key、expected_revision。
拒绝调用者提供 owner_user_id、roles、thread_id、checkpoint_ns；strategy 仅 standard/review。
成功接收返回 202 `{run_id,status,events_url}`；同会话忙/幂等冲突返回 409；额度不足和繁忙有明确机器码。
SSE 事件包含递增 sequence、run_id、stage 与已校验终态；重连只补读，不重新创建或计费。
知识库、图片、导出、缓存键和记忆均含授权范围；不能仅按 conversation_id 或 query 缓存。

## 4. 队列决策与原型限制

选择 RQ 的 JSONSerializer 作为 P0/P3 的传输基线。队列仅携带服务端生成 run_id，
不把 token、密码、任意工具名或文档正文放入任务参数。
PostgreSQL runs/outbox 是事实来源：接收和 outbox 在同一事务落库，Redis 入队失败仍可重新派发。
RQ 同 delivery_id 使用 unique 入队；不同 delivery 的重复执行仍须通过数据库 claim/finish 防重。
租约过期由应用恢复器重新派发，过期执行者的结果提交以 epoch 和有效租约为条件拒绝。
原型只模拟结果写入，不声称对外部模型提供 exactly-once，也未实现完整额度、公平调度和取消。

API Redis 池最多 16，连接等待 2 秒、连接超时 2 秒、读写 5 秒；Worker 独立池读写 90 秒，
worker_ttl 60 秒让阻塞取任务的等待小于 socket timeout。连接不跨 fork 复用，自动网络重试为零；
业务恢复由有界派发与 PostgreSQL 状态负责。PG 原型每执行子进程池上限 4，不在模型等待时持有事务。
RQ 的 fork 每作业执行模型：检索索引必须放独立共享读取服务；P3 不能在每个子进程重复加载索引。
P3 还需全局模型并发/RPM/TPM、每用户调度、周期恢复器、心跳与取消；P0 不能替代这些交付。

## 5. 基线与运行

```text
uv sync --extra dev --extra p0
python scripts/prepare_p0_fixture.py
docker compose --env-file data/p0-fixture/.env -f infra/p0/compose.yml up -d
python scripts/run_p0_probes.py
```

生成的合成凭据位于被忽略的 `data/p0-fixture/.env`；测试启动器将其作为数据加载，不执行 shell source。
真实进程恢复探针在 Linux 运行；Windows 只跑单元测试或使用 CI。已有 fixture 不重复生成/覆盖凭据。
CI 自动加载和掩码，服务端口仅绑定 127.0.0.1：Keycloak 28081、Postgres 25432、Redis 26379。
`start-dev` 和该 compose 只用于隔离原型，不是生产部署配置。

单元测试验证 JWT 与 API 身份边界；真实测试用两个账号登录，验证 checkpoint 重开，
验证 Worker/子进程硬中断后恢复、重复投递、过期提交拒绝、outbox 入队失败后再派发。
1/2/5/10/20 并发每档发起 5×并发数的合成持久化接收，记录 CPU、Python、系统、池上限和样本 p95。
这只是数据库任务接收层基线，不是完整 HTTP/Agent/模型延迟，也不表示 P5 的 10 用户门禁已通过。
已有真实回答约 20–121 秒、旁路约 67–133 秒的观察来自总体计划；不是 p95，P3/P5 要分层重新测量。
CI summary 保存本次门禁数量与合成接收基线；无报告或真实测试跳过不能标为 P0 通过。

## 6. 迁移设计与 P1 前置条件

先备份 SQLite/检查点/反馈/导出并在副本验证；旧对话由管理员提供明确的归属映射，未知归属隔离只读。
迁移校验消息、版本、引用、审批、反馈与导出数量及内容摘要；不能扩大批准状态。
旧 SQLite 检查点不承诺二进制直接导入 PG：先验证序列化兼容，不能承接的旧流程保留只读历史。
切换窗口暂停旧写入，新系统开始写后不允许双写；回退先保存新数据，不用旧库覆盖新账本。
迁移/回退脚本和正式演练属于 P5；P0 不迁移真实用户数据。

P1 可在身份原型、队列恢复、PG checkpoint 实测全部通过后启动。
仍需确认正式部署环境、SMTP/域名/HTTPS、初始管理员、旧数据归属、供应商并发/RPM/TPM与价格配置，
这些不阻挡本阶段的隔离原型，但在对应注册、迁移和真实并发门禁前必须落实。
