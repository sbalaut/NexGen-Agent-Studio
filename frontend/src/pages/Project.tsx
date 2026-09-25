import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, Edge, Node, ReactFlow } from "@xyflow/react";
import { api, appConfig, del, fmtTime, hasFeature, KB, post, put, Project, User } from "../api";
import { connLabel, ModelInput, useConnections } from "../models";
import type { Nav } from "../App";
import { Badge, Button, Card, Classification, Empty, Field, Modal, PageHead, Status, Tabs, useAction, useToast } from "../ui";

const SECTION_LABEL: Record<string, string> = {
  process_description: "Process description", startup: "Startup", shutdown: "Shutdown", interlock: "Interlock / trip",
  troubleshooting: "Troubleshooting", equipment_spec: "Equipment specification", other: "Other",
};

export default function ProjectPage({ pid, tab, user, nav }: { pid: string; tab: string; user: User; nav: Nav }) {
  const [p, setP] = useState<Project | null>(null);
  useEffect(() => { api<Project>(`/projects/${pid}`).then(setP).catch(() => nav("projects")); }, [pid]);
  if (!p) return <p className="muted">Loading…</p>;
  const canBuild = user.roles.includes("Admin") || user.roles.includes("Builder");
  const editor = canBuild && p.membership !== "viewer";
  const owner = canBuild && p.membership === "owner";
  return (
    <>
      <div className="crumbs"><button onClick={() => nav("projects")}>Projects</button> / {p.name}</div>
      <PageHead title={<span className="row">{p.name}<Classification c={p.classification_floor} /></span>}
        sub={p.description || undefined} />
      <Tabs value={tab} onChange={(t) => nav(`project/${pid}/${t}`)}
        items={[["knowledge", "Knowledge bases"], ["agents", "Agents"], ["assistants", "Published assistants"], ["members", "Members"]]} />
      {tab === "knowledge" && <Knowledge p={p} editor={editor} owner={owner} />}
      {tab === "agents" && <Agents p={p} editor={editor} nav={nav} />}
      {tab === "assistants" && <Assistants p={p} owner={owner} nav={nav} />}
      {tab === "members" && <Members p={p} owner={owner} me={user} />}
    </>
  );
}

// ------------------------------------------------------------------ knowledge
function Knowledge({ p, editor, owner }: { p: Project; editor: boolean; owner: boolean }) {
  const [kbs, setKbs] = useState<KB[]>([]);
  const [sel, setSel] = useState<KB | null>(null);
  const [newKb, setNewKb] = useState(false);
  const [ix, setIx] = useState<IndexSettings>(emptySettings());
  const { busy, run } = useAction();
  const load = () => api<KB[]>(`/projects/${p.id}/knowledge-bases`).then((x) => { setKbs(x); setSel((s) => x.find((k) => k.id === s?.id) || s); });
  useEffect(() => { load(); }, []);
  if (sel) return <KBDetail kb={sel} editor={editor && !!sel.can_write} owner={owner} back={() => { setSel(null); load(); }} />;
  return (
    <>
      <div className="row between" style={{ marginBottom: 14 }}>
        <p className="muted" style={{ margin: 0 }}>Upload manuals, check how they were read, then index them for search.</p>
        {editor && <Button kind="primary" onClick={() => setNewKb(true)}>New knowledge base</Button>}
      </div>
      {kbs.length === 0 ? <Empty title="No knowledge bases you can read">{editor ? "Create one to upload documents." : "Ask a project owner for access."}</Empty> :
        <div className="grid3">{kbs.map((k) => (
          <button key={k.id} className="tile" onClick={() => setSel(k)}>
            <div className="row between"><span className="tile-title">{k.name}</span><Classification c={k.classification} /></div>
            <div className="stats"><span>{k.documents} documents</span><span>{k.chunks ?? 0} indexed passages</span></div>
            <div className="row"><span className="small muted">Index:</span><Status s={k.last_index_status || "none"} /></div>
          </button>))}</div>}
      {newKb && (
        <Modal title="New knowledge base" onClose={() => setNewKb(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            if (await run(() => post(`/projects/${p.id}/knowledge-bases`, { name: f.get("name"), description: f.get("d"), classification: f.get("c"),
                ...settingsBody(ix) }), "Knowledge base created")) {
              setNewKb(false); load();
            }
          }}>
            <Field label="Name"><input name="name" required placeholder="e.g. CDU operating manual" /></Field>
            <Field label="Description"><textarea name="d" /></Field>
            <Field label="Classification" hint={`Cannot be below the project minimum (${p.classification_floor}).`}>
              <select name="c" defaultValue={p.classification_floor}><option>Public</option><option>Internal</option><option>Restricted</option></select>
            </Field>
            <IndexSettingsFields v={ix} onChange={setIx} />
            <div className="modal-foot"><Button type="button" onClick={() => setNewKb(false)}>Cancel</Button><Button kind="primary" disabled={busy}>Create</Button></div>
          </form>
        </Modal>
      )}
    </>
  );
}

