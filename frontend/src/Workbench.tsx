import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import Markdown from "react-markdown";
import { Api, ApiError, resource } from "./api";
import { clearPending, pendingRun, runLabel, savePending, terminal, watchRun, type Run } from "./runs";
import { canReview, canSend, cleanEvidence, sidebarWidth } from "./view";
import type {
  Auth,
  Citation,
  Device,
  Export,
  Profile,
  Session,
  Turn,
  Workbench,
} from "./types";

function Icon({ name }: { name: string }) {
  const paths: Record<string, string> = {
    plus: "M12 5v14M5 12h14",
    search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    arrow: "M12 19V5M5 12l7-7 7 7",
    chat: "M21 15a4 4 0 0 1-4 4H7l-5 3V6a4 4 0 0 1 4-4h11a4 4 0 0 1 4 4z",
    more: "M5 12h.01M12 12h.01M19 12h.01",
    user: "M20 21a8 8 0 0 0-16 0M12 3a5 5 0 1 0 0 10 5 5 0 0 0 0-10",
    close: "M6 6l12 12M6 18L18 6",
    copy: "M9 9h12v12H9zM15 5V3H3v12h2",
    folder: "M3 7h7l2-3h9v17H3z",
    archive: "M3 3h18v5H3zM5 8v13h14V8M10 12h4",
    trash: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7",
  };
  return (
    <svg
      width="19"
      height="19"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || paths.chat} />
    </svg>
  );
}

export function Welcome({ auth }: { auth: Auth }) {
  return (
    <main className="welcome">
      <div className="brandmark">W</div>
      <span className="eyebrow">YOUR WMS CONFIGURATION COPILOT</span>
      <h1>让复杂配置，变成清晰对话。</h1>
      <p>
        从知识查询到配置方案，每一步都有可追溯的依据。
        <br />
        登录后进入你的私人工作台。
      </p>
      <div className="welcome-actions">
        <button className="primary" onClick={() => void auth.login()}>
          登录工作台
        </button>
        <button onClick={() => void auth.register()}>创建账号</button>
      </div>
      <button className="text-button" onClick={() => void auth.recover()}>
        找回密码 · 前往身份服务选择“忘记密码”
      </button>
      <small>注册需要验证邮箱。新账号不会自动获得知识库权限。</small>
    </main>
  );
}

function PrivateImage({ api, path }: { api: Api; path: string }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    let allocated = "";
    api
      .image(path)
      .then((value) => {
        allocated = value;
        if (active) setUrl(value);
        else URL.revokeObjectURL(value);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
      if (allocated) URL.revokeObjectURL(allocated);
    };
  }, [api, path]);
  return failed ? (
    <small>图片暂时无法显示，原文仍可查看。</small>
  ) : url ? (
    <img className="evidence-image" src={url} alt="文档引用插图" />
  ) : (
    <small>正在加载插图…</small>
  );
}

export function Evidence({
  citations,
  legacy = "",
  api,
}: {
  citations: Citation[];
  legacy?: string;
  api?: Api;
}) {
  const [openSources, setOpenSources] = useState<Record<number, boolean>>({});
  if (!citations.length && !legacy) return null;
  return (
    <details className="evidence">
      <summary>
        引用证据 <span>{citations.length || "历史"}</span>
      </summary>
      <div className="evidence-list">
        {citations.map((c, index) => (
          <details
            className="evidence-source"
            key={c.evidence_id || index}
            onToggle={(e) => {
              const opened = e.currentTarget.open;
              setOpenSources((s) => ({ ...s, [index]: opened }));
            }}
          >
            <summary>
              <span className="source-index">{index + 1}</span>
              <span>
                {c.source}
                <small>
                  {c.page_start
                    ? `第 ${c.page_start}${c.page_end && c.page_end !== c.page_start ? `–${c.page_end}` : ""} 页`
                    : "文档片段"}
                </small>
              </span>
            </summary>
            <p>{cleanEvidence(c.excerpt)}</p>
            {api &&
              openSources[index] &&
              c.images?.map((path) => (
                <PrivateImage key={path} api={api} path={path} />
              ))}
          </details>
        ))}
        {legacy && <p>{cleanEvidence(legacy)}</p>}
      </div>
    </details>
  );
}

