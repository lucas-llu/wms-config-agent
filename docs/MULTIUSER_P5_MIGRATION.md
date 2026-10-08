# P5 迁移、回退与压测：第一批工具，尚未退出阶段

2026-10-07。基线 dev/d1e1b76，feature/migration-pressure。
P4 合并后的复跑已全部通过。P5 当前只开发和演练；没有切换本机旧服务、公开注册或生产数据。

## 首批边界

1. 停止全部旧写入后，用 SQLite backup 保存业务库与可选检查点库，包含 WAL 中的已提交数据。
   清点会话/轮次/版本/审批/导出元数据/反馈/删除标记/标题，记录文件与逻辑内容 SHA256。
   不能覆盖已存在备份；不反序列化旧检查点。备份含私人内容，必须放在受限 ACL/0700 目录，
   不能上传 GitHub、日志或供应商。Windows chmod 不替代 ACL，需要运维确认。
2. 每条旧会话显式指定已有 OIDC 用户 UUID、已授权的有限 Workspace 与原因。
   不创建默认拥有者、不把所有历史分给首位用户。未分配、证据来源缺失/越界、状态摘要损坏均隔离保留。
   同一用户在另一工作区的成员关系不能替代本次目标工作区授权。
3. 目标必须已安装/核验 P1–P4 schema 和 007/008，处于 frozen。迁移用独立离线 p1_migrator 凭据，
   运行/API 用户不能持有或继承此角色。001–006 校验和不改；新空库安装器已实现并经隔离 PG 验证，
   正式环境角色/数据库权限仍需运维核验。`prepare_p1_database.py` / `prepare_p3_database.py`
   **仍仅限一次性 CI，不是生产安装器**。
4. 同一事务写入并核对目标行内容，保存源/投影摘要和审批原记录；不扩大旧批准权限。
   重试同一批次不重复写；后续补充归属可新增批次，但已有绑定/内容不同会拒绝，不能自动改拥有者。
   新用户月额度不导入旧 state.tokens_used；旧估算不伪装为供应商会计账本。
5. 首批旧会话只读，数据库限制继续运行及正文/审批修改，前端给出提示；可以另起新对话。
   检查点不写入新执行命名空间。不兼容旧流程不能伪称恢复成功。
   **旧导出文件当前仅保留原备份中的元数据，目标下载不可用；附件/导出文件/索引的受权搬迁与核对尚待后续实现。**
6. 数据库 release_state 使用版本校验与同事务行锁：active → draining → frozen → rollback_readonly。
   draining 拒绝新会话/任务，允许已接纳工作完成；冻结前要求活跃任务与未知/在途调用全部核对完。
   frozen 阻止业务内容写入；角色变化和账本控制进程也须由运维停止，不能只依赖此内容触发器做全系统静止证明。
   普通运行账号和平台管理员 API 无权直接改发布状态。
7. 回退准备：停止所有 API/执行/控制写入，保存**新期间三个 WMS schema 的完整数据**（含用量/未知预留/检查点），
   外部身份服务、附件/导出文件与索引另外备份，本工具不冒充整机/外部身份恢复。
   校验 dump 并在另一个空库恢复演练。首批工具不自动 pg_restore，不重新启用旧源写入，不做反向数据覆盖。
   反向迁移与源端防双写封锁/恢复验证仍是正式切换前置条件，不能把“有备份”当成“回退已验收”。

## 使用（只在授权副本/测试库）

新建空目标库时，先由 DBA 确认目标数据库、独立的 schema 管理账号，以及三个预期角色的权限边界。
安装器读取 `WMS_SCHEMA_ADMIN_DSN`、`WMS_P1_RUNTIME_PASSWORD`、`WMS_P3_CONTROL_PASSWORD`
三个私有环境变量；不得将密码写进命令行、仓库或报告。先运行只读预检，再显式安装：

```text
python -m scripts.install_multiuser_schema --expected-db <目标数据库名>
python -m scripts.install_multiuser_schema --expected-db <目标数据库名> --apply
```

安装器仅接纳新空库或完整匹配校验和的已安装库；它在单个事务内安装 001–008 与检查点表，
初始 `release_state` 为 `frozen`，不会自动开放写入。重复运行只核验版本、表所有权、
强制行级隔离和运行账号；现有一次性 CI 库没有 001 安装账本，不能直接冒充已安装生产库。
检查点依赖版本变化时必须先审查，不自动迁移未知 SQL。

若 `p1_runtime` 和 `p3_control` 已存在，且 DBA 对这个新库撤销了 `PUBLIC CONNECT`，
安装器会保持失败关闭，绝不跳过已有角色密码校验。DBA 在核实目标库与角色后，
须先仅对这两个运行角色显式授予目标库 `CONNECT`，然后再执行上述 `--apply`；
错误密码仍会拒绝安装，目标业务 schema 保持空白。此预授权是正式部署的人工权限前置条件，
不能把迁移所有者设为 LOGIN，也不能授权运行角色继承它。

```text
python -m scripts.p5_tools snapshot --source <业务库副本> --destination <新私有备份目录> --checkpoints <检查点副本> --confirm-source-stopped
python -m scripts.p5_tools verify --bundle <备份目录>
python -m scripts.p5_tools import --bundle <备份目录> --ownership-plan <私有归属JSON> --operator <平台运维用户UUID>
```