function KBDetail({ kb, editor, owner, back }: { kb: KB; editor: boolean; owner: boolean; back: () => void }) {
  const [data, setData] = useState<any>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [grants, setGrants] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [cls, setCls] = useState(kb.classification);
  const [tab, setTab] = useState<"docs" | "settings" | "graph">("docs");
  const { busy, run } = useAction();
  const toast = useToast();
  const load = () => api(`/knowledge-bases/${kb.id}/documents`).then(setData);
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, []);

  async function upload(files: FileList | null, replaces = "") {
    if (!files?.length) return;
    for (const file of Array.from(files)) {
      const fd = new FormData(); fd.append("file", file); fd.append("classification", cls); fd.append("replaces", replaces);
      await run(() => api(`/knowledge-bases/${kb.id}/documents`, { method: "POST", body: fd }), `Uploaded ${file.name}`);
    }
    if (fileRef.current) fileRef.current.value = "";
    load();
  }
  const building = data?.generations?.[0]?.status === "building";
  const confirmed = data?.documents?.filter((d: any) => d.confirmed_revision).length || 0;
  return (
    <>
      <div className="crumbs"><button onClick={back}>Knowledge bases</button> / {kb.name}</div>
      <div className="row between" style={{ marginBottom: 14 }}>
        <div className="row"><h2>{kb.name}</h2><Classification c={kb.classification} /></div>
        <div className="row">
          {owner && <Button onClick={() => setGrants(true)}>Who can read this</Button>}
          {editor && <Button kind="primary" disabled={busy || building || !confirmed}
            title={!confirmed ? "Confirm at least one preview first" : ""}
            onClick={async () => { await run(() => post(`/knowledge-bases/${kb.id}/index`), "Indexing started"); load(); }}>
            {building ? "Indexing…" : "Build search index"}</Button>}
        </div>
      </div>
      <Tabs value={tab} onChange={setTab} items={[["docs", "Documents"], ["settings", "Index settings"],
        ...(hasFeature("graphrag") ? [["graph", "Knowledge graph"]] as [string, string][] : [])] as any} />
      {tab === "settings" && <KBSettings kb={kb} editor={editor} />}
      {tab === "graph" && <GraphView kb={kb} />}
      {tab === "docs" && <>
      {editor && (
        <Card title="Add documents" className="stack">
          <div className="row">
            <input ref={fileRef} type="file" multiple accept=".pdf,.docx,.txt,.md" onChange={(e) => upload(e.target.files)} style={{ maxWidth: 420 }} />
            <label className="row small">Classification
              <select value={cls} onChange={(e) => setCls(e.target.value)} style={{ width: 140 }}>
                <option>Public</option><option>Internal</option><option>Restricted</option></select></label>
          </div>
          <p className="muted small" style={{ margin: 0 }}>Text PDFs, Word (.docx), .txt and Markdown. Scanned PDFs are detected but not read (OCR is not included).
            Each upload creates an immutable revision; you check the extracted text before it can be indexed.</p>
        </Card>
      )}
      <div style={{ height: 16 }} />
      <Card title="Documents">
        {!data ? <p className="muted">Loading…</p> : data.documents.length === 0 ? <Empty title="No documents yet" /> : (
          <table>
            <thead><tr><th>File</th><th>Revision</th><th>Status</th><th>Classification</th><th>Uploaded</th><th /></tr></thead>
            <tbody>{data.documents.map((d: any) => (
              <tr key={d.id}>
                <td><b>{d.filename}</b>{d.error && <div className="small" style={{ color: "var(--warn)" }}>{d.error}</div>}</td>
                <td>rev {d.revision_no}{d.confirmed_revision && d.confirmed_revision !== d.revision_no ? <div className="small muted">rev {d.confirmed_revision} confirmed</div> : null}</td>
                <td><Status s={d.status} /></td>
                <td><Classification c={d.classification} /></td>
                <td className="small muted">{fmtTime(d.created_at)}</td>
                <td className="row" style={{ justifyContent: "flex-end" }}>
                  {["preview_ready", "confirmed"].includes(d.status) && <Button onClick={() => setPreview(d.revision_id)}>
                    {d.status === "preview_ready" && editor ? "Check & confirm" : "View extraction"}</Button>}
                  {editor && <label className="btn" style={{ cursor: "pointer" }}>New revision
                    <input type="file" hidden accept=".pdf,.docx,.txt,.md" onChange={(e) => upload(e.target.files, d.id)} /></label>}
                  {editor && <Button kind="danger" onClick={async () => {
                    if (!confirm(`Delete ${d.filename}? It is removed from search immediately; datasets and trained models that used it are restricted.`)) return;
                    const r = await run(() => del(`/documents/${d.id}`), "Document deleted");
                    if (r?.restricted_dataset_versions?.length) toast("info", `${r.restricted_dataset_versions.length} dataset version(s) restricted`);
                    load();
                  }}>Delete</Button>}
                </td>
              </tr>))}</tbody>
          </table>
        )}
      </Card>
      <div style={{ height: 16 }} />
      <Card title="Search index history">
        {data?.generations?.length ? (
          <table><thead><tr><th>Built</th><th>Status</th><th>Passages</th><th>Embedding model</th><th>Graph</th><th /></tr></thead>
            <tbody>{data.generations.map((g: any) => (
              <tr key={g.id}><td className="small">{fmtTime(g.created_at)}</td><td><Status s={g.status} />
                {g.id === data.active_generation && <Badge tone="ok">in use</Badge>}</td>
                <td>{g.chunk_count}</td><td className="mono small">{g.embedding_connection ? g.embedding_connection + " / " : ""}{g.embedding_model}</td>
                <td className="small">{g.graph_status || "—"}</td>
                <td className="small" style={{ color: "var(--err)" }}>{g.error}</td></tr>))}</tbody></table>
        ) : <p className="muted">Not indexed yet. Confirm document previews, then build the index.</p>}
        <p className="muted small" style={{ marginTop: 8 }}>A new index replaces the old one only when it finishes successfully. If it fails, the previous index stays in use.</p>
      </Card>
      </>}
      {preview && <PreviewEditor rid={preview} editor={editor} onClose={() => { setPreview(null); load(); }} />}
      {grants && <Grants kb={kb} onClose={() => setGrants(false)} />}
    </>
  );
}

