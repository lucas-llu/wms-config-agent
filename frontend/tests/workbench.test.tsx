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

it("approves only after explicit confirmation and refreshes the draft", async () => {
  const { user, request } = fixture({
    data: {
      ...workbench,
      state: {
        status: "review_required",
        configuration_tasks: [
          {
            task_id: "t",
            title: "配置任务",
            steps: ["步骤"],
            validation_steps: ["验证"],
            rollback_steps: ["回退"],
          },
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
  await user.selectOptions(
    screen.getByRole("combobox", { name: "审查决定" }),
    "approve",
  );
  await user.type(
    screen.getByRole("textbox", { name: "审查意见" }),
    "已核对文档",
  );
  await user.click(screen.getByLabelText("我确认已检查配置方案与证据"));
  expect(screen.getByRole("button", { name: "提交审查" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "提交审查" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa/review",
      "POST",
      { expected_revision: 2, decision: "approve", comment: "已核对文档" },
    ),
  );
});
it("exports approved drafts through authenticated downloads and can validate current drafts", async () => {
  const { user, request, api } = fixture({
    data: {
      ...workbench,
      state: { status: "approved" },
      approvals: [
        { revision: 1, decision: "approve", actor: "A", comment: "通过" },
      ],
    },
    handler: (path, method) =>
      path.endsWith("/exports") && method === "POST"
        ? { export_id: "export:a" }
        : undefined,
  });
  await open(user);
  await user.click(
    within(document.querySelector(".topbar") as HTMLElement).getByRole(
      "button",
      { name: "工作区" },
    ),
  );
  await user.click(screen.getByRole("button", { name: "导出 JSON" }));
  await waitFor(() =>
    expect(api.download).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa/exports/export%3Aa/download",
    ),
  );
  await user.click(screen.getByRole("button", { name: "验证草稿" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa/validate",
      "POST",
      { expected_revision: 2 },
    ),
  );
});
it("archives, unarchives and restores through owned endpoints", async () => {
  const { user, request } = fixture();
  await screen.findByRole("button", { name: "Trolley 配置" });
  await user.click(screen.getByLabelText("操作 Trolley 配置"));
  await user.click(screen.getByText("归档", { selector: ".menu button" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa/archive",
      "POST",
      undefined,
    ),
  );
  await user.click(
    within(document.querySelector(".list-tabs") as HTMLElement).getByRole(
      "button",
      { name: "归档" },
    ),
  );
  await user.click(screen.getByText("取消归档"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa/unarchive",
      "POST",
      undefined,
    ),
  );
  await user.click(screen.getByRole("button", { name: "回收站" }));
  await user.click(await screen.findByText("恢复"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa/restore",
      "POST",
      undefined,
    ),
  );
});
it("resizes, searches, toggles mobile navigation and starts a clean conversation", async () => {
  const { user, request } = fixture();
  await open(user);
  fireEvent.change(screen.getByRole("slider", { name: "侧栏宽度" }), {
    target: { value: "350" },
  });
  expect(document.querySelector(".shell")).toHaveStyle({
    "--sidebar-width": "350px",
  });
  await user.type(screen.getByRole("textbox", { name: "搜索对话" }), "Trolley");
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(expect.stringContaining("q=Trolley")),
  );
  await user.click(screen.getByRole("button", { name: "打开侧栏" }));
  await user.click(screen.getByRole("button", { name: "关闭侧栏" }));
  await user.click(screen.getByRole("button", { name: "刷新对话列表" }));
  await user.click(screen.getByRole("button", { name: "新对话" }));
  await screen.findByText("今天，想完成哪项配置？");
  await user.click(screen.getByText("梳理入库收货配置流程"));
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue(
    "梳理入库收货配置流程",
  );
});
it("copies and records feedback, never replacing evidence from earlier turns", async () => {
  const { user, request } = fixture();
  const copy = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: copy },
    configurable: true,
  });
  await open(user);
  await user.click(screen.getByRole("button", { name: "复制回答" }));
  expect(copy).toHaveBeenCalledWith("先确认适用范围。");
  await user.click(screen.getByText("有帮助"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/conversations/session%3Aa/feedback",
      "POST",
      { revision: 2, kind: "thumbs_up" },
    ),
  );
});
it("revokes an individual other device and logs out even after backend logout failure", async () => {
  const { user, request, auth } = fixture();
  await user.click(await screen.findByRole("button", { name: /测试用户/ }));
  await user.click(screen.getByText("登录设备"));
  await screen.findByText("其他浏览器");
  await user.click(
    screen.getByText("退出", { selector: ".device-row button" }),
  );
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/v1/me/devices/device%3Ab", "DELETE"),
  );
  await user.click(screen.getByText("个人资料"));
  await user.click(screen.getByText("退出当前账号"));
  await waitFor(() => expect(auth.logout).toHaveBeenCalled());
});
it("keeps uncertain delivery visible, does not retry writes, and lets the user dismiss errors", async () => {
  const { user, request } = fixture({
    handler: (path, method) =>
      method === "POST" && path === "/v1/conversations"
        ? Promise.reject(new Error("连接中断"))
        : undefined,
  });
  await screen.findByRole("button", { name: "Trolley 配置" });
  await user.type(screen.getByRole("textbox", { name: "输入问题" }), "新问题");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await screen.findByRole("alert");
  expect(request.mock.calls.filter((c) => c[1] === "POST")).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "关闭提示" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

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
