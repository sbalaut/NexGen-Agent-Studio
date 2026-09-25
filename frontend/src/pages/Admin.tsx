import { useEffect, useState } from "react";
import { api, fmtTime, post, put, User } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Card, Classification, Field, Modal, PageHead, Tabs, useAction } from "../ui";

const ROLES = [
  ["Admin", "Users, model connections, audit"], ["Builder", "Knowledge bases, agents, publishing"],
  ["Reviewer", "Engineer review of flagged answers"], ["TrainingOperator", "Datasets, training jobs, evaluation"],
  ["Viewer", "Uses published assistants"],
];

export default function AdminPage({ user, tab, nav }: { user: User; tab: string; nav: Nav }) {
  return (
    <>
      <PageHead title="Administration" />
      <Tabs value={tab} onChange={(t) => nav("admin/" + t)} items={[["users", "Users & roles"], ["models", "Models"], ["status", "System status"], ["audit", "Audit log"]]} />
      {tab === "users" && <Users me={user} />}
      {tab === "models" && <Models />}
      {tab === "status" && <SystemStatus />}
      {tab === "audit" && <Audit />}
    </>
  );
}

function RoleChecks({ roles, perms, setRoles, setPerms }: any) {
  return (
    <>
      <div className="field"><span className="field-label">Roles (combine as needed)</span>
        {ROLES.map(([r, d]) => <label key={r} className="row small" style={{ marginBottom: 4 }}>
          <input type="checkbox" checked={roles.includes(r)} onChange={(e) => setRoles(e.target.checked ? [...roles, r] : roles.filter((x: string) => x !== r))} />
          <b>{r}</b><span className="muted">{d}</span></label>)}</div>
      <label className="row small" style={{ marginBottom: 12 }}><input type="checkbox" checked={perms.includes("model.promote")}
        onChange={(e) => setPerms(e.target.checked ? ["model.promote"] : [])} /><b>Model promotion</b>
        <span className="muted">may approve deploying a trained model (separate from training)</span></label>
    </>
  );
}

function Users({ me }: { me: User }) {
  const [rows, setRows] = useState<any[]>([]);
  const [edit, setEdit] = useState<any>(null);
  const [roles, setRoles] = useState<string[]>([]);
  const [perms, setPerms] = useState<string[]>([]);
  const [reset, setReset] = useState<any>(null);
  const { busy, run } = useAction();
  const load = () => api<any[]>("/users").then(setRows);
  useEffect(() => { load(); }, []);
  const open = (u: any) => { setEdit(u); setRoles(u?.roles || ["Viewer"]); setPerms(u?.permissions || []); };
  return (
    <Card title="Accounts" actions={<Button kind="primary" onClick={() => open({})}>New account</Button>}>
      <table><thead><tr><th>Person</th><th>Roles</th><th>Status</th><th>Created</th><th /></tr></thead>
        <tbody>{rows.map((u) => (
          <tr key={u.id}><td><b>{u.display_name}</b> <span className="muted">@{u.username}</span></td>
            <td><div className="row" style={{ gap: 4 }}>{u.roles.map((r: string) => <Badge key={r} tone="info">{r}</Badge>)}
              {u.permissions.map((p: string) => <Badge key={p} tone="warn">{p}</Badge>)}</div></td>
            <td>{u.active ? <Badge tone="ok">active</Badge> : <Badge>disabled</Badge>}</td>
            <td className="small muted">{fmtTime(u.created_at)}</td>
            <td><div className="row"><Button kind="ghost" onClick={() => open(u)}>Edit</Button><Button kind="ghost" onClick={() => setReset(u)}>Reset password</Button></div></td></tr>))}</tbody></table>
      {edit && (
        <Modal title={edit.id ? `Edit @${edit.username}` : "New account"} onClose={() => setEdit(null)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            const ok = edit.id
              ? await run(() => put(`/users/${edit.id}`, { display_name: f.get("dn"), active: f.get("active") === "on", roles, permissions: perms }), "Saved; the user's sessions were signed out")
              : await run(() => post("/users", { username: f.get("un"), display_name: f.get("dn"), password: f.get("pw"), roles, permissions: perms }), "Account created");
            if (ok) { setEdit(null); load(); }
          }}>
            {!edit.id && <Field label="Username" hint="Lowercase letters, digits, . _ -"><input name="un" required pattern="[a-z][a-z0-9_.\-]{2,63}" /></Field>}
            <Field label="Display name"><input name="dn" required defaultValue={edit.display_name} /></Field>
            {!edit.id && <Field label="Initial password" hint="At least 12 characters. Share it securely."><input name="pw" type="password" minLength={12} required /></Field>}
            <RoleChecks roles={roles} perms={perms} setRoles={setRoles} setPerms={setPerms} />
            {edit.id && <label className="row" style={{ marginBottom: 12 }}><input type="checkbox" name="active" defaultChecked={!!edit.active} disabled={edit.id === me.id} />Account active</label>}
            <div className="modal-foot"><Button type="button" onClick={() => setEdit(null)}>Cancel</Button><Button kind="primary" disabled={busy || !roles.length}>Save</Button></div>
          </form>
        </Modal>)}
      {reset && (
        <Modal title={`Reset password for @${reset.username}`} onClose={() => setReset(null)}>
          <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget);
            if (await run(() => post(`/users/${reset.id}/reset-password`, { password: f.get("pw") }), "Password reset; sessions revoked")) setReset(null); }}>
            <Field label="New password"><input name="pw" type="password" minLength={12} required /></Field>
            <div className="modal-foot"><Button type="button" onClick={() => setReset(null)}>Cancel</Button><Button kind="primary" disabled={busy}>Reset</Button></div>
          </form>
        </Modal>)}
    </Card>
  );
}

