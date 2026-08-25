# WMS Configuration Agent V2 — 需求合理性评估、技术选型与 10 天开发计划

> 版本：0.1
> 日期：2026-08-26
> 前置基线：V1 RAG/MCP MVP 已完成
> 目标：在不破坏 V1 检索质量和安全边界的前提下，交付可多轮、可恢复、可审查的多 Agent 配置方案 MVP
> 执行状态：Day 1 已于 2026-08-26 完成，开发分支为 `dev`

## 1. 结论摘要

新增的 Agent 需求方向合理，但不能将 `smart-appointment-ai-agent` 的代码直接搬入。两个项目的业务风险和产物粒度不同：

- 预约项目的核心是“一次意图 → 一个专业 Agent → 一次业务结果”；
- WMS V2 的核心是“长期会话目标 → 需求基线 → 配置任务 DAG → 多来源证据 → 冲突与风险 → 人工审查的方案包”。

因此，V2 应吸收新项目的“路由和分层”思想，但使用更严格的状态契约、持久化、证据门禁、依赖图、草案修订和人机审批。

**10 天可交付的是 Agent MVP，不是生产自动配置平台。** 本计划不包含真实 WMS 写入、多租户鉴权、云部署和全领域配置模板库。

## 2. 对照范围与方法

评估使用了新项目的 README 与实际实现，主要对照：

- `README.md`：多 Agent、五层架构、任务分类和业务扩展思路；
- `agents/task_classification_agent.py`：主调度器与回调协调；
- `agents/task_classification/task_classifier.py`：LLM 意图分类；
- `agents/task_classification/agent_router.py`：专业 Agent 路由；
- `agents/task_classification/state_manager.py` 与 `config/constants.py`：共享状态与转移；
- `agents/consultant/*`：RAG 作为一个专业 Agent 的组件化方式；
- `agents/appointment/*`：输入解析、匹配、业务处理和消息组装分解；
- `api/chat_handler.py`：会话入口与流式响应。

评估不以 README 的功能声明代替代码证据。例如，README 声明多轮状态，但实际 `SharedState` 只有一个 `StateEnum`，`chat_handler.py` 还使用模块级全局 `session_id`；这不足以承载 WMS 方案会话。

## 3. 需求合理性对照

### 3.1 可以复用的思想

| 新项目思想 | 合理性 | WMS V2 采用方式 |
|---|---|---|
| 先识别任务再路由 | 合理 | 区分原子问答、方案目标、草案查看/修订和超出范围请求 |
| 主调度器协调专业 Agent | 合理 | Supervisor 只负责状态图、路由、预算、中断和安全，不生成领域事实 |
| Agent 窄职责分工 | 合理 | Requirement、Planning、Knowledge、Conflict、Validation、Composer 分离 |
| RAG 作为专业知识 Agent | 非常合理 | 直接封装 V1 Hybrid RAG，不新建低配 FAISS 检索链 |
| Agent / Service / Repository 分层 | 合理 | Agent 节点不跨过 Service 访问 SQLite/Chroma/BM25 |
| 组件化的输入解析、匹配、处理和组装 | 合理 | 将 LLM 抽取、确定性校验、RAG 检索、DAG 检查和渲染拆开 |
| Provider 工厂与失败回退 | 合理 | 复用老项目现有 `BaseLLM`/factory/`BudgetedLLM`，不引入另一套模型层 |
| 进度/思考事件与最终答案分开 | 合理 | 定义结构化 progress/interrupt/draft/final 事件，不对用户暴露隐藏推理 |

### 3.2 不能直接复用的设计

