import { useEffect, useState } from "react";
import type { Api } from "./api";

type Policy = {
  workspace_id: string;
  name: string;
  collections: string[];
  modules: string[];
  sites: string[];
  environments: string[];
};
type Snapshot = {
  workspaces: { workspace_id: string; policy: Policy; revision: number }[];
  memberships: {
    user_id: string;
    workspace_id: string;
    role: string;
    active: boolean;
    revision: number;
  }[];
};
type Audit = {
  action: string;
  resource_id: string;
  reason: string;
  created_at: string;
};
const dimensions = ["collections", "modules", "sites", "environments"] as const;
const labels = {
  collections: "知识集合",
  modules: "业务模块",
  sites: "站点",
  environments: "环境",
};
const empty = {
  workspace_id: "",
  name: "",
  collections: "",
  modules: "",
  sites: "",
  environments: "",
};

export function AuthorizationPanel({
  api,
  accounts,
}: {
  api: Api;
  accounts: { user_id: string; nickname: string }[];
}) {
  const [snapshot, setSnapshot] = useState<Snapshot>({
    workspaces: [],
    memberships: [],
  });
  const [audit, setAudit] = useState<Audit[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [user, setUser] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [role, setRole] = useState("member");
  const [active, setActive] = useState(true);
  const [scope, setScope] = useState(empty);
  const [revision, setRevision] = useState(0);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const load = async () => {
    const [s, a] = await Promise.all([
      api.request<Snapshot>("/v1/admin/authorization"),
      api.request<Audit[]>("/v1/admin/audit"),
    ]);
    setSnapshot(s);
    setAudit(a);
  };
  useEffect(() => {
    void load().catch((e) => setError(e.message));
  }, [api]);
  const member = snapshot.memberships.find(
    (m) => m.user_id === user && m.workspace_id === workspace,
  );
  useEffect(() => {
    setRole(member?.role ?? "member");
    setActive(member?.active ?? true);
    setConfirmed(false);
  }, [member, user, workspace]);
  const save = async (kind: "membership" | "scope") => {
    if (busy || !confirmed || reason.trim().length < 5) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (kind === "membership") {
        await api.request("/v1/admin/memberships", "PUT", {
          user_id: user,
          workspace_id: workspace,
          role,
          enabled: active,
          expected_revision: member?.revision ?? 0,
          reason,
        });
      } else {
        const lists = Object.fromEntries(
          dimensions.map((d) => [
            d,
            scope[d]
              .split(",")
              .map((v) => v.trim())
              .filter(Boolean),
          ]),
        );
        await api.request(
          `/v1/admin/workspaces/${encodeURIComponent(scope.workspace_id)}/scope`,
          "PUT",
          { name: scope.name, ...lists, expected_revision: revision, reason },
        );
        setScope(empty);
        setRevision(0);
      }
      setConfirmed(false);
      setReason("");
      await load();
      setNotice("授权变更已保存并审计");
    } catch (e) {
      setError(
        (e instanceof Error ? e.message : "授权操作失败") +
          " 请刷新授权并重新确认。",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="management-section">
      <h3>工作区授权</h3>
      <p>
        平台管理员设置有限的知识范围和成员角色；不会获得成员的私人聊天。撤销授权会阻止后续访问与执行。
      </p>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <button
        disabled={busy}
        onClick={() => {
          setConfirmed(false);
          void load().catch((e) => setError(e.message));
        }}
      >
        刷新授权
      </button>
      <details>
        <summary>知识范围与工作区</summary>
        <div className="management-list">
          {snapshot.workspaces.map((w) => (
            <button
              disabled={busy}
              key={w.workspace_id}
              onClick={() => {
                setScope({
                  workspace_id: w.workspace_id,
                  name: w.policy.name,
                  ...Object.fromEntries(
                    dimensions.map((d) => [d, w.policy[d].join(", ")]),
                  ),
                } as typeof empty);
                setRevision(w.revision);
                setConfirmed(false);
              }}
            >
              {w.policy.name} · {w.workspace_id}
            </button>
          ))}
        </div>
        <button
          disabled={busy}
          onClick={() => {
            setScope(empty);
            setRevision(0);
            setConfirmed(false);
          }}
        >
          新建工作区
        </button>
        <div className="management-grid">
          <label>
            工作区编号
            <input
              aria-label="授权工作区编号"
              placeholder="workspace:example"
              disabled={revision > 0 || busy}
              value={scope.workspace_id}
              onChange={(e) =>
                setScope({ ...scope, workspace_id: e.target.value })
              }
            />
          </label>
          <label>
            工作区名称
            <input
              aria-label="授权工作区名称"
              value={scope.name}
              disabled={busy}
              onChange={(e) => setScope({ ...scope, name: e.target.value })}
            />
          </label>
          {dimensions.map((d) => (
            <label key={d}>
              {labels[d]}（逗号分隔）
              <input
                aria-label={`授权${labels[d]}`}
                value={scope[d]}
                disabled={busy}
                onChange={(e) => setScope({ ...scope, [d]: e.target.value })}
              />
            </label>
          ))}
        </div>
        <p>
          每项必须是明确允许的值，不接受通配符或
          legacy；缩小范围后旧对话也受新范围约束。
        </p>
        <button
          disabled={
            busy ||
            !confirmed ||
            reason.trim().length < 5 ||
            !scope.workspace_id ||
            !scope.name ||
            dimensions.some((d) => !scope[d].trim())
          }
          onClick={() => void save("scope")}
        >
          保存知识范围
        </button>
      </details>
      <details>
        <summary>成员与角色</summary>
        <div className="management-grid">
          <label>
            成员
            <select
              aria-label="授权成员"
              disabled={busy}
              value={user}
              onChange={(e) => setUser(e.target.value)}
            >
              <option value="">选择账号</option>
              {accounts.map((a) => (
                <option key={a.user_id} value={a.user_id}>
                  {a.nickname || a.user_id}
                </option>
              ))}
            </select>
          </label>
          <label>
            工作区
            <select
              aria-label="授权工作区"
              disabled={busy}
              value={workspace}
              onChange={(e) => setWorkspace(e.target.value)}
            >
              <option value="">选择工作区</option>
              {snapshot.workspaces.map((w) => (
                <option key={w.workspace_id} value={w.workspace_id}>
                  {w.policy.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            角色
            <select
              aria-label="授权角色"
              disabled={busy}
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="member">成员</option>
              <option value="reviewer">审核员</option>
              <option value="workspace_admin">
                工作区管理员（非平台管理员）
              </option>
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              aria-label="启用成员授权"
              disabled={busy}
              checked={active}
              onChange={(e) => setActive(e.target.checked)}
            />
            启用成员授权
          </label>
        </div>
        <p>
          {member
            ? `当前角色 ${member.role} · ${member.active ? "已授权" : "已撤销"} · 版本 ${member.revision}`
            : "尚无授权，将新建成员关系"}
        </p>
        <button
          disabled={
            busy ||
            !confirmed ||
            reason.trim().length < 5 ||
            !user ||
            !workspace
          }
          onClick={() => void save("membership")}
        >
          保存成员授权
        </button>
      </details>
      <label>
        授权操作原因
        <textarea
          aria-label="授权操作原因"
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
        我确认授权范围与角色变更，操作将被审计
      </label>
      <details>
        <summary>最近管理审计（最多 100 条）</summary>
        {audit.map((a, i) => (
          <p key={i}>
            {new Date(a.created_at).toLocaleString("zh-CN", {
              timeZone: "Asia/Shanghai",
            })}{" "}
            · {a.action} · {a.resource_id} · {a.reason}
          </p>
        ))}
      </details>
    </section>
  );
}
