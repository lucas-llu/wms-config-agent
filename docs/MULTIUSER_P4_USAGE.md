# P4 用量与额度：实施中

2026-10-07，基线 dev/2119b9b，分支 feature/usage-quota。
本阶段不启动生产、不迁移旧账、不调用付费供应商；新模式需明确 WMS_P4_ENABLED=1，且只能接 P3 持久任务入口。

## 第一批实现

- 月 token 额度：默认 1000000，运行预算预留 100000，均可配置，管理员可调整各用户上限。
  提交/outbox/预留同事务；同键重试不重复占用；账号级短事务锁防止并发绕过。
- 每次供应商调用独立 admission/attempt；本地检查点缓存命中不创建第二笔调用。
  供应商返回、平台估算、未知待对账分开。输入/输出/缓存/推理子集不能重复加到 total。
  [DeepSeek usage 官方合同](https://api-docs.deepseek.com/api/create-chat-completion/)仅用于字段含义，
  不把 DeepSeek 原生价格当成 OpenCode Go 的代理收费。
- 未确认价格默认未定价，不显示确定费用或用 0 冒充未知。价格为管理员配置的 version/currency/
  input/cached/output 每百万 token 十进制字符串，明确 confirmed。每个运行冻结价格快照，费用标注估算。
- 取消/失败的已知消耗仍记账；未知调用保留原调用预算待对账，未使用预留释放。
  返回结果即使随后身份被撤销也先以元数据写入计量，不发布迟到回答。
- UTC 保存事件，Asia/Shanghai 分日/月。跨月调用转移未使用运行预算到调用发生月份，
  老月份已知消耗与未知保留不被转走。供应商超出预算仍如实记用量，阻止进一步超额，不抹掉实际消耗。
- 对话回收/永久删除不删会计记录。用量表无聊天正文、token、密码或模型返回文本，只含 opaque 绑定与数字。
- 用户仅看本人汇总/明细，可按模型、策略、对话过滤；管理员看账号/聚合，不取得私人聊天。
  修改额度、账号启停、成员关系、待对账确认均需平台角色与审计；普通 workspace_admin 不是平台管理员。

## 接口与开关

个人：GET /v1/me/usage、/v1/me/usage/attempts。
管理：/v1/admin/accounts、/accounts/{id}/quota、/accounts/{id}/status、/memberships、
/usage、/usage/unresolved、/usage/{attempt}/reconcile。
对账必须携带 expected_revision 与原因；不能重复确认同一笔未知消耗。

WMS_MONTHLY_TOKENS、WMS_RUN_TOKEN_RESERVE、WMS_MODEL_PRICE_JSON 为可选配置。
生产 API/执行器必须匹配模型/计量配置。005 迁移在 004 后运行，不能作为公开请求启动 DDL。
测试迁移器只在隔离 WMS_P3_LIVE=1 环境使用，正式迁移与旧数据回退仍属于 P5。

## 未退出的验收门禁

真实 PG/OIDC 的额度竞争、对账、权限、删除保留、跨月/价格版本与故障结算；
完整执行器计量集成、前端用量/管理权限流程；全量 Python90%、前端覆盖与原有 P3故障回归。
当前文档是开发记录，不代表以上全部退出条件已通过；最终以 PR 审查/测试记录为准。
