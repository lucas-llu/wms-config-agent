# P2 用户中心与 React 工作台

状态：`feature/user-workbench` 实现与验收中。P1 已经通过 #121 合入 dev。
尚未开放真实多人使用；下面的界面不代表 P3 队列/租约或 P4 计费已经完成。

## 范围与设计

- React + TypeScript + Vite；公共 OIDC 客户端使用授权码 + PKCE S256。
  access/refresh token 仅由适配器保存在内存，不写 localStorage、URL、日志或对话库。
- 注册、邮箱验证、找回/修改密码交由统一样式的 Keycloak 页面，不在应用里收集密码。
  找回密码入口进入身份页面选择“忘记密码”，不自行构造恢复凭证或猜测登录动作 URL。
- 用户中心显示身份服务的邮箱/验证状态、业务昵称/账号状态及当前用户的登录会话。
  设备操作调用固定版本的 Keycloak 自助账号 API，仅转交当前用户 token，不使用管理员凭据。
- 密码更新事件监听器在身份服务端撤销旧在线/离线会话；不能依赖用户勾选“退出其他设备”。
  修改密码后可能需要重新登录。监听器必须部署并通过重置/旧 access/refresh token 门禁。
- 新账号不自动获得 Workspace/知识库权限；授权为空时可管理账号，但不能发送配置请求。
- 对话新建、搜索、重命名、归档、删除、恢复及多选永久删除调用 P1 归属接口。
  回收站一键清空针对当前列表显示的最多 100 条；更多数据需后续分页，不假装删除不可见记录。
- 固定对话/工作区栏、独立消息滚动区、可调侧栏、窄屏抽屉；即时问题显示、处理动画，
  Enter 发送 / Shift+Enter 换行、尊重中文输入法，Ctrl+C 不绑定维护动作。
- 默认使用完整片段/一次补查；单次旁路按钮消费后复位，不能由旧状态悄悄继承。
  为此只纳入原目录中的后端回答策略增强及相关测试，原 Streamlit 视觉修改保留不覆盖。
- 当前/历史轮次、草稿、逐轮逐来源折叠证据、审批和受保护导出；历史轮次只读。
  引用图片从父对话引用记录映射到受限只读图片索引，校验格式后以带认证的栅格 blob 显示。
  不暴露目录路径，不开放任意 HTML/SVG，不把 bearer token 拼进图片 URL。
- 未完成的长请求不会自动重试写入；提示刷新已有结果。并发队列、断线执行恢复与取消归 P3，
  当前处理动画只表示请求处理中，不伪造排队/检索的实时阶段或未经引用校验的答案流。

## 运行

前端开发：`cd frontend` → `npm ci --ignore-scripts` → `npm run dev`（仅 127.0.0.1:5173）。
开发代理只转发 `/v1` 至本机 8510；后端仍执行身份/归属与 Origin 检查。
生产构建：`npm run build`；将 `WMS_FRONTEND_DIST` 设为构建目录，由 ASGI 工厂服务 `/` 和 `/assets`。
只挂载编译产物，绝不挂载项目、语料或导出目录。HTML 带 CSP，正式 issuer/域名必须 HTTPS。

身份客户端 `wms-workbench` 必须为 public、禁用隐式/密码授权，PKCE S256 必须，
redirect URI / web origins / logout redirect 只配置真实站点，不使用全域通配符。
启用注册、强制邮箱验证、有限期一次性恢复凭证、强密码、暴力破解保护；
身份反向代理必须对注册/认证/密码恢复表单限制频率，正式 SMTP 不使用测试 Mailpit。
`WMS_WEB_CLIENT_ID` 配置客户端；其余私密后端配置沿用 P1，不进入前端编译变量。

隔离 CI 使用 `infra/p2/compose.yml` 叠加 P0 服务、Mailpit 合成邮箱、固定 Keycloak 26.7.4
密码会话监听器和合成模型。该 compose、start-dev、30 秒邮件链接和合成回答都不是生产部署。
原来的用户数据/知识库/模型密钥没有被搬迁或用于这些测试。

## 退出门禁

Python 全量 >=90% 覆盖率、前端类型/构建/交互测试、真实双浏览器注册→验证邮箱→登录→
私人对话→退出重登、恢复链接重复/过期拒绝、未知邮箱统一提示、密码更新后旧会话失效。
发现功能问题按 Issue → bugfix 分支 → 修复 PR，再进入功能 PR；审查 comment 后才 merge dev。
执行记录在测试结束后补录，不把跳过当作通过。

P2 之后仍需 P3 持久任务/租约/公平调度、P4 用量管理、P5 迁移/压测；完整公开上线尚未达标。

参考：[Keycloak JS](https://www.keycloak.org/securing-apps/javascript-adapter)、
[账号会话授权实现](https://github.com/keycloak/keycloak/blob/26.7.4/services/src/main/java/org/keycloak/services/resources/account/SessionResource.java)。
