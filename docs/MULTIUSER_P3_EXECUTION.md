# P3：后台执行、全局调度与恢复合同

2026-10-06。功能分支 `feature/run-recovery`，主 PR #140、独立修复 PR #147。
后续执行器/资源上限/检查点接管/React 恢复已实现，正在最终故障门禁。
**不等于 P4 费用/月额度或 P5 正式迁移、真实供应商压测和公开灰度已完成。**

## 执行链路

1. 固定 OIDC issuer、当前会话内省、账号/成员与父对话归属检查。
2. 幂等键、预期版本、用户上限；同事务保存 runs/outbox。新对话编号绑定用户、Workspace、键，
   首次 202 丢失后仍可查回原任务。同对话 active 部分唯一约束；默认待处理 5、运行 2。
3. PG 保存用户轮转位置，每批每用户一个队首，按最近派发时间公平轮转。
4. RQ JSONSerializer 只传 run_id；固定 `workers.runs.execute_run`，Worker 在导入/执行前
   校验函数名、单参数编号、空 kwargs。不同投递由 PG claim 防重。
5. 共享执行进程初始化一次模型 HTTP 池、嵌入与只读向量/关键词索引；RQ 子进程仅访问认证内部接口，
   不重新加载知识库。所有副本共享 PG 全局资源许可。
6. 不保存用户 access/refresh token 或请求 lambda。由已验证提交保存 issuer/sub/SID/iat；
   独立 Keycloak 会话只读服务账号检查 enabled/notBefore/在线 SID，再用 p1_runtime 当前身份 RLS。
   模型/检索、心跳、提交重复检查；身份服务不可用时停止下一步、等待安全恢复。
7. 当前回合本地暂存，已核验结果、用户/助手轮次、终态/事件一次短事务提交；
   模型等待不占用 PG 连接或事务。删除/成员撤销只终止对应任务，不能中断其他用户调度。
8. React 即刻显示问题，展示阶段、取消、事件补读。公开模式禁用旧同步 continue/同步生成 MCP，
   防止绕过后台执行权和全局额度；旧 Streamlit/stdio 仍是隔离主机入口，不是公开多用户入口。

## 检查点与调用恢复

- claim 的 thread/epoch 映射绑定当前对话；复制前验证源属于同一父对话，不能混用同用户的不同记忆。
  写策略要求当前有效 run/epoch，保存器事务前后检查租约。旧代次只读，迟到写入拒绝。
- 可信快照承接 channel values/versions、pending writes。上个回合 thread 指针随最终结果提交；
  多轮仍使用近期消息与滚动摘要。永久删除清理全部代次检查点。
- 私有 `run_model_calls` 保存请求指纹、实际 attempt、归一化结果。模型已返回但节点未提交时，
  接管复用相同请求结果；已发出但结果未知则 uncertain，禁止后续调用/自动重放。
  不承诺外部 exactly-once，也不伪造已知费用。
- 仅明确 429 拒绝最多 2 次退避，每次独立预留/记录；底层 provider 重试 0。
  超时/5xx 可能已执行，默认不自动重发。
- 取消是协作式：排队立即终止，运行阻止下一步/提交。未调用许可释放，已发请求不承诺零费用。
- PG 重启后，借出前只读健康检查丢弃失效连接，不盲目重复写事务。
  Redis 失联/重启后 PG outbox 重派；PG 始终是事实源。

## 全局资源与池预算

供应商/模型身份使用固定 hash。短事务原子预留在途、review 子上限、60 秒 RPM/TPM；
配置不一致的副本拒绝启动。Redis 重启不能清空供应商在途预算。
UTF-8 输入字节加 framing/最大输出作为保守预算，已知实际 token 返回后修正窗口保留量；
这不是精确 tokenizer、月额度或账单，真实计量/价格/结算/未知费用对账属于 P4。

检索另有全局许可及本地上限，限制“用户数 × hybrid 线程”的放大；dense/sparse 仍可并行。
只读索引进程不做 ingestion；原生嵌入/数值计算线程还需配置 OMP/MKL 上限。

默认执行并发 4、模型在途 4、review 1、RPM 60、TPM 200000；排队/执行 600 秒、租约 45 秒。
未知许可保持时间必须超过供应商 timeout 加保护窗口。1M context 不等于 1M 输出/TPM，限额需供应商确认。

单进程最大池：公开 API PG 8；执行 PG 8、控制 PG 4；每并行图 loop-scoped checkpoint 4；
模型 HTTP 16/keepalive 8、SID HTTP 16/keepalive 8；RQ 子进程内部 HTTP 4。
Redis 独立池 8，等待/连接 2 秒，调度读写 5 秒、Worker 阻塞读写 90 秒；自动网络重试 0。
部署需按全部副本/Worker 数量计算连接总预算，不能只看一个池。

