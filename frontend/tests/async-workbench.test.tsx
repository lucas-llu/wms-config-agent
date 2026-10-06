import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Api, ApiError } from "../src/api";
import { WorkbenchApp } from "../src/Workbench";
import { savePending, type Run } from "../src/runs";
import { fakeAuth, profile, session, workbench } from "./fixtures";

afterEach(() => sessionStorage.clear());
const run: Run = {
  run_id: "run:a",
  conversation_id: session.session_id,
  status: "queued",
  stage: "queued",
  sequence: 1,
  result_revision: null,
  events_url: "/v1/runs/run:a/events",
};
function fixture(options: { restored?: boolean; unknown?: boolean } = {}) {
  let active: Run | null = options.restored ? run : null;
  let data = workbench;
  let finish = () => {};
  let receive = (_id: string, _data: string) => {};
  const events = vi.fn(async (_path, _cursor, signal, callback) => {
    receive = callback;
    await new Promise<void>((resolve) => {
      finish = resolve;
      signal.addEventListener("abort", () => resolve(), { once: true });
    });
  });
  const request = vi.fn(
    async (path: string, method = "GET", body?: unknown): Promise<unknown> => {
      if (path === "/v1/me") return { ...profile, durable_runs: true };
      if (path.startsWith("/v1/conversations?")) return [session];
      if (path.endsWith("/workbench")) return data;
      if (path.endsWith("/runs/active")) return active;
      if (
        (path.endsWith("/runs") || path === "/v1/conversations") &&
        method === "POST"
      ) {
        active = run;
        if (options.unknown) throw new ApiError(503);
        return run;
      }
      if (path.startsWith("/v1/runs/lookup?")) return active;
      if (path.endsWith("/request")) return { message: "测试后台问题" };
      if (path.endsWith("/cancel")) {
        active = {
          ...run,
          status: "cancelled",
          stage: "cancelled",
          sequence: 2,
        };
        finish();
        return active;
      }
      if (path === "/v1/runs/run%3Aa") return active;
      throw new Error(
        `Unhandled synthetic path ${path} ${method} ${JSON.stringify(body)}`,
      );
    },
  );
  const api = { request, events } as unknown as Api;
  render(<WorkbenchApp api={api} auth={fakeAuth()} />);
  return {
    request,
    events,
    user: userEvent.setup(),
    complete: async () => {
      active = {
        ...run,
        status: "succeeded",
        stage: "succeeded",
        sequence: 3,
        result_revision: 3,
      };
      data = {
        ...workbench,
        session: { ...session, current_revision: 3 },
        revision: 3,
        turns: [
          ...workbench.turns,
          { turn_id: "u", revision: 2, role: "user", message: "测试后台问题" },
          {
            turn_id: "a",
            revision: 3,
            role: "assistant",
            message: "已核验的后台结果",
          },
        ],
      };
      await act(async () => {
        receive("run:a/3", JSON.stringify(active));
        finish();
      });
    },
  };
}
async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Trolley 配置" }));
}
it("immediately shows the question, stages and only committed final answer", async () => {
  const f = fixture();
  await open(f.user);
  await f.user.type(
    screen.getByRole("textbox", { name: "输入问题" }),
    "测试后台问题",
  );
  await f.user.click(screen.getByRole("button", { name: "发送问题" }));
  await screen.findByText("已排队，等待执行");
  expect(screen.getByText("测试后台问题")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("");
  expect(f.request.mock.calls.find((c) => c[1] === "POST")?.[2]).toMatchObject({
    message: "测试后台问题",
    expected_revision: 2,
    idempotency_key: expect.any(String),
  });
  await f.complete();
  await screen.findByText("已核验的后台结果");
  expect(screen.queryByText("已排队，等待执行")).not.toBeInTheDocument();
  expect(
    sessionStorage.getItem(`wms.run:${profile.user_id}:workspace:test`),
  ).toBeNull();
});
it("cancels a queued run and doesn't append a fabricated answer", async () => {
  const f = fixture();
  await open(f.user);
  await f.user.type(
    screen.getByRole("textbox", { name: "输入问题" }),
    "测试后台问题",
  );
  await f.user.click(screen.getByRole("button", { name: "发送问题" }));
  await f.user.click(await screen.findByRole("button", { name: "取消处理" }));
  await screen.findByText("已取消");
  expect(screen.queryByText("已核验的后台结果")).not.toBeInTheDocument();
  expect(f.request).toHaveBeenCalledWith("/v1/runs/run%3Aa/cancel", "POST");
});
it("restores a run on refresh without sending POST", async () => {
  savePending(profile.user_id, "workspace:test", {
    key: "a".repeat(32),
    workspace: "workspace:test",
    run: "run:a",
    conversation: session.session_id,
  });
  const f = fixture({ restored: true });
  await screen.findByText("已排队，等待执行");
  expect(f.request.mock.calls.some((c) => c[1] === "POST")).toBe(false);
  await f.complete();
  await screen.findByText("已核验的后台结果");
});
it("lost acceptance reconnects by idempotency key, never resends the question", async () => {
  const f = fixture({ unknown: true });
  await open(f.user);
  await f.user.type(
    screen.getByRole("textbox", { name: "输入问题" }),
    "测试后台问题",
  );
  await f.user.click(screen.getByRole("button", { name: "发送问题" }));
  await f.user.click(
    await screen.findByRole("button", { name: "重新连接进度" }),
  );
  await screen.findByText("已排队，等待执行");
  expect(f.request.mock.calls.filter((c) => c[1] === "POST")).toHaveLength(1);
  expect(
    f.request.mock.calls.some((c) => c[0].startsWith("/v1/runs/lookup?")),
  ).toBe(true);
  await f.complete();
  await screen.findByText("已核验的后台结果");
});
it("can start a new view while the old run continues without stale UI writes", async () => {
  const f = fixture();
  await open(f.user);
  await f.user.type(
    screen.getByRole("textbox", { name: "输入问题" }),
    "测试后台问题",
  );
  await f.user.click(screen.getByRole("button", { name: "发送问题" }));
  await screen.findByText("已排队，等待执行");
  await f.user.click(screen.getByRole("button", { name: "新对话" }));
  await f.complete();
  expect(screen.queryByText("已核验的后台结果")).not.toBeInTheDocument();
});
