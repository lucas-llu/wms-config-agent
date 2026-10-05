import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { Api, ApiError } from "../src/api";
import { Evidence, Welcome, WorkbenchApp } from "../src/Workbench";
import { fakeAuth, profile, session, workbench } from "./fixtures";

function fixture(
  options: {
    profile?: typeof profile;
    data?: typeof workbench;
    handler?: (path: string, method: string, body: unknown) => unknown;
  } = {},
) {
  const request = vi.fn(
    async (path: string, method = "GET", body?: unknown) => {
      if (options.handler) {
        const custom = options.handler(path, method, body);
        if (custom !== undefined) return custom;
      }
      if (path === "/v1/me") return options.profile || profile;
      if (path.includes("/workbench")) return options.data || workbench;
      if (
        path.startsWith("/v1/conversations?") ||
        path.startsWith("/v1/trash?")
      )
        return [session];
      if (path === "/v1/me/devices")
        return [
          {
            id: "device:a",
            current: true,
            browser: "测试浏览器",
            ip_address: "127.0.0.1",
            last_access: 1,
          },
          {
            id: "device:b",
            current: false,
            browser: "其他浏览器",
            ip_address: "127.0.0.1",
            last_access: 2,
          },
        ];
      return { session };
    },
  );
  const api = {
    request,
    download: vi.fn(async () => {}),
    image: vi.fn(async () => "blob:synthetic"),
  } as unknown as Api;
  const auth = fakeAuth();
  render(<WorkbenchApp api={api} auth={auth} />);
  return { request, api, auth, user: userEvent.setup() };
}
async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Trolley 配置" }));
  await screen.findByText("先确认适用范围。");
}

it("shows login/register/recovery without exposing secrets", async () => {
  const auth = fakeAuth();
  render(<Welcome auth={auth} />);
  await userEvent.click(screen.getByText("登录工作台"));
  await userEvent.click(screen.getByText("创建账号"));
  await userEvent.click(screen.getByText(/找回密码/));
  expect(auth.login).toHaveBeenCalledOnce();
  expect(auth.register).toHaveBeenCalledOnce();
  expect(auth.recover).toHaveBeenCalledOnce();
});
it("new users get no knowledge scope and cannot send", async () => {
  fixture({ profile: { ...profile, workspaces: [] } });
  await screen.findByText(/尚未获工作区/);
  expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
});
it("puts the question in chat immediately, blocks duplicate Enter, resets per-turn review and removes old clarification", async () => {
  let resolve!: (x: unknown) => void;
  const pending = new Promise((r) => (resolve = r));
  const { request, user } = fixture({
    handler: (path, method) =>
      method === "POST" && path.endsWith("/continue") ? pending : undefined,
  });
  await open(user);
  expect(screen.getByText("你使用哪个版本？")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /单次旁路核验/ }));
  await user.type(
    screen.getByRole("textbox", { name: "输入问题" }),
    "测试新问题",
  );
  fireEvent.keyDown(screen.getByRole("textbox", { name: "输入问题" }), {
    key: "Enter",
  });
  await screen.findByText("测试新问题");
  expect(screen.getByRole("status")).toHaveTextContent("正在处理");
  expect(screen.queryByText("你使用哪个版本？")).not.toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole("textbox", { name: "输入问题" }), {
    key: "Enter",
  });
  expect(request.mock.calls.filter((c) => c[1] === "POST")).toHaveLength(1);
  expect(request).toHaveBeenCalledWith(
    "/v1/conversations/session%3Aa/continue",
    "POST",
    { message: "测试新问题", expected_revision: 2, answer_strategy: "review" },
  );
  await act(async () => resolve({ session }));
  await waitFor(() =>
    expect(screen.queryByRole("status")).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("button", { name: /单次旁路核验/ })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});