| 新项目现状 | WMS 风险 | V2 修正要求 |
|---|---|---|
| `SharedState` 仅保存当前 Agent 枚举 | 无法保存需求基线、决策、证据、任务依赖与草案 | 引入版本化 `ConfigurationSessionState` |
| 模块级全局 `session_id` | 多会话串话，无法恢复或并行 | 每个业务会话使用服务端签发的显式 handle，SQLite 持久化 |
| 进程内存状态，无 checkpoint | 中断、崩溃、用户隔天继续时状态丢失 | LangGraph checkpointer + 业务 Session Repository |
| LLM 分类失败直接转 `other` | 方案目标可被错误拒绝，且无可解释信心 | 规则 + 结构化 LLM 双路；低置信度追问 |
| 状态路由只是 classify/appointment/consult | 不支持需求补全、任务分解、验证、修订与审批 | 使用显式可恢复状态图 |
| 新项目 RAG 是单路 FAISS，缺少页码引用和证据门禁 | 可生成看似完整但无法审计的配置步骤 | 保留 V1 Chroma + BM25 + RRF + Citation + `evidence_sufficient` |
| “流式”实现是 LLM 完整返回后逐字输出 | 不能反映长运行图的真实进度 | 输出节点开始/结束、工具、中断、草案差异等事件 |
| 专业 Agent 部分代码直接导入 DB Router | 破坏文档声明的分层，难以测试和控权 | 所有存储/环境操作通过 Service/Repository/Adapter |
| 无引用冲突、DAG、revision、乐观锁和审批 | 配置方案无法可靠变更和审查 | 将这些作为 P0 契约和确定性测试门禁 |
| 只有少量且偏真实 LLM 的测试 | 结果不稳定，无法在 CI 中复现 | Fake LLM/RAG 为默认，真实 Provider 仅作 opt-in 接受测试 |

### 3.3 合理性检查结果

| 检查维度 | 结论 | 说明 |
|---|---|---|
| 业务必要性 | 通过 | 单次 RAG 无法管理一个跨模块配置目标的需求演进和完整产物 |
| 与 V1 复用关系 | 通过 | RAG/MCP/Trace/Evaluation 可作为 Agent 工具与安全基座，无需重写 |
| Agent 角色数量 | 有条件通过 | 7 个逻辑角色中，Conflict/Validation 优先实现为确定性节点；不必为每个角色启动独立 LLM |
| 状态与持久化 | 通过 | 显式会话 handle、revision、checkpoint 和乐观锁是长会话必需能力 |
| 技术复杂度 | 可控 | LangGraph 只用作状态图运行时，领域契约、RAG、MCP 和存储仍归本项目控制 |
| 10 天可行性 | 限定范围后通过 | 只交付本地、只读、一个脱敏参考场景的 Agent MVP；不承诺生产写入和全领域泛化 |
| 安全边界 | 通过 | 方案生成与真实执行分离，环境工具默认禁用且只读 |
| 验收可测性 | 通过 | 需求已转换为 FR-AGT ID、状态契约和可量化门禁 |

## 4. 技术选型

### 4.1 选型结论

| 领域 | 最终选型 | 版本/约束 |
|---|---|---|
| Agent 编排 | LangGraph `StateGraph` | `langgraph>=1.2,<1.3`；只使用开源运行时 |
| 持久化检查点 | LangGraph SQLite Checkpointer | `langgraph-checkpoint-sqlite>=3.1,<3.2` |
| 业务状态 | Python 3.12 `TypedDict` + frozen/slotted dataclass 边界模型 | 所有 checkpoint 值必须 JSON/msgpack 可序列化 |
| 会话数据 | SQLite Repository | WAL、foreign keys、revision 乐观锁、原子提交 |
| RAG | 现有 HybridSearch + SafeReranker + ResponseBuilder | 保留 Chroma/BM25/RRF/证据门禁 |
| LLM | 现有 BaseLLM/Factory/BudgetedLLM | temperature=0；严格结构化输出；Fake 默认 |
| 外部接口 | 现有 stdio MCP + 6 个粗粒度 session tools | 显式 `session_id` 和 `expected_revision`；保留 V1 工具 |
| 方案输出 | 规范 JSON + 确定性 Markdown renderer | 只从批准 revision 导出；内容指纹 |
| 可观测 | 扩展 TraceContext + 本地 JSONL + Dashboard Agent Sessions 页 | 不强制 LangSmith 或其他 SaaS |
| 测试 | pytest + Fake LLM/RAG + 临时 SQLite | 真实 LLM 测试 opt-in；覆盖率不低于现有 90% 门禁 |

### 4.2 为什么选 LangGraph

