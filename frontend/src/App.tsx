import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, appConfig, hasFeature, loadConfig, post, setCsrf, User } from "./api";
import { Button, Field, Modal, useAction } from "./ui";
import ProjectsPage from "./pages/Projects";
import ProjectPage from "./pages/Project";
import BuilderPage from "./pages/Builder";
import ChatPage from "./pages/Chat";
import ReviewPage from "./pages/Review";
import ImprovePage from "./pages/Improve";
import AdminPage from "./pages/Admin";
import ConnectPage from "./pages/Connect";

export type Nav = (hash: string) => void;

function useHash(): [string[], Nav] {
  const read = () => (location.hash.replace(/^#\/?/, "") || "").split("/");
  const [parts, setParts] = useState(read);
  useEffect(() => {
    const f = () => setParts(read());
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  return [parts, (h) => { location.hash = "#/" + h; }];
}

function Login({ onDone }: { onDone: (u: User) => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [mode, setMode] = useState<"in" | "up">("in");
  const pub = appConfig.public_mode;
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setErr(""); setBusy(true);
    try {
      const r = mode === "in"
        ? await post<{ user: User; csrf_token: string }>("/auth/login", { username: f.get("username"), password: f.get("password") })
        : await post<{ user: User; csrf_token: string }>("/auth/signup", { username: f.get("username"), password: f.get("password"),
            display_name: f.get("display_name"), email: f.get("email") || null });
      setCsrf(r.csrf_token); onDone(r.user);
    } catch (x: any) { setErr(x.message); } finally { setBusy(false); }
  }
  return (
    <div className="login-wrap">
      <div className="login-art">
        <div className="logo" style={{ color: "#fff" }}><span className="logo-mark" style={{ background: "#fff", color: "#0f5c63" }}>N</span>NexAgent Studio</div>
        <div>
          <h1>{pub ? "Build AI agents on your own documents and tools — without code." : "Build document-grounded assistants your engineers can trust."}</h1>
          <ul>
            {pub ? <>
              <li>Use OpenAI, Anthropic, Gemini or local models with your own API keys</li>
              <li>Connect MCP servers and let agents use their tools</li>
              <li>Teams of agents, knowledge graphs (GraphRAG) and cited answers</li>
              <li>Answers can be checked against your documents before they are shown</li>
            </> : <>
              <li>Build assistants from your manuals without writing code</li>
              <li>Every answer is checked against its sources before anyone sees it</li>
              <li>Engineers review and correct any answer the checks cannot confirm</li>
              <li>Approved corrections can improve local models, with separate sign-off at every step</li>
            </>}
          </ul>
        </div>
        <div style={{ opacity: .7, fontSize: 12.5 }}>{pub ? "Your keys and documents stay in your private workspace." : "Runs on your own server. No data leaves the plant network."}</div>
      </div>
      <div className="login-form">
        <form onSubmit={submit}>
          <h2 style={{ marginBottom: 18 }}>{mode === "in" ? "Sign in" : "Create your account"}</h2>
          <Field label="Username" hint={mode === "up" ? "3-64 lowercase letters, digits, . _ -" : undefined}>
            <input name="username" autoComplete="username" required autoFocus /></Field>
          {mode === "up" && <Field label="Your name"><input name="display_name" required maxLength={120} /></Field>}
          {mode === "up" && <Field label="Email (optional)"><input name="email" type="email" /></Field>}
          <Field label="Password" hint={mode === "up" ? `At least ${appConfig.min_password} characters` : undefined}>
            <input name="password" type="password" autoComplete={mode === "in" ? "current-password" : "new-password"} required
              minLength={mode === "up" ? appConfig.min_password : undefined} /></Field>
          {err && <div className="notice error" style={{ marginBottom: 12 }}>{err}</div>}
          <Button kind="primary" disabled={busy} style={{ width: "100%", justifyContent: "center" }}>{mode === "in" ? "Sign in" : "Create account"}</Button>
          {appConfig.allow_signup ? (
            <p className="small" style={{ marginTop: 14 }}>{mode === "in" ? "New here? " : "Already have an account? "}
              <button type="button" className="btn ghost" onClick={() => { setMode(mode === "in" ? "up" : "in"); setErr(""); }}>
                {mode === "in" ? "Create an account" : "Sign in"}</button></p>
          ) : <p className="muted small" style={{ marginTop: 14 }}>Accounts are created by your administrator. There are no default passwords.</p>}
        </form>
      </div>
    </div>
  );
}

function PasswordDialog({ onClose }: { onClose: () => void }) {
  const { busy, run } = useAction();
  return (
    <Modal title="Change password" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault(); const f = new FormData(e.currentTarget);
        const ok = await run(() => post("/auth/password", { current_password: f.get("cur"), new_password: f.get("new") }), "Password changed");
        if (ok) onClose();
      }}>
        <Field label="Current password"><input name="cur" type="password" required /></Field>
        <Field label="New password" hint="At least 12 characters. Other sessions are signed out."><input name="new" type="password" minLength={12} required /></Field>
        <div className="modal-foot"><Button type="button" onClick={onClose}>Cancel</Button><Button kind="primary" disabled={busy}>Save</Button></div>
      </form>
    </Modal>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [reviewCount, setReviewCount] = useState(0);
  const [pw, setPw] = useState(false);
  const [parts, nav] = useHash();

  useEffect(() => {
    loadConfig().then(() => api<{ user: User; csrf_token: string }>("/auth/me"))
      .then((r) => { setCsrf(r.csrf_token); setUser(r.user); })
      .catch((e) => { if (!(e instanceof ApiError && e.status === 401)) console.warn(e); })
      .finally(() => setLoading(false));
  }, []);
  const has = (r: string) => !!user?.roles.includes(r);
  useEffect(() => {
    if (!user || !has("Reviewer") || !hasFeature("review")) return;
    const load = () => api<any[]>("/review/items").then((x) => setReviewCount(x.length)).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [user, parts.join("/")]);

  if (loading) return <div className="main muted">Loading…</div>;
  if (!user) return <Login onDone={setUser} />;
  const canBuildHome = has("Admin") || has("Builder");
  const [view, a, b] = parts[0] ? parts : [canBuildHome ? "projects" : has("Reviewer") ? "review" : "chat"];
  const item = (key: string, label: string, target = key, extra?: React.ReactNode) => (
    <button className={"nav-item" + (view === key ? " active" : "")} onClick={() => nav(target)}>{label}{extra}</button>
  );
  const canBuild = has("Admin") || has("Builder");
  return (
    <div className="shell">
      <aside className="side" aria-label="Main navigation">
        <div className="logo"><span className="logo-mark">N</span>NexAgent Studio</div>
        <div className="nav-group">Use</div>
        {item("chat", "Assistants")}
        <div className="nav-group">Build</div>
        {item("projects", "Projects & knowledge", "projects")}
        {canBuild && item("builder", "Agent builder", "projects")}
        {canBuild && (hasFeature("mcp") || appConfig.public_mode || has("Admin")) && item("connect", "Models, keys & MCP tools", "connect")}
        {has("Reviewer") && hasFeature("review") && <><div className="nav-group">Answer review</div>
          {item("review", "Review queue", "review", reviewCount ? <span className="count">{reviewCount}</span> : null)}</>}
        {hasFeature("training") && (has("TrainingOperator") || user.permissions.includes("model.promote")) && <><div className="nav-group">Model improvement</div>
          {item("improve", "Datasets & training")}</>}
        {has("Admin") && <><div className="nav-group">Administration</div>{item("admin", "Users, models & audit")}</>}
        <div className="side-foot">
          <div style={{ fontWeight: 600 }}>{user.display_name}</div>
          <div className="muted small">{user.roles.join(", ")}</div>
          <div className="row" style={{ marginTop: 8 }}>
            <Button kind="ghost" onClick={() => setPw(true)}>Password</Button>
            <Button kind="ghost" onClick={async () => { await post("/auth/logout").catch(() => {}); setCsrf(""); setUser(null); location.hash = ""; }}>Sign out</Button>
          </div>
        </div>
      </aside>
      <main className="main">
        {view === "projects" && <ProjectsPage user={user} nav={nav} />}
        {view === "project" && a && <ProjectPage key={a} pid={a} tab={b || "knowledge"} user={user} nav={nav} />}
        {view === "builder" && a && <BuilderPage key={a} wid={a} nav={nav} />}
        {view === "builder" && !a && <ProjectsPage user={user} nav={nav} />}
        {view === "chat" && <ChatPage user={user} assistantId={a} />}
        {view === "review" && <ReviewPage itemId={a} nav={nav} />}
        {view === "improve" && <ImprovePage user={user} pid={a} tab={b} nav={nav} />}
        {view === "admin" && <AdminPage user={user} tab={a || "users"} nav={nav} />}
        {view === "connect" && <ConnectPage user={user} tab={a || "keys"} nav={nav} />}
      </main>
      {pw && <PasswordDialog onClose={() => setPw(false)} />}
    </div>
  );
}
