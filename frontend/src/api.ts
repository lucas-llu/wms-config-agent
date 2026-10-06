import type { Auth } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code?: string,
  ) {
    super(
      code === "conversation_busy"
        ? "该对话正在处理，请等待或重新打开查看进度。"
        : code === "user_queue_full"
          ? "你的待处理请求已满，请等待已有任务完成。"
          : code === "idempotency_conflict"
            ? "发送标识已用于不同内容，请重新发起操作。"
            : status === 401
              ? "登录已过期，请重新登录。"
              : status === 403
                ? "当前账号没有此操作权限，或授权已失效。"
                : status === 404
                  ? "对话不存在或你无权访问。"
                  : status === 409
                    ? "对话已更新，请重新打开后再操作。"
                    : status === 422
                      ? "请检查输入内容。"
                      : "服务暂时不可用，请稍后查看已保存的结果。",
    );
  }
}

export class Api {
  constructor(private auth: Auth) {}
  async request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    if (
      !path.startsWith("/v1/") ||
      path.startsWith("//") ||
      path.includes("://")
    )
      throw new Error("Invalid API path");
    const response = await fetch(path, {
      method,
      credentials: "omit",
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${await this.auth.token()}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => undefined);
      const code =
        typeof body?.detail === "string" &&
        [
          "conversation_busy",
          "user_queue_full",
          "idempotency_conflict",
        ].includes(body.detail)
          ? body.detail
          : undefined;
      throw new ApiError(response.status, code);
    }
    return response.status === 204
      ? (undefined as T)
      : (response.json() as Promise<T>);
  }
  async events(
    path: string,
    cursor: string,
    signal: AbortSignal,
    receive: (id: string, data: string) => void,
  ): Promise<void> {
    if (
      !path.startsWith("/v1/runs/") ||
      path.includes("://") ||
      path.startsWith("//")
    )
      throw new Error("Invalid event path");
    const response = await fetch(path, {
      cache: "no-store",
      credentials: "omit",
      signal,
      headers: {
        Authorization: `Bearer ${await this.auth.token()}`,
        "Last-Event-ID": cursor,
        Accept: "text/event-stream",
      },
    });
    if (!response.ok) throw new ApiError(response.status);
    if (
      !response.headers.get("content-type")?.startsWith("text/event-stream") ||
      !response.body
    )
      throw new Error("Invalid event stream");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (!signal.aborted) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        if (buffer.length > 65536)
          throw new Error("Event stream exceeded limit");
        let end: number;
        while ((end = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const lines = block.split("\n");
          const id = lines.find((line) => line.startsWith("id: "))?.slice(4);
          const data = lines
            .filter((line) => line.startsWith("data: "))
            .map((line) => line.slice(6))
            .join("\n");
          if (id && data && !signal.aborted) receive(id, data);
        }
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  async download(path: string): Promise<void> {
    if (!path.startsWith("/v1/conversations/") || path.includes("://"))
      throw new Error("Invalid download path");
    const response = await fetch(path, {
      credentials: "omit",
      cache: "no-store",
      headers: { Authorization: `Bearer ${await this.auth.token()}` },
    });
    if (!response.ok) throw new ApiError(response.status);
    const href = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = "wms-solution";
    anchor.click();
    URL.revokeObjectURL(href);
  }
  async image(path: string): Promise<string> {
    if (!path.startsWith("/v1/conversations/") || path.includes("://"))
      throw new Error("Invalid image path");
    const response = await fetch(path, {
      credentials: "omit",
      cache: "no-store",
      headers: { Authorization: `Bearer ${await this.auth.token()}` },
    });
    if (!response.ok) throw new ApiError(response.status);
    const blob = await response.blob();
    if (
      !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(
        blob.type,
      )
    )
      throw new Error("图片类型不受支持。");
    return URL.createObjectURL(blob);
  }
}

export const resource = (id: string) =>
  `/v1/conversations/${encodeURIComponent(id)}`;
