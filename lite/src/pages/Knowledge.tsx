import { useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, Edge, Node, ReactFlow } from "@xyflow/react";
import type { Nav } from "../App";
import { ModelInput, SUGGESTED, useData } from "../data";
import { ACCEPT } from "../lib/ingest";
import { addDocument, buildIndex, removeDocument } from "../lib/knowledge";
import { canEmbed, PROVIDER_LABEL } from "../lib/providers";
import { search } from "../lib/search";
import { byIndex, deleteKb, newId, put } from "../lib/store";
import type { Community, Doc, Entity, Evidence, KB, Relation } from "../lib/types";
import { Badge, Button, Card, Empty, Field, Modal, PageHead, SourceCard, Tabs, useAction, useToast } from "../ui";

type Settings = Pick<KB, "embedConnection" | "embedModel" | "graphMode" | "graphConnection" | "graphModel">;

function SettingsFields({ v, onChange }: { v: Settings; onChange: (v: Settings) => void }) {
  const { connections } = useData();
  const set = (p: Partial<Settings>) => onChange({ ...v, ...p });
  const ec = connections.find((c) => c.id === v.embedConnection), gc = connections.find((c) => c.id === v.graphConnection);
  return <>
    <div className="grid2" style={{ gap: 8 }}>
      <Field label="Embedding connection" hint="Semantic search. Without it, keyword search (BM25) is used.">
        <select value={v.embedConnection} onChange={(e) => { const c = connections.find((x) => x.id === e.target.value);
          set({ embedConnection: e.target.value, embedModel: c ? SUGGESTED[c.kind]?.embed || "" : "" }); }}>
          <option value="">None — keyword search only</option>
          {connections.filter((c) => canEmbed(c.kind)).map((c) => <option key={c.id} value={c.id}>{c.name} · {PROVIDER_LABEL[c.kind]}</option>)}</select></Field>
      <Field label="Embedding model"><ModelInput conn={ec} kind="embed" value={v.embedModel} onChange={(m) => set({ embedModel: m })}
        placeholder={ec ? "e.g. " + (SUGGESTED[ec.kind]?.embed || "embedding model") : "—"} /></Field>
    </div>
    <Field label="Knowledge graph (GraphRAG)" hint="Rules: equipment tags and headings, free. Model: any entities and relations, plus topic summaries (costs tokens).">
      <select value={v.graphMode} onChange={(e) => set({ graphMode: e.target.value as KB["graphMode"] })}>
        <option value="off">Off</option><option value="rules">Rules — tags and headings (no model calls)</option>
        <option value="llm">Model extraction — entities, relations, topic summaries</option></select></Field>
    {v.graphMode === "llm" && <div className="grid2" style={{ gap: 8 }}>
      <Field label="Graph connection"><select value={v.graphConnection} onChange={(e) => { const c = connections.find((x) => x.id === e.target.value);
        set({ graphConnection: e.target.value, graphModel: c ? SUGGESTED[c.kind]?.chat || "" : "" }); }}>
        <option value="">Choose…</option>{connections.map((c) => <option key={c.id} value={c.id}>{c.name} · {PROVIDER_LABEL[c.kind]}</option>)}</select></Field>
      <Field label="Graph model" hint="A small, cheap model is fine"><ModelInput conn={gc} kind="chat" value={v.graphModel} onChange={(m) => set({ graphModel: m })} /></Field>
    </div>}
  </>;
}

