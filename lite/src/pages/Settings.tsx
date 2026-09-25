import { useRef, useState } from "react";
import { useData } from "../data";
import { listModels, PRESETS, PROVIDER_LABEL } from "../lib/providers";
import { exportWorkspace, importWorkspace, newId, rememberKeys, wipeAll } from "../lib/store";
import type { Connection, ProviderKind } from "../lib/types";
import { Badge, Button, Card, Empty, Field, Modal, PageHead, useAction, useToast } from "../ui";

const HINT: Record<ProviderKind, string> = {
  openai: "Chat, tools and embeddings. Key from platform.openai.com.",
  anthropic: "Chat and tools (Claude). No embeddings — pair with OpenAI or Gemini for semantic search.",
  gemini: "Chat, tools and embeddings. Key from aistudio.google.com.",
  "openai-compatible": "Any service with the OpenAI API (Groq, Together, OpenRouter, a local server…). It must allow browser calls (CORS).",
};

export default function SettingsPage() {
  const { connections, setConnections, reload } = useData();
  const [add, setAdd] = useState(false);
  const [kind, setKind] = useState<ProviderKind>("openai");
  const [remember, setRemember] = useState(rememberKeys());
  const [tested, setTested] = useState<Record<string, { ok: boolean; text: string }>>({});
  const file = useRef<HTMLInputElement>(null);
  const { busy, run } = useAction();
  const toast = useToast();

  return (
    <>
      <PageHead title="API keys & data" sub="Your keys never leave this browser except to call the provider you added them for." />
      <div className="stack">
        <Card title="Model connections" actions={<Button kind="primary" onClick={() => setAdd(true)}>Add API key</Button>}>
          {connections.length === 0 ? <Empty title="No API keys yet">Add an OpenAI, Anthropic or Gemini key to start building agents.</Empty> :
            <table><thead><tr><th>Name</th><th>Provider</th><th>Key</th><th /></tr></thead><tbody>
              {connections.map((c) => (
                <tr key={c.id}><td><b>{c.name}</b></td><td>{PROVIDER_LABEL[c.kind]}<div className="muted small">{c.kind === "openai-compatible" ? c.baseUrl : PRESETS[c.kind]}</div></td>
                  <td className="mono">…{c.apiKey.slice(-4)}</td>
                  <td><div className="row">
                    <Button onClick={() => run(async () => {
                      try { const m = await listModels(c); setTested({ ...tested, [c.id]: { ok: true, text: `${m.length} models: ${m.slice(0, 12).join(", ")}${m.length > 12 ? "…" : ""}` } }); }
                      catch (e: any) { setTested({ ...tested, [c.id]: { ok: false, text: e.message } }); }
                    })} disabled={busy}>Test</Button>
                    <Button kind="danger" onClick={() => { if (confirm(`Remove the key "${c.name}" from this browser?`)) setConnections(connections.filter((x) => x.id !== c.id)); }}>Remove</Button>
                  </div>
                    {tested[c.id] && <div className={"small " + (tested[c.id].ok ? "" : "notice error")} style={{ marginTop: 6 }}>
                      {tested[c.id].ok && <Badge tone="ok">key works</Badge>} {tested[c.id].text}</div>}
                  </td></tr>))}
            </tbody></table>}
          <label className="row small" style={{ marginTop: 12 }}>
            <input type="checkbox" checked={remember} onChange={(e) => { setRemember(e.target.checked); setConnections(connections, e.target.checked); }} />
            Remember keys on this device (otherwise they are forgotten when you close the tab)</label>
          <p className="muted small" style={{ marginTop: 8 }}>Keys are stored in this browser's storage for this site only. Anyone with access to this
            browser profile could read them — do not use this on a shared computer. Set spending limits on your provider accounts.</p>
        </Card>

        <Card title="Your data">
          <p className="small">Knowledge bases, documents, knowledge graphs, agents and MCP settings are stored in this browser (IndexedDB).
            Export them to move to another device or keep a backup. Exports never contain API keys or MCP auth headers.</p>
          <div className="row">
            <Button onClick={() => run(async () => {
              const blob = new Blob([await exportWorkspace()], { type: "application/json" });
              const a = document.createElement("a");
              a.href = URL.createObjectURL(blob); a.download = `nexagent-lite-${new Date().toISOString().slice(0, 10)}.json`; a.click();
              setTimeout(() => URL.revokeObjectURL(a.href), 5000);
            }, "Export downloaded")}>Export workspace</Button>
            <Button onClick={() => file.current?.click()}>Import…</Button>
            <input ref={file} type="file" accept="application/json,.json" hidden onChange={async (e) => {
              const f = e.target.files?.[0]; if (!f) return;
              const n = await run(async () => importWorkspace(await f.text()));
              if (n !== undefined) { toast("ok", `Imported ${n} items`); await reload(); }
              e.target.value = "";
            }} />
            <Button kind="danger" onClick={async () => {
              if (!confirm("Delete ALL NexAgent Lite data in this browser, including API keys? This cannot be undone.")) return;
              await wipeAll(); location.reload();
            }}>Delete everything</Button>
          </div>
        </Card>
      </div>

      {add && (
        <Modal title="Add an API key" onClose={() => setAdd(false)}>
          <form onSubmit={(e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            const name = String(f.get("name")).trim();
            if (connections.some((c) => c.name === name)) { toast("error", "A connection with that name exists."); return; }
            const base = String(f.get("base") || "").trim();
            if (kind === "openai-compatible" && !/^https:\/\/|^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?/.test(base)) { toast("error", "The base URL must start with https://"); return; }
            const c: Connection = { id: newId(), name, kind, baseUrl: base, apiKey: String(f.get("key")).trim() };
            setConnections([...connections, c]); setAdd(false); toast("ok", "Key saved in this browser");
          }}>
            <Field label="Provider" hint={HINT[kind]}>
              <select value={kind} onChange={(e) => setKind(e.target.value as ProviderKind)}>
                {(Object.keys(PROVIDER_LABEL) as ProviderKind[]).map((k) => <option key={k} value={k}>{PROVIDER_LABEL[k]}</option>)}</select></Field>
            <Field label="Name" hint="Shown when you pick a model, e.g. my-openai"><input name="name" required maxLength={40} defaultValue={"my-" + kind.split("-")[0]} key={kind} /></Field>
            {kind === "openai-compatible" && <Field label="Base URL"><input name="base" required placeholder="https://api.groq.com/openai" /></Field>}
            <Field label="API key" hint={kind === "openai-compatible" ? "Leave empty if the service needs no key" : undefined}><input name="key" type="password" required={kind !== "openai-compatible"} minLength={kind === "openai-compatible" ? 0 : 8} autoComplete="off" /></Field>
            <div className="modal-foot"><Button type="button" onClick={() => setAdd(false)}>Cancel</Button><Button kind="primary">Save</Button></div>
          </form>
        </Modal>)}
    </>
  );
}