function Models() {
  const [c, setC] = useState<any>(null);
  const [avail, setAvail] = useState<any>({ models: [] });
  const [bm, setBm] = useState<any>({ models: [] });
  const [addConn, setAddConn] = useState(false);
  const { busy, run } = useAction();
  const load = () => { api("/models/connections").then(setC); api("/models/available").then(setAvail); api("/training/base-models").then(setBm).catch(() => {}); };
  useEffect(() => { load(); }, []);
  if (!c) return <p className="muted">Loading…</p>;
  const opts = (cur: string) => [...new Set([...avail.models, cur].filter(Boolean))].map((m: string) => <option key={m}>{m}</option>);
  return (
    <div className="stack">
      <Card title="Default models">
        {!avail.reachable && <div className="notice error" style={{ marginBottom: 10 }}>{avail.message}</div>}
        <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget);
          if (await run(() => put("/models/defaults", { generation_model: f.get("g"), review_model: f.get("r"), embedding_model: f.get("e") }), "Defaults saved")) load(); }}>
          <div className="grid3">
            <Field label="Answer model"><select name="g" defaultValue={c.defaults.generation_model}>{opts(c.defaults.generation_model)}</select></Field>
            <Field label="Review model"><select name="r" defaultValue={c.defaults.review_model}>{opts(c.defaults.review_model)}</select></Field>
            <Field label="Embedding model" hint="Changing it requires re-indexing every knowledge base."><select name="e" defaultValue={c.defaults.embedding_model}>{opts(c.defaults.embedding_model)}</select></Field>
          </div>
          <Button kind="primary" disabled={busy}>Save defaults</Button>
        </form>
      </Card>
      <Card title="Model connections" actions={<Button onClick={() => setAddConn(true)}>Add connection</Button>}>
        <table><thead><tr><th>Name</th><th>Type</th><th>URL</th><th>May receive up to</th><th /></tr></thead>
          <tbody>{c.connections.map((x: any) => <tr key={x.id}><td><b>{x.name}</b> {x.external ? <Badge tone="warn">external</Badge> : <Badge tone="ok">local</Badge>}</td>
            <td>{x.kind}</td><td className="mono small">{x.base_url}</td><td><Classification c={x.max_classification} /></td>
            <td><Button kind="ghost" onClick={async () => { await run(() => api(`/models/connections/${x.id}/enabled?enabled=${!x.enabled}`, { method: "PUT" }), "Updated"); load(); }}>{x.enabled ? "Disable" : "Enable"}</Button></td></tr>)}</tbody></table>
        <p className="muted small">Content above a connection's limit is blocked before anything is sent. Hosts must be on NEXAGENT_ALLOWED_MODEL_HOSTS.</p>
      </Card>
      <Card title="Base models for fine-tuning">
        <table><tbody>{bm.models.map((m: any) => <tr key={m.id}><td><b>{m.name}</b></td><td className="mono small">{m.path}</td><td>{m.license}</td></tr>)}</tbody></table>
        <form className="grid3" style={{ marginTop: 10, alignItems: "end" }} onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget);
          if (await run(() => post("/training/base-models", { name: f.get("n"), path: f.get("p"), license: f.get("l"), notes: "" }), "Registered")) load(); }}>
          <Field label="Name"><input name="n" required /></Field>
          <Field label="Folder (inside the approved models directory)"><input name="p" required /></Field>
          <Field label="Licence (as accepted)"><input name="l" required placeholder="e.g. Apache-2.0" /></Field>
          <Button disabled={busy}>Register base model</Button>
        </form>
      </Card>
      {addConn && <Modal title="Add model connection" onClose={() => setAddConn(false)}>
        <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget);
          if (await run(() => post("/models/connections", { name: f.get("n"), kind: f.get("k"), base_url: f.get("u"), max_classification: f.get("c"),
            external: f.get("x") === "on", api_key: f.get("key") || null }), "Connection added")) { setAddConn(false); load(); } }}>
          <Field label="Name"><input name="n" required pattern="[a-z0-9][a-z0-9._\-]+" /></Field>
          <Field label="Type"><select name="k"><option value="ollama">Ollama</option><option value="openai">OpenAI-compatible (vLLM etc.)</option></select></Field>
          <Field label="Base URL"><input name="u" required placeholder="http://gpu-server:11434" /></Field>
          <Field label="Highest classification it may receive"><select name="c" defaultValue="Internal"><option>Public</option><option>Internal</option><option>Restricted</option></select></Field>
          <label className="row" style={{ marginBottom: 12 }}><input type="checkbox" name="x" />Outside the plant network (never allowed Restricted content)</label>
          <Field label="API key (optional, stored encrypted)"><input name="key" type="password" /></Field>
          <div className="modal-foot"><Button type="button" onClick={() => setAddConn(false)}>Cancel</Button><Button kind="primary" disabled={busy}>Add</Button></div>
        </form></Modal>}
    </div>
  );
}