export default function KnowledgePage({ kid, tab, nav }: { kid?: string; tab?: string; nav: Nav }) {
  const { kbs, connections, reload } = useData();
  const [add, setAdd] = useState(false);
  const [s, setS] = useState<Settings>({ embedConnection: "", embedModel: "", graphMode: "rules", graphConnection: "", graphModel: "" });
  const kb = kbs.find((k) => k.id === kid);
  if (kid && kb) return <KBDetail kb={kb} tab={tab || "docs"} nav={nav} />;
  return (
    <>
      <PageHead title="Knowledge & GraphRAG" sub="Upload documents, index them for search and build a knowledge graph. Everything stays in this browser."
        actions={<Button kind="primary" onClick={() => {
          const e = connections.find((c) => canEmbed(c.kind));
          setS({ embedConnection: e?.id || "", embedModel: e ? SUGGESTED[e.kind]?.embed || "" : "", graphMode: "rules", graphConnection: "", graphModel: "" });
          setAdd(true);
        }}>New knowledge base</Button>} />
      {kbs.length === 0 ? <Empty title="No knowledge bases yet">Create one, then add .pdf, .docx, .md or .txt files.</Empty> :
        <div className="grid3">{kbs.map((k) => (
          <button key={k.id} className="tile" onClick={() => nav(`knowledge/${k.id}`)}>
            <span className="tile-title">{k.name}</span>
            <span className="small muted">{k.description || "No description"}</span>
            <span className="row"><Badge tone={k.status === "ready" ? "ok" : k.status === "failed" ? "error" : k.status === "indexing" ? "info" : "neutral"}>{k.status}</Badge>
              {k.graphMode !== "off" && <Badge tone="info">graph: {k.graphMode}</Badge>}</span>
          </button>))}</div>}
      {add && (
        <Modal title="New knowledge base" onClose={() => setAdd(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            const k: KB = { id: newId(), name: String(f.get("name")).trim(), description: String(f.get("d") || ""), ...s, status: "empty", note: "", builtAt: null, createdAt: Date.now() };
            await put("kbs", k); await reload(); setAdd(false); nav(`knowledge/${k.id}`);
          }}>
            <Field label="Name"><input name="name" required maxLength={80} placeholder="e.g. Product manuals" /></Field>
            <Field label="Description"><textarea name="d" rows={2} /></Field>
            <SettingsFields v={s} onChange={setS} />
            <div className="modal-foot"><Button type="button" onClick={() => setAdd(false)}>Cancel</Button><Button kind="primary">Create</Button></div>
          </form>
        </Modal>)}
    </>
  );
}

function KBDetail({ kb, tab, nav }: { kb: KB; tab: string; nav: Nav }) {
  const { connections, reload } = useData();
  const [docs, setDocs] = useState<Doc[]>([]);
  const [progress, setProgress] = useState("");
  const [over, setOver] = useState(false);
  const [settings, setSettings] = useState<Settings>(kb);
  const abort = useRef<AbortController | null>(null);
  const [indexing, setIndexing] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const { busy, run } = useAction();
  const toast = useToast();
  const loadDocs = () => byIndex("docs", "kbId", kb.id).then((d) => setDocs(d.sort((a, b) => a.filename.localeCompare(b.filename))));
  useEffect(() => { loadDocs(); }, [kb.id]);

  async function upload(files: FileList | File[] | null) {
    for (const f of Array.from(files || [])) {
      setProgress(`Reading ${f.name}…`);
      const d = await run(() => addDocument(kb, f));
      if (d) toast(d.warning ? "info" : "ok", d.warning ? `${f.name}: ${d.warning}` : `${f.name}: ${d.chunks} passages`);
    }
    setProgress(""); await loadDocs(); await reload();
  }
  async function build() {
    abort.current = new AbortController(); setIndexing(true);
    const r = await buildIndex(kb.id, connections, setProgress, abort.current.signal);
    abort.current = null; setIndexing(false); setProgress(""); await reload();
    toast(r.status === "ready" ? "ok" : "error", r.status === "ready" ? "Index ready" : r.note);
  }
  return (
    <>
      <div className="crumbs"><button onClick={() => nav("knowledge")}>Knowledge</button> / {kb.name}</div>
      <PageHead title={<span className="row">{kb.name}<Badge tone={kb.status === "ready" ? "ok" : kb.status === "failed" ? "error" : "neutral"}>{kb.status}</Badge></span>}
        sub={kb.note || kb.description || undefined}
        actions={<>
          {indexing ? <Button onClick={() => abort.current?.abort()}>Cancel</Button> :
            <Button kind="primary" disabled={!docs.length || busy} onClick={build}>{kb.status === "ready" ? "Rebuild index" : "Build index"}</Button>}
          <Button kind="danger" onClick={async () => { if (confirm(`Delete ${kb.name} and its documents from this browser?`)) { await deleteKb(kb.id); await reload(); nav("knowledge"); } }}>Delete</Button>
        </>} />
      {progress && <div className="notice" role="status" style={{ marginBottom: 12 }}>{progress}</div>}
      <Tabs value={tab} onChange={(t) => nav(`knowledge/${kb.id}/${t}`)} items={[["docs", "Documents"], ["search", "Try search"], ["graph", "Knowledge graph"], ["settings", "Settings"]]} />
      {tab === "docs" && <>
        <div className={"dropzone" + (over ? " over" : "")} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}>
          Drop .pdf, .docx, .md or .txt files here, or <button className="linklike" onClick={() => fileRef.current?.click()}>choose files</button>
          <input ref={fileRef} type="file" multiple accept={ACCEPT} hidden aria-label="Add documents" onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
          <div className="small" style={{ marginTop: 6 }}>Files are read in your browser. Scanned PDFs need OCR first. After adding files, build the index.</div>
        </div>
        <div style={{ height: 14 }} />
        {docs.length === 0 ? <Empty title="No documents yet" /> :
          <Card><table><thead><tr><th>File</th><th>Passages</th><th>Added</th><th /></tr></thead><tbody>{docs.map((d) => (
            <tr key={d.id}><td><b>{d.filename}</b>{d.warning && <div className="small" style={{ color: "var(--warn)" }}>{d.warning}</div>}</td><td>{d.chunks}</td>
              <td className="small muted">{new Date(d.addedAt).toLocaleString()}</td>
              <td><Button kind="danger" onClick={async () => { await removeDocument(d); await loadDocs(); toast("info", "Removed — rebuild the index to update the graph"); }}>Remove</Button></td></tr>))}
          </tbody></table></Card>}
      </>}
      {tab === "search" && <TrySearch kb={kb} />}
      {tab === "graph" && <GraphView kb={kb} />}
      {tab === "settings" && <Card title="Index settings">
        <SettingsFields v={settings} onChange={setSettings} />
        <p className="muted small">Changes apply at the next index build. Changing the embedding model re-embeds every passage.</p>
        <Button kind="primary" onClick={async () => { await put("kbs", { ...kb, ...settings }); await reload(); toast("ok", "Saved — rebuild the index to apply"); }}>Save settings</Button>
      </Card>}
    </>
  );
}

