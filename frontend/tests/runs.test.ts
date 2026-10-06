import { afterEach, expect, it, vi } from "vitest";
import { Api, ApiError } from "../src/api";
import {
  clearPending,
  pendingRun,
  runLabel,
  savePending,
  terminal,
  watchRun,
  type Run,
} from "../src/runs";
import { fakeAuth } from "./fixtures";

const run: Run = {
  run_id: "run:a",
  conversation_id: "session:a",
  status: "queued",
  stage: "queued",
  sequence: 1,
  result_revision: null,
  events_url: "/v1/runs/run:a/events",
};
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

it("stores only owner-scoped opaque reconnect metadata", () => {
  const value = { key: "a".repeat(32), workspace: "ws", run: "run:a" };
  savePending("a", "ws", value);
  expect(pendingRun("a", "ws")).toEqual(value);
  expect(pendingRun("b", "ws")).toBeUndefined();
  expect(pendingRun("a", "other")).toBeUndefined();
  clearPending("a", "ws");
  expect(pendingRun("a", "ws")).toBeUndefined();
  sessionStorage.setItem("wms.run:a:ws", "bad json");
  expect(pendingRun("a", "ws")).toBeUndefined();
  sessionStorage.setItem(
    "wms.run:a:ws",
    JSON.stringify({ key: "bad", workspace: "ws" }),
  );
  expect(pendingRun("a", "ws")).toBeUndefined();
});
it("has explicit terminal and uncertainty states", () => {
  expect(terminal("recovery_required")).toBe(false);
  expect(runLabel({ ...run, stage: "uncertain" })).toContain("未知");
  expect(runLabel({ ...run, stage: "unknown" })).toBe("正在处理");
});
it("replays only matching monotonically sequenced events and never submits a task", async () => {
  const finished = {
    ...run,
    status: "succeeded",
    stage: "succeeded",
    sequence: 3,
    result_revision: 2,
  };
  const update = vi.fn();
  const events = vi.fn(async (_path, _cursor, _signal, receive) => {
    receive("run:a/1", JSON.stringify({ ...run, sequence: 1 }));
    receive(
      "other/2",
      JSON.stringify({ ...run, run_id: "other", sequence: 2 }),
    );
    receive("run:a/1", JSON.stringify(run));
    receive("wrong", JSON.stringify({ ...run, sequence: 2 }));
    receive("run:a/3", JSON.stringify(finished));
  });
  const request = vi.fn(async () => finished);
  const result = await watchRun(
    { events, request } as unknown as Api,
    run,
    update,
    new AbortController().signal,
  );
  expect(result.status).toBe("succeeded");
  expect(update).toHaveBeenCalledTimes(3);
  expect(events).toHaveBeenCalledWith(
    "/v1/runs/run%3Aa/events",
    "run:a/0",
    expect.anything(),
    expect.anything(),
  );
  expect(request.mock.calls).toEqual([["/v1/runs/run%3Aa"]]);
});
it("reconnects with the last cursor after routine stream close", async () => {
  vi.useFakeTimers();
  let count = 0;
  const events = vi.fn(async (_path, _cursor, _signal, receive) => {
    receive(`run:a/${++count}`, JSON.stringify({ ...run, sequence: count }));
  });
  const request = vi
    .fn()
    .mockResolvedValueOnce(run)
    .mockResolvedValue({ ...run, status: "cancelled" });
  const promise = watchRun(
    { events, request } as unknown as Api,
    run,
    vi.fn(),
    new AbortController().signal,
  );
  await vi.advanceTimersByTimeAsync(1000);
  await promise;
  expect(events.mock.calls[1][1]).toBe("run:a/1");
  expect(request).toHaveBeenCalledTimes(2);
});
it("bounds network retries without repeating POST and aborts authorization failures", async () => {
  vi.useFakeTimers();
  const api = {
    events: vi.fn().mockRejectedValue(new Error("offline")),
    request: vi.fn(),
  } as unknown as Api;
  const promise = expect(
    watchRun(api, run, vi.fn(), new AbortController().signal),
  ).rejects.toThrow("不会重复发送");
  await vi.advanceTimersByTimeAsync(10000);
  await promise;
  expect(api.events).toHaveBeenCalledTimes(5);
  expect(api.request).not.toHaveBeenCalled();
  vi.useRealTimers();
  for (const status of [401, 403, 404]) {
    const denied = {
      events: vi.fn().mockRejectedValue(new ApiError(status)),
      request: vi.fn(),
    } as unknown as Api;
    await expect(
      watchRun(denied, run, vi.fn(), new AbortController().signal),
    ).rejects.toMatchObject({ status });
    expect(denied.events).toHaveBeenCalledTimes(1);
  }
});
it("abort cancels a pending reconnect wait and stale updates", async () => {
  const controller = new AbortController();
  const api = {
    events: vi.fn(async () => controller.abort()),
    request: vi.fn(),
  } as unknown as Api;
  expect(await watchRun(api, run, vi.fn(), controller.signal)).toEqual(run);
  expect(api.request).not.toHaveBeenCalled();
  const stopped = new AbortController();
  stopped.abort();
  expect(await watchRun(api, run, vi.fn(), stopped.signal)).toEqual(run);
});
it("reads authenticated bounded SSE frames including split Unicode bytes", async () => {
  const bytes = new TextEncoder().encode(
    'id: run:a/1\nevent: progress\ndata: {"stage":"生成"}\n\n: heartbeat\n\n',
  );
  const fetcher = vi.fn(
    async (_url: RequestInfo | URL, _options?: RequestInit) =>
      new Response(
        new ReadableStream({
          start(c) {
            c.enqueue(bytes.slice(0, 58));
            c.enqueue(bytes.slice(58));
            c.close();
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
  );
  vi.stubGlobal("fetch", fetcher);
  const receive = vi.fn();
  const api = new Api(fakeAuth());
  await api.events(
    "/v1/runs/run:a/events",
    "run:a/0",
    new AbortController().signal,
    receive,
  );
  expect(receive).toHaveBeenCalledWith("run:a/1", '{"stage":"生成"}');
  expect(fetcher.mock.calls[0][1]).toMatchObject({
    credentials: "omit",
    cache: "no-store",
    headers: {
      Authorization: "Bearer synthetic-token",
      "Last-Event-ID": "run:a/0",
    },
  });
  await expect(
    api.events(
      "https://evil.invalid",
      "",
      new AbortController().signal,
      receive,
    ),
  ).rejects.toThrow("Invalid");
});
it("rejects unauthorized, non-SSE and oversized streams", async () => {
  const api = new Api(fakeAuth());
  const receive = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("private", { status: 401 })),
  );
  await expect(
    api.events("/v1/runs/a/events", "", new AbortController().signal, receive),
  ).rejects.toMatchObject({ status: 401 });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("not SSE")),
  );
  await expect(
    api.events("/v1/runs/a/events", "", new AbortController().signal, receive),
  ).rejects.toThrow("Invalid event");
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("x".repeat(65537), {
          headers: { "Content-Type": "text/event-stream" },
        }),
    ),
  );
  await expect(
    api.events("/v1/runs/a/events", "", new AbortController().signal, receive),
  ).rejects.toThrow("limit");
});