export function WorkbenchApp({ api, auth }: { api: Api; auth: Auth }) {
  const [profile, setProfile] = useState<Profile>();
  const [workspace, setWorkspace] = useState("");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [data, setData] = useState<Workbench>();
  const [query, setQuery] = useState("");
  const [listMode, setListMode] = useState<"active" | "archived" | "trash">(
    "active",
  );
  const [tab, setTab] = useState<"chat" | "workspace">("chat");
  const [panel, setPanel] = useState<"profile" | "devices">();
  const [devices, setDevices] = useState<Device[]>([]);
  const [nickname, setNickname] = useState("");
  const [message, setMessage] = useState("");
  const [review, setReview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState("");
  const [activeRun, setActiveRun] = useState<Run>();
  const [disconnected, setDisconnected] = useState(false);
  const runStream = useRef<AbortController | undefined>(undefined);
  const [error, setError] = useState("");
  const [locked, setLocked] = useState(false);
  const [action, setAction] = useState<{
    type: "rename" | "delete" | "purge" | "empty" | "devices";
    session?: Session;
    snapshot?: { count: number; fingerprint: string };
  }>();
  const [title, setTitle] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [trashSnapshot, setTrashSnapshot] = useState<{
    count: number;
    fingerprint: string;
  }>();
  const [decision, setDecision] = useState("approve");
  const [comment, setComment] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [width, setWidth] = useState(288);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const requestLock = useRef(false);
  const listEpoch = useRef(0);
  const viewEpoch = useRef(0);
  const latest = useRef<HTMLDivElement>(null);
  const messages = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (action && !dialog.current?.open) dialog.current?.showModal();
  }, [action]);
  const handleError = (e: unknown) => {
    setError(e instanceof Error ? e.message : "操作未完成，请稍后再试。");
    if (e instanceof ApiError && [401, 403].includes(e.status)) {
      runStream.current?.abort();
      if (profile) clearPending(profile.user_id, workspace);
      setPending(""); setActiveRun(undefined); setBusy(false);
      setLocked(true);
      setData(undefined);
      setSessions([]);
    }
  };
  useEffect(() => () => runStream.current?.abort(), []);
  const follow = (run: Run, epoch: number) => {
    runStream.current?.abort();
    const controller = new AbortController(); runStream.current = controller;
    setActiveRun(run); setDisconnected(false); setBusy(!terminal(run.status));
    requestLock.current = !terminal(run.status);
    void watchRun(api, run, value => { if (epoch === viewEpoch.current && !controller.signal.aborted) setActiveRun(value); }, controller.signal)
      .then(async result => {
        if (controller.signal.aborted || epoch !== viewEpoch.current) return;
        if (terminal(result.status)) {
          setBusy(false); requestLock.current = false; setPending(""); setActiveRun(undefined);
          if (profile) clearPending(profile.user_id, workspace);
          if (result.status !== "succeeded") setError(runLabel(result));
          await open(result.conversation_id);
          if (result.status !== "succeeded") setError(runLabel(result));
          await list();
        }
      }).catch(error => {
        if (controller.signal.aborted || epoch !== viewEpoch.current) return;
        handleError(error); setDisconnected(true);
      });
  };
  const reconnect = async () => {
    if (!profile) return;
    try {
      const saved = pendingRun(profile.user_id, workspace);
      const params = new URLSearchParams({ workspace_id: workspace, idempotency_key: saved?.key || "" });
      if (saved?.conversation) params.set("conversation_id", saved.conversation);
      const run = activeRun || await api.request<Run>(`/v1/runs/lookup?${params}`);
      setError(""); setDisconnected(false);
      await open(run.conversation_id, undefined, run);
    } catch (error) { handleError(error); }
  };
  useEffect(() => {
    runStream.current?.abort(); viewEpoch.current++;
    setData(undefined); setPending(""); setActiveRun(undefined); setBusy(false);
    requestLock.current = false;
  }, [workspace]);
  const list = async () => {
    const epoch = ++listEpoch.current;
    const path = listMode === "trash" ? "/v1/trash" : "/v1/conversations";
    const parameters = new URLSearchParams({
      workspace_id: workspace,
      q: query,
      archived: String(listMode === "archived"),
    });
    const rows = await api.request<Session[]>(`${path}?${parameters}`);
    const snapshot =
      listMode === "trash"
        ? await api.request<{ count: number; fingerprint: string }>(
            `/v1/trash/snapshot?${new URLSearchParams({ workspace_id: workspace })}`,
          )
        : undefined;
    if (epoch === listEpoch.current) {
      setSessions(rows);
      setTrashSnapshot(snapshot);
    }
  };
  useEffect(() => {
    let active = true;
    api
      .request<Profile>("/v1/me")
      .then((p) => {
        if (active) {
          setProfile(p);
          setNickname(p.nickname);
          setWorkspace(p.workspaces[0]?.workspace_id || "");
        }
      })
      .catch(handleError);
    return () => {
      active = false;
    };
  }, [api]);
  useEffect(() => {
    if (!profile) return;
    let active = true;
    const check = () => {
      void api
        .request<Profile>("/v1/me")
        .then((p) => {
          if (!active) return;
          if (p.user_id !== profile.user_id) {
            setLocked(true);
            setData(undefined);
            setSessions([]);
            return;
          }
          setProfile(p);
          if (
            workspace &&
            !p.workspaces.some((w) => w.workspace_id === workspace)
          ) {
            setData(undefined);
            setSessions([]);
            setWorkspace("");
            setError("工作区授权已变更，请重新选择工作区。");
          }
        })
        .catch(handleError);
    };
    const interval = setInterval(check, 30000);
    window.addEventListener("focus", check);
    return () => {
      active = false;
      clearInterval(interval);
      window.removeEventListener("focus", check);
    };
  }, [api, profile?.user_id, workspace]);
  useEffect(() => {
    setSelected([]);
    if (workspace) {
      void list().catch(handleError);
    }
    return () => {
      listEpoch.current++;
    };
  }, [workspace, query, listMode]);
  useEffect(() => {
    if (!profile?.durable_runs || !workspace) return;
    const saved = pendingRun(profile.user_id, workspace);
    if (!saved) return;
    let alive = true;
    const parameters = new URLSearchParams({ workspace_id: workspace, idempotency_key: saved.key });
    if (saved.conversation) parameters.set("conversation_id", saved.conversation);
    void api.request<Run>(saved.run ? `/v1/runs/${encodeURIComponent(saved.run)}` : `/v1/runs/lookup?${parameters}`)
      .then(run => { if (alive) return open(run.conversation_id, undefined, run); })
      .catch(error => { if (alive) handleError(error); });
    return () => { alive = false; runStream.current?.abort(); };
  }, [profile?.user_id, profile?.durable_runs, workspace]);
  useEffect(() => {
    if (data?.turns.length || pending)
      latest.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
    else if (messages.current) messages.current.scrollTop = 0;
  }, [data?.turns.length, pending, busy]);
  const open = async (id: string, revision?: number, accepted?: Run) => {
    runStream.current?.abort();
    setSidebarOpen(false);
    const epoch = ++viewEpoch.current;
    setError("");
    const result = await api.request<Workbench>(
      `${resource(id)}/workbench${revision ? `?revision=${revision}` : ""}`,
    );
    if (epoch === viewEpoch.current) {
      setData(result);
      setPending("");
      setTab("chat");
      setPanel(undefined);
      if (profile?.durable_runs && !revision) {
        const run = accepted || await api.request<Run | null>(`${resource(id)}/runs/active`);
        if (epoch !== viewEpoch.current) return;
        if (run && !terminal(run.status)) {
          const question = await api.request<{ message: string }>(`/v1/runs/${encodeURIComponent(run.run_id)}/request`);
          if (epoch !== viewEpoch.current) return;
          setPending(question.message); follow(run, epoch);
        } else {
          setBusy(false); requestLock.current = false; setActiveRun(undefined);
          if (accepted && terminal(accepted.status)) {
            clearPending(profile.user_id, workspace);
            if (accepted.status !== "succeeded") setError(runLabel(accepted));
          }
        }
      }
    }
  };
  const newChat = () => {
    runStream.current?.abort();
    if (profile?.durable_runs) { setBusy(false); requestLock.current = false; setActiveRun(undefined); }
    setSidebarOpen(false);
    viewEpoch.current++;
    setData(undefined);
    setPending("");
    setMessage("");
    setReview(false);
    setTab("chat");
    setPanel(undefined);
    setError("");
  };
  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!workspace || requestLock.current || !canSend(message, busy)) return;
    requestLock.current = true;
    setBusy(true);
    setError("");
    setPending(message.trim());
    setMessage("");
    setTab("chat");
    const strategy = review ? "review" : "standard";
    setReview(false);
    try {
      if (profile?.durable_runs) {
        const key = crypto.randomUUID();
        const id = data?.session.session_id;
        savePending(profile.user_id, workspace, { key, workspace, conversation: id });
        const run = await api.request<Run>(id ? `${resource(id)}/runs` : "/v1/conversations", "POST",
          id ? { message: message.trim(), expected_revision: data!.session.current_revision, answer_strategy: strategy, idempotency_key: key }
             : { goal: message.trim(), workspace_id: workspace, answer_strategy: strategy, idempotency_key: key });
        savePending(profile.user_id, workspace, { key, workspace, conversation: run.conversation_id, run: run.run_id });
        await open(run.conversation_id, undefined, run); await list();
        return;
      }
      const result = await api.request<{
        session?: Session;
        session_id?: string;
      }>(
        data
          ? `${resource(data.session.session_id)}/continue`
          : "/v1/conversations",
        "POST",
        data
          ? {
              message: message.trim(),
              expected_revision: data.session.current_revision,
              answer_strategy: strategy,
            }
          : {
              goal: message.trim(),
              workspace_id: workspace,
              answer_strategy: strategy,
            },
      );
      const id = result.session?.session_id || result.session_id;
      if (!id) throw new Error("处理已结束，请刷新对话列表查看结果。");
      await open(id);
      await list();
    } catch (e) {
      handleError(e);
      if (profile?.durable_runs) setDisconnected(true);
      setError(
        (e instanceof Error ? e.message : "连接中断。") +
          " 不会自动重复发送，请刷新列表查看已保存的结果。",
      );
    } finally {
      if (profile?.durable_runs && runStream.current && !runStream.current.signal.aborted) return;
      requestLock.current = false;
      setBusy(false);
    }
  };
  const keydown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  };
  const mutate = async (
    id: string,
    suffix: string,
    method = "POST",
    body?: unknown,
  ) => {
    await api.request(`${resource(id)}${suffix}`, method, body);
    await list();
    if (data?.session.session_id === id) {
      if (method === "DELETE") newChat();
      else await open(id);
    }
  };
  const historical = !!data && data.revision !== data.session.current_revision;
  const role = profile?.workspaces.find(
    (w) => w.workspace_id === workspace,
  )?.role;
  const confirmAction = async () => {
    if (!action) return;
    try {
      if (action.type === "rename" && action.session)
        await mutate(action.session.session_id, "", "PATCH", { title });
      if (action.type === "delete" && action.session)
        await mutate(action.session.session_id, "", "DELETE");
      if (action.type === "purge") {
        const expected_revisions = Object.fromEntries(
          sessions
            .filter((s) => selected.includes(s.session_id))
            .map((s) => [s.session_id, s.current_revision]),
        );
        await api.request(
          `/v1/trash/purge?${new URLSearchParams({ workspace_id: workspace })}`,
          "POST",
          { expected_revisions },
        );
        setSelected([]);
        await list();
      }
      if (action.type === "devices") {
        await api.request("/v1/me/logout-others", "POST");
        setDevices(await api.request("/v1/me/devices"));
      }
      if (action.type === "empty" && action.snapshot) {
        await api.request(
          `/v1/trash/empty?${new URLSearchParams({ workspace_id: workspace })}`,
          "POST",
          { fingerprint: action.snapshot.fingerprint },
        );
        setSelected([]);
        await list();
      }
      setAction(undefined);
    } catch (e) {
      handleError(e);
    }
  };
  const userPanel = async (next: "profile" | "devices") => {
    setSidebarOpen(false);
    setPanel(next);
    if (next === "devices") {
      try {
        setDevices(await api.request("/v1/me/devices"));
      } catch (e) {
        handleError(e);
      }
    }
  };
  const evidenceTurns =
    data?.turns.filter(
      (t) =>
        t.role === "assistant" && (t.citations?.length || t.legacy_evidence),
    ) || [];
  if (locked)
    return (
      <main className="welcome">
        <div className="brandmark">W</div>
        <h1>需要重新确认登录与权限</h1>
        <p>{error}</p>
        <button className="primary" onClick={() => void auth.login()}>
          重新登录
        </button>
      </main>
    );
  return (
    <div
      className="shell"
      style={{ "--sidebar-width": `${width}px` } as React.CSSProperties}
    >
      <aside className={`sidebar ${sidebarOpen ? "opened" : ""}`}>
        <button
          className="mobile-only sidebar-close"
          aria-label="关闭侧栏"
          onClick={() => setSidebarOpen(false)}
        >
          <Icon name="close" />
        </button>
        <div className="brand">
          <span className="brandmark small">W</span>
          <span>
            WMS Assistant<small>你的配置工作台</small>
          </span>
        </div>
        <button className="new-chat" disabled={busy && !activeRun} onClick={newChat}>
          <Icon name="plus" />
          新对话<span aria-hidden="true">↵</span>
        </button>
        <label className="search">
          <Icon name="search" />
          <input
            aria-label="搜索对话"
            placeholder="搜索你的对话"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="list-tabs">
          {[
            ["active", "对话"],
            ["archived", "归档"],
            ["trash", "回收站"],
          ].map(([mode, label]) => (
            <button
              key={mode}
              aria-pressed={listMode === mode}
              disabled={busy}
              onClick={() => setListMode(mode as typeof listMode)}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          className="text-button refresh-list"
          aria-label="刷新对话列表"
          disabled={busy || !workspace}
          onClick={() => void list().catch(handleError)}
        >
          ↻ 刷新对话列表
        </button>
        <div className="conversation-list">
          {sessions.map((s) => (
            <div
              className={`conversation-row ${data?.session.session_id === s.session_id ? "selected" : ""}`}
              key={s.session_id}
            >
              {listMode === "trash" && (
                <input
                  type="checkbox"
                  aria-label={`选择 ${s.display_title || s.goal}`}
                  checked={selected.includes(s.session_id)}
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? [...selected, s.session_id]
                        : selected.filter((id) => id !== s.session_id),
                    )
                  }
                />
              )}
              <button
                className="conversation-title"
                title={s.display_title || s.goal}
                disabled={busy || listMode === "trash"}
                onClick={() => void open(s.session_id).catch(handleError)}
              >
                <Icon name="chat" />
                <span>{s.display_title || s.goal}</span>
              </button>
              {listMode === "trash" ? (
                <button
                  className="restore"
                  onClick={() =>
                    void mutate(s.session_id, "/restore").catch(handleError)
                  }
                >
                  恢复
                </button>
              ) : (
                <details className="row-menu">
                  <summary aria-label={`操作 ${s.display_title || s.goal}`}>
                    <Icon name="more" />
                  </summary>
                  <div className="menu">
                    <button
                      disabled={busy}
                      onClick={() => {
                        setTitle(s.display_title || s.goal);
                        setAction({ type: "rename", session: s });
                      }}
                    >
                      重命名
                    </button>
                    <button
                      disabled={busy}
                      onClick={() =>
                        void mutate(
                          s.session_id,
                          listMode === "archived" ? "/unarchive" : "/archive",
                        ).catch(handleError)
                      }
                    >
                      {listMode === "archived" ? "取消归档" : "归档"}
                    </button>
                    <button
                      className="danger"
                      disabled={busy}
                      onClick={() => setAction({ type: "delete", session: s })}
                    >
                      移入回收站
                    </button>
                  </div>
                </details>
              )}
            </div>
          ))}
          {!sessions.length && (
            <p className="empty-list">
              {listMode === "trash"
                ? "回收站为空"
                : "还没有对话，开始一个新问题吧。"}
            </p>
          )}
        </div>
        {listMode === "trash" && !!sessions.length && (
          <div className="trash-actions">
            <small>
              已选 {selected.length} / {sessions.length} 条
            </small>
            <div>
              <button
                disabled={!selected.length}
                onClick={() => setAction({ type: "purge" })}
              >
                删除所选
              </button>
              <button
                className="danger"
                disabled={!trashSnapshot?.count}
                onClick={() =>
                  setAction({ type: "empty", snapshot: trashSnapshot })
                }
              >
                清空回收站
              </button>
            </div>
          </div>
        )}
        <div className="sidebar-footer">
          <label>
            当前工作区
            <select
              aria-label="选择工作区"
              disabled={busy}
              value={workspace}
              onChange={(e) => {
                newChat();
                setWorkspace(e.target.value);
              }}
            >
              {profile?.workspaces.map((w) => (
                <option key={w.workspace_id} value={w.workspace_id}>
                  {w.workspace_id}
                </option>
              ))}
            </select>
          </label>
          <label className="sidebar-size">
            侧栏宽度
            <input
              aria-label="侧栏宽度"
              type="range"
              min="240"
              max="420"
              value={width}
              onChange={(e) => setWidth(sidebarWidth(Number(e.target.value)))}
            />
          </label>
          <button
            className="account-button"
            disabled={busy}
            onClick={() => void userPanel("profile")}
          >
            <span className="avatar">
              {(profile?.nickname || profile?.email || "U")
                .slice(0, 1)
                .toUpperCase()}
            </span>
            <span>
              {profile?.nickname || "我的账号"}
              <small>资料与登录设备</small>
            </span>
            <Icon name="more" />
          </button>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <button
            className="mobile-only"
            aria-label="打开侧栏"
            onClick={() => setSidebarOpen(true)}
          >
            <Icon name="more" />
          </button>
          <div className="workspace-tabs">
            <button
              aria-pressed={tab === "chat" && !panel}
              onClick={() => {
                setPanel(undefined);
                setTab("chat");
              }}
            >
              <Icon name="chat" />
              对话
            </button>
            <button
              aria-pressed={tab === "workspace" && !panel}
              onClick={() => {
                setPanel(undefined);
                setTab("workspace");
              }}
            >
              <Icon name="folder" />
              工作区
            </button>
          </div>
          <div className="topbar-note">
            <span className="private-dot" />
            私人对话 · 不自动写入 WMS
          </div>
        </header>
        {error && (
          <div role="alert" className="error-banner">
            {error}
            <button aria-label="关闭提示" onClick={() => setError("")}>
              <Icon name="close" />
            </button>
          </div>
        )}
        {panel ? (
          <section className="account-page">
            <span className="eyebrow">YOUR ACCOUNT</span>
            <h1>用户中心</h1>
            <p className="muted">
              管理个人资料与登录设备，你的对话始终属于你。
            </p>
            <nav className="account-tabs">
              <button
                aria-pressed={panel === "profile"}
                onClick={() => void userPanel("profile")}
              >
                个人资料
              </button>
              <button
                aria-pressed={panel === "devices"}
                onClick={() => void userPanel("devices")}
              >
                登录设备
              </button>
            </nav>
            {panel === "profile" ? (
              <>
                <form
                  className="account-card"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void api
                      .request<Profile>("/v1/me", "PATCH", { nickname })
                      .then((p) => setProfile({ ...profile, ...p }))
                      .catch(handleError);
                  }}
                >
                  <h2>个人资料</h2>
                  <label>
                    昵称
                    <input
                      aria-label="昵称"
                      value={nickname}
                      maxLength={80}
                      onChange={(e) => setNickname(e.target.value)}
                    />
                  </label>
                  <div className="profile-line">
                    <span>邮箱</span>
                    <span>
                      {profile?.email || "身份服务管理"}{" "}
                      <small>
                        {profile?.email_verified ? "已验证" : "待验证"}
                      </small>
                    </span>
                  </div>
                  <div className="profile-line">
                    <span>账号状态</span>
                    <span>
                      {profile?.status === "active" ? "正常" : "不可用"}
                    </span>
                  </div>
                  <button className="primary">保存资料</button>
                </form>
                <div className="account-card">
                  <h2>账号安全</h2>
                  <p>密码和邮箱验证由身份服务处理，本应用不保存密码。</p>
                  <button onClick={() => void auth.changePassword()}>
                    修改密码
                  </button>
                  <button
                    className="text-button danger"
                    onClick={() =>
                      void api
                        .request("/v1/logout", "POST")
                        .finally(() => auth.logout())
                        .catch(handleError)
                    }
                  >
                    退出当前账号
                  </button>
                </div>
              </>
            ) : (
              <div className="account-card">
                <div className="card-heading">
                  <h2>登录设备</h2>
                  <button onClick={() => setAction({ type: "devices" })}>
                    退出其他设备
                  </button>
                </div>
                {devices.map((d) => (
                  <div className="device-row" key={d.id}>
                    <Icon name="user" />
                    <div>
                      <strong>
                        {d.browser}
                        {d.current && <small>当前会话</small>}
                      </strong>
                      <p>
                        {d.ip_address} · 最近访问{" "}
                        {new Date(d.last_access * 1000).toLocaleString("zh-CN")}
                      </p>
                    </div>
                    {!d.current && (
                      <button
                        onClick={() =>
                          void api
                            .request(
                              `/v1/me/devices/${encodeURIComponent(d.id)}`,
                              "DELETE",
                            )
                            .then(() => userPanel("devices"))
                            .catch(handleError)
                        }
                      >
                        退出
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        ) : tab === "workspace" ? (
          <section className="workspace-page">
            <div className="workspace-heading">
              <div>
                <span className="eyebrow">CONFIGURATION WORKSPACE</span>
                <h1>配置工作区</h1>
              </div>
              {data && (
                <label>
                  对话轮次
                  <select
                    aria-label="对话轮次"
                    value={data.revision}
                    disabled={busy}
                    onChange={(e) =>
                      void open(data.session.session_id, Number(e.target.value))
                        .then(() => setTab("workspace"))
                        .catch(handleError)
                    }
                  >
                    {Array.from(
                      { length: data.session.current_revision },
                      (_, i) => i + 1,
                    )
                      .reverse()
                      .map((r) => (
                        <option key={r} value={r}>
                          第 {r} 轮
                          {r === data.session.current_revision ? " · 当前" : ""}
                        </option>
                      ))}
                  </select>
                </label>
              )}
            </div>
            {!data ? (
              <div className="empty-workspace">
                <Icon name="folder" />
                <h2>从一段对话开始</h2>
                <p>确认需求后，配置草稿、证据和审批会整理到这里。</p>
              </div>
            ) : (
              <>
                {historical && (
                  <p className="notice">
                    正在查看历史轮次，只读，不会修改当前方案。
                  </p>
                )}
                <div className="workspace-grid">
                  <section className="workspace-card">
                    <h2>
                      配置草稿{" "}
                      <span>{data.state.configuration_tasks?.length || 0}</span>
                    </h2>
                    {data.state.configuration_tasks?.length ? (
                      data.state.configuration_tasks.map((t) => (
                        <details key={t.task_id} open>
                          <summary>
                            {t.title}
                            <small>
                              {t.module} · {t.risk_level || "待评估"}
                            </small>
                          </summary>
                          <ol>
                            {t.steps?.map((s, i) => (
                              <li key={i}>{s}</li>
                            ))}
                          </ol>
                          <details>
                            <summary>验证与回退</summary>
                            <p>{t.validation_steps?.join("；")}</p>
                            <p>{t.rollback_steps?.join("；")}</p>
                          </details>
                        </details>
                      ))
                    ) : (
                      <p className="muted">
                        本轮没有配置草稿。知识问答可能只生成回答；提出完整配置目标后会逐步完善方案。
                      </p>
                    )}
                    <button
                      disabled={busy || historical}
                      onClick={() =>
                        void api
                          .request(
                            `${resource(data.session.session_id)}/validate`,
                            "POST",
                            {
                              expected_revision: data.session.current_revision,
                            },
                          )
                          .then(() => open(data.session.session_id))
                          .then(() => setTab("workspace"))
                          .catch(handleError)
                      }
                    >
                      验证草稿
                    </button>
                    <details
                      className="validation-report"
                      open={
                        !!data.state.validation_findings?.length ||
                        !!data.state.conflicts?.length
                      }
                    >
                      <summary>验证结果与冲突</summary>
                      {data.state.validation_findings?.length ? (
                        <ul>
                          {data.state.validation_findings.map(
                            (finding, index) => (
                              <li key={index}>
                                <strong>
                                  {finding.severity === "blocking"
                                    ? "阻塞"
                                    : finding.severity === "warning"
                                      ? "提醒"
                                      : "信息"}
                                  ：
                                </strong>
                                {finding.message ||
                                  finding.description ||
                                  "需要复核该发现"}
                              </li>
                            ),
                          )}
                        </ul>
                      ) : (
                        <p className="muted">
                          尚无验证发现；这不代表配置已批准或已在实际环境验证。
                        </p>
                      )}
                      {data.state.conflicts?.map((conflict, index) => (
                        <p
                          key={index}
                          className={conflict.blocking ? "notice" : "muted"}
                        >
                          {conflict.blocking ? "需要解决：" : "待复核："}
                          {conflict.summary}
                        </p>
                      ))}
                    </details>
                  </section>
                  <section className="workspace-card">
                    <h2>审批与导出</h2>
                    <p className="muted">
                      仅有审查权限的对话所有者可提交审批。引用证据不等于批准配置。
                    </p>
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        if (!confirmed) return;
                        void api
                          .request(
                            `${resource(data.session.session_id)}/review`,
                            "POST",
                            {
                              expected_revision: data.session.current_revision,
                              decision,
                              comment,
                            },
                          )
                          .then(() => open(data.session.session_id))
                          .then(() => {
                            setTab("workspace");
                            setComment("");
                            setConfirmed(false);
                          })
                          .catch(handleError);
                      }}
                    >
                      <select
                        aria-label="审查决定"
                        value={decision}
                        onChange={(e) => setDecision(e.target.value)}
                      >
                        <option value="approve">批准</option>
                        <option value="revise">要求修改</option>
                        <option value="reject">拒绝</option>
                      </select>
                      <textarea
                        aria-label="审查意见"
                        placeholder="填写审查意见"
                        value={comment}
                        onChange={(e) => setComment(e.target.value)}
                        maxLength={4000}
                      />
                      <label className="checkbox">
                        <input
                          type="checkbox"
                          checked={confirmed}
                          onChange={(e) => setConfirmed(e.target.checked)}
                        />
                        我确认已检查配置方案与证据
                      </label>
                      <button
                        disabled={
                          busy ||
                          !canReview(role, data.state.status, historical) ||
                          !confirmed ||
                          !comment.trim()
                        }
                      >
                        提交审查
                      </button>
                    </form>
                    <div className="export-actions">
                      {["markdown", "json"].map((format) => (
                        <button
                          key={format}
                          disabled={
                            busy ||
                            historical ||
                            data.state.status !== "approved" ||
                            !["reviewer", "workspace_admin"].includes(
                              role || "",
                            )
                          }
                          onClick={() =>
                            void api
                              .request<Export>(
                                `${resource(data.session.session_id)}/exports`,
                                "POST",
                                {
                                  expected_revision:
                                    data.session.current_revision,
                                  format,
                                },
                              )
                              .then((e) =>
                                api.download(
                                  `${resource(data.session.session_id)}/exports/${encodeURIComponent(e.export_id)}/download`,
                                ),
                              )
                              .catch(handleError)
                          }
                        >
                          导出 {format === "json" ? "JSON" : "Markdown"}
                        </button>
                      ))}
                    </div>
                    {data.approvals.map((a, i) => (
                      <p key={i} className="approval-line">
                        第 {a.revision} 轮 · {a.decision} · {a.comment}
                      </p>
                    ))}
                  </section>
                </div>
                <section className="workspace-card">
                  <h2>引用证据 · 按对话轮次</h2>
                  {evidenceTurns.length ? (
                    evidenceTurns.map((t, i) => (
                      <details className="round-evidence" key={t.turn_id}>
                        <summary>
                          第 {i + 1} 次回答 · 对话轮次 {t.revision}
                        </summary>
                        <Evidence
                          api={api}
                          citations={t.citations || []}
                          legacy={t.legacy_evidence}
                        />
                      </details>
                    ))
                  ) : (
                    <p className="muted">
                      本轮没有文档证据。历史证据不会被新一轮覆盖。
                    </p>
                  )}
                </section>
                <section className="workspace-card">
                  <h2>已确认范围与记忆</h2>
                  <dl>
                    {Object.entries(data.state.confirmed_context || {}).map(
                      ([k, v]) => (
                        <div key={k}>
                          <dt>{k}</dt>
                          <dd>{String(v)}</dd>
                        </div>
                      ),
                    )}
                  </dl>
                  <details>
                    <summary>滚动摘要</summary>
                    <p>
                      {data.state.conversation_summary ||
                        "近期消息仍在上下文中，尚无压缩摘要。"}
                    </p>
                  </details>
                </section>
              </>
            )}
          </section>
        ) : (
          <>
            <div className="messages" ref={messages}>
              <div className="chat-content">
                {!data && !pending ? (
                  <div className="chat-welcome">
                    <span className="brandmark">W</span>
                    <span className="eyebrow">WMS ASSISTANT</span>
                    <h1>今天，想完成哪项配置？</h1>
                    <p>
                      直接提问，或描述你的业务目标。
                      <br />
                      我们会一起梳理需求，形成有依据的配置方案。
                    </p>
                    <div className="suggestions">
                      {[
                        "梳理入库收货配置流程",
                        "排查 RF 拣货选项未显示",
                        "帮我规划一套配置方案",
                      ].map((s) => (
                        <button
                          key={s}
                          disabled={!workspace}
                          onClick={() => setMessage(s)}
                        >
                          <Icon name="chat" />
                          {s}
                          <span>↗</span>
                        </button>
                      ))}
                    </div>
                    {profile && !workspace && (
                      <p className="notice">
                        账号已就绪，尚未获工作区/知识库授权，请联系管理员。
                      </p>
                    )}
                  </div>
                ) : (
                  <>
                    {data?.turns.map((t) => (
                      <article className={`turn ${t.role}`} key={t.turn_id}>
                        {t.role === "assistant" && (
                          <span className="assistant-mark">W</span>
                        )}
                        <div className="turn-body">
                          <Markdown
                            components={{
                              img: () => null,
                              a: ({ children }) => <span>{children}</span>,
                            }}
                          >
                            {t.message}
                          </Markdown>
                          {t.role === "assistant" && (
                            <>
                              <Evidence
                                api={api}
                                citations={t.citations || []}
                                legacy={t.legacy_evidence}
                              />
                              <div className="answer-tools">
                                <button
                                  aria-label="复制回答"
                                  title="复制回答"
                                  onClick={() =>
                                    void navigator.clipboard
                                      .writeText(t.message)
                                      .catch(handleError)
                                  }
                                >
                                  <Icon name="copy" />
                                </button>
                                <button
                                  title="记录有帮助反馈"
                                  onClick={() =>
                                    void api
                                      .request(
                                        `${resource(data.session.session_id)}/feedback`,
                                        "POST",
                                        {
                                          revision: t.revision,
                                          kind: "thumbs_up",
                                        },
                                      )
                                      .catch(handleError)
                                  }
                                >
                                  有帮助
                                </button>
                                <span>
                                  {t.metadata?.answer_strategy === "review"
                                    ? "本次已开启旁路核验"
                                    : "默认回答"}
                                </span>
                              </div>
                            </>
                          )}
                        </div>
                      </article>
                    ))}
                    {pending && (
                      <article className="turn user pending">
                        <div className="turn-body">
                          <p>{pending}</p>
                        </div>
                      </article>
                    )}
                    {busy && (
                      <article className="turn assistant">
                        <span className="assistant-mark">W</span>
                        <div role="status" className="thinking">
                          <span />
                          <span />
                          <span />
                          <p>{activeRun ? runLabel(activeRun) : "正在处理你的问题…"}</p>
                          {activeRun && <button type="button" disabled={activeRun.status === "cancelling"}
                            onClick={() => void api.request<Run>(`/v1/runs/${encodeURIComponent(activeRun.run_id)}/cancel`, "POST").then(setActiveRun).catch(handleError)}>取消处理</button>}
                        </div>
                      </article>
                    )}
                    {disconnected && profile?.durable_runs && <button type="button" onClick={() => void reconnect()}>重新连接进度</button>}
                    {!busy && data?.state.open_questions?.length ? (
                      <section className="questions">
                        <h3>还需要补充的信息</h3>
                        {data.state.open_questions.map((q, i) => (
                          <p key={i}>{q.text}</p>
                        ))}
                        <small>在下方继续回复即可。</small>
                      </section>
                    ) : null}
                  </>
                )}
                <div ref={latest} />
              </div>
            </div>
            <div className="composer-dock">
              <form className="composer" onSubmit={submit}>
                <textarea
                  aria-label="输入问题"
                  placeholder={
                    workspace
                      ? "继续提问，或补充配置需求…"
                      : "请先获得工作区授权"
                  }
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  onKeyDown={keydown}
                  disabled={busy || !workspace || historical}
                  maxLength={16000}
                  rows={2}
                />
                <div className="composer-bottom">
                  <button
                    type="button"
                    className={`review-toggle ${review ? "enabled" : ""}`}
                    aria-pressed={review}
                    disabled={busy}
                    onClick={() => setReview(!review)}
                  >
                    ✧ 单次旁路核验{review ? " · 已开启" : ""}
                  </button>
                  <button
                    className="send"
                    aria-label="发送问题"
                    disabled={
                      !workspace || !canSend(message, busy) || historical
                    }
                  >
                    <Icon name="arrow" />
                  </button>
                </div>
              </form>
              <p className="composer-note">
                回答可能有误，请结合引用核验。不会自动写入 WMS。
                <span>Enter 发送 · Shift+Enter 换行</span>
              </p>
            </div>
          </>
        )}
        {(data || pending) && !panel && (
          <button
            className="return-current"
            title="回到当前回答"
            aria-label="回到当前回答"
            onClick={() => {
              setTab("chat");
              requestAnimationFrame(() =>
                latest.current?.scrollIntoView?.({
                  behavior: "smooth",
                  block: "end",
                }),
              );
            }}
          >
            <Icon name="arrow" />
          </button>
        )}
      </main>
      {action && (
        <dialog
          ref={dialog}
          onCancel={() => setAction(undefined)}
          role="dialog"
          aria-modal="true"
          aria-label={
            action.type === "rename"
              ? "重命名对话"
              : action.type === "delete"
                ? "移入回收站"
                : action.type === "devices"
                  ? "退出其他设备"
                  : "永久删除对话"
          }
          className="modal"
          onClick={(e) => e.stopPropagation()}
        >
          <button
            className="modal-close"
            aria-label="关闭弹窗"
            onClick={() => setAction(undefined)}
          >
            <Icon name="close" />
          </button>
          <h2>
            {action.type === "rename"
              ? "重命名对话"
              : action.type === "delete"
                ? "移入回收站？"
                : action.type === "devices"
                  ? "退出其他设备？"
                  : "永久删除所选对话？"}
          </h2>
          {action.type === "rename" ? (
            <input
              aria-label="对话名称"
              autoFocus
              maxLength={120}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          ) : (
            <p>
              {action.type === "delete"
                ? "之后可以在回收站恢复。"
                : action.type === "devices"
                  ? "其他设备需重新登录，当前设备会保留。"
                  : `将永久删除 ${action.type === "empty" ? action.snapshot?.count : selected.length} 条对话及关联记录，无法恢复。`}
            </p>
          )}
          <div className="modal-actions">
            <button onClick={() => setAction(undefined)}>取消</button>
            <button
              className={action.type === "rename" ? "primary" : "danger solid"}
              disabled={
                action.type === "rename"
                  ? !title.trim()
                  : action.type === "purge"
                    ? !selected.length
                    : false
              }
              onClick={() => void confirmAction()}
            >
              {action.type === "rename" ? "保存" : "确认"}
            </button>
          </div>
        </dialog>
      )}
    </div>
  );
}