function PreviewEditor({ rid, editor, onClose }: { rid: string; editor: boolean; onClose: () => void }) {
  const [d, setD] = useState<any>(null);
  const [edits, setEdits] = useState<Record<string, { section_type?: string; include?: boolean }>>({});
  const [headings, setHeadings] = useState<Record<string, string>>({});
  const { busy, run } = useAction();
  useEffect(() => { api(`/revisions/${rid}/preview`).then(setD); }, [rid]);
  if (!d) return <Modal title="Extraction preview" onClose={onClose} wide><p className="muted">Loading…</p></Modal>;
  const editable = editor && d.revision.status === "preview_ready";
  const groups: [string, any[]][] = [];
  for (const b of d.blocks) {
    const last = groups[groups.length - 1];
    if (last && last[0] === b.heading) last[1].push(b); else groups.push([b.heading, [b]]);
  }
  const typeOf = (b: any) => edits[b.id]?.section_type ?? headings[b.heading] ?? b.section_type;
  const incl = (b: any) => edits[b.id]?.include ?? !!b.include;
  async function save(confirmAfter: boolean) {
    const blocks = Object.entries(edits).map(([id, v]) => ({ id, ...v }));
    await run(() => put(`/revisions/${rid}/blocks`, { blocks, heading_types: headings }));
    if (confirmAfter && await run(() => post(`/revisions/${rid}/confirm`), "Preview confirmed. Build the index to make it searchable.")) onClose();
    else if (!confirmAfter) onClose();
  }
  return (
    <Modal title={`Extraction preview — ${d.revision.filename} (rev ${d.revision.revision_no})`} onClose={onClose} wide>
      <p className="muted small">Check that text and tables were read correctly, and that each section has the right type.
        Question scope depends on it: a question asking to "describe the process" only searches <b>Process description</b> sections.</p>
      <table>
        <thead><tr><th style={{ width: 60 }}>Use</th><th>Text</th><th style={{ width: 210 }}>Section type</th><th style={{ width: 120 }}>Location</th></tr></thead>
        <tbody>
          {groups.map(([h, blocks], gi) => (
            <Fragment key={gi + h}>
              <tr className="heading-row"><td /><td>{h}</td><td>
                {editable ? <select value={headings[h] ?? blocks[0].section_type} onChange={(e) => setHeadings({ ...headings, [h]: e.target.value })}>
                  {d.section_types.map((s: string) => <option key={s} value={s}>{SECTION_LABEL[s]}</option>)}</select>
                  : SECTION_LABEL[blocks[0].section_type]}</td><td className="small muted">whole section</td></tr>
              {blocks.map((b) => (
                <tr key={b.id} className={"block-row" + (incl(b) ? "" : " excluded")}>
                  <td><input type="checkbox" disabled={!editable} checked={incl(b)} aria-label="Include block"
                    onChange={(e) => setEdits({ ...edits, [b.id]: { ...edits[b.id], include: e.target.checked } })} /></td>
                  <td>{b.kind === "table_row" && <Badge>table row</Badge>} {b.text}
                    {b.equipment_tags.length > 0 && <div className="row" style={{ marginTop: 4 }}>{b.equipment_tags.map((t: string) => <Badge key={t} tone="info">{t}</Badge>)}</div>}</td>
                  <td>{editable ? <select value={typeOf(b)} onChange={(e) => setEdits({ ...edits, [b.id]: { ...edits[b.id], section_type: e.target.value } })}>
                    {d.section_types.map((s: string) => <option key={s} value={s}>{SECTION_LABEL[s]}</option>)}</select> : SECTION_LABEL[b.section_type]}</td>
                  <td className="small muted">{b.location}</td>
                </tr>))}
            </Fragment>
          ))}
        </tbody>
      </table>
      <div className="modal-foot">
        <Button onClick={onClose}>Close</Button>
        {editable && <Button onClick={() => save(false)} disabled={busy}>Save changes</Button>}
        {editable && <Button kind="primary" onClick={() => save(true)} disabled={busy}>Save & confirm for indexing</Button>}
      </div>
    </Modal>
  );
}

