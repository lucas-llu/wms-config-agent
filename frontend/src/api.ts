import type { Auth } from "./types";

export class ApiError extends Error {
  constructor(public status: number) {
    super(
      status === 401
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
    if (!response.ok) throw new ApiError(response.status);
    return response.status === 204
      ? (undefined as T)
      : (response.json() as Promise<T>);
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
