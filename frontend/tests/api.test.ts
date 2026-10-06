import { afterEach, expect, it, vi } from "vitest";
import { Api, ApiError, resource } from "../src/api";
import { fakeAuth } from "./fixtures";
afterEach(() => vi.unstubAllGlobals());
it("maps only safe run conflict codes, never arbitrary backend error text", async () => {
  const api = new Api(fakeAuth());
  for (const [code, text] of [
    ["conversation_busy", "正在处理"],
    ["user_queue_full", "已满"],
    ["idempotency_conflict", "不同内容"],
  ]) {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: code }), { status: 409 }),
      ),
    );
    await expect(
      api.request("/v1/conversations/s/runs", "POST", {}),
    ).rejects.toThrow(text);
  }
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "PRIVATE DETAILS" }), {
          status: 503,
        }),
    ),
  );
  await expect(api.request("/v1/me")).rejects.toThrow("服务暂时不可用");
});
it("gets a fresh in-memory token; never sends it to a foreign URL or retries writes", async () => {
  const fetcher = vi.fn(
    async (_url: RequestInfo | URL, _options?: RequestInit) =>
      new Response("{}"),
  );
  vi.stubGlobal("fetch", fetcher);
  const api = new Api(fakeAuth());
  await api.request("/v1/me");
  await api.request("/v1/conversations", "POST", { goal: "Synthetic" });
  expect(fetcher.mock.calls[0][1]).toMatchObject({
    credentials: "omit",
    cache: "no-store",
    headers: { Authorization: "Bearer synthetic-token" },
  });
  await expect(api.request("https://evil.invalid")).rejects.toThrow("Invalid");
  await expect(api.request("//evil")).rejects.toThrow("Invalid");
  expect(fetcher).toHaveBeenCalledTimes(2);
});
it.each([401, 403, 404, 409, 422, 503])(
  "returns opaque localized error %i without leaking provider bodies",
  async (status) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("PRIVATE DETAILS", { status })),
    );
    const api = new Api(fakeAuth());
    await expect(api.request("/v1/me")).rejects.toMatchObject({ status });
    expect(new ApiError(status).message).not.toContain("PRIVATE");
  },
);
it("supports empty responses, protected downloads and URL cleanup", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(null, { status: 204 })),
  );
  const api = new Api(fakeAuth());
  expect(await api.request("/v1/me", "DELETE")).toBeUndefined();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("synthetic")),
  );
  URL.createObjectURL = vi.fn(() => "blob:synthetic");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  await api.download("/v1/conversations/s/exports/e/download");
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:synthetic");
  await expect(api.download("https://evil.invalid")).rejects.toThrow("Invalid");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(null, { status: 404 })),
  );
  await expect(api.download("/v1/conversations/s/file")).rejects.toMatchObject({
    status: 404,
  });
});
it("loads only authenticated raster image blobs", async () => {
  const api = new Api(fakeAuth());
  URL.createObjectURL = vi.fn(() => "blob:synthetic");
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("bytes", { headers: { "Content-Type": "image/png" } }),
    ),
  );
  expect(await api.image("/v1/conversations/s/image")).toBe("blob:synthetic");
  await expect(api.image("https://evil.invalid")).rejects.toThrow("Invalid");
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("<script/>", { headers: { "Content-Type": "text/html" } }),
    ),
  );
  await expect(api.image("/v1/conversations/s/image")).rejects.toThrow(
    "不受支持",
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(null, { status: 401 })),
  );
  await expect(api.image("/v1/conversations/s/image")).rejects.toMatchObject({
    status: 401,
  });
});
it("encodes opaque conversation IDs as one path segment", () => {
  expect(resource("a/b?c")).toBe("/v1/conversations/a%2Fb%3Fc");
});