function Grants({ kb, onClose }: { kb: KB; onClose: () => void }) {
  const [rows, setRows] = useState<any[]>([]);
  const { run } = useAction();
  const load = () => api<any[]>(`/knowledge-bases/${kb.id}/grants`).then(setRows);
  useEffect(() => { load(); }, []);
  const level = (r: any) => (r.can_write ? "write" : r.can_read ? "read" : "none");
  return (
    <Modal title={`Access to ${kb.name}`} onClose={onClose} wide>
      <p className="muted small">Only project members can be given access. People without read access get "not found" from assistants —
        their questions never see this knowledge base.</p>
      <table><thead><tr><th>Member</th><th>Project role</th><th>Access</th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.user_id}><td>{r.display_name} <span className="muted">@{r.username}</span></td><td>{r.membership}</td>
            <td><div className="pill-select">{(["none", "read", "write"] as const).map((l) => (
              <button key={l} className={level(r) === l ? "on" : ""} disabled={l === "write" && r.membership === "viewer"}
                onClick={async () => { await run(() => put(`/knowledge-bases/${kb.id}/grants`, { user_id: r.user_id, can_read: l !== "none", can_write: l === "write" }), "Access updated"); load(); }}>
                {l}</button>))}</div></td></tr>))}</tbody></table>
    </Modal>
  );
}

// ------------------------------------------------------------------ index settings (embeddings + GraphRAG)
type IndexSettings = { embedding_connection: string; embedding_model: string; graph_mode: string; graph_connection: string; graph_model: string };
const emptySettings = (kb?: KB): IndexSettings => ({ embedding_connection: kb?.embedding_connection || "", embedding_model: kb?.embedding_model || "",
  graph_mode: kb?.graph_mode || "off", graph_connection: kb?.graph_connection || "", graph_model: kb?.graph_model || "" });
const settingsBody = (v: IndexSettings) => ({ embedding_connection: v.embedding_connection || null, embedding_model: v.embedding_model || null,
  graph_mode: v.graph_mode, graph_connection: v.graph_mode === "llm" ? v.graph_connection || null : null,
  graph_model: v.graph_mode === "llm" ? v.graph_model || null : null });

function IndexSettingsFields({ v, onChange, disabled }: { v: IndexSettings; onChange: (v: IndexSettings) => void; disabled?: boolean }) {
  const conns = useConnections();
  const set = (p: Partial<IndexSettings>) => onChange({ ...v, ...p });
  const embedConns = conns.filter((c) => c.kind !== "anthropic");   // Anthropic has no embedding API
  return (
    <fieldset disabled={disabled} style={{ border: 0, padding: 0, margin: 0 }}>
      <div className="grid2" style={{ gap: 8 }}>
        <Field label="Embedding connection" hint={appConfig.public_mode ? "OpenAI, Gemini or any OpenAI-compatible endpoint. Anthropic has no embedding models." : "Empty = server default"}>
          <select value={v.embedding_connection} onChange={(e) => set({ embedding_connection: e.target.value, embedding_model: "" })}>
            <option value="">Server default</option>{embedConns.map((c) => <option key={c.name} value={c.name}>{connLabel(c)}</option>)}</select></Field>
        <Field label="Embedding model" hint="e.g. text-embedding-3-small, text-embedding-004">
          <ModelInput conn={v.embedding_connection} kind="embed" value={v.embedding_model} onChange={(m) => set({ embedding_model: m })}
            placeholder={v.embedding_connection ? "Model name" : "Server default"} /></Field>
      </div>
      {hasFeature("graphrag") && <>
        <Field label="Knowledge graph (GraphRAG)" hint="Built with the search index. Rules finds equipment tags and headings for free; a model finds any entities and relations but costs tokens.">
          <select value={v.graph_mode} onChange={(e) => set({ graph_mode: e.target.value })}>
            <option value="off">Off</option><option value="rules">Rules — equipment tags, headings (no model calls)</option>
            <option value="llm">Model extraction — entities, relations and topic summaries</option></select></Field>
        {v.graph_mode === "llm" && <div className="grid2" style={{ gap: 8 }}>
          <Field label="Graph connection"><select value={v.graph_connection} onChange={(e) => set({ graph_connection: e.target.value, graph_model: "" })}>
            <option value="">Server default</option>{conns.map((c) => <option key={c.name} value={c.name}>{connLabel(c)}</option>)}</select></Field>
          <Field label="Graph model" hint="A small, cheap model is fine"><ModelInput conn={v.graph_connection} kind="chat" value={v.graph_model}
            onChange={(m) => set({ graph_model: m })} placeholder="Default" /></Field></div>}
      </>}
    </fieldset>
  );
}

function KBSettings({ kb, editor }: { kb: KB; editor: boolean }) {
  const [v, setV] = useState<IndexSettings>(emptySettings(kb));
  const { busy, run } = useAction();
  return (
    <Card title="How this knowledge base is indexed">
      <IndexSettingsFields v={v} onChange={setV} disabled={!editor} />
      <p className="muted small">Changes apply the next time you build the search index. Questions are embedded with the same model, using the
        knowledge-base owner's connection.</p>
      {editor && <Button kind="primary" disabled={busy} onClick={() => run(() => put(`/knowledge-bases/${kb.id}/settings`, settingsBody(v)), "Saved — rebuild the index to apply")}>Save settings</Button>}
    </Card>
  );
}