function TrySearch({ kb }: { kb: KB }) {
  const { connections } = useData();
  const [q, setQ] = useState("");
  const [graph, setGraph] = useState<"off" | "local" | "global" | "both">(kb.graphMode === "off" ? "off" : "both");
  const [res, setRes] = useState<Evidence[] | null>(null);
  const { busy, run } = useAction();
  return <Card title="Try a search" actions={null}>
    <form className="row" onSubmit={async (e) => { e.preventDefault(); const r = await run(() => search([kb], connections, q, { topK: 6, graph }));
      if (r) setRes(r.map((x, i) => ({ ...x, ref: i + 1 }))); }}>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask something the documents answer…" style={{ flex: 1, minWidth: 220 }} aria-label="Search query" />
      <select value={graph} onChange={(e) => setGraph(e.target.value as any)} style={{ width: 190 }} aria-label="Graph mode">
        <option value="off">Graph off</option><option value="local">Graph: local</option><option value="global">Graph: global</option><option value="both">Graph: both</option></select>
      <Button kind="primary" disabled={busy || !q.trim()}>Search</Button>
    </form>
    <div className="stack" style={{ marginTop: 12 }}>
      {res && (res.length ? res.map((e) => <SourceCard key={e.ref} c={e} />) : <p className="muted">No matching passages.</p>)}
    </div>
  </Card>;
}

// ------------------------------------------------------------------ graph view
const COLORS = ["#2563eb", "#16a34a", "#c2410c", "#7c3aed", "#0f766e", "#be185d", "#a16207", "#475569", "#0369a1", "#9333ea"];

export function forceLayout(ents: Entity[], rels: Relation[]): Record<string, { x: number; y: number }> {
  const n = ents.length, idx = new Map(ents.map((e, i) => [e.id, i]));
  const w = ents.map((e) => 20 + e.name.length * 7);
  const comms = [...new Set(ents.map((e) => e.community))];
  const x = new Float64Array(n), y = new Float64Array(n);
  ents.forEach((e, i) => { const a = (2 * Math.PI * comms.indexOf(e.community)) / Math.max(1, comms.length); x[i] = 300 * Math.cos(a) + (i % 7) * 13; y[i] = 300 * Math.sin(a) + (i % 5) * 17; });
  const links = rels.filter((r) => idx.has(r.src) && idx.has(r.dst)).map((r) => [idx.get(r.src)!, idx.get(r.dst)!]);
  for (let step = 0; step < 300; step++) {
    const t = 1 - step / 300, fx = new Float64Array(n), fy = new Float64Array(n);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      let dx = x[i] - x[j], dy = (y[i] - y[j]) * 2.2;
      const d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2), min = (w[i] + w[j]) / 2 + 20;
      const f = (d < min ? (min - d) * 0.5 : 0) + 2500 / d2;
      dx /= d; dy /= d; fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
    }
    for (const [a, b] of links) { const dx = x[b] - x[a], dy = y[b] - y[a], d = Math.hypot(dx, dy) + 0.01, f = (d - 140) * 0.05;
      fx[a] += dx / d * f; fy[a] += dy / d * f; fx[b] -= dx / d * f; fy[b] -= dy / d * f; }
    for (let i = 0; i < n; i++) { fx[i] -= x[i] * 0.01; fy[i] -= y[i] * 0.01; x[i] += Math.max(-30, Math.min(30, fx[i])) * t; y[i] += Math.max(-30, Math.min(30, fy[i])) * t; }
  }
  return Object.fromEntries(ents.map((e, i) => [e.id, { x: x[i] - w[i] / 2, y: y[i] }]));
}