## 前端恢复

`/v1/me.durable_runs` 标记选择异步模式；未启用的旧部署保持 P2。
首条 POST `/v1/conversations` 和续问 `/{id}/runs` 都要 idempotency_key，返回 202。
`/v1/runs/lookup` 查回首次接收响应丢失的任务；`/{run}/request` 只向所属用户返回待处理问题。

事件只有 run_id/递增 sequence/阶段/已提交版本指针，不输出未校验正文。
游标绑定运行，终态前排空分页；空闲心跳、20 秒连接轮换。fetch 从内存获取新 Bearer，重连只 GET。
连续网络失败最多 5 次后给“重新连接进度”，不自动 POST 问题。
sessionStorage 仅存当前用户/Workspace 的 opaque key/run/conversation，不存 token、问题、证据、回答。
页面切换中止旧订阅，旧任务不能覆盖新页面；刷新补读原任务，不再次执行。

## 启用与运维

生产基础设施/凭据由管理员配置，不能使用 CI 合成账号或 Keycloak start-dev。

1. 分离迁移账号、p1_runtime/p3_control：非 owner/superuser/BYPASSRLS，不能继承 owner。
   按 001/官方 checkpoint setup/002/003 顺序迁移；已执行迁移校验和不可变。
   `prepare_p3_database.py` 只用于 `WMS_P3_LIVE=1` 隔离测试，不是公开启动迁移器。
2. Keycloak 专用 confidential client + client_credentials，会话只读角色首版使用
   realm-management/view-users，禁止 manage-users，不给浏览器；正式账号域需细化可查询范围。
3. Redis 私网、TLS、专用 ACL，限制 rq:* 键/通知域，禁止 FLUSH/CONFIG/DEBUG。
   RQ 固定版本的 INFO/事务/Lua/通知权限必须实测，不能套 cache-only ACL。
   WMS_REDIS_URL 使用含专用用户名/凭据的 rediss；只有隔离 localhost 测试允许非 TLS。
4. 每个共享执行副本一个 Uvicorn worker、只读挂载索引；内部接口只私网 HTTPS 或 loopback，
   WMS_EXECUTION_TOKEN 至少 32 字符。公开网关不能访问内部路由。
5. 启动有限 RQ Worker、检查内部 ready，再设公开 API `WMS_P3_ENABLED=1`。
   新公开 API 不加载模型/索引；不兼容或未就绪的执行服务拒绝初次启用。

```text
python -m uvicorn api.execution_bootstrap:from_environment --factory --workers 1 --host 127.0.0.1 --port 8531 --no-access-log
python -m workers.runs
python -m uvicorn api.bootstrap:from_environment --factory --host 127.0.0.1 --port 8510 --no-access-log
```

必填：WMS_OIDC_ISSUER、USER_DB_DSN、CONTROL_DB_DSN、API_CLIENT_SECRET、SESSION_CLIENT_ID/SECRET、
EXECUTION_URL/TOKEN、REDIS_URL；上述全部变量均以 WMS_ 开头，模型/索引沿用 settings。
可调同前缀：EXECUTORS、MODEL_INFLIGHT、REVIEW_INFLIGHT、MODEL_RPM/TPM、MODEL_WAIT_SECONDS、
MODEL_HOLD_SECONDS、PENDING_PER_USER、RUNNING_PER_USER、RUN_LEASE_SECONDS、QUEUE_SECONDS、EXECUTION_SECONDS。
Windows 可运行公开/执行 API；RQ fork Worker 的验收部署基线为 Linux。

## 验收边界

本地 Python 权限/模型/恢复契约与回归；React 类型/构建、完整源码覆盖门槛不降低。
隔离 CI 实测真实 SID、两账号私有资源、完整图首问/续问、重复投递、公平调度、全局许可竞争、
取消/撤销/删除、代次写保护和源绑定；真实 RQ/执行硬中断与 Redis 重启，额外隔离 PG 停止/重启。
真实两浏览器注册、邮件验证、问答、退出/重登、密码与设备撤销走新的 RQ 链路。

合成 1/2/5/10/20 档记录 CPU/Python/池/模型峰值和总耗时；使用合成应用身份及模型，
**不是 20 个真实 OIDC 用户/真实供应商的生产 p95，也不替代 P5 的 10 用户门槛。**
最终数量/覆盖率以 PR #147/#140 最终审查 comment 为准；环境跳过不能算实测通过。