const COMMUNITY_COLORS = ["#2563eb", "#16a34a", "#c2410c", "#7c3aed", "#0f766e", "#be185d", "#a16207", "#475569", "#0369a1", "#9333ea"];

/** Small force-directed layout: labels repel, relations pull together, communities cluster. */
function forceLayout(ents: any[], rels: any[]): Record<string, { x: number; y: number }> {
  const n = ents.length;
  const idx = Object.fromEntries(ents.map((e, i) => [e.id, i]));
  const w = ents.map((e) => 20 + String(e.name).length * 7);
  const comms = [...new Set(ents.map((e) => e.community ?? -1))];
  const x = new Float64Array(n), y = new Float64Array(n);
  ents.forEach((e, i) => {
    const ci = comms.indexOf(e.community ?? -1), a = (2 * Math.PI * ci) / Math.max(1, comms.length);
    x[i] = 300 * Math.cos(a) + (i % 7) * 13; y[i] = 300 * Math.sin(a) + (i % 5) * 17;
  });
  const links = rels.filter((r) => r.src in idx && r.dst in idx).map((r) => [idx[r.src], idx[r.dst]]);
  for (let step = 0; step < 300; step++) {
    const t = 1 - step / 300, fx = new Float64Array(n), fy = new Float64Array(n);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      let dx = x[i] - x[j], dy = (y[i] - y[j]) * 2.2;          // labels are wide: push more vertically
      const d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2), min = (w[i] + w[j]) / 2 + 20;
      const f = (d < min ? (min - d) * 0.5 : 0) + 2500 / d2;
      dx /= d; dy /= d; fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
    }
    for (const [a, b] of links) {
      const dx = x[b] - x[a], dy = y[b] - y[a], d = Math.sqrt(dx * dx + dy * dy) + 0.01, f = (d - 140) * 0.05;
      fx[a] += (dx / d) * f; fy[a] += (dy / d) * f; fx[b] -= (dx / d) * f; fy[b] -= (dy / d) * f;
    }
    for (let i = 0; i < n; i++) { fx[i] -= x[i] * 0.01; fy[i] -= y[i] * 0.01;
      x[i] += Math.max(-30, Math.min(30, fx[i])) * t; y[i] += Math.max(-30, Math.min(30, fy[i])) * t; }
  }
  return Object.fromEntries(ents.map((e, i) => [e.id, { x: x[i] - w[i] / 2, y: y[i] }]));
}

function GraphView({ kb }: { kb: KB }) {
  const [q, setQ] = useState("");
  const [data, setData] = useState<any>(null);
  const [pick, setPick] = useState<any>(null);
  const load = (query = q) => api(`/knowledge-bases/${kb.id}/graph?limit=80&q=${encodeURIComponent(query)}`).then(setData);
  useEffect(() => { load(""); }, [kb.id]);
  const { nodes, edges } = useMemo(() => {
    if (!data) return { nodes: [] as Node[], edges: [] as Edge[] };
    const pos = forceLayout(data.entities, data.relations);
    const nodes: Node[] = data.entities.map((e: any) => {
      const c = e.community ?? -1;
      const color = c < 0 ? "#64748b" : COMMUNITY_COLORS[c % COMMUNITY_COLORS.length];
      return { id: e.id, position: pos[e.id], data: { label: e.name }, style: { background: color, color: "#fff", border: 0, borderRadius: 14,
        fontSize: 12, padding: "4px 9px", width: "auto", minWidth: 40, opacity: 0.65 + Math.min(0.35, e.degree / 15) } };
    });
    const edges: Edge[] = data.relations.map((r: any, i: number) => ({ id: "r" + i, source: r.src, target: r.dst, label: ["related_to", "mentioned_with"].includes(r.relation) ? undefined : r.relation,
      style: { strokeWidth: Math.min(4, 0.6 + r.weight / 2), stroke: "#94a3b8" }, labelStyle: { fontSize: 9 } }));
    return { nodes, edges };
  }, [data]);
  if (!data) return <p className="muted">Loading…</p>;
  const byId = Object.fromEntries(data.entities.map((e: any) => [e.id, e]));
  return (
    <div className="grid2" style={{ gridTemplateColumns: "minmax(0,2fr) minmax(260px,1fr)", alignItems: "start" }}>
      <Card title={<span className="row">Entities and relations <span className="muted small">{data.status}</span></span>}
        actions={<form className="row" onSubmit={(e) => { e.preventDefault(); load(); }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find an entity, e.g. P-101" style={{ width: 200 }} aria-label="Find entity" />
          <Button>Search</Button>{q && <Button type="button" kind="ghost" onClick={() => { setQ(""); load(""); }}>Clear</Button>}</form>}>
        {data.entities.length === 0 ? <Empty title="No graph yet">Turn on GraphRAG under Index settings, then build the search index.</Empty> :
          <div className="graph-view" style={{ height: 520, border: "1px solid var(--line)", borderRadius: 8 }}>
            <ReactFlow nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.15, maxZoom: 1.6 }} minZoom={0.1} proOptions={{ hideAttribution: true }} nodesConnectable={false}
              onNodeClick={(_, n) => setPick(byId[n.id])}><Background gap={18} size={1} /><Controls showInteractive={false} /></ReactFlow>
          </div>}
        {pick && <div className="notice" style={{ marginTop: 10 }}><b>{pick.name}</b> <Badge>{pick.type}</Badge> · {pick.degree} links
          {pick.description && <p className="small" style={{ margin: "4px 0 0" }}>{pick.description}</p>}</div>}
      </Card>
      <Card title="Topics (communities)">
        {data.communities.length === 0 ? <p className="muted small">No communities yet.</p> : data.communities.map((c: any) => (
          <details key={c.community} style={{ marginBottom: 8 }}>
            <summary><span className="dot" style={{ background: COMMUNITY_COLORS[c.community % COMMUNITY_COLORS.length], display: "inline-block", width: 10, height: 10, borderRadius: 5, marginRight: 6 }} />
              <b>{c.title || `Topic ${c.community + 1}`}</b> <span className="muted small">{c.entity_count} entities</span></summary>
            <p className="small">{c.summary || "No summary (rules mode builds communities without model summaries)."}</p>
          </details>))}
        <p className="muted small">Summaries are generated by a model and are only used to find sources; answers must still cite the original passages.</p>
      </Card>
    </div>
  );
}

