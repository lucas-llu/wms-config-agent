import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AuthorizationPanel } from "../src/AuthorizationPanel";
import { ReconciliationPanel } from "../src/ReconciliationPanel";
import type { Api } from "../src/api";
const w = {
  workspace_id: "workspace:fixture",
  revision: 3,
  policy: {
    workspace_id: "workspace:fixture",
    name: "测试库",
    collections: ["fixture"],
    modules: ["inbound"],
    sites: ["DC01"],
    environments: ["test"],
  },
};
const a = { user_id: "u", nickname: "测试成员" };
const m = {
  user_id: "u",
  workspace_id: w.workspace_id,
  role: "reviewer",
  active: true,
  revision: 4,
};
function auth(fail = false) {
  const request = vi.fn(async (path: string, method = "GET") => {
    if (method === "PUT") {
      if (fail) throw new Error("版本已更新");
      return {};
    }
    if (path === "/v1/admin/audit")
      return [
        {
          action: "membership",
          resource_id: w.workspace_id,
          reason: "审批完成",
          created_at: "2026-10-01T00:00:00Z",
        },
      ];
    return { workspaces: [w], memberships: [m] };
  });
  render(
    <AuthorizationPanel api={{ request } as unknown as Api} accounts={[a]} />,
  );
  return { request, user: userEvent.setup() };
}
async function confirm(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
) {
  await user.type(screen.getByLabelText(label), "已核验的审批与供应商记录");
  await user.click(screen.getByRole("checkbox", { name: /我/ }));
}
it("edits bounded scope and new workspaces with expected versions and an audited confirmation", async () => {
  const { request, user } = auth();
  await screen.findByText("测试库 · workspace:fixture");
  await user.click(screen.getByText("知识范围与工作区"));
  await user.click(screen.getByText("测试库 · workspace:fixture"));
  expect(screen.getByLabelText("授权工作区编号")).toBeDisabled();
  expect(screen.getByText("保存知识范围")).toBeDisabled();
  await user.clear(screen.getByLabelText("授权站点"));
  await user.type(screen.getByLabelText("授权站点"), "DC02, DC03");
  await confirm(user, "授权操作原因");
  await user.click(screen.getByText("保存知识范围"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/admin/workspaces/workspace%3Afixture/scope",
      "PUT",
      expect.objectContaining({
        sites: ["DC02", "DC03"],
        expected_revision: 3,
      }),
    ),
  );
  await screen.findByText("授权变更已保存并审计");
  await user.click(screen.getByText("新建工作区"));
  await user.type(screen.getByLabelText("授权工作区编号"), "workspace:new");
  await user.type(screen.getByLabelText("授权工作区名称"), "新工作区");
  for (const label of ["知识集合", "业务模块", "站点", "环境"])
    await user.type(screen.getByLabelText(`授权${label}`), "explicit");
  await confirm(user, "授权操作原因");
  await user.click(screen.getByText("保存知识范围"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/admin/workspaces/workspace%3Anew/scope",
      "PUT",
      expect.objectContaining({ expected_revision: 0 }),
    ),
  );
});
it("revokes a member and changes roles without granting platform administration", async () => {
  const { request, user } = auth();
  await screen.findByText("测试库 · workspace:fixture");
  await user.click(screen.getByText("成员与角色"));
  await user.selectOptions(screen.getByLabelText("授权成员"), "u");
  await user.selectOptions(screen.getByLabelText("授权工作区"), w.workspace_id);
  await screen.findByText(/当前角色 reviewer/);
  await user.selectOptions(
    screen.getByLabelText("授权角色"),
    "workspace_admin",
  );
  await user.click(screen.getByLabelText("启用成员授权"));
  await confirm(user, "授权操作原因");
  await user.click(screen.getByText("保存成员授权"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/v1/admin/memberships",
      "PUT",
      expect.objectContaining({
        role: "workspace_admin",
        enabled: false,
        expected_revision: 4,
      }),
    ),
  );
  await user.click(screen.getByText("最近管理审计（最多 100 条）"));
  expect(screen.getByText(/审批完成/)).toBeVisible();
  await user.click(screen.getByText("刷新授权"));
});
it("keeps failed authorization edits visible for refresh and reconfirmation", async () => {
  const { user } = auth(true);
  await screen.findByText("测试库 · workspace:fixture");
  await user.click(screen.getByText("成员与角色"));
  await user.selectOptions(screen.getByLabelText("授权成员"), "u");
  await user.selectOptions(screen.getByLabelText("授权工作区"), w.workspace_id);
  await confirm(user, "授权操作原因");
  await user.click(screen.getByText("保存成员授权"));
  await screen.findByRole("alert");
  expect(screen.getByText(/版本已更新/)).toBeVisible();
});
function reconciliation(fail = false) {
  const request = vi.fn(async () => {
    if (fail) throw new Error("记录已结算");
    return {};
  });
  const reload = vi.fn(async () => {});
  render(
    <ReconciliationPanel
      api={{ request } as unknown as Api}
      rows={[
        {
          attempt_id: "attempt",
          owner_user_id: "u",
          revision: 2,
          source: "unknown",
          allocated: 200,
        },
      ]}
      reload={reload}
    />,
  );
  return { request, reload, user: userEvent.setup() };
}
async function fill(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText(/attempt · unknown/));
  for (const [label, value] of [
    ["确认输入 tokens", "100"],
    ["确认输出 tokens", "20"],
    ["缓存命中 tokens", "30"],
    ["推理 tokens", "10"],
  ])
    await user.type(screen.getByLabelText(label), value);
  await confirm(user, "对账依据与原因");
}
it("reconciles once with subset totals and requires all fields, reason and confirmation", async () => {
  const { request, reload, user } = reconciliation();
  await fill(user);
  expect(screen.getByText("确认总量 120 tokens")).toBeVisible();
  await user.click(screen.getByText("确认对账"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(reload).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledWith(
    "/v1/admin/usage/attempt/reconcile",
    "POST",
    expect.objectContaining({
      expected_revision: 2,
      input_tokens: 100,
      output_tokens: 20,
      cached_tokens: 30,
      reasoning_tokens: 10,
    }),
  );
  await screen.findByText("对账已完成并审计，额度已重新结算");
});
it("rejects inconsistent subsets and preserves a failed reconciliation for refresh", async () => {
  const { user } = reconciliation(true);
  await fill(user);
  await user.clear(screen.getByLabelText("缓存命中 tokens"));
  await user.type(screen.getByLabelText("缓存命中 tokens"), "101");
  expect(screen.getByText("确认对账")).toBeDisabled();
  await user.clear(screen.getByLabelText("缓存命中 tokens"));
  await user.type(screen.getByLabelText("缓存命中 tokens"), "30");
  await user.click(screen.getByText("确认对账"));
  await screen.findByRole("alert");
  await user.click(screen.getByText("取消对账"));
});
it("shows an empty reconciliation queue", () => {
  render(
    <ReconciliationPanel api={{} as Api} rows={[]} reload={async () => {}} />,
  );
  expect(screen.getByText("暂无待对账记录")).toBeVisible();
});