import 默认仅 dry-run；检查隔离项与摘要、确认目标及凭据后才可显式 `--apply`。
私有归属 JSON 的形状：`{"session:...":{"user_id":"已有UUID","workspace_id":"workspace:explicit","reason":"明确核验原因"}}`。
离线目标连接来自 `WMS_MIGRATION_DSN`，不在命令行放密码。临时运维身份不映射成公众平台管理员。

```text
python -m scripts.p5_tools phase --target draining --revision <当前版本> --operator <UUID> --reason <原因>
python -m scripts.p5_tools phase --target frozen --revision <新版本> --operator <UUID> --reason <原因>
python -m scripts.p5_tools preserve --destination <新私有dump路径> --confirm-workers-stopped
python -m scripts.p5_tools phase --target rollback_readonly --revision <新版本> --operator <UUID> --reason <原因>
```

phase 同样默认 dry-run；生产发布不提供无证据的 `active` 一键复位。
preserve 需要同版本 pg_dump；凭据仅在子进程环境，错误输出不公开；失败的部分 dump 不能作为有效备份。
完整恢复、业务数量/摘要/RLS检查通过且新旧写入封锁已验明之前，不允许开放写入。

## 压测预算与报告

首批 supplier 工具仅测试供应商请求/用量，固定非客户合成提示，不发送私人对话或 WMS 知识。
`WMS_P5_SUPPLIER_LIVE=1` + 私有显式预算 JSON 才能调用；必须 approved=true、max_calls、concurrency、
rpm、tpm、max_output_tokens、total_token_budget。预算未确认不调用；重试固定 0。
并发、每分钟请求/预留 token、总预算受约束；未知结果保留预算并停止新请求，供应商超预算也停止后续请求。
开始前独占占用报告文件，崩溃/中断留 incomplete，不自动免费重跑。每次新批次仍须人类明确批准。
不要把 1M 上下文当成并发/输出/费用限额；代理价格未确认时不显示实际费用。

```text
python -m scripts.p5_tools supplier --approval <私有预算JSON> --settings <已核验配置> --report <新报告路径>
```

结果只有次数、状态、时延、token 来源/数字和错误类型，无 API key、原始响应、reasoning 或私人内容。
`scope=supplier_only_synthetic_prompt_not_platform_slo` 明确不是平台 10 用户 p95，不能用它宣布多人上线。
供应商套餐按 [OpenCode Go](https://opencode.ai/v2/docs/console/go) 核验，不套用 DeepSeek 原生价格；
[DeepSeek API 字段定义](https://api-docs.deepseek.com/api/create-chat-completion/)仅用于用量子集解释。

## 本机合成多用户负载观察（2026-10-08）

固定本机测试版本 `4f0bd50`，Windows 主机上的公开 API/共享执行器、Docker 中的
PostgreSQL 17、Redis、Keycloak 26.7.4 与一个 Linux RQ Worker；知识与模型均为
`fixture` 合成实现，真实供应商调用 **0**。10 个专用测试账号各自通过真实 Keycloak
授权码登录，获得同一个仅允许 `fixture/inbound/DC01/test` 的合成工作区成员资格。
负载报告仅保存数值与终态，保存在本机受限目录 `D:/software/wms-test/config/p5-load-report.json`。

| 同阶段提交请求数 | 独立 OIDC 账号数 | 请求接收 p95 | 完成情况 |
|---:|---:|---:|---:|
| 1 | 1 | 0.078 秒 | 1/1 成功 |
| 2 | 2 | 0.093 秒 | 2/2 成功 |
| 5 | 5 | 0.141 秒 | 5/5 成功 |
| 10 | 10 | 0.328 秒 | 10/10 成功 |
| 20 | 10 | 0.469 秒 | 20/20 成功，含 4 次旁路核验 |

合计 38/38 个合成任务完成。对最后一档的 20 个任务，逐一以另一账号访问任务与父对话，
各 20 次均返回 404。表中 p95 是这次小样本的 HTTP 接收耗时，不含排队和回答时间；
第 20 档是 **10 个账号提交 20 请求**，不是 20 个独立用户。尚未采集 CPU/内存/连接池
峰值、等待公平性和长历史读取，也未做真实供应商默认/旁路混合压测；不得以此替代灰度门禁。

## P5 尚待完成/外部前置

- 部署主机/域名/HTTPS、PG/Redis/OIDC 与发信服务、正式管理员和已有历史的归属/证据来源确认。
- 源端可验证的停写封锁；目标 schema 生产安装/权限审计；文件与索引搬迁；旧检查点兼容/只读策略的人工验收。
- 私有数据副本的完整迁移数量/内容摘要与恢复演练；post-cutover 数据保存/反向迁移或明确只读回退。
- 经批准的真实供应商调用/费用预算与并发/RPM/TPM；10 个真实 OIDC 用户的完整默认/旁路混合端到端测试。
- 1/2/5/10/20 梯度、同对话冲突、取消/断线/硬终止/依赖故障、长历史读取性能、CPU/内存/池/许可峰值。
  接收 p95≤1s、普通读取 p95≤500ms、受控错误率<1%、泄露/重复提交/应用内重复记账 0。
- 小团队灰度和回退演练通过后再开放多人使用。当前首批工具和合成测试不意味着上述退出条件已经达到。