// ------------------------------------------------------------------ agents
function Agents({ p, editor, nav }: { p: Project; editor: boolean; nav: Nav }) {
  const [rows, setRows] = useState<any[]>([]);
  const [wizard, setWizard] = useState(false);
  useEffect(() => { api<any[]>(`/projects/${p.id}/workflows`).then(setRows); }, []);
  return (
    <>
      <div className="row between" style={{ marginBottom: 14 }}>
        <p className="muted" style={{ margin: 0 }}>An agent is a workflow you build visually: search, prompts, models, tool-using agents and agent teams.</p>
        {editor && <Button kind="primary" onClick={() => setWizard(true)}>New agent</Button>}
      </div>
      {rows.length === 0 ? <Empty title="No agents yet">{editor ? "The guided setup creates a working agent in one step." : ""}</Empty> :
        <table><thead><tr><th>Agent</th><th>Latest version</th><th>Valid</th><th>Created</th><th /></tr></thead>
          <tbody>{rows.map((w) => (
            <tr key={w.id} className="clickable" onClick={() => nav(`builder/${w.id}`)}>
              <td><b>{w.name}</b><div className="small muted">{w.description}</div></td><td>v{w.latest_version}</td>
              <td>{w.valid ? <Badge tone="ok">valid</Badge> : <Badge tone="error">has errors</Badge>}</td>
              <td className="small muted">{fmtTime(w.created_at)}</td><td><Button kind="ghost">Open builder →</Button></td></tr>))}</tbody></table>}
      {wizard && <Wizard p={p} onClose={() => setWizard(false)} nav={nav} />}
    </>
  );
}

const TEMPLATES: [string, string, string][] = [
  ["qa", "Knowledge Q&A", "Search → prompt → model → answer review. Every answer is checked against the sources."],
  ["agent", "Tool-using agent", "An agent that decides when to search the knowledge bases or call MCP tools."],
  ["chat", "Simple chat", "Prompt → model → answer, with no knowledge search. Needs a project without mandatory review."],
];

