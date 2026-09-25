import { useEffect, useState } from "react";
import { api, appConfig, del, fmtTime, hasFeature, post, put, User } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Card, Classification, Empty, Field, Modal, PageHead, Tabs, useAction } from "../ui";

const PROVIDERS: [string, string, string][] = [
  ["openai", "OpenAI", "e.g. gpt-4.1-mini for answers, text-embedding-3-small for search"],
  ["anthropic", "Anthropic (Claude)", "Chat and tools only — Anthropic has no embedding API; pair it with OpenAI/Gemini for search"],
  ["gemini", "Google Gemini", "Chat, tools and embeddings (e.g. gemini-embedding-001)"],
  ["openai-compatible", "OpenAI-compatible (Groq, Together, vLLM…)", "Needs the https base URL of the service"],
];

export default function ConnectPage({ user, tab, nav }: { user: User; tab: string; nav: Nav }) {
  const items: [string, string][] = [["keys", "Model connections"]];
  if (hasFeature("mcp")) items.push(["mcp", "MCP servers & tools"]);
  return (
    <>
      <PageHead title="Models, keys & MCP tools" sub="Connect language-model providers with your own API keys, and MCP servers whose tools your agents may use." />
      <Tabs value={tab} onChange={(t) => nav("connect/" + t)} items={items} />
      {tab === "keys" && <Keys user={user} />}
      {tab === "mcp" && <MCP user={user} />}
    </>
  );
}

