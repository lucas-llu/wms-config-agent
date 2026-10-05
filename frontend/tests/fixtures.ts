import { vi } from "vitest";
import type { Auth, Profile, Session, Workbench } from "../src/types";

export const profile: Profile = {
  user_id: "user:a",
  nickname: "测试用户",
  email: "user-a@example.invalid",
  email_verified: true,
  status: "active",
  workspaces: [{ workspace_id: "workspace:test", role: "reviewer" }],
};
export const session: Session = {
  session_id: "session:a",
  goal: "Trolley 配置",
  status: "paused",
  current_revision: 2,
};
export const workbench: Workbench = {
  session,
  revision: 2,
  state: {
    status: "paused",
    open_questions: [{ text: "你使用哪个版本？" }],
    confirmed_context: { module: "inbound" },
    conversation_summary: "测试摘要",
  },
  approvals: [],
  turns: [
    {
      turn_id: "turn:1",
      role: "user",
      message: "我想配置 Trolley",
      revision: 1,
    },
    {
      turn_id: "turn:2",
      role: "assistant",
      message: "先确认适用范围。",
      revision: 2,
      citations: [
        {
          evidence_id: "e:1",
          source: "synthetic.pdf",
          page_start: 3,
          excerpt: "Synthetic evidence. [IMAGE: abc]",
        },
      ],
    },
  ],
};
export function fakeAuth(): Auth {
  return {
    token: vi.fn(async () => "synthetic-token"),
    login: vi.fn(async () => {}),
    register: vi.fn(async () => {}),
    recover: vi.fn(async () => {}),
    changePassword: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  };
}