function Wizard({ p, onClose, nav }: { p: Project; onClose: () => void; nav: Nav }) {
  const [kbs, setKbs] = useState<KB[]>([]);
  const [defaults, setDefaults] = useState<any>({});
  const [pick, setPick] = useState<string[]>([]);
  const [template, setTemplate] = useState("qa");
  const conns = useConnections();
  const [conn, setConn] = useState("");
  const [model, setModel] = useState("");
  const [rmodel, setRmodel] = useState("");
  const { busy, run } = useAction();
  const review = p.require_review !== 0;
  useEffect(() => {
    api<KB[]>(`/projects/${p.id}/knowledge-bases`).then((k) => { setKbs(k); setPick(k.filter((x) => x.chunks).map((x) => x.id)); });
    api("/models/connections").then((c) => setDefaults(c.defaults));
  }, []);
  useEffect(() => { if (!conn && conns.length) setConn((conns.find((c) => c.personal) || conns[0]).name); }, [conns]);
  const needsKb = template !== "chat";
  const hosted = conns.find((c) => c.name === conn)?.kind !== "ollama";
  const chatBlocked = template === "chat" && review;
  return (
    <Modal title="New agent — guided setup" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault(); const f = new FormData(e.currentTarget);
        const r = await run(() => post<any>(`/projects/${p.id}/workflows`, { name: f.get("name"), description: f.get("d"), kb_ids: needsKb ? pick : [],
          template, connection: conn || "local-ollama", model, review_model: rmodel || (hosted ? model : "") }), "Agent created");
        if (r) nav(`builder/${r.id}`);
      }}>
        <div className="field"><span className="field-label">1. Starting point</span>
          <div className="stack" style={{ gap: 6 }}>{TEMPLATES.filter(([k]) => k !== "agent" || hasFeature("agents")).map(([k, label, hint]) => (
            <label key={k} className="row" style={{ alignItems: "flex-start" }}><input type="radio" name="tpl" checked={template === k} onChange={() => setTemplate(k)} />
              <span><b>{label}</b><div className="muted small">{hint}</div></span></label>))}</div></div>
        {chatBlocked && <div className="notice warn small" style={{ marginBottom: 10 }}>This project requires answer review, so a simple chat cannot be saved as valid. Choose Knowledge Q&amp;A.</div>}
        <Field label="2. Name"><input name="name" required placeholder="e.g. Pump manual assistant" /></Field>
        <Field label="What should it help with?"><textarea name="d" placeholder="Answers questions from the pump operating manual." /></Field>
        {needsKb && <div className="field"><span className="field-label">3. Knowledge bases to search</span>
          {kbs.length === 0 ? <span className="muted small">No readable knowledge bases in this project yet{template === "agent" ? " — the agent can still use MCP tools" : ""}.</span> :
            <div className="checks">{kbs.map((k) => (
              <label key={k.id}><input type="checkbox" checked={pick.includes(k.id)}
                onChange={(e) => setPick(e.target.checked ? [...pick, k.id] : pick.filter((x) => x !== k.id))} />
                {k.name} {!k.chunks && <span className="muted small">(not indexed)</span>}</label>))}</div>}
        </div>}
        <div className="grid2" style={{ gap: 8 }}>
          <Field label="Model connection" hint={conns.length ? "" : <span style={{ color: "var(--err)" }}>No connection yet — add your API key under “Models, keys &amp; MCP tools”.</span>}>
            <select value={conn} onChange={(e) => { setConn(e.target.value); setModel(""); setRmodel(""); }}>
              {conns.map((c) => <option key={c.name} value={c.name}>{connLabel(c)}</option>)}</select></Field>
          <Field label="Answer model" hint={hosted ? "Required for this provider" : `Empty = ${defaults.generation_model || "server default"}`}>
            <ModelInput conn={conn} kind="chat" value={model} onChange={setModel} placeholder="e.g. gpt-4o-mini, claude-sonnet-4-5, gemini-2.5-flash" /></Field>
        </div>
        {review && template !== "chat" && <Field label="Review model" hint="Checks each draft against the sources, in addition to the built-in number/unit/equipment checks.">
          <ModelInput conn={conn} kind="chat" value={rmodel} onChange={setRmodel} placeholder={hosted ? "Empty = same as answer model" : `Empty = ${defaults.review_model || "server default"}`} /></Field>}
        <div className="notice" style={{ marginBottom: 12 }}>{review
          ? "Every answer passes the Answer Review step. Answers that cannot be confirmed are withheld or sent to an engineer."
          : "This project does not require review: answers go straight to the user through a Direct Output node. You can add an Answer Review node in the builder."}</div>
        <div className="modal-foot"><Button type="button" onClick={onClose}>Cancel</Button>
          <Button kind="primary" disabled={busy || (template === "qa" && !pick.length) || chatBlocked || !conns.length || (hosted && !model.trim())}>Create and open builder</Button></div>
      </form>
    </Modal>
  );
}

