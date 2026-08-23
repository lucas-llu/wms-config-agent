## 2. 核心特点

### WMS / JDA MOCA 领域约束

- **Citation First**：检索证据与引用是回答前置条件；证据不足时返回“未找到可靠依据”。
- **专有名词优先**：MOCA 命令、变量、表名、策略名和配置键依赖 BM25/关键词精确召回，同时用 Dense Embedding 处理同义表达。
- **版本与范围敏感**：Chunk 元数据必须支持 `product_version`、`module`、`site`、`environment`、`config_type` 等过滤字段；字段缺失时不得猜测。
- **结构化回答**：默认输出结论、位置、步骤、验证、适用范围、风险与引用，方便研发人员直接核对。
- **安全只读**：知识检索与未来 MOCA/SQL 工具严格分层。真实系统工具默认采用只读账号、命令 allowlist、超时、返回行数限制与审计日志。
- **脱敏入库**：客户名、仓库号、主机地址、账号口令、业务单据和内部密钥在摄取前必须脱敏，不得进入 Git。

### RAG 策略与设计亮点
本项目在 RAG 链路的关键环节采用了经典的工程化优化策略，平衡了检索的查准率与查全率，具体思想如下：
- **分块策略 (Chunking Strategy)**：采用智能分块与上下文增强，为高质量检索打下基础。
    - **智能分块**：摒弃机械的定长切分，采用语义感知的切分策略以保留完整语义；
    - **上下文增强**：为 Chunk 注入文档元数据（标题、页码）和图片描述（Image Caption），确保检索时不仅匹配文本，还能感知上下文。
- **粗排召回 (Coarse Recall / Hybrid Search)**：采用 **混合检索** 策略作为第一阶段召回，快速筛选候选集。
    - 结合 **稀疏检索 (Sparse Retrieval/BM25)** 利用关键词精确匹配，解决专有名词查找问题；
    - 结合 **稠密检索 (Dense Retrieval/Embedding)** 利用语义向量，解决同义词与模糊表达问题；
    - 两者互补，通过 RRF (Reciprocal Rank Fusion) 算法融合，确保查全率与查准率的平衡。
- **精排重排 (Rerank / Fine Ranking)**：在粗排召回的基础上进行深度语义排序。
	- 采用 Cross-Encoder（专用重排模型）或 LLM Rerank（可选后端）对候选集进行逐一打分，识别细微的语义差异。
    - 通过 **"粗排(低成本泛召回) -> 精排(高成本精过滤)"** 的两段式架构，在不牺牲整体响应速度的前提下大幅提升 Top-Results 的精准度。

### 全链路可插拔架构 (Pluggable Architecture)
鉴于 AI 技术的快速演进，本项目在架构设计上追求**极致的灵活性**，拒绝与特定模型或供应商强绑定。**整个系统**（不仅是 RAG 链路）的每一个核心环节均定义了抽象接口，支持"乐高积木式"的自由替换与组合：

- **LLM 调用层插拔 (LLM Provider Agnostic)**：
    - 核心推理 LLM 通过统一的抽象接口封装，支持**多协议**无缝切换：
        - **Azure OpenAI**：企业级 Azure 云端服务，符合合规与安全要求；
        - **OpenAI API**：直接对接 OpenAI 官方接口；
        - **本地模型**：支持 Ollama、vLLM、LM Studio 等本地私有化部署方案；
        - **其他云服务**：DeepSeek、Anthropic Claude 等第三方 API。
    - 通过配置文件一键切换后端，**零代码修改**即可完成 LLM 迁移，便于成本优化、隐私合规或 A/B 测试。

- **Embedding & Rerank 模型插拔 (Model Agnostic)**：
    - Embedding 模型与 Rerank 模型同样采用统一接口封装；
    - 支持云端服务（OpenAI Embedding, Cohere Rerank）与本地模型（Sentence-Transformers, BGE）自由切换。

- **RAG Pipeline 组件插拔**：
    - **Loader（解析器）**：支持 PDF、Markdown、Code 等多种文档解析器独立替换；
    - **Smart Splitter（切分策略）**：语义切分、定长切分、递归切分等策略可配置；
    - **Transformation（元数据/图文增强逻辑）**：OCR、Image Captioning 等增强模块可独立配置。

- **检索策略插拔 (Retrieval Strategy)**：
    - 支持动态配置纯向量、纯关键词或混合检索模式；
    - 支持灵活更换向量数据库后端（如从 Chroma 迁移至 Qdrant、Milvus）。

- **评估体系插拔 (Evaluation Framework)**：
    - 评估模块不锁定单一指标，支持挂载不同的 Evaluator（如 Ragas, DeepEval）以适应不同的业务考核维度。

这种设计确保开发者可以**零代码修改**即可进行 A/B 测试、成本优化或隐私迁移，使系统具备极强的生命力与环境适应性。