it("keeps Shift+Enter and IME composition from submitting", async () => {
  const { user, request } = fixture();
  await screen.findByRole("button", { name: "Trolley 配置" });
  const box = screen.getByRole("textbox", { name: "输入问题" });
  await user.type(box, "问题");
  fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  expect(request.mock.calls.every((c) => c[1] !== "POST")).toBe(true);
  fireEvent.keyDown(box, { key: "c", ctrlKey: true });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("creates a conversation only once and preserves server identity ownership", async () => {
  const { user, request } = fixture();
  await screen.findByRole("button", { name: "Trolley 配置" });
  await user.type(
    screen.getByRole("textbox", { name: "输入问题" }),
    "我的新问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await screen.findByText("先确认适用范围。");
  expect(request).toHaveBeenCalledWith("/v1/conversations", "POST", {
    goal: "我的新问题",
    workspace_id: "workspace:test",
    answer_strategy: "standard",
  });
});
it("renders clean collapsed sources by turn and sticky workspace controls", async () => {
  const { user } = fixture({
    data: {
      ...workbench,
      turns: [
        ...workbench.turns,
        {
          ...workbench.turns[1],
          turn_id: "turn:3",
          revision: 2,
          message: "第二个回答",
        },
      ],
    },
  });
  await open(user);
  expect(document.querySelector("details.evidence")).not.toHaveAttribute(
    "open",
  );
  expect(document.body.textContent).not.toContain("[IMAGE:");
  await user.click(
    within(document.querySelector(".topbar") as HTMLElement).getByRole(
      "button",
      { name: "工作区" },
    ),
  );
  expect(screen.getByText("第 1 次回答 · 对话轮次 2")).toBeInTheDocument();
  expect(screen.getByText("第 2 次回答 · 对话轮次 2")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "回到当前回答" }));
  await screen.findByText("先确认适用范围。");
});
it("renames and deletes through compact confirmation dialogs", async () => {
  const { user, request } = fixture();
  await user.click(await screen.findByLabelText("操作 Trolley 配置"));
  await user.click(screen.getByText("重命名"));
  const dialog = screen.getByRole("dialog", { name: "重命名对话" });
  await user.clear(within(dialog).getByRole("textbox"));
  await user.type(within(dialog).getByRole("textbox"), "新标题");
  await user.click(within(dialog).getByText("保存"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa",
      "PATCH",
      { title: "新标题" },
    ),
  );
  await user.click(screen.getByText("移入回收站"));
  await user.click(within(screen.getByRole("dialog")).getByText("确认"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa",
      "DELETE",
      undefined,
    ),
  );
});
it("supports trash multiselect and explicit empty-bin confirmation", async () => {
  const { user, request } = fixture();
  await screen.findByRole("button", { name: "Trolley 配置" });
  await user.click(screen.getByRole("button", { name: "回收站" }));
  await user.click(
    await screen.findByRole("checkbox", { name: "选择 Trolley 配置" }),
  );
  await user.click(screen.getByRole("button", { name: "删除所选" }));
  await user.click(within(screen.getByRole("dialog")).getByText("取消"));
  expect(request.mock.calls.filter((c) => c[1] === "POST")).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "清空回收站" }));
  await user.click(within(screen.getByRole("dialog")).getByText("确认"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/trash/purge?workspace_id=workspace%3Atest",
      "POST",
      { expected_revisions: { "session:a": 2 } },
    ),
  );
});
it("edits profile and opens password action without handling passwords locally", async () => {
  const { user, request, auth } = fixture();
  await user.click(await screen.findByRole("button", { name: /测试用户/ }));
  await screen.findByText("user-a@example.invalid");
  await user.clear(screen.getByRole("textbox", { name: "昵称" }));
  await user.type(screen.getByRole("textbox", { name: "昵称" }), "新昵称");
  await user.click(screen.getByText("保存资料"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/v1/me", "PATCH", {
      nickname: "新昵称",
    }),
  );
  await user.click(screen.getByText("修改密码"));
  expect(auth.changePassword).toHaveBeenCalledOnce();
});
it("shows only caller devices and can revoke others with confirmation", async () => {
  const { user, request } = fixture();
  await user.click(await screen.findByRole("button", { name: /测试用户/ }));
  await user.click(screen.getByText("登录设备"));
  await screen.findByText("其他浏览器");
  await user.click(screen.getByText("退出其他设备"));
  await user.click(within(screen.getByRole("dialog")).getByText("确认"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/v1/me/logout-others", "POST"),
  );
});
it("clears private content on authorization failure instead of retaining another identity view", async () => {
  const { user } = fixture({
    handler: (path) =>
      path.includes("/workbench")
        ? Promise.reject(new ApiError(403))
        : undefined,
  });
  await user.click(await screen.findByRole("button", { name: "Trolley 配置" }));
  await screen.findByText("需要重新确认登录与权限");
  expect(screen.queryByText("先确认适用范围。")).not.toBeInTheDocument();
});
it("historical drafts disable approvals and exports while remaining readable", async () => {
  const { user } = fixture({
    data: {
      ...workbench,
      revision: 1,
      state: {
        status: "review_required",
        configuration_tasks: [
          { task_id: "t", title: "配置任务", steps: ["配置步骤"] },
        ],
      },
    },
  });
  await open(user);
  await user.click(
    within(document.querySelector(".topbar") as HTMLElement).getByRole(
      "button",
      { name: "工作区" },
    ),
  );
  expect(screen.getByText(/历史轮次/)).toBeInTheDocument();
  expect(screen.getByText("配置步骤")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "验证草稿" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "提交审查" })).toBeDisabled();
});
it("renders legacy evidence without markup and lazily loads protected images", async () => {
  const image = vi.fn(async () => "blob:fixture");
  URL.revokeObjectURL = vi.fn();
  const { unmount } = render(
    <Evidence
      api={{ image } as unknown as Api}
      citations={[
        {
          source: "synthetic.pdf",
          excerpt: "Evidence [IMAGE: bad]",
          images: ["/v1/conversations/s/image"],
        },
      ]}
      legacy="Legacy [IMAGE: bad]"
    />,
  );
  expect(image).not.toHaveBeenCalled();
  const details = document.querySelector(
    "details.evidence-source",
  ) as HTMLDetailsElement;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  await screen.findByAltText("文档引用插图");
  expect(image).toHaveBeenCalledOnce();
  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fixture");
});