function Keys({ user }: { user: User }) {
  const [d, setD] = useState<any>(null);
  const [add, setAdd] = useState(false);
  const [provider, setProvider] = useState("openai");
  const [models, setModels] = useState<Record<string, any>>({});
  const { busy, run } = useAction();
  const load = () => api("/me/connections").then(setD);
  useEffect(() => { load(); }, []);
  if (!d) return <p className="muted">Loading…</p>;
  const canPersonal = appConfig.public_mode || user.roles.includes("Admin");
  return (
    <div className="stack">
      <Card title="My connections" actions={canPersonal && <Button kind="primary" onClick={() => setAdd(true)}>Add API key</Button>}>
        {!canPersonal && <div className="notice">On this server, model connections are managed by the administrator (see Shared connections below).</div>}
        {d.mine.length === 0 ? (canPersonal && <Empty title="No personal keys yet">Add an OpenAI, Anthropic or Gemini key to use those models in your agents.</Empty>) :
          <table><thead><tr><th>Name (use this in agents)</th><th>Provider</th><th>May receive</th><th>Added</th><th /></tr></thead>
            <tbody>{d.mine.map((c: any) => (
              <tr key={c.id}><td><b className="mono">{c.name}</b></td><td>{c.kind}<div className="muted small">{c.base_url}</div></td>
                <td><Classification c={c.max_classification} /></td><td className="small muted">{fmtTime(c.created_at)}</td>
                <td><div className="row">
                  <Button onClick={async () => { const m = await run(() => api(`/me/connections/${c.name}/models`)); if (m) setModels({ ...models, [c.name]: m }); }}>Test & list models</Button>
                  <Button kind="danger" onClick={async () => { if (confirm("Delete this key?")) { await run(() => del(`/me/connections/${c.id}`), "Deleted"); load(); } }}>Delete</Button>
                </div>
                  {models[c.name] && (models[c.name].reachable ? <div className="small" style={{ marginTop: 6, maxHeight: 120, overflow: "auto" }}>
                    <Badge tone="ok">key works</Badge> {models[c.name].models.slice(0, 60).join(", ")}</div>
                    : <div className="notice error small" style={{ marginTop: 6 }}>{models[c.name].message}</div>)}
                </td></tr>))}</tbody></table>}
        <p className="muted small" style={{ marginTop: 10 }}>{d.notice}</p>
      </Card>
      <Card title="Shared connections (set up by the administrator)">
        {d.shared.length === 0 ? <p className="muted">None.</p> :
          <table><tbody>{d.shared.map((c: any) => <tr key={c.name}><td className="mono">{c.name}</td><td>{c.kind}</td><td><Classification c={c.max_classification} /></td></tr>)}</tbody></table>}
      </Card>
      {add && (
        <Modal title="Add an API key" onClose={() => setAdd(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            if (await run(() => post("/me/connections", { name: f.get("name"), provider, api_key: f.get("key"),
              base_url: provider === "openai-compatible" ? f.get("base") : null, max_classification: f.get("cls") }), "Key saved")) { setAdd(false); load(); }
          }}>
            <Field label="Provider" hint={PROVIDERS.find((p) => p[0] === provider)?.[2]}>
              <select value={provider} onChange={(e) => setProvider(e.target.value)}>{PROVIDERS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></Field>
            <Field label="Name" hint="Short name you pick in agents, e.g. my-openai"><input name="name" required pattern="[a-z0-9][a-z0-9._\-]{1,40}" defaultValue={"my-" + provider.split("-")[0]} /></Field>
            {provider === "openai-compatible" && <Field label="Base URL (https)"><input name="base" required placeholder="https://api.groq.com/openai" /></Field>}
            <Field label="API key" hint="Stored encrypted; it is never shown again."><input name="key" type="password" required minLength={8} autoComplete="off" /></Field>
            <Field label="Highest document classification this provider may receive" hint="Restricted content is never sent to an outside provider.">
              <select name="cls" defaultValue="Internal"><option>Public</option><option>Internal</option></select></Field>
            <div className="modal-foot"><Button type="button" onClick={() => setAdd(false)}>Cancel</Button><Button kind="primary" disabled={busy}>Save</Button></div>
          </form>
        </Modal>)}
    </div>
  );
}

function MCP({ user }: { user: User }) {
  const [d, setD] = useState<any>(null);
  const [add, setAdd] = useState(false);
  const [transport, setTransport] = useState("http");
  const [shared, setShared] = useState(!appConfig.public_mode);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const { busy, run } = useAction();
  const admin = user.roles.includes("Admin");
  const load = () => api("/mcp/servers").then(setD);
  useEffect(() => { load(); }, []);
  if (!d) return <p className="muted">Loading…</p>;
  const canAdd = admin || appConfig.public_mode;
  return (
    <div className="stack">
      <div className="notice">MCP (Model Context Protocol) servers give agents extra tools: a web search, a database query, your email… New tools start
        <b> disabled</b>: switch on only the tools you trust. Each server has a classification limit, and content above it is never sent to that server.</div>
      <div className="row between"><span />{canAdd && <Button kind="primary" onClick={() => setAdd(true)}>Add MCP server</Button>}</div>
      {d.servers.length === 0 ? <Empty title="No MCP servers yet">{canAdd ? "Add a remote MCP server by its URL." : "Ask an administrator to add one."}</Empty> :
        d.servers.map((s: any) => {
          const mine = !s.shared || admin;
          return (
            <Card key={s.id} title={<span className="row">{s.name}<Badge tone={s.shared ? "info" : "neutral"}>{s.shared ? "shared" : "personal"}</Badge>
              <Badge>{s.transport === "http" ? "remote" : "local command"}</Badge><Classification c={s.max_classification} />
              {!s.enabled && <Badge tone="error">disabled</Badge>}</span>}
              actions={mine && <>
                <Button onClick={async () => { const r = await run(() => post(`/mcp/servers/${s.id}/refresh`)); if (r) load(); }}>Refresh tools</Button>
                <Button onClick={async () => { await run(() => put(`/mcp/servers/${s.id}/enabled`, { enabled: !s.enabled })); load(); }}>{s.enabled ? "Disable" : "Enable"}</Button>
                <Button kind="danger" onClick={async () => { if (confirm("Delete this MCP server? Agents using its tools will fail validation.")) { await run(() => del(`/mcp/servers/${s.id}`), "Deleted"); load(); } }}>Delete</Button>
              </>}>
              {s.url && <div className="mono small muted">{s.url}</div>}
              {s.command && <div className="mono small muted">{s.command} {s.args.join(" ")}</div>}
              {s.last_error && <div className="notice error small" style={{ margin: "8px 0" }}>Last check failed: {s.last_error}</div>}
              <table style={{ marginTop: 8 }}><thead><tr><th>Tool</th><th>What it does</th><th>Agents may use it</th></tr></thead>
                <tbody>{s.tools.map((t: any) => (
                  <tr key={t.name}><td className="mono"><b>{t.name}</b>
                    <div><button className="btn ghost small" onClick={() => setOpen({ ...open, [t.id]: !open[t.id] })}>{open[t.id] ? "hide" : "inputs"}</button></div>
                    {open[t.id] && <pre className="small" style={{ whiteSpace: "pre-wrap", maxWidth: 360 }}>{JSON.stringify(t.schema?.properties || {}, null, 1)}</pre>}</td>
                    <td className="small">{t.description}</td>
                    <td>{mine ? <label className="row small"><input type="checkbox" checked={!!t.enabled}
                      onChange={async (e) => { await run(() => put(`/mcp/servers/${s.id}/tools/${encodeURIComponent(t.name)}`, { enabled: e.target.checked })); load(); }} />
                      {t.enabled ? "enabled" : "disabled"}</label> : <Badge tone="ok">enabled</Badge>}</td></tr>))}</tbody></table>
              {s.tools.length === 0 && <p className="muted small">No tools listed. Use "Refresh tools".</p>}
            </Card>);
        })}
      {add && (
        <Modal title="Add MCP server" onClose={() => setAdd(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            const headers: Record<string, string> = {};
            const hn = String(f.get("hname") || "").trim(), hv = String(f.get("hvalue") || "").trim();
            if (hn && hv) headers[hn] = hv;
            const env: Record<string, string> = {};
            String(f.get("env") || "").split("\n").map((l) => l.trim()).filter(Boolean).forEach((l) => { const i = l.indexOf("="); if (i > 0) env[l.slice(0, i)] = l.slice(i + 1); });
            const body: any = { name: f.get("name"), transport, max_classification: f.get("cls"), shared: admin && shared, headers, env };
            if (transport === "http") body.url = f.get("url"); else { body.command = f.get("command"); body.args = String(f.get("args") || "").split(" ").filter(Boolean); }
            if (await run(() => post("/mcp/servers", body), "MCP server added — review and enable its tools")) { setAdd(false); load(); }
          }}>
            <Field label="Name"><input name="name" required pattern="[a-z0-9][a-z0-9._\-]{1,40}" placeholder="web-search" /></Field>
            {admin && d.stdio_allowed && <Field label="Type"><select value={transport} onChange={(e) => setTransport(e.target.value)}>
              <option value="http">Remote (URL, Streamable HTTP)</option><option value="stdio">Local command on this server (admin only)</option></select></Field>}
            {transport === "http" ? <>
              <Field label="Server URL" hint={appConfig.public_mode && !(admin && shared) ? "Must be https:// and reachable on the public internet" : "e.g. https://mcp.example.com/mcp"}>
                <input name="url" required placeholder="https://…/mcp" /></Field>
              <div className="grid2" style={{ gap: 8 }}>
                <Field label="Auth header name (optional)"><input name="hname" placeholder="Authorization" /></Field>
                <Field label="Auth header value" hint="Stored encrypted"><input name="hvalue" type="password" placeholder="Bearer …" /></Field></div>
            </> : <>
              <Field label="Command"><input name="command" required placeholder="/usr/bin/python3" /></Field>
              <Field label="Arguments (space-separated)"><input name="args" placeholder="-m my_mcp_server" /></Field>
              <Field label="Environment (NAME=value per line, stored encrypted)"><textarea name="env" rows={3} /></Field>
            </>}
            <Field label="Highest classification this server may receive">
              <select name="cls" defaultValue="Public"><option>Public</option><option>Internal</option>{(admin && shared) && <option>Restricted</option>}</select></Field>
            {admin && <label className="row small" style={{ marginBottom: 12 }}><input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} />
              Shared with all users (otherwise only you can use it)</label>}
            <div className="modal-foot"><Button type="button" onClick={() => setAdd(false)}>Cancel</Button><Button kind="primary" disabled={busy}>Add & list tools</Button></div>
          </form>
        </Modal>)}
    </div>
  );
}