// ------------------------------------------------------------------ assistants
function Assistants({ p, owner, nav }: { p: Project; owner: boolean; nav: Nav }) {
  const [rows, setRows] = useState<any[]>([]);
  const [wfs, setWfs] = useState<any[]>([]);
  const [pub, setPub] = useState(false);
  const [newKey, setNewKey] = useState<any>(null);
  const { busy, run } = useAction();
  const load = () => api<any[]>(`/projects/${p.id}/assistants`).then(setRows);
  useEffect(() => { load(); api<any[]>(`/projects/${p.id}/workflows`).then(setWfs); }, []);
  return (
    <>
      <div className="row between" style={{ marginBottom: 14 }}>
        <p className="muted" style={{ margin: 0 }}>Published assistants are available to project members (under Assistants) and, with a key, to other plant systems.</p>
        {owner && <Button kind="primary" onClick={() => setPub(true)} disabled={!wfs.length}>Publish an agent</Button>}
      </div>
      {rows.length === 0 ? <Empty title="Nothing published yet" /> : rows.map((a) => (
        <Card key={a.id} title={<span className="row">{a.name}<Status s={a.status} /><span className="muted small">/{a.slug} · {a.workflow_name} v{a.version_no}</span></span>}
          actions={<>
            {a.status === "published" && <Button onClick={() => nav(`chat/${a.id}`)}>Open chat</Button>}
            {owner && a.previous_version_id && <Button onClick={async () => { await run(() => post(`/assistants/${a.id}/rollback`), "Rolled back to the previous version"); load(); }}>Roll back</Button>}
            {owner && a.status === "published" && <Button kind="danger" onClick={async () => { if (confirm("Unpublish and revoke all keys?")) { await run(() => post(`/assistants/${a.id}/unpublish`), "Unpublished"); load(); } }}>Unpublish</Button>}
          </>}>
          <div className="row between"><b className="small">API keys</b>
            {owner && a.status === "published" && <Button kind="ghost" onClick={async () => {
              const name = prompt("Key name (e.g. the system that will use it)"); if (!name) return;
              const r = await run(() => post(`/assistants/${a.id}/keys`, { name })); if (r) { setNewKey(r); load(); }
            }}>+ New key</Button>}</div>
          {a.keys.length === 0 ? <p className="muted small">No keys.</p> : <table><tbody>{a.keys.map((k: any) => (
            <tr key={k.id}><td>{k.name}</td><td className="mono">nxk_{k.prefix}_…</td><td><Classification c={k.max_classification} /></td>
              <td className="small muted">{fmtTime(k.created_at)}</td>
              <td>{k.revoked_at ? <Badge>revoked</Badge> : owner && <Button kind="danger" onClick={async () => { await run(() => post(`/keys/${k.id}/revoke`), "Key revoked"); load(); }}>Revoke</Button>}</td></tr>))}</tbody></table>}
        </Card>
      ))}
      {pub && (
        <Modal title="Publish an agent" onClose={() => setPub(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            if (await run(() => post(`/projects/${p.id}/assistants`, { workflow_id: f.get("wf"), name: f.get("name"), slug: f.get("slug") }), "Published")) { setPub(false); load(); }
          }}>
            <Field label="Agent (latest valid version is published)"><select name="wf">{wfs.map((w) => <option key={w.id} value={w.id} disabled={!w.valid}>{w.name} v{w.latest_version}{w.valid ? "" : " (invalid)"}</option>)}</select></Field>
            <Field label="Assistant name"><input name="name" required /></Field>
            <Field label="Address (lowercase, digits, hyphens)" hint="Re-publishing with the same address replaces the version and keeps the previous one for rollback.">
              <input name="slug" required pattern="[a-z0-9][a-z0-9\-]+" placeholder="cdu-manual" /></Field>
            <div className="modal-foot"><Button type="button" onClick={() => setPub(false)}>Cancel</Button><Button kind="primary" disabled={busy}>Publish</Button></div>
          </form>
        </Modal>
      )}
      {newKey && (
        <Modal title="New API key" onClose={() => setNewKey(null)}>
          <p>{newKey.notice}</p>
          <div className="key-box">{newKey.key}</div>
          <p className="small muted" style={{ marginTop: 10 }}>Use it as <code>Authorization: Bearer &lt;key&gt;</code> on <code>POST api/v1/assistants/&lt;address&gt;/ask</code>.
            Scope: {newKey.knowledge_bases.length} knowledge base(s) you can read today.</p>
          <div className="modal-foot"><Button kind="primary" onClick={() => { navigator.clipboard?.writeText(newKey.key); setNewKey(null); }}>Copy & close</Button></div>
        </Modal>
      )}
    </>
  );
}

// ------------------------------------------------------------------ members
function Members({ p, owner, me }: { p: Project; owner: boolean; me: User }) {
  const [rows, setRows] = useState<any[]>([]);
  const [dir, setDir] = useState<any[]>([]);
  const { busy, run } = useAction();
  const load = () => api<any[]>(`/projects/${p.id}/members`).then(setRows);
  useEffect(() => { load(); if (owner) api<any[]>("/users/directory").then(setDir); }, []);
  return (
    <div className="grid2">
      <Card title="Members">
        <table><thead><tr><th>Person</th><th>Account roles</th><th>Project access</th><th /></tr></thead>
          <tbody>{rows.map((m) => (
            <tr key={m.user_id}><td>{m.display_name} <span className="muted">@{m.username}</span></td>
              <td className="small">{m.roles.join(", ")}</td>
              <td>{owner ? <select value={m.membership} onChange={async (e) => { await run(() => put(`/projects/${p.id}/members`, { username: m.username, membership: e.target.value }), "Updated"); load(); }}>
                <option value="owner">owner</option><option value="editor">editor</option><option value="viewer">viewer</option></select> : m.membership}</td>
              <td>{(owner || m.user_id === me.id) && <Button kind="danger" onClick={async () => { if (confirm("Remove this member? Their knowledge-base access in this project is removed too.")) { await run(() => del(`/projects/${p.id}/members/${m.user_id}`), "Removed"); load(); } }}>Remove</Button>}</td></tr>))}</tbody></table>
      </Card>
      {owner && (
        <Card title="Add a member">
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            if (await run(() => put(`/projects/${p.id}/members`, { username: f.get("u"), membership: f.get("m") }), "Member added")) { load(); (e.target as HTMLFormElement).reset(); }
          }}>
            <Field label="Account"><select name="u" required><option value="">Choose…</option>
              {dir.filter((u) => !rows.some((r) => r.user_id === u.id)).map((u) => <option key={u.id} value={u.username}>{u.display_name} (@{u.username})</option>)}</select></Field>
            <Field label="Project access" hint="Owners manage members and access; editors build; viewers use published assistants. Reviewers and training operators join as viewers.">
              <select name="m" defaultValue="viewer"><option value="viewer">viewer</option><option value="editor">editor</option><option value="owner">owner</option></select></Field>
            <p className="muted small">After adding, give knowledge-base read access under Knowledge bases → Who can read this.</p>
            <Button kind="primary" disabled={busy}>Add</Button>
          </form>
        </Card>
      )}
    </div>
  );
}
