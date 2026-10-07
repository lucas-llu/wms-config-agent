import { useEffect, useRef, useState } from "react";
import type { Api } from "./api";
import { AuthorizationPanel } from "./AuthorizationPanel";
import { ReconciliationPanel, type Unresolved } from "./ReconciliationPanel";

type Summary = {
  period: string;
  timezone: string;
  today_requests: number;
  month_requests: number;
  quota: number;
  used: number;
  reserved: number;
  remaining: number;
  unknown_attempts: number;
  estimated_attempts: number;
  input_tokens: number;
  output_tokens: number;
  costs: { currency: string; amount: string }[];
};
type Attempt = {
  attempt_id: string;
  model: string;
  strategy: string;
  source: string;
  status: string;
  total_tokens: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost: string | null;
  currency: string | null;
  price_version: string | null;
  started_at: string;
  revision: number;
};
type Account = {
  user_id: string;
  nickname: string;
  status: string;
  monthly_tokens: number;
  quota_revision: number;
};
export function UsagePanel({
  api,
  admin = false,
}: {
  api: Api;
  admin?: boolean;
}) {
  const [summary, setSummary] = useState<Summary>();
  const [rows, setRows] = useState<Attempt[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [aggregate, setAggregate] = useState<
    {
      known_tokens: number;
      model: string;
      strategy: string;
      unknown_attempts: number;
    }[]
  >([]);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [model, setModel] = useState("");
  const [conversation, setConversation] = useState("");
  const [target, setTarget] = useState<Account>();
  const [quota, setQuota] = useState(0);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unresolved, setUnresolved] = useState<Unresolved[]>([]);
  const generation = useRef(0);
  const load = async () => {
    const current = ++generation.current;
    const params = new URLSearchParams();
    if (filter) params.set("strategy", filter);
    if (model) params.set("model", model);
    if (conversation) params.set("conversation_id", conversation);
    const [s, r] = await Promise.all([
      api.request<Summary>("/v1/me/usage"),
      api.request<Attempt[]>(`/v1/me/usage/attempts?${params}`),
    ]);
    if (admin) {
      const [a, g, u] = await Promise.all([
        api.request<Account[]>("/v1/admin/accounts"),
        api.request<typeof aggregate>("/v1/admin/usage"),
        api.request<typeof unresolved>("/v1/admin/usage/unresolved"),
      ]);
      if (current !== generation.current) return;
      setAccounts(a);
      setAggregate(g);
      setUnresolved(u);
    }
    if (current !== generation.current) return;
    setSummary(s);
    setRows(r);
  };
  useEffect(() => {
    let alive = true;
    void load().catch((e) => {
      if (alive) setError(e.message);
    });
    return () => {
      alive = false;
      generation.current++;
    };
  }, [api, admin, filter, model, conversation]);
  const change = async (status?: boolean) => {
    if (!target || !confirmed || reason.trim().length < 5) return;
    setBusy(true);
    setError("");
    try {
      await api.request(
        `/v1/admin/accounts/${target.user_id}/${status === undefined ? "quota" : "status"}`,
        "PUT",
        status === undefined
          ? {
              monthly_tokens: quota,
              expected_revision: target.quota_revision,
              reason,
            }
          : { enabled: status, reason },
      );
      setTarget(undefined);
      setConfirmed(false);
      setReason("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "管理操作失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="account-panel">
      <h2>{admin ? "额度与账号管理" : "我的用量"}</h2>
      <p>
        账期与时间按 Asia/Shanghai
        展示；费用是估算，不是供应商账单。未知消耗保留额度等待对账。
      </p>
      {error && <p role="alert">{error}</p>}
      {summary && (
        <div className="profile-line">
          <p>
            今日请求 {summary.today_requests} · 本月请求{" "}
            {summary.month_requests}
          </p>
          <p>
            已用 {summary.used} · 预留 {summary.reserved} · 剩余{" "}
            {summary.remaining} / {summary.quota} tokens
          </p>
          <p>
            输入 {summary.input_tokens} · 输出 {summary.output_tokens} · 未知{" "}
            {summary.unknown_attempts} · 平台估算 {summary.estimated_attempts}
          </p>
          {summary.costs.length ? (
            summary.costs.map((c) => (
              <p key={c.currency}>
                估算费用 {c.amount} {c.currency}
              </p>
            ))
          ) : (
            <p>费用未知或价格未配置，不显示为 0</p>
          )}
        </div>
      )}
      <label>
        回答策略
        <select
          aria-label="用量策略"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        >
          <option value="">全部</option>
          <option value="standard">默认</option>
          <option value="review">旁路</option>
        </select>
      </label>
      <label>
        模型
        <input
          aria-label="用量模型"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        />
      </label>
      <label>
        对话编号
        <input
          aria-label="用量对话"
          value={conversation}
          onChange={(e) => setConversation(e.target.value)}
        />
      </label>
      <button onClick={() => void load().catch((e) => setError(e.message))}>
        刷新用量
      </button>
      <div
        className="usage-table"
        role="region"
        aria-label="调用明细表"
        tabIndex={0}
      >
        <table>
          <caption>逐次调用明细（最多 100 条）</caption>
          <thead>
            <tr>
              <th>时间</th>
              <th>模型/策略</th>
              <th>来源</th>
              <th>tokens</th>
              <th>估算费用</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.attempt_id}>
                <td>
                  {new Date(r.started_at).toLocaleString("zh-CN", {
                    timeZone: "Asia/Shanghai",
                  })}
                </td>
                <td>
                  {r.model} / {r.strategy}
                </td>
                <td>
                  {
                    (
                      {
                        provider: "供应商返回",
                        estimated: "平台估算",
                        unknown: "未知/待对账",
                        rejected: "已拒绝",
                      } as Record<string, string>
                    )[r.source]
                  }
                </td>
                <td>{r.total_tokens ?? "未知"}</td>
                <td>
                  {r.estimated_cost === null
                    ? "未定价/未知"
                    : `${r.estimated_cost} ${r.currency}（${r.price_version}）`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {admin && (
        <>
          <h3>平台账号（不提供私人聊天正文）</h3>
          {accounts.map((a) => (
            <button
              key={a.user_id}
              aria-label={`管理账号 ${a.user_id}`}
              onClick={() => {
                setTarget(a);
                setQuota(a.monthly_tokens);
                setConfirmed(false);
                setReason("");
              }}
            >
              {a.nickname || a.user_id} · {a.status}
            </button>
          ))}
          <h3>聚合用量</h3>
          {aggregate.map((a, i) => (
            <p key={i}>
              {a.model} / {a.strategy} · {a.known_tokens} tokens · 未知{" "}
              {a.unknown_attempts}
            </p>
          ))}
          <ReconciliationPanel api={api} rows={unresolved} reload={load} />
          <AuthorizationPanel api={api} accounts={accounts} />
          {target && (
            <fieldset>
              <legend>修改 {target.nickname || target.user_id}</legend>
              <label>
                月 token 额度
                <input
                  aria-label="月 token 额度"
                  type="number"
                  min="0"
                  max="1000000000"
                  value={quota}
                  onChange={(e) => setQuota(Number(e.target.value))}
                />
              </label>
              <label>
                操作原因
                <textarea
                  aria-label="管理操作原因"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                />
                我确认此操作影响账号/额度且将被审计
              </label>
              <button
                disabled={busy || !confirmed || reason.trim().length < 5}
                onClick={() => void change()}
              >
                保存额度
              </button>
              <button
                disabled={busy || !confirmed || reason.trim().length < 5}
                onClick={() => void change(false)}
              >
                停用账号
              </button>
              <button
                disabled={busy || !confirmed || reason.trim().length < 5}
                onClick={() => void change(true)}
              >
                启用账号
              </button>
              <button onClick={() => setTarget(undefined)}>取消修改</button>
            </fieldset>
          )}
        </>
      )}
    </section>
  );
}