### MCP 生态集成 (Copilot / ReSearch)
本项目的核心设计完全遵循 Model Context Protocol (MCP) 标准，这使得它不仅是一个独立的问答服务，更是一个即插即用的知识上下文提供者。

- **工作原理**：
    - 我们的 Server 作为一个 **MCP Server** 运行，暴露一组标准的 `tools` 和 `resources` 接口。
    - **MCP Clients**（如 GitHub Copilot, ReSearch Agent, Claude Desktop 等）可以直接连接到这个 Server。
    - **无缝接入**：当你在 GitHub Copilot 中提问时，Copilot 作为一个 MCP Host，能够自动发现并调用本项目提供的 `query_wms_knowledge`、`locate_configuration`、`get_document_summary` 等工具，获取私有 WMS 文档证据后再组织回答。
- **优势**：
    - **零前端开发**：无需为知识库开发专门的 Chat UI，直接复用开发者已有的编辑器（VS Code）和 AI 助手。
    - **上下文互通**：Copilot 可以同时看到你的代码文件和我们的知识库内容，进行更深度的推理。
    - **标准兼容**：任何支持 MCP 的 AI Agent（不仅是 Copilot）都可以即刻接入我们的知识库，一次开发，处处可用。

### 多模态图像处理 (Multimodal Image Processing)
本项目采用了经典的 **"Image-to-Text" (图转文)** 策略来处理文档中的图像内容，实现了低成本且高效的多模态检索：
- **图像描述生成 (Captioning)**：利用 LLM 的视觉能力，自动提取文档中插图的核心信息，并生成详细的文字描述（Caption）。
- **统一向量空间**：将生成的图像描述文字直接嵌入到文档文本块（Chunk）中进行向量化。
- **优势**：
    - **架构统一**：无需引入复杂的 CLIP 等多模态向量库，复用现有的纯文本 RAG 检索链路即可实现“搜文字出图”。
    - **语义对齐**：通过 LLM 将图像的视觉特征转化为语义理解，使用户能通过自然语言精准检索到图表、流程图等视觉信息。

### 可观测性、可视化管理与评估体系 (Observability, Visual Management & Evaluation)
针对 RAG 系统常见的“黑盒”问题，本项目致力于让每一次生成过程都**透明可见**且**可量化**，并提供完整的**本地可视化管理平台**：
- **全链路白盒化 (White-box Tracing)**：
    - 记录并可视化 RAG 流水线的每一个中间状态：覆盖 Ingestion（加载→切分→增强→编码→存储）与 Query（查询预处理→Dense/Sparse 召回→融合→重排→响应构建）两条完整链路。
    - 开发者可以清晰看到“系统为什么选了这个文档”以及“Rerank 起了什么作用”，从而精准定位坏 Case。
- **可视化管理平台 (Visual Management Dashboard)**：
    - 基于 Streamlit 的本地 Web 管理面板，提供六大功能页面：
        - **系统总览**：展示当前可插拔组件配置（LLM/Embedding/Splitter/Reranker）与数据资产统计。
        - **数据浏览器**：查看已索引的文档列表、Chunk 详情（原文、metadata 各字段、关联图片），支持搜索过滤。
        - **Ingestion 管理**：通过界面选择文件触发摄取、实时展示各阶段进度、支持删除已摄入文档（跨 4 个存储的协调删除）。
        - **Query 追踪**：查询历史列表，耗时瀑布图，Dense/Sparse 召回对比，Rerank 前后排名变化。
        - **Ingestion 追踪**：摄取历史列表，各阶段耗时与处理详情。
        - **评估面板**：运行评估任务、查看各项指标、历史趋势对比。
    - 所有页面基于 Trace 中的 `method`/`provider` 字段**动态渲染**，更换可插拔组件后 Dashboard 自动适配，无需修改代码。
- **自动化评估闭环 (Automated Evaluation)**：
    - 集成 Ragas 等评估框架（可插拔），为每一次检索和生成计算“体检报告”（如召回率 Hit Rate、准确性 Faithfulness 等指标）。
    - 拒绝“凭感觉”调优，建立基于数据的迭代反馈回路，确保每一次策略调整（如修改 Chunk Size 或更换 Reranker）都有量化的分数支撑。

### Agent 与业务扩展能力 (Agent & Domain Extensibility)

- **Agent 客户端**：通过 MCP 调用知识检索、来源读取和配置定位工具，并根据问题决定调用顺序。
- **WMS 数据源扩展**：后续可增加 Markdown、HTML、Word、导出的配置清单和经过脱敏的历史问题记录。
- **只读诊断工具**：在独立安全边界内接入受控的 MOCA/SQL 查询，用于验证配置是否存在或生效。
- **检索策略扩展**：针对命令、表名和配置键增强关键词召回，针对自然语言问题使用语义召回，并通过版本、模块和站点元数据过滤结果。
- **回答策略扩展**：根据配置定位、故障排查、版本对比等不同意图选择固定输出结构和证据要求。

