import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { UsagePanel } from "../src/UsagePanel";
import { Api } from "../src/api";

const summary = { period:"2026-10-01",timezone:"Asia/Shanghai",today_requests:2,month_requests:3,quota:1000,used:100,reserved:200,remaining:700,unknown_attempts:1,estimated_attempts:1,input_tokens:80,output_tokens:20,costs:[] };
const row = { attempt_id:"a",model:"synthetic",strategy:"standard",source:"unknown",status:"unknown",total_tokens:null,input_tokens:null,output_tokens:null,estimated_cost:null,currency:null,price_version:null,started_at:"2026-10-01T00:00:00Z",revision:1 };
function fixture(admin = false, fail = false) {
  const request = vi.fn(async (path: string, method = "GET") => {
    if (method === "PUT") { if (fail) throw new Error("权限已撤销"); return {}; }
    if (path === "/v1/me/usage") return summary;
    if (path.startsWith("/v1/me/usage/attempts")) return [row];
    if (path === "/v1/admin/accounts") return [{ user_id:"u",nickname:"测试账号",status:"active",monthly_tokens:1000,quota_revision:1 }];
    if (path === "/v1/admin/usage") return [{ model:"synthetic",strategy:"standard",known_tokens:100,unknown_attempts:1 }];
    if (path === "/v1/admin/usage/unresolved") return [{ attempt_id:"a",revision:1,source:"unknown",allocated:200 }];
    throw new Error("not supported");
  });
  render(<UsagePanel api={{ request } as unknown as Api} admin={admin} />);
  return { request,user:userEvent.setup() };
}
it("shows unknown as unknown, separates reserved quota and only reads own endpoints",async () => {
  const { request,user }=fixture();
  await screen.findByText(/剩余 700/);
  expect(screen.getByText(/费用未知或价格未配置/)).toBeInTheDocument();
  expect(screen.getByText("未知/待对账")).toBeInTheDocument();
  expect(request.mock.calls.some(c=>c[0].startsWith("/v1/admin"))).toBe(false);
  await user.selectOptions(screen.getByLabelText("用量策略"),"review");
  await user.type(screen.getByLabelText("用量模型"),"synthetic");
  await user.type(screen.getByLabelText("用量对话"),"conversation");
  await user.click(screen.getByText("刷新用量"));
  await waitFor(()=>expect(request.mock.calls.some(c=>c[0].includes("strategy=review")&&c[0].includes("conversation_id=conversation"))).toBe(true));
});
it("admin quota/status writes require explicit confirmation and an audited reason",async () => {
  const { request,user }=fixture(true);
  await user.click(await screen.findByText("测试账号 · active"));
  expect(screen.getByText("保存额度")).toBeDisabled();
  await user.clear(screen.getByLabelText("月 token 额度")); await user.type(screen.getByLabelText("月 token 额度"),"2000");
  await user.type(screen.getByLabelText("管理操作原因"),"审批通过的额度调整");
  await user.click(screen.getByLabelText("我确认此操作影响账号/额度且将被审计"));
  await user.click(screen.getByText("保存额度"));
  await waitFor(()=>expect(request).toHaveBeenCalledWith("/v1/admin/accounts/u/quota","PUT",{monthly_tokens:2000,expected_revision:1,reason:"审批通过的额度调整"}));
  await user.click(screen.getByText("测试账号 · active"));
  await user.type(screen.getByLabelText("管理操作原因"),"经过批准的停用请求");
  await user.click(screen.getByLabelText("我确认此操作影响账号/额度且将被审计"));
  await user.click(screen.getByText("停用账号"));
  await waitFor(()=>expect(request).toHaveBeenCalledWith("/v1/admin/accounts/u/status","PUT",{enabled:false,reason:"经过批准的停用请求"}));
});
it("failed management writes remain visible without pretending they succeeded",async () => {
  const { user }=fixture(true,true);
  await user.click(await screen.findByText("测试账号 · active"));
  await user.type(screen.getByLabelText("管理操作原因"),"经过批准的启用请求");
  await user.click(screen.getByLabelText("我确认此操作影响账号/额度且将被审计"));
  await user.click(screen.getByText("启用账号"));
  await screen.findByRole("alert");
  expect(screen.getByText("权限已撤销")).toBeInTheDocument();
  await user.click(screen.getByText("取消修改"));
});