function SystemStatus() {
  const [s, setS] = useState<any>(null);
  useEffect(() => { api("/system/status").then(setS); }, []);
  if (!s) return <p className="muted">Checking…</p>;
  return (
    <div className="grid2">
      <Card title="Ollama">
        {s.ollama.reachable ? <>
          <div className="notice ok" style={{ marginBottom: 10 }}>Reachable — {s.ollama.models.length} model(s) installed.</div>
          {s.ollama.missing.length > 0 && <div className="notice error">Configured but not installed: {s.ollama.missing.join(", ")}. Run <code>ollama pull &lt;name&gt;</code>.</div>}
          <p className="small mono">{s.ollama.models.join(", ")}</p></> : <div className="notice error">{s.ollama.message}</div>}
      </Card>
      <Card title="Background work">
        <table><tbody>{s.jobs.map((j: any, i: number) => <tr key={i}><td className="mono small">{j.kind}</td><td><Badge>{j.status}</Badge></td><td>{j.n}</td></tr>)}</tbody></table>
        <dl className="kv" style={{ marginTop: 10 }}><dt>Training Python</dt><dd className="mono">{s.training_python}</dd><dt>Models folder</dt><dd className="mono">{s.models_dir}</dd></dl>
      </Card>
    </div>
  );
}

function Audit() {
  const [rows, setRows] = useState<any[]>([]);
  const [q, setQ] = useState("");
  useEffect(() => { api<any[]>("/audit?limit=1000").then(setRows); }, []);
  const f = rows.filter((r) => (r.action + r.username + r.details).toLowerCase().includes(q.toLowerCase()));
  return (
    <Card title="Audit log (append-only)" actions={<input placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 240 }} aria-label="Filter audit log" />}>
      <table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Details</th></tr></thead>
        <tbody>{f.slice(0, 400).map((r) => <tr key={r.id}><td className="small muted">{fmtTime(r.created_at)}</td><td>{r.username ? "@" + r.username : <span className="muted">system</span>}</td>
          <td className="mono small">{r.action}</td><td className="mono small" style={{ maxWidth: 520, wordBreak: "break-all" }}>{r.details === "{}" ? "" : r.details}</td></tr>)}</tbody></table>
    </Card>
  );
}
