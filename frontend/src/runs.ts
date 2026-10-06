import { ApiError, type Api } from "./api";

export type Run = {
  run_id: string;
  conversation_id: string;
  status: string;
  stage: string;
  sequence: number;
  result_revision: number | null;
  events_url: string;
};

export const terminal = (status: string) =>
  ["succeeded", "cancelled", "failed", "uncertain", "timed_out", "authorization_required"].includes(status);

export const runLabel = (run: Run) => ({
  queued: "已排队，等待执行", running: "正在处理", cancelling: "正在取消…",
  retrieving: "正在检索知识库", generating: "正在生成回答", validating: "正在核验引用",
  persisting: "正在保存结果", recovery_required: "正在接管检查点，恢复执行",
  succeeded: "已完成", cancelled: "已取消", uncertain: "模型结果未知，已停止自动重试",
  failed: "处理未完成，请检查已保存的对话", timed_out: "排队等待超时",
  authorization_required: "执行授权已失效，请重新登录",
}[run.stage] || "正在处理");

type Pending = { key: string; workspace: string; conversation?: string; run?: string };
export function savePending(user: string, workspace: string, value: Pending) {
  // Only opaque identifiers, NEVER tokens, messages, evidence or model output.
  sessionStorage.setItem(`wms.run:${user}:${workspace}`, JSON.stringify(value));
}
export function pendingRun(user: string, workspace: string): Pending | undefined {
  try {
    const value = JSON.parse(sessionStorage.getItem(`wms.run:${user}:${workspace}`) || "null");
    if (value && value.workspace === workspace && typeof value.key === "string" && /^[a-f0-9-]{32,36}$/.test(value.key)) return value;
  } catch { /* Corrupt metadata is ignored, not executed. */ }
  return undefined;
}
export function clearPending(user: string, workspace: string) {
  sessionStorage.removeItem(`wms.run:${user}:${workspace}`);
}

export async function watchRun(api: Api, initial: Run, update: (run: Run) => void, signal: AbortSignal): Promise<Run> {
  let run = initial;
  let sequence = 0; // A new view can always replay persisted progress; no execution is created.
  let failures = 0;
  while (!signal.aborted && !terminal(run.status)) {
    try {
      await api.events(`/v1/runs/${encodeURIComponent(run.run_id)}/events`, `${run.run_id}/${sequence}`, signal, (id, data) => {
        const event = JSON.parse(data);
        if (event.run_id !== run.run_id || !Number.isSafeInteger(event.sequence) || event.sequence <= sequence || id !== `${run.run_id}/${event.sequence}`) return;
        sequence = event.sequence;
        run = { ...run, ...event };
        update(run);
      });
      if (signal.aborted) break;
      run = await api.request<Run>(`/v1/runs/${encodeURIComponent(run.run_id)}`);
      if (signal.aborted) break;
      update(run);
      failures = 0;
    } catch (error) {
      if (signal.aborted) break;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) throw error;
      if (++failures >= 5) throw new Error("连接中断，任务不会重复发送。可重新连接查看进度。");
    }
    if (!terminal(run.status)) await new Promise<void>((resolve) => {
      const timer = setTimeout(done, Math.min(200 * 2 ** failures, 2000));
      function done() { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); }
      signal.addEventListener("abort", done, { once: true });
      if (signal.aborted) done();
    });
  }
  return run;
}
