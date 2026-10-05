// Development-only entry: not part of the production index.html build.
import { createRoot } from "react-dom/client";
import { WorkbenchApp } from "../src/Workbench";
import type { Api } from "../src/api";
import type { Auth, Workbench, Profile, Session } from "../src/types";
import "../src/style.css";

const session: Session = {
  session_id: "session:demo",
  goal: "演示：梳理 Trolley 配置",
  status: "paused",
  current_revision: 2,
};
const profile: Profile = {
  user_id: "demo",
  nickname: "演示用户",
  email: "demo@example.invalid",
  email_verified: true,
  status: "active",
  workspaces: [{ workspace_id: "workspace:demo", role: "reviewer" }],
};
let data: Workbench = {
  session,
  revision: 2,
  state: {
    status: "paused",
    confirmed_context: { module: "inbound", environment: "test" },
    open_questions: [],
  },
  approvals: [],
  turns: [
    {
      turn_id: "u",
      role: "user",
      message: "我希望梳理一套配置流程，如何开始？",
      revision: 1,
    },
    {
      turn_id: "a",
      role: "assistant",
      message:
        "先确认业务目标、适用版本和工作区范围。\n\n我们可以逐步梳理配置任务，再检查对应的文档证据。\n\n*这是合成界面示例，不是 WMS 配置建议。*",
      revision: 2,
      citations: [
        {
          source: "synthetic-demo.pdf",
          page_start: 9,
          excerpt:
            "Synthetic preview only: confirm the business scope before preparing a configuration plan.",
        },
      ],
    },
  ],
};
const api = {
  request: async (
    path: string,
    method = "GET",
    body?: { goal?: string; message?: string },
  ) => {
    if (path === "/v1/me") return profile;
    if (path.startsWith("/v1/me/devices")) return [];
    if (path.includes("/workbench")) return data;
    if (path.startsWith("/v1/trash/snapshot"))
      return { count: 0, fingerprint: "a".repeat(64) };
    if (path.startsWith("/v1/trash")) return [];
    if (path.startsWith("/v1/conversations?")) return [session];
    if (
      method === "POST" &&
      (path === "/v1/conversations" || path.endsWith("/continue"))
    ) {
      await new Promise((r) => setTimeout(r, 500));
      const revision = ++session.current_revision;
      data = {
        ...data,
        session,
        revision,
        turns: [
          ...data.turns,
          {
            turn_id: "u" + revision,
            role: "user",
            message: body?.message || body?.goal || "",
            revision,
          },
          {
            turn_id: "a" + revision,
            role: "assistant",
            message:
              "演示回答：本页面只展示交互，没有连接身份、数据库或模型服务。正式入口需要部署身份服务与 P1/P2 后端。",
            revision,
          },
        ],
      };
      return { session };
    }
    throw new Error("预览只支持阅读与合成对话，不会修改真实数据。");
  },
  download: async () => {},
  image: async () => {
    throw new Error("预览无真实图片");
  },
} as unknown as Api;
const auth: Auth = {
  token: async () => "",
  login: async () => {},
  register: async () => {},
  recover: async () => {},
  changePassword: async () => {},
  logout: async () => {
    location.reload();
  },
};
createRoot(document.getElementById("root")!).render(
  <>
    <div className="preview-banner">
      界面演示 · 合成数据 · 不连接实际身份或 WMS
    </div>
    <WorkbenchApp api={api} auth={auth} />
  </>,
);
const style = document.createElement("style");
style.textContent =
  ".preview-banner{height:26px;text-align:center;background:#f3f7ec;color:#7b8c65;font-size:10px;letter-spacing:1px;padding-top:4px}.shell{height:calc(100dvh - 26px)}";
document.head.appendChild(style);