function GraphView({ kb }: { kb: KB }) {
  const [data, setData] = useState<{ ents: Entity[]; rels: Relation[]; comms: Community[] } | null>(null);
  const [q, setQ] = useState("");
  const [pick, setPick] = useState<Entity | null>(null);
  useEffect(() => { Promise.all([byIndex("entities", "kbId", kb.id), byIndex("relations", "kbId", kb.id), byIndex("communities", "kbId", kb.id)])
    .then(([ents, rels, comms]) => setData({ ents, rels, comms: comms.sort((a, b) => b.size - a.size) })); }, [kb.id, kb.builtAt]);
  const shown = useMemo(() => {
    if (!data) return null;
    let ents = [...data.ents].sort((a, b) => b.degree - a.degree);
    if (q.trim()) {
      const hit = new Set(ents.filter((e) => e.norm.includes(q.trim().toLowerCase())).map((e) => e.id));
      for (const r of data.rels) if (hit.has(r.src) || hit.has(r.dst)) { hit.add(r.src); hit.add(r.dst); }
      ents = ents.filter((e) => hit.has(e.id));
    }
    ents = ents.slice(0, 80);
    const ids = new Set(ents.map((e) => e.id));
    const merged = new Map<string, Relation>();
    for (const r of data.rels) if (ids.has(r.src) && ids.has(r.dst)) {
      const k = r.src + r.dst + r.relation; const m = merged.get(k);
      merged.set(k, m ? { ...m, weight: m.weight + r.weight } : r);
    }
    const rels = [...merged.values()];
    const pos = forceLayout(ents, rels);
    const nodes: Node[] = ents.map((e) => ({ id: e.id, position: pos[e.id], data: { label: e.name }, style: { background: COLORS[e.community % COLORS.length],
      color: "#fff", border: 0, borderRadius: 14, fontSize: 12, padding: "4px 9px", width: "auto", opacity: 0.65 + Math.min(0.35, e.degree / 15) } }));
    const edges: Edge[] = rels.map((r) => ({ id: r.id, source: r.src, target: r.dst, label: ["mentioned_with", "related_to"].includes(r.relation) ? undefined : r.relation,
      style: { stroke: "#94a3b8", strokeWidth: Math.min(4, 0.6 + r.weight / 2) }, labelStyle: { fontSize: 10 } }));
    return { nodes, edges };
  }, [data, q]);
  if (!data || !shown) return <p className="muted">Loading…</p>;
  if (!data.ents.length) return <Empty title="No knowledge graph yet">Turn GraphRAG on under Settings, then build the index.</Empty>;
  return (
    <div className="grid2" style={{ gridTemplateColumns: "minmax(0,2fr) minmax(260px,1fr)", alignItems: "start" }}>
      <Card title={`${data.ents.length} entities · ${data.rels.length} relations`} actions={<input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter, e.g. P-101" aria-label="Filter entities" style={{ width: 190 }} />}>
        <div className="graph-view" style={{ height: 520, border: "1px solid var(--line)", borderRadius: 8 }}>
          <ReactFlow nodes={shown.nodes} edges={shown.edges} fitView fitViewOptions={{ padding: 0.15, maxZoom: 1.6 }} minZoom={0.1} nodesConnectable={false}
            proOptions={{ hideAttribution: true }} onNodeClick={(_, n) => setPick(data.ents.find((e) => e.id === n.id) || null)}>
            <Background gap={18} size={1} /><Controls showInteractive={false} /></ReactFlow>
        </div>
        {pick && <div className="notice" style={{ marginTop: 10 }}><b>{pick.name}</b> <Badge>{pick.type}</Badge> · {pick.degree} links · in {pick.chunkIds.length} passage(s)
          {pick.description && <p className="small" style={{ margin: "4px 0 0" }}>{pick.description}</p>}</div>}
      </Card>
      <Card title="Topics (communities)">
        {data.comms.length === 0 ? <p className="muted small">No topics with more than one entity.</p> : data.comms.map((c) => (
          <details key={c.id} style={{ marginBottom: 8 }}>
            <summary><span style={{ background: COLORS[c.community % COLORS.length], display: "inline-block", width: 10, height: 10, borderRadius: 5, marginRight: 6 }} />
              <b>{c.title}</b> <span className="muted small">{c.size} entities</span></summary>
            <p className="small">{c.summary}</p>
          </details>))}
        <p className="muted small">Topic summaries are generated and only help find sources; answers must cite the original passages.</p>
      </Card>
    </div>
  );
}
