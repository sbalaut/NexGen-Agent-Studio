import { useState } from "react";
import { useData } from "../data";
import { listTools } from "../lib/mcp";
import { newId, put, remove } from "../lib/store";
import type { McpServer } from "../lib/types";
import { Badge, Button, Card, Empty, Field, Modal, PageHead, useAction } from "../ui";

export default function ToolsPage() {
  const { servers, reload } = useData();
  const [add, setAdd] = useState(false);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const { busy, run } = useAction();

  async function refresh(s: McpServer) {
    let next: McpServer;
    try {
      const tools = await listTools(s.url, s.headers);
      const was = new Map(s.tools.map((t) => [t.name, t.enabled]));
      next = { ...s, lastError: null, checkedAt: Date.now(),
        tools: tools.map((t) => ({ name: t.name, description: t.description, schema: t.inputSchema, enabled: was.get(t.name) ?? false })) };
    } catch (e: any) { next = { ...s, lastError: e.message, checkedAt: Date.now() }; }
    await put("mcp", next); await reload();
    return next;
  }

  return (
    <>
      <PageHead title="MCP tools" sub="Connect remote MCP (Model Context Protocol) servers and choose which of their tools your agents may call." />
      <div className="notice" style={{ marginBottom: 14 }}>
        Tools start <b>disabled</b> — switch on only tools you trust. Calls go directly from this browser to the server, so the server must use
        https, speak “Streamable HTTP”, and allow this site in its CORS settings (allow the <code>Mcp-Session-Id</code> and
        <code> MCP-Protocol-Version</code> headers and expose <code>Mcp-Session-Id</code>). Local command (stdio) servers need the server edition.
      </div>
      <div className="row between" style={{ marginBottom: 12 }}><span /><Button kind="primary" onClick={() => setAdd(true)}>Add MCP server</Button></div>
      {servers.length === 0 ? <Empty title="No MCP servers">Add a server by its URL, e.g. https://example.com/mcp</Empty> : (
        <div className="stack">{servers.map((s) => (
          <Card key={s.id} title={<span className="row">{s.name}<Badge>{s.tools.filter((t) => t.enabled).length}/{s.tools.length} tools on</Badge></span>}
            actions={<>
              <Button disabled={busy} onClick={() => run(() => refresh(s))}>Refresh tools</Button>
              <Button kind="danger" onClick={async () => { if (confirm(`Remove ${s.name}? Agents using its tools will stop validating.`)) { await remove("mcp", s.id); await reload(); } }}>Remove</Button>
            </>}>
            <div className="mono small muted">{s.url}{Object.keys(s.headers).length ? " · auth header set" : ""}</div>
            {s.lastError && <div className="notice error small" style={{ margin: "8px 0" }}>{s.lastError}</div>}
            {s.tools.length > 0 && <table style={{ marginTop: 8 }}><thead><tr><th>Tool</th><th>What it does</th><th>Agents may use it</th></tr></thead>
              <tbody>{s.tools.map((t) => (
                <tr key={t.name}><td className="mono"><b>{t.name}</b>
                  <div><button className="btn ghost small" onClick={() => setOpen({ ...open, [s.id + t.name]: !open[s.id + t.name] })}>{open[s.id + t.name] ? "hide" : "inputs"}</button></div>
                  {open[s.id + t.name] && <pre className="small" style={{ whiteSpace: "pre-wrap", maxWidth: 360 }}>{JSON.stringify(t.schema?.properties || {}, null, 1)}</pre>}</td>
                  <td className="small">{t.description}</td>
                  <td><label className="row small"><input type="checkbox" checked={t.enabled} onChange={async (e) => {
                    await put("mcp", { ...s, tools: s.tools.map((x) => (x.name === t.name ? { ...x, enabled: e.target.checked } : x)) }); await reload();
                  }} />{t.enabled ? "enabled" : "disabled"}</label></td></tr>))}</tbody></table>}
          </Card>))}</div>)}
      {add && (
        <Modal title="Add MCP server" onClose={() => setAdd(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            const headers: Record<string, string> = {};
            const hn = String(f.get("hname") || "").trim(), hv = String(f.get("hvalue") || "").trim();
            if (hn && hv) headers[hn] = hv;
            const s: McpServer = { id: newId(), name: String(f.get("name")).trim(), url: String(f.get("url")).trim(), headers, tools: [], lastError: null, checkedAt: null };
            await put("mcp", s);
            setAdd(false);
            await run(() => refresh(s));
          }}>
            <Field label="Name"><input name="name" required maxLength={40} pattern="[A-Za-z0-9._\-]{2,40}" placeholder="web-search" /></Field>
            <Field label="Server URL" hint="https://…/mcp (http:// only for localhost)"><input name="url" type="url" required placeholder="https://example.com/mcp" /></Field>
            <div className="grid2" style={{ gap: 8 }}>
              <Field label="Auth header name (optional)"><input name="hname" placeholder="Authorization" /></Field>
              <Field label="Auth header value" hint="Kept in this browser"><input name="hvalue" type="password" placeholder="Bearer …" /></Field></div>
            <div className="modal-foot"><Button type="button" onClick={() => setAdd(false)}>Cancel</Button><Button kind="primary" disabled={busy}>Add &amp; list tools</Button></div>
          </form>
        </Modal>)}
    </>
  );
}