LangGraph 的开源运行时直接覆盖 V2 需要的持久化 checkpoint、thread 状态、故障恢复、streaming 和 human-in-the-loop interrupt。与自行实现运行时相比，10 天 MVP 可以把更多时间用在 WMS 领域契约和证据质量上。

同时，不采用默认自由 ReAct 循环，原因是配置方案对可复现路由、预算、证据和审批有硬性要求。编排使用显式 StateGraph，LLM 只负责需求抽取、规划建议和文本组装等需要语义理解的节点。

官方依据（选型时检查）：

- [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)：定位为持久执行、streaming、human-in-the-loop 和 persistence 的低层 Agent 运行时；
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：检查点支持会话内存、故障恢复、人工介入与状态历史；
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：可暂停图并在外部输入后从 checkpoint 恢复；
- [LangGraph PyPI](https://pypi.org/project/langgraph/)：2026-08-26 检查时的稳定系列为 1.2.x，支持 Python 3.12；
- [MCP 2025-11-25 Specification](https://modelcontextprotocol.io/specification/2025-11-25)：继续使用标准 Tools/Resources 契约；业务会话使用显式 handle，不依赖客户端隐式内存。

### 4.3 不选方案

| 方案 | 不选原因 |
|---|---|
| 照搬新项目自实现 Router + Enum State | 不支持持久化、checkpoint、revision、DAG、审批和故障恢复 |
| 单一 ReAct Agent | 工具路由和循环不易形成硬性可验收边界，可复现性不足 |
| 自研完整持久化图运行时 | 10 天内容易把时间花在通用基础设施，而非 WMS 契约与证据质量 |
| AutoGen/CrewAI 式自由 Agent 群聊 | 目标、状态字段所有权、证据门禁和确定性顺序不够清晰 |
| 新建 FastAPI + 独立前端 | 首个 10 天优先完成 Agent 内核和 MCP 契约；新前端不是产品闭环的必要条件 |
| 将 checkpoint 当作唯一业务数据库 | checkpoint 适合图恢复，不适合方案列表、revision 审计、批准和导出查询；需要独立 Repository |

### 4.4 实施前技术 Spike 门禁

Day 1 必须在不实现业务功能的情况下完成：

1. 锁定 LangGraph 和 SQLite checkpointer 依赖，在 Windows + Python 3.12 安装；
2. 运行一个三节点图，验证 checkpoint、interrupt、进程重启恢复和 async stream；
3. 验证 checkpoint 数据不包含不可序列化对象；
4. 运行 V1 全量测试、ruff 和公开基准；
5. 若未通过，当日使用相同 State/Node 契约切换为项目内显式状态机，不让框架评估拖延整个计划。

## 5. 10 天开发计划

### 5.1 计划假设

- 时长：10 个连续开发日，单人主导，每日有一个可演示、可回归的增量；
- 数据：仅使用授权、脱敏的公开 fixture 和本地临时索引；
- Provider：默认 Fake LLM/RAG，真实 Provider 测试是 opt-in；
- 业务样板：先完成一个脱敏的 Inbound Appointment/Receiving 跨配置场景，用于验证多轮与任务依赖；
- 质量：每天必须通过当日新增测试与相关 V1 回归；Day 5 和 Day 10 运行全量门禁。

### 5.2 需求到开发日追溯

| 需求 | 主开发日 | 最终验收日 |
|---|---|---|
| FR-AGT-001 任务识别 | Day 3 | Day 10 |
| FR-AGT-002 多会话隔离 | Day 2、Day 8 | Day 10 |
| FR-AGT-003 需求补全 | Day 3 | Day 10 |
| FR-AGT-004 目标分解 | Day 4 | Day 10 |
| FR-AGT-005 会话内消歧 | Day 5 | Day 10 |
| FR-AGT-006 证据绑定 | Day 5、Day 6 | Day 10 |
| FR-AGT-007 冲突检测 | Day 6 | Day 10 |
| FR-AGT-008 草案版本化 | Day 2、Day 7 | Day 10 |
| FR-AGT-009 草案验证 | Day 6 | Day 10 |
| FR-AGT-010 人工审查 | Day 7、Day 8 | Day 10 |
| FR-AGT-011 方案导出 | Day 7、Day 8 | Day 10 |
| FR-AGT-012 中断恢复 | Day 1、Day 2、Day 3 | Day 10 |
| FR-AGT-013 预算与循环控制 | Day 3、Day 6 | Day 10 |
| FR-AGT-014 V1 兼容 | Day 1–10 每日回归 | Day 10 |
| FR-AGT-015 审计可观测 | Day 3 开始埋点，Day 9 收口 | Day 10 |

### Day 1 — 架构 Spike、依赖和核心契约

**状态**：已完成（2026-08-26）。

**目标**：证明选定运行时与现有项目兼容，冻结 Agent 数据契约。

**开发内容**：

- 新增并锁定 LangGraph/SQLite checkpointer 依赖；
- 创建 `agents/contracts.py`，定义 SessionState、ConfigurationTask、Evidence、Conflict、Finding、Solution；
- 定义状态枚举、字段所有权、revision 和状态指纹；
- 创建三节点 Spike 图验证 interrupt/restart/stream；
- 建立 `agent.enabled: false` 的默认配置和 fail-fast 校验。

**测试**：契约序列化、非法枚举、稳定指纹、Spike 中断恢复、V1 全量回归。

**退出标准**：依赖可安装，重启后能恢复 Spike 图，V1 门禁无回归；否则启用自建状态机回退。

**完成记录**：

- 锁定 `langgraph==1.2.11` 与 `langgraph-checkpoint-sqlite==3.1.1`（`pyproject.toml` 保留兼容范围）；
- 新增可序列化的 Agent 状态、任务、证据、冲突、验证与方案契约，以及稳定指纹和字段所有权检查；
- 新增三节点 async StateGraph 验证器，已验证 stream、interrupt、关闭/重开 SQLite 后 resume；
- `agent.enabled` 默认关闭，持久化路径、预算、人工审批和环境工具开关均已纳入强类型配置；
- Day 1 定向测试 19 passed；全量回归 304 passed / 1 opt-in skipped，总覆盖率 90.62%，Ruff check/format 通过。

### Day 2 — 会话 Repository、checkpoint 与 revision

**目标**：提供可持久化、可隔离、可并发保护的业务会话。

**开发内容**：

- 实现 SQLite `SessionRepository`，表覆盖 sessions/revisions/turns/decisions/approvals/exports；
- 实现 WAL、foreign keys、原子事务和 `expected_revision` 乐观锁；
- 封装 SQLite checkpointer factory，把 graph thread ID 与业务 `session_id` 对齐；
- 实现 create/get/append_turn/update_revision/cancel 基础用例；
- 将数据文件纳入 `.gitignore` 和隐私说明。

**测试**：两会话交替写入、过期 revision 冲突、事务回滚、进程重启、故障/部分写入恢复。

**退出标准**：FR-AGT-002/008/012 的基础存储契约通过，不存在全局业务会话状态。

### Day 3 — Supervisor、意图路由与 Requirement Agent

**目标**：从一句用户目标创建会话，并在需求缺口处可恢复暂停。

**开发内容**：

- 实现 Supervisor StateGraph 骨架、状态 Reducer 和允许转移表；
- 实现原子问答/方案目标/草案操作/超出范围路由；
- 实现 Requirement Agent 的结构化抽取、字段合并与最少追问；
- 实现低置信度追问、interrupt/resume 和近期轮次 + 摘要上下文策略；
- 增加节点、重试、时间和 Token 预算计数器。

**测试**：意图分类数据集、重复字段不追问、假设不写入 confirmed facts、中断恢复、预算超限暂停。

**退出标准**：用 Fake LLM 完成 3 轮需求补全，重启后继续，FR-AGT-001/003/013 通过。

### Day 4 — Planning Agent 与配置任务 DAG

**目标**：将确认的需求基线转换为可验证的任务图。

**开发内容**：

- 实现 Planning Agent 结构化输出契约；
- 实现 `ConfigurationTask` 稳定 ID、去重、依赖边、拓扑排序和环检测；
- 为 Inbound Appointment/Receiving 创建首个脱敏任务模板，仅作规划先验而非硬编码答案；
- 定义每个任务的 evidence requirement、validation criterion 和 rollback requirement；
- 实现需求基线变更后的初步影响/失效标记。

**测试**：无环、有环、重复任务、缺失前置、稳定排序、基线变更失效。

**退出标准**：脱敏场景产出稳定、无环、可解释的任务 DAG，FR-AGT-004 通过。

### Day 5 — Knowledge Agent 与 V1 RAG 证据适配

**目标**：让每个配置任务使用现有 RAG 获取可审计证据。

**开发内容**：

- 实现 `KnowledgeAdapter`，复用 HybridSearch/SafeReranker/ResponseBuilder；
- 实现会话指代消歧为 standalone query，强制注入版本/模块/站点 filters；
- 实现 Evidence Registry、稳定 evidence ID、去重、来源范围和知识库指纹；
- 对独立子任务允许有界并行检索，结果按 task ID 稳定合并；
- 将 `evidence_sufficient=false` 转换为显式证据缺口，不生成支持结论。

**测试**：过滤传递、指代消歧、引用映射、证据去重、空结果、单路检索失败、并行稳定合并。

**退出标准**：每个任务都具有 supported/partial/unsupported 证据状态，FR-AGT-005/006 通过；运行全量 V1 门禁。

### Day 6 — 依赖/冲突与确定性 Validation

**目标**：阻止带冲突、无引用或不可回滚的草案进入审查。

**开发内容**：

- 实现版本、模块、站点、环境和步骤范围冲突检测；
- 实现证据覆盖、任务完整、前置、验证、回滚和风险规则；
- 实现冲突保留与阻塞，不让 LLM 静默选一个来源；
- 实现定向重检索最多 2 轮与追问 interrupt；
- 实现 validation findings 稳定 ID 和重跑幂等。

**测试**：冲突来源保留、版本错配、无引用命令、缺回滚、定向检索上限、相同草案重跑稳定。

**退出标准**：FR-AGT-007/009/013 通过；无证据或有阻塞冲突的草案 100% 不进入 review。

### Day 7 — Solution Composer、修订、审批与导出

**目标**：完成从验证草案到可审查方案包的产品闭环。

**开发内容**：

- 实现 Composer，仅从结构化事实/任务/证据生成 Solution；
- 实现 Markdown 确定性 renderer 和 JSON Schema 导出；
- 实现 revise 影响分析、revision 递增、受影响任务/证据/finding 失效传播；
- 实现 REVIEW_REQUIRED/APPROVED/REJECTED 转移和过期 revision 审批拒绝；
- 实现导出指纹、原子写入和相同 revision 幂等导出。

**测试**：草案前导出拒绝、过期审批拒绝、修订失效、JSON/Markdown 语义对齐、指纹一致、导出幂等。

**退出标准**：FR-AGT-008/010/011 通过，脱敏场景可导出完整方案包。

### Day 8 — MCP Session Tools 与 V1 兼容

**目标**：通过现有 MCP Server 对外提供完整会话用例。

**开发内容**：

- 实现 start/continue/get/validate/review/export 6 个粗粒度工具；
- 定义完整 inputSchema/outputSchema、结构化错误和 MCP annotations；
- 实现 progress/interrupt/draft/final 响应格式与纯 Markdown 降级展示；
- 保留 V1 3 个工具名称、Schema 和响应契约；
- 将协议跟进列入独立兼容测试，首版不强制一次性升级实现中的协议版本。

**测试**：tools/list 顺序/Schema snapshot、会话 E2E、错误映射、stdout 不污染、两会话交替调用、V1 MCP E2E。

**退出标准**：FR-AGT-002/014 通过，一个本地 MCP Client 可继续同一会话并导出批准方案。

### Day 9 — Agent Trace、Dashboard 与安全加固

**目标**：使 Agent 每个决策和工具调用可审计，并验证默认只读边界。

**开发内容**：

- 扩展 TraceContext，增加 session/revision/graph/node/tool/interrupt/approval/budget 字段；
- 扩展 TraceService 的有界读取与隐私过滤；
- 新增 Dashboard `Agent Sessions` 页：会话列表、当前状态、任务 DAG、证据覆盖、冲突、中断与审批历史；
- 执行 Prompt Injection、伪造引用、跳过审批、未授权工具、超预算和敏感日志测试；
- 确保 Environment Inspector 默认禁用，未实现时不对外暴露任何写工具。

**测试**：Trace 完整/脱敏、Dashboard AppTest、未授权工具阻断、审批绕过阻断、会话数据不入 Git。

**退出标准**：FR-AGT-015 通过，高危安全测试拦截率 100%。

### Day 10 — 端到端验收、Agent 评估与发布收口

**目标**：证明从业务目标到批准方案的闭环可复现，且不损害 V1。

**开发内容**：

- 建立 Agent golden scenarios：正常方案、需求变更、证据不足、版本冲突、中断恢复、两会话隔离；
- 实现意图准确率、必需字段、DAG 有效率、引用覆盖/支持率、冲突检出率、完整率和恢复率报告；
- 运行 Fake 公开发布门禁，再使用一个真实 Provider 运行 opt-in 接受测试；
- 更新 README、MCP 文档、查询/方案模式选择、隐私、故障恢复和安全边界；
- 生成发布候选报告与已知限制，Agent 默认开关仅在全部 P0 门禁通过后开启。

**测试**：ruff check/format、全量 pytest + coverage ≥ 90%、V1 公开检索基准、V1 MCP/Dashboard E2E、V2 Agent E2E、隐私和密钥扫描。

**退出标准**：15 个 FR-AGT 中所有 P0 通过；Agent golden scenarios 通过；V1 无回归；已知限制和延期项有书面记录。

## 6. 每日质量门禁

每天结束前至少执行：

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts main.py
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts main.py
.\.venv\Scripts\python.exe -m pytest <当日新增测试> <受影响的 V1 测试> -q
```

Day 5 与 Day 10 执行：

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing --cov-fail-under=90
.\.venv\Scripts\python.exe scripts\run_benchmark.py `
  --dataset tests\fixtures\golden_test_set.json `
  --output data\evaluation\agent-regression.json `
  --enforce-thresholds
```

真实 LLM 接受测试保持 opt-in，不得成为默认 CI 的不稳定前置。

## 7. 里程碑与范围切除顺序

| 里程碑 | 时点 | 可演示结果 |
|---|---|---|
| M7 状态基座 | Day 2 | 两个隔离会话可持久化、revision 保护、重启恢复 |
| M8 需求与规划 | Day 4 | 业务目标经多轮补全后生成任务 DAG |
| M9 证据与验证 | Day 6 | 任务绑定 V1 RAG 证据，冲突/缺口被阻断 |
| M10 方案闭环 | Day 8 | MCP 可完成 start→continue→review→export |
| M11 Agent MVP | Day 10 | 可观测、有安全门禁、有评估报告且 V1 无回归 |

如果进度落后，按以下顺序切除，不得切除安全、证据和恢复能力：

1. 延后 Dashboard Agent Sessions 图形化，保留 CLI/JSON 可观测；
2. 延后 Markdown 之外的多格式导出；
3. 延后独立多 Agent 并行，保留顺序状态图；
4. 延后真实 Provider 的自动化接受测试，保留 Fake 确定性门禁。

**不可切除**：session 隔离、revision、checkpoint 恢复、引用覆盖、冲突暴露、循环预算、显式审批、默认只读与 V1 回归门禁。

## 8. 延期到 Agent MVP 之后的能力

- 受控的真实 MOCA/SQL 只读 Environment Inspector；
- 任何真实 WMS 写入或自动 Apply 能力；
- Postgres/Redis、多租户、SSO、RBAC 和团队级审批流；
- 全量 WMS 模块的配置模板和依赖知识图谱；
- 云端 Agent Server、Streamable HTTP 与长任务队列；
- 主动用户画像/推荐类 Agent：这是预约项目的业务需求，对 WMS 配置 MVP 不是必需能力；
- 自由 Agent-to-Agent 协商：只有当评估证明显式图无法覆盖某类任务，且新模式可继续满足审计与预算门禁时再引入。
