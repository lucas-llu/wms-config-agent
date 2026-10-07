import { useState } from "react";
import type { Api } from "./api";
export type Unresolved = {
  attempt_id: string;
  owner_user_id: string;
  revision: number;
  source: string;
  allocated: number;
};
const initial = {
  input_tokens: "",
  output_tokens: "",
  cached_tokens: "",
  reasoning_tokens: "",
};
const labels = {
  input_tokens: "确认输入 tokens",
  output_tokens: "确认输出 tokens",
  cached_tokens: "缓存命中 tokens",
  reasoning_tokens: "推理 tokens",
};

export function ReconciliationPanel({
  api,
  rows,
  reload,
}: {
  api: Api;
  rows: Unresolved[];
  reload: () => Promise<void>;
}) {
  const [target, setTarget] = useState<Unresolved>();
  const [values, setValues] = useState(initial);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const tokens = Object.fromEntries(
    Object.entries(values).map(([k, v]) => [k, Number(v)]),
  );
  const valid =
    Object.values(values).every(
      (v) => v !== "" && /^\d+$/.test(v) && Number(v) <= 1000000000,
    ) &&
    tokens.cached_tokens <= tokens.input_tokens &&
    tokens.reasoning_tokens <= tokens.output_tokens;
  const save = async () => {
    if (!target || busy || !valid || !confirmed || reason.trim().length < 5)
      return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.request(
        `/v1/admin/usage/${encodeURIComponent(target.attempt_id)}/reconcile`,
        "POST",
        { ...tokens, expected_revision: target.revision, reason },
      );
      setTarget(undefined);
      setConfirmed(false);
      await reload();
      setNotice("对账已完成并审计，额度已重新结算");
    } catch (e) {
      setError(
        (e instanceof Error ? e.message : "对账失败") +
          " 请刷新待对账记录，不要重复确认。",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="management-section">
      <h3>待对账</h3>
      <p>
        只确认供应商已结束的未知/估算调用。请核对供应商记录；缓存属于输入、推理属于输出，不额外加到总量。未定价仍不会显示为确定费用。
      </p>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {!rows.length && <p>暂无待对账记录</p>}
      <div className="management-list">
        {rows.map((r) => (
          <button
            disabled={busy}
            key={r.attempt_id}
            onClick={() => {
              setTarget(r);
              setValues(initial);
              setReason("");
              setConfirmed(false);
              setError("");
              setNotice("");
            }}
          >
            {r.attempt_id} · {r.source} · 调用预算 {r.allocated}
          </button>
        ))}
      </div>
      {target && (
        <fieldset>
          <legend>核对调用 {target.attempt_id}</legend>
          <p>
            账号 {target.owner_user_id} · 版本 {target.revision}
          </p>
          <div className="management-grid">
            {(Object.keys(initial) as (keyof typeof initial)[]).map((k) => (
              <label key={k}>
                {labels[k]}
                <input
                  aria-label={labels[k]}
                  disabled={busy}
                  type="number"
                  min="0"
                  max="1000000000"
                  step="1"
                  value={values[k]}
                  onChange={(e) =>
                    setValues({ ...values, [k]: e.target.value })
                  }
                />
              </label>
            ))}
          </div>
          <p>
            {valid
              ? `确认总量 ${tokens.input_tokens + tokens.output_tokens} tokens`
              : "请填写全部非负整数；缓存与推理不能超过各自所属总量"}
          </p>
          <label>
            对账依据与原因
            <textarea
              aria-label="对账依据与原因"
              maxLength={500}
              disabled={busy}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <label>
            <input
              type="checkbox"
              disabled={busy}
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            我已核对供应商记录，确认结算且将被审计
          </label>
          <button
            disabled={busy || !valid || !confirmed || reason.trim().length < 5}
            onClick={() => void save()}
          >
            确认对账
          </button>
          <button disabled={busy} onClick={() => setTarget(undefined)}>
            取消对账
          </button>
        </fieldset>
      )}
    </section>
  );
}
