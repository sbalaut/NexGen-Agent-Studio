import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  addEdge, Background, Connection, Controls, Edge, Handle, MiniMap, Node, NodeProps, Position, ReactFlow,
  ReactFlowProvider, useEdgesState, useNodesState, useReactFlow,
} from "@xyflow/react";
import { api, followRun, hasFeature, KB, post } from "../api";
import { modelsFor } from "../models";
import type { Nav } from "../App";
import { AnswerText, Badge, Button, Field, PageHead, SourceCard, Status, Tabs, useAction, useToast } from "../ui";

type Catalog = { nodes: Record<string, any>; condition_rules: Record<string, string>; section_types: string[] };
const ICON: Record<string, [string, string]> = {
  input: ["Q", "#475569"], retrieval: ["⌕", "#1d7a4a"], prompt: ["¶", "#7c3aed"], llm: ["✦", "#c2410c"],
  review: ["✓", "#0f5c63"], condition: ["◇", "#a16207"], output: ["→", "#235e9c"],
  agent: ["◎", "#9333ea"], team: ["⚇", "#be185d"], final: ["⇥", "#0369a1"],
};
const PORT_COLOR: Record<string, string> = { text: "#2563eb", evidence: "#16a34a", prompt: "#7c3aed", draft: "#ea580c",
  reviewed: "#0f5c63", any: "#64748b", same: "#64748b" };
const SECTION_LABEL: Record<string, string> = { process_description: "Process description", startup: "Startup", shutdown: "Shutdown",
  interlock: "Interlock / trip", troubleshooting: "Troubleshooting", equipment_spec: "Equipment spec", other: "Other" };

let CATALOG: Catalog | null = null;

function summary(type: string, d: any, kbNames: Record<string, string>): string {
  switch (type) {
    case "retrieval": return `${(d.kb_ids || []).map((k: string) => kbNames[k] || "?").join(", ") || "no knowledge base"} · top ${d.top_k} · ${d.scope} scope${d.graph && d.graph !== "off" ? ` · graph ${d.graph}` : ""}`;
    case "agent": return `${d.name || "Agent"} · ${d.connection}/${d.model || "default"} · ${(d.kb_ids || []).length} KB · ${(d.mcp_tools || []).length} tools`;
    case "team": return `${(d.members || []).length} member(s): ${(d.members || []).map((m: any) => m.name).join(", ")}`;
    case "llm": return `${d.model || "default model"} · temp ${d.temperature}`;
    case "review": return `${d.model_review ? "model + " : ""}built-in checks · repair ${d.allow_repair ? "once" : "off"} · fallback: ${d.fallback}`;
    case "condition": return `${d.rule}${d.rule === "has_evidence" ? "" : ` = ${d.argument}`}`;
    case "prompt": return (d.template || "").slice(0, 60).replace(/\n/g, " ") + "…";
    default: return CATALOG?.nodes[type]?.description || "";
  }
}

function NxNode({ id, type, data, selected }: NodeProps) {
  const spec = CATALOG?.nodes[type as string];
  if (!spec) return null;
  const [icon, color] = ICON[type as string] || ["•", "#555"];
  const ins = Object.entries(spec.inputs) as [string, string][];
  const outs = Object.entries(spec.outputs) as [string, string][];
  const d = data as any;
  return (
    <div className={`nx-node ${selected ? "selected" : ""} ${d.__status ? "status-" + d.__status : ""}`} aria-label={`${spec.label} node ${id}`}>
      <div className="nx-node-head"><span className="nx-node-icon" style={{ background: color }}>{icon}</span>{spec.label}
        {d.__status && <span style={{ marginLeft: "auto" }}><Status s={d.__status} /></span>}</div>
      <div className="nx-node-body">
        {ins.map(([p, t]) => (
          <div className="nx-port" key={"i" + p}>
            <Handle type="target" position={Position.Left} id={p} style={{ background: PORT_COLOR[t], top: "auto", position: "relative", left: -16, transform: "none" }} />
            <span style={{ marginRight: "auto" }}>{p}{spec.optional_inputs?.includes(p) ? " (optional)" : ""}</span><span style={{ color: PORT_COLOR[t] }}>{t}</span>
          </div>))}
        {outs.map(([p, t]) => (
          <div className="nx-port" key={"o" + p} style={{ justifyContent: "flex-end" }}>
            <span style={{ color: PORT_COLOR[t] }}>{t === "same" ? "pass-through" : t}</span><span>{p}</span>
            <Handle type="source" position={Position.Right} id={p} style={{ background: PORT_COLOR[t], position: "relative", right: -16, top: "auto", transform: "none" }} />
          </div>))}
        <div style={{ marginTop: 4, fontSize: 11.5, overflowWrap: "anywhere" }}>{d.__summary}</div>
        {d.__nodeSummary && <div style={{ marginTop: 4, fontSize: 11.5, color: "var(--ink-2)", overflowWrap: "anywhere" }}>{d.__nodeSummary}</div>}
      </div>
    </div>
  );
}

export default function BuilderPage(props: { wid: string; nav: Nav }) {
  return <ReactFlowProvider><Builder {...props} /></ReactFlowProvider>;
}

function clean(nodes: Node[], edges: Edge[]) {
  return {
    nodes: nodes.map((n) => ({ id: n.id, type: n.type, position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      data: Object.fromEntries(Object.entries(n.data || {}).filter(([k]) => !k.startsWith("__"))) })),
    edges: edges.map((e) => ({ id: e.id, source: e.source, sourceHandle: e.sourceHandle, target: e.target, targetHandle: e.targetHandle })),
  };
}

function Builder({ wid, nav }: { wid: string; nav: Nav }) {
  const [cat, setCat] = useState<Catalog | null>(CATALOG);
  const [wf, setWf] = useState<any>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [view, setView] = useState<"canvas" | "list">("canvas");
  const [kbs, setKbs] = useState<KB[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [conns, setConns] = useState<string[]>(["local-ollama"]);
  const [mcp, setMcp] = useState<McpServer[]>([]);
  const [search, setSearch] = useState("");
  const history = useRef<{ past: any[]; future: any[] }>({ past: [], future: [] });
  const { screenToFlowPosition } = useReactFlow();
  const { busy, run } = useAction();
  const toast = useToast();

  const kbNames = useMemo(() => Object.fromEntries(kbs.map((k) => [k.id, k.name])), [kbs]);

  async function load(version?: number) {
    const w = await api(`/workflows/${wid}${version ? `?version=${version}` : ""}`);
    setWf(w);
    setNodes(w.version.graph.nodes.map((n: any) => ({ ...n, data: { ...n.data } })));
    setEdges(w.version.graph.edges.map((e: any) => ({ ...e, animated: false })));
    setErrors(w.version.errors);
    setDirty(false);
    history.current = { past: [], future: [] };
    return w;
  }
  useEffect(() => {
    (async () => {
      if (!CATALOG) { CATALOG = await api<Catalog>("/builder/catalog"); setCat(CATALOG); }
      const w = await load();
      api<KB[]>(`/projects/${w.workflow.project_id}/knowledge-bases`).then(setKbs);
      const c = await api("/models/connections");
      const names: string[] = c.connections.filter((x: any) => x.enabled).map((x: any) => x.name);
      setConns(names);
      const avail = names.includes("local-ollama") ? await api("/models/available").catch(() => ({ models: [] })) : { models: [] };
      setModels([...(avail.models || []), ...c.promoted.map((p: any) => "promoted:" + p.alias)]);
      if (hasFeature("mcp")) api("/mcp/servers").then((r) => setMcp(r.servers.filter((x: McpServer) => x.enabled))).catch(() => {});
    })().catch((e) => toast("error", e.message));
  }, [wid]);

  // decorate nodes with summaries for display
  const shown = useMemo(() => nodes.map((n) => ({ ...n, data: { ...n.data, __summary: summary(n.type!, n.data, kbNames) } })), [nodes, kbNames]);

  const snapshot = () => ({ nodes: JSON.parse(JSON.stringify(nodes)), edges: JSON.parse(JSON.stringify(edges)) });
  const commit = () => { history.current.past.push(snapshot()); history.current.future = []; if (history.current.past.length > 80) history.current.past.shift(); setDirty(true); };
  const undo = () => { const s = history.current.past.pop(); if (!s) return; history.current.future.push(snapshot()); setNodes(s.nodes); setEdges(s.edges); setDirty(true); };
  const redo = () => { const s = history.current.future.pop(); if (!s) return; history.current.past.push(snapshot()); setNodes(s.nodes); setEdges(s.edges); setDirty(true); };
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || (e.target as HTMLElement).closest("input,textarea,select")) return;
      if (e.key === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      if (e.key === "y" || (e.key === "z" && e.shiftKey)) { e.preventDefault(); redo(); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  });

  const portType = (nodeId: string, port: string, dir: "in" | "out"): string => {
    const n = nodes.find((x) => x.id === nodeId); if (!n || !cat) return "?";
    const spec = cat.nodes[n.type!];
    let t = dir === "in" ? spec.inputs[port] : spec.outputs[port];
    if (t === "same") { const inc = edges.find((e) => e.target === nodeId); t = inc ? portType(inc.source, inc.sourceHandle!, "out") : "any"; }
    return t;
  };
  const onConnect = useCallback((c: Connection) => {
    const from = portType(c.source!, c.sourceHandle!, "out"), to = portType(c.target!, c.targetHandle!, "in");
    if (to !== "any" && from !== "any" && from !== to) {
      toast("error", `Cannot connect ${from} to an input that expects ${to}.` + (to === "reviewed" ? " Answers must pass through Answer Review first." : ""));
      return;
    }
    commit();
    setEdges((eds) => addEdge({ ...c, id: "e" + Date.now().toString(36) }, eds));
  }, [nodes, edges, cat]);

  function addNode(type: string, position?: { x: number; y: number }) {
    if (!cat) return;
    commit();
    const id = type + "-" + Math.random().toString(36).slice(2, 6);
    setNodes((ns) => [...ns, { id, type, position: position || { x: 200 + ns.length * 20, y: 320 }, data: JSON.parse(JSON.stringify(cat.nodes[type].defaults)) }]);
    setSelected(id);
  }
  const updateData = (id: string, patch: any) => {
    commit();
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
  };

  async function save() {
    const r = await run(() => post(`/workflows/${wid}/versions`, { graph: clean(nodes, edges), base_version: wf.workflow.latest_version }));
    if (r) { toast(r.valid ? "ok" : "info", r.valid ? `Saved version ${r.version_no}` : `Saved version ${r.version_no} with ${r.errors.length} problem(s)`); await load(); }
  }
  async function validate() {
    const r = await run(() => post(`/workflows/${wid}/validate`, { graph: clean(nodes, edges), base_version: wf.workflow.latest_version }));
    if (r) { setErrors(r.errors); toast(r.valid ? "ok" : "error", r.valid ? "No problems found" : `${r.errors.length} problem(s) found`); }
  }

  const setNodeStatus = (nodeId: string, status: string, text?: string) =>
    setNodes((ns) => ns.map((n) => (n.id === nodeId ? { ...n, data: { ...n.data, __status: status, __nodeSummary: text } } : n)));
  const clearStatus = () => setNodes((ns) => ns.map((n) => ({ ...n, data: Object.fromEntries(Object.entries(n.data).filter(([k]) => k !== "__status" && k !== "__nodeSummary")) })));

  if (!wf || !cat) return <p className="muted">Loading builder…</p>;
  const sel = nodes.find((n) => n.id === selected);
  const readOnly = !wf.can_edit || wf.version.version_no !== wf.workflow.latest_version;
  return (
    <>
      <div className="crumbs"><button onClick={() => nav(`project/${wf.workflow.project_id}/agents`)}>Agents</button> / {wf.workflow.name}</div>
      <PageHead title={<span className="row">{wf.workflow.name}<Badge>v{wf.version.version_no}{dirty ? " · unsaved" : ""}</Badge>
        {errors.length ? <Badge tone="error">{errors.length} problem(s)</Badge> : <Badge tone="ok">valid</Badge>}</span>}
        actions={<>
          <Tabs value={view} onChange={setView} items={[["canvas", "Canvas"], ["list", "List & form view"]]} />
          <select aria-label="Version" value={wf.version.version_no} onChange={(e) => load(parseInt(e.target.value))} style={{ width: 150 }}>
            {wf.versions.map((v: any) => <option key={v.version_no} value={v.version_no}>v{v.version_no} {v.valid ? "" : "(invalid)"} · {v.username}</option>)}
          </select>
          <Button onClick={undo} disabled={readOnly || !history.current.past.length} title="Ctrl+Z">Undo</Button>
          <Button onClick={redo} disabled={readOnly || !history.current.future.length} title="Ctrl+Y">Redo</Button>
          <Button onClick={validate} disabled={busy}>Check</Button>
          {wf.version.version_no !== wf.workflow.latest_version && wf.can_edit &&
            <Button onClick={async () => { const v = wf.version; await load(); setNodes(v.graph.nodes); setEdges(v.graph.edges); setDirty(true); toast("info", `Loaded v${v.version_no} as a draft; save to restore it.`); }}>Restore this version</Button>}
          {!readOnly && <Button kind="primary" onClick={save} disabled={busy || !dirty}>Save version</Button>}
        </>} />
      {readOnly && <div className="notice warn" style={{ marginBottom: 12 }}>{wf.can_edit ? "You are viewing an older version (read-only)." : "You can view this agent but not edit it."}</div>}
      {errors.length > 0 && (
        <div className="notice error validation" style={{ marginBottom: 12 }}>
          <b>This version cannot run until these are fixed:</b><ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </div>)}
      {view === "canvas" ? (
        <div className="builder">
          <div className="palette" role="toolbar" aria-label="Node palette">
            <input placeholder="Find a node…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search nodes" />
            {Object.entries(cat.nodes).filter(([, s]) => (s.label + s.description).toLowerCase().includes(search.toLowerCase())).map(([t, s]) => (
              <button key={t} className="palette-item" draggable={!readOnly} disabled={readOnly} title={s.description}
                onDragStart={(e) => e.dataTransfer.setData("application/nx-node", t)} onClick={() => addNode(t)}>
                <span className="nx-node-icon" style={{ background: ICON[t][1] }}>{ICON[t][0]}</span>{s.label}
              </button>))}
            <span className="palette-hint">Drag or click to add · connect ports of the same colour · Backspace deletes</span>
          </div>
          <div className="canvas" onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { const t = e.dataTransfer.getData("application/nx-node"); if (t) addNode(t, screenToFlowPosition({ x: e.clientX, y: e.clientY })); }}>
            <ReactFlow nodes={shown} edges={edges} nodeTypes={NODE_TYPES}
              onNodesChange={(ch) => { if (!readOnly) { if (ch.some((c) => c.type === "remove")) commit(); onNodesChange(ch); if (ch.some((c) => c.type === "position" && !c.dragging)) setDirty(true); } else onNodesChange(ch.filter((c) => c.type === "select")); }}
              onEdgesChange={(ch) => { if (!readOnly) { if (ch.some((c) => c.type === "remove")) commit(); onEdgesChange(ch); } }}
              onConnect={readOnly ? undefined : onConnect} onNodeDragStart={() => commit()}
              onSelectionChange={({ nodes: s }) => setSelected(s[0]?.id || null)}
              nodesDraggable={!readOnly} nodesConnectable={!readOnly} deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
              fitView fitViewOptions={{ padding: 0.08, maxZoom: 1 }} minZoom={0.2} proOptions={{ hideAttribution: true }}>
              <Background gap={18} size={1} />
              <Controls showInteractive={false} />
              <MiniMap pannable zoomable style={{ height: 90 }} />
            </ReactFlow>
          </div>
          <aside className="inspector" aria-label="Node settings">
            {sel ? <NodeForm node={sel} cat={cat} kbs={kbs} models={models} conns={conns} mcp={mcp} readOnly={readOnly}
              onChange={(p) => updateData(sel.id, p)} /> :
              <div className="muted small"><h3>Settings</h3>Select a node to change its settings.</div>}
            <TestPanel wid={wid} disabled={dirty || errors.length > 0} version={wf.version.version_no} onStatus={setNodeStatus} onReset={clearStatus} />
          </aside>
        </div>
      ) : (
        <ListView nodes={nodes} edges={edges} cat={cat} kbs={kbs} models={models} conns={conns} mcp={mcp} readOnly={readOnly}
          onData={updateData} onAdd={(t: string) => addNode(t)} onRemoveNode={(id: string) => { commit(); setNodes((ns) => ns.filter((n) => n.id !== id)); setEdges((es) => es.filter((e) => e.source !== id && e.target !== id)); }}
          onConnect={(c: Connection) => onConnect(c)} onRemoveEdge={(id: string) => { commit(); setEdges((es) => es.filter((e) => e.id !== id)); }}
          test={<TestPanel wid={wid} disabled={dirty || errors.length > 0} version={wf.version.version_no} onStatus={() => {}} onReset={() => {}} />} />
      )}
    </>
  );
}

const NODE_TYPES = Object.fromEntries(Object.keys(ICON).map((t) => [t, NxNode]));

type McpServer = { id: string; name: string; enabled: number; shared: boolean; tools: { id: string; name: string; description: string; enabled: number }[] };

function ConnModel({ d, conns, models, onChange, allowInherit = false }: { d: any; conns: string[]; models: string[];
  onChange: (p: any) => void; allowInherit?: boolean }) {
  const [list, setList] = useState<string[]>([]);
  const effective = d.connection || "";
  useEffect(() => { let live = true; modelsFor(effective).then((m) => live && setList(m)); return () => { live = false; }; }, [effective]);
  const options = [...new Set([...list, ...(effective === "local-ollama" ? models : []), d.model].filter(Boolean))];
  const listId = "models" + useId().replace(/:/g, "");
  return (
    <div className="grid2" style={{ gap: 8 }}>
      <Field label="Connection" hint={allowInherit ? "Empty = same as the team leader" : "Your own API keys appear here too"}>
        <select value={d.connection || ""} onChange={(e) => onChange({ connection: e.target.value })}>
          {allowInherit && <option value="">Same as leader</option>}
          {[...new Set([...conns, d.connection].filter(Boolean))].map((c) => <option key={c} value={c}>{c}{conns.includes(c) ? "" : " (unavailable)"}</option>)}
        </select></Field>
      <Field label="Model" hint="Pick from the list or type a model name">
        <input list={listId} value={d.model || ""} placeholder={allowInherit ? "Same as leader" : "Default"} onChange={(e) => onChange({ model: e.target.value })} />
        <datalist id={listId}>{options.map((m) => <option key={m} value={m} />)}</datalist></Field>
    </div>
  );
}

function KbChecks({ kbs, value, onChange }: { kbs: KB[]; value: string[]; onChange: (v: string[]) => void }) {
  if (!kbs.length) return <p className="muted small">This project has no knowledge bases yet.</p>;
  return <div className="checks" style={{ flexDirection: "column" }}>{kbs.map((k) => (
    <label key={k.id}><input type="checkbox" checked={(value || []).includes(k.id)}
      onChange={(e) => onChange(e.target.checked ? [...(value || []), k.id] : (value || []).filter((x) => x !== k.id))} />{k.name}</label>))}</div>;
}

function McpChecks({ mcp, value, onChange }: { mcp: McpServer[]; value: string[]; onChange: (v: string[]) => void }) {
  if (!hasFeature("mcp")) return null;
  const tools = mcp.flatMap((s) => s.tools.filter((t) => t.enabled).map((t) => ({ ...t, server: s.name })));
  const known = new Set(tools.map((t) => t.id));
  const missing = (value || []).filter((v) => !known.has(v));
  return (
    <div className="field"><span className="field-label">MCP tools</span>
      {!tools.length && <p className="muted small">No MCP tools are enabled. Add a server under “Models, keys &amp; MCP tools”.</p>}
      <div className="checks" style={{ flexDirection: "column" }}>{tools.map((t) => (
        <label key={t.id} title={t.description}><input type="checkbox" checked={(value || []).includes(t.id)}
          onChange={(e) => onChange(e.target.checked ? [...(value || []), t.id] : (value || []).filter((x) => x !== t.id))} />
          <span><b>{t.server}</b> · {t.name}</span></label>))}</div>
      {missing.length > 0 && <div className="notice warn small">Unavailable tools selected: {missing.join(", ")}.
        <button className="linklike" type="button" onClick={() => onChange((value || []).filter((v) => known.has(v)))}>Remove them</button></div>}
    </div>
  );
}

// ------------------------------------------------------------------ settings form
function NodeForm({ node, cat, kbs, models, conns, mcp, readOnly, onChange }: { node: Node; cat: Catalog; kbs: KB[]; models: string[]; conns: string[];
  mcp: McpServer[]; readOnly: boolean; onChange: (p: any) => void }) {
  const d = { ...(cat.nodes[node.type!]?.defaults || {}), ...(node.data as any) };
  const spec = cat.nodes[node.type!];
  const num = (k: string, v: string, int = false) => onChange({ [k]: int ? parseInt(v || "0") : parseFloat(v || "0") });
  return (
    <fieldset disabled={readOnly} style={{ border: 0, padding: 0, margin: 0 }}>
      <h3>{spec.label} <span className="muted small mono">{node.id}</span></h3>
      <p className="muted small">{spec.description}</p>
      {node.type === "retrieval" && <>
        <div className="field"><span className="field-label">Knowledge bases</span>
          <div className="checks" style={{ flexDirection: "column" }}>{kbs.map((k) => (
            <label key={k.id}><input type="checkbox" checked={(d.kb_ids || []).includes(k.id)}
              onChange={(e) => onChange({ kb_ids: e.target.checked ? [...(d.kb_ids || []), k.id] : d.kb_ids.filter((x: string) => x !== k.id) })} />
              {k.name}</label>))}</div>
          <span className="field-hint">Each user only ever searches the knowledge bases they personally can read.</span></div>
        <Field label="Passages to retrieve"><input type="number" min={1} max={20} value={d.top_k} onChange={(e) => num("top_k", e.target.value, true)} /></Field>
        <Field label="Section scope" hint="Automatic: 'describe the process' questions only search process descriptions; startup/interlock content only when asked.">
          <select value={d.scope} onChange={(e) => onChange({ scope: e.target.value })}>
            <option value="auto">Automatic from the question</option><option value="fixed">Only the types below</option><option value="all">All sections</option></select></Field>
        {d.scope === "fixed" && <div className="checks" style={{ marginBottom: 12 }}>{cat.section_types.map((s) => (
          <label key={s}><input type="checkbox" checked={(d.section_types || []).includes(s)}
            onChange={(e) => onChange({ section_types: e.target.checked ? [...(d.section_types || []), s] : d.section_types.filter((x: string) => x !== s) })} />{SECTION_LABEL[s]}</label>))}</div>}
        {hasFeature("graphrag") && <Field label="Knowledge graph (GraphRAG)" hint="Needs a knowledge base indexed with a graph. Local follows linked equipment and topics; global adds community summaries for broad questions.">
          <select value={d.graph || "off"} onChange={(e) => onChange({ graph: e.target.value })}>
            <option value="off">Off — passages only</option><option value="local">Local — boost linked passages</option>
            <option value="global">Global — add topic summaries</option><option value="both">Both</option></select></Field>}
      </>}
      {node.type === "agent" && <>
        <Field label="Agent name"><input value={d.name} onChange={(e) => onChange({ name: e.target.value })} /></Field>
        <Field label="Instructions"><textarea rows={6} value={d.instructions} onChange={(e) => onChange({ instructions: e.target.value })} /></Field>
        <ConnModel d={d} conns={conns} models={models} onChange={onChange} />
        <div className="field"><span className="field-label">Knowledge bases it may search</span>
          <KbChecks kbs={kbs} value={d.kb_ids} onChange={(v) => onChange({ kb_ids: v })} /></div>
        <McpChecks mcp={mcp} value={d.mcp_tools} onChange={(v) => onChange({ mcp_tools: v })} />
        <div className="grid2" style={{ gap: 8 }}>
          <Field label="Max tool steps"><input type="number" min={1} max={15} value={d.max_steps} onChange={(e) => num("max_steps", e.target.value, true)} /></Field>
          <Field label="Temperature"><input type="number" step={0.1} min={0} max={1.5} value={d.temperature} onChange={(e) => num("temperature", e.target.value)} /></Field></div>
      </>}
      {node.type === "team" && <TeamForm d={d} kbs={kbs} models={models} conns={conns} mcp={mcp} onChange={onChange} num={num} />}
      {node.type === "final" && <div className="notice warn small">The answer is shown without Answer Review. Only projects that do not require review may use this node.</div>}
      {node.type === "prompt" && <>
        <Field label="Instructions to the model"><textarea rows={7} value={d.system} onChange={(e) => onChange({ system: e.target.value })} /></Field>
        <Field label="Message template" hint="Use {question} and {context}. Sources are inserted as numbered, fenced blocks."><textarea rows={4} value={d.template} onChange={(e) => onChange({ template: e.target.value })} /></Field>
      </>}
      {(node.type === "llm" || node.type === "review") && <>
        {node.type === "review" && <label className="row" style={{ marginBottom: 12 }}><input type="checkbox" checked={d.model_review} onChange={(e) => onChange({ model_review: e.target.checked })} />
          Also ask a review model (recommended)</label>}
        {(node.type === "llm" || d.model_review) && <>
          <Field label="Connection"><select value={d.connection} onChange={(e) => onChange({ connection: e.target.value })}>{conns.map((c) => <option key={c}>{c}</option>)}</select></Field>
          <Field label="Model" hint="Empty = administrator default. 'promoted:' models come from approved fine-tuning.">
            <select value={d.model} onChange={(e) => onChange({ model: e.target.value })}><option value="">Default</option>
              {[...new Set([...models, d.model].filter(Boolean))].map((m) => <option key={m}>{m}</option>)}</select></Field>
        </>}
        {node.type === "llm" && <div className="grid2" style={{ gap: 8 }}>
          <Field label="Temperature"><input type="number" step={0.1} min={0} max={1.5} value={d.temperature} onChange={(e) => num("temperature", e.target.value)} /></Field>
          <Field label="Max tokens"><input type="number" min={16} max={4096} value={d.max_tokens} onChange={(e) => num("max_tokens", e.target.value, true)} /></Field></div>}
        {node.type === "review" && <>
          <label className="row" style={{ marginBottom: 12 }}><input type="checkbox" checked={d.allow_repair} onChange={(e) => onChange({ allow_repair: e.target.checked })} />Allow one automatic repair attempt</label>
          <Field label="While an engineer reviews" hint="What the user sees if the answer cannot be confirmed.">
            <select value={d.fallback} onChange={(e) => onChange({ fallback: e.target.value })}>
              <option value="extract">Show relevant source extracts word-for-word</option>
              <option value="not_found">Say the answer could not be verified</option>
              <option value="human">Only say it is under review</option></select></Field>
          <div className="notice small">Built-in checks always run: citations, equipment-to-value binding, numbers & units, limits, normal vs startup/trip conditions, negation, scope and revision.</div>
        </>}
      </>}
      {node.type === "condition" && <>
        <Field label="Rule"><select value={d.rule} onChange={(e) => onChange({ rule: e.target.value, argument: e.target.value === "verdict_is" ? "pass" : "" })}>
          <option value="verdict_is">Review verdict is…</option><option value="has_evidence">Sources were found</option><option value="text_contains">Question contains…</option></select></Field>
        {d.rule === "verdict_is" && <Field label="Verdict"><select value={d.argument} onChange={(e) => onChange({ argument: e.target.value })}>
          <option value="pass">pass</option><option value="needs_human_review">needs engineer review</option><option value="revise">revise</option></select></Field>}
        {d.rule === "text_contains" && <Field label="Text"><input value={d.argument} onChange={(e) => onChange({ argument: e.target.value })} /></Field>}
        <p className="muted small">The value continues on the <b>true</b> or <b>false</b> output; nodes only on the other branch are skipped.</p>
      </>}
    </fieldset>
  );
}

function TeamForm({ d, kbs, models, conns, mcp, onChange, num }: any) {
  const members: any[] = d.members || [];
  const setMember = (i: number, p: any) => onChange({ members: members.map((m, k) => (k === i ? { ...m, ...p } : m)) });
  return <>
    <Field label="Leader instructions"><textarea rows={5} value={d.instructions} onChange={(e) => onChange({ instructions: e.target.value })} /></Field>
    <ConnModel d={d} conns={conns} models={models} onChange={onChange} />
    <div className="grid2" style={{ gap: 8 }}>
      <Field label="Max delegation steps"><input type="number" min={1} max={15} value={d.max_steps} onChange={(e) => num("max_steps", e.target.value, true)} /></Field>
      <Field label="Temperature"><input type="number" step={0.1} min={0} max={1.5} value={d.temperature} onChange={(e) => num("temperature", e.target.value)} /></Field></div>
    <h4 style={{ margin: "12px 0 6px" }}>Members ({members.length}/6)</h4>
    {members.map((m, i) => (
      <details key={i} className="member" open={members.length === 1}>
        <summary><b>{m.name || "(unnamed)"}</b> <span className="muted small">{m.role}</span></summary>
        <div style={{ paddingTop: 8 }}>
          <div className="grid2" style={{ gap: 8 }}>
            <Field label="Name" hint="letters, digits, _ or -"><input value={m.name} onChange={(e) => setMember(i, { name: e.target.value.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 32) })} /></Field>
            <Field label="Role (what the leader sees)"><input value={m.role} onChange={(e) => setMember(i, { role: e.target.value })} /></Field></div>
          <Field label="Instructions"><textarea rows={3} value={m.instructions} onChange={(e) => setMember(i, { instructions: e.target.value })} /></Field>
          <ConnModel d={m} conns={conns} models={models} allowInherit onChange={(p) => setMember(i, p)} />
          <div className="field"><span className="field-label">Knowledge bases</span>
            <KbChecks kbs={kbs} value={m.kb_ids} onChange={(v) => setMember(i, { kb_ids: v })} /></div>
          <McpChecks mcp={mcp} value={m.mcp_tools} onChange={(v) => setMember(i, { mcp_tools: v })} />
          <Button kind="danger" disabled={members.length <= 1} onClick={() => onChange({ members: members.filter((_, k) => k !== i) })}>Remove member</Button>
        </div>
      </details>))}
    <Button disabled={members.length >= 6} onClick={() => onChange({ members: [...members, { name: "member" + (members.length + 1), role: "", instructions: "",
      connection: "", model: "", kb_ids: [], mcp_tools: [] }] })}>Add member</Button>
  </>;
}

// ------------------------------------------------------------------ accessible list/form alternative
function ListView({ nodes, edges, cat, kbs, models, conns, mcp, readOnly, onData, onAdd, onRemoveNode, onConnect, onRemoveEdge, test }: any) {
  const [src, setSrc] = useState(""); const [dst, setDst] = useState(""); const [addType, setAddType] = useState("condition");
  const outs = nodes.flatMap((n: Node) => Object.keys(cat.nodes[n.type!].outputs).map((p) => `${n.id}|${p}`));
  const ins = nodes.flatMap((n: Node) => Object.keys(cat.nodes[n.type!].inputs).map((p) => `${n.id}|${p}`));
  return (
    <div className="grid2" style={{ alignItems: "start" }}>
      <div className="stack">
        {nodes.map((n: Node) => (
          <section key={n.id} className="card" aria-label={`${cat.nodes[n.type!].label} ${n.id}`}>
            <NodeForm node={n} cat={cat} kbs={kbs} models={models} conns={conns} mcp={mcp} readOnly={readOnly} onChange={(p: any) => onData(n.id, p)} />
            {!readOnly && <Button kind="danger" onClick={() => onRemoveNode(n.id)}>Remove node</Button>}
          </section>))}
        {!readOnly && <div className="row"><select value={addType} onChange={(e) => setAddType(e.target.value)} style={{ width: 220 }} aria-label="Node type to add">
          {Object.entries(cat.nodes).map(([t, s]: any) => <option key={t} value={t}>{s.label}</option>)}</select><Button onClick={() => onAdd(addType)}>Add node</Button></div>}
      </div>
      <div className="stack">
        <section className="card">
          <h3>Connections</h3>
          <table><thead><tr><th>From</th><th>To</th><th /></tr></thead><tbody>
            {edges.map((e: Edge) => (<tr key={e.id}><td className="mono">{e.source}.{e.sourceHandle}</td><td className="mono">{e.target}.{e.targetHandle}</td>
              <td>{!readOnly && <Button kind="ghost" onClick={() => onRemoveEdge(e.id)}>Remove</Button>}</td></tr>))}</tbody></table>
          {!readOnly && <div className="row" style={{ marginTop: 10 }}>
            <select value={src} onChange={(e) => setSrc(e.target.value)} aria-label="Connect from" style={{ width: 200 }}><option value="">From…</option>{outs.map((o: string) => <option key={o} value={o}>{o.replace("|", ".")}</option>)}</select>
            <select value={dst} onChange={(e) => setDst(e.target.value)} aria-label="Connect to" style={{ width: 200 }}><option value="">To…</option>{ins.map((o: string) => <option key={o} value={o}>{o.replace("|", ".")}</option>)}</select>
            <Button disabled={!src || !dst} onClick={() => { const [s, sh] = src.split("|"); const [t, th] = dst.split("|"); onConnect({ source: s, sourceHandle: sh, target: t, targetHandle: th }); }}>Connect</Button>
          </div>}
        </section>
        <section className="card">{test}</section>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ test run
function TestPanel({ wid, disabled, version, onStatus, onReset }: { wid: string; disabled: boolean; version: number;
  onStatus: (id: string, s: string, t?: string) => void; onReset: () => void }) {
  const [q, setQ] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<any[]>([]);
  const [result, setResult] = useState<any>(null);
  const [hl, setHl] = useState<number | null>(null);
  const { busy, run } = useAction();
  useEffect(() => {
    if (!runId) return;
    return followRun(runId, (type, data) => {
      if (type === "node") { setEvents((e) => [...e.filter((x) => !(x.node_id === data.node_id)), data]); onStatus(data.node_id, data.status, data.summary || data.error); }
      if (type === "final") api(`/runs/${runId}`).then(setResult);
    });
  }, [runId]);
  return (
    <div className="run-panel" style={{ marginTop: 16, paddingLeft: 0, paddingRight: 0 }}>
      <h3>Test this agent</h3>
      <p className="muted small">Runs saved version v{version} for real, with your own access rights. Progress appears on the canvas.</p>
      <textarea value={q} onChange={(e) => setQ(e.target.value)} rows={3} placeholder="Ask a question as a user would…" aria-label="Test question" />
      <div className="row" style={{ marginTop: 8 }}>
        <Button kind="primary" disabled={disabled || busy || !q.trim()} title={disabled ? "Save a valid version first" : ""}
          onClick={async () => { onReset(); setEvents([]); setResult(null);
            const r = await run(() => post(`/workflows/${wid}/test?version=${version}`, { question: q })); if (r) setRunId(r.run_id); }}>Run test</Button>
        {runId && !result && <Button onClick={() => post(`/runs/${runId}/cancel`).catch(() => {})}>Cancel</Button>}
      </div>
      {events.length > 0 && <ul className="progress-list" style={{ marginTop: 12 }}>{events.map((e) => (
        <li key={e.node_id}><span className={"dot " + e.status} /><b>{e.node_type}</b><span className="muted small">{e.summary || e.error}{e.duration_ms != null ? ` · ${e.duration_ms} ms` : ""}</span></li>))}</ul>}
      {result && (
        <div className="stack" style={{ marginTop: 12 }}>
          <div className="row"><Status s={result.status} />{result.error && <span className="small" style={{ color: "var(--err)" }}>{result.error}</span>}</div>
          {result.answer?.answer && <AnswerText text={result.answer.answer} onCite={setHl} />}
          {result.answer?.notice && <div className="notice">{result.answer.notice}</div>}
          {(result.answer?.citations || []).map((c: any) => <SourceCard key={c.ref} c={c} highlight={hl === c.ref} />)}
          {(result.answer?.extracts || []).map((c: any) => <SourceCard key={"x" + c.ref} c={c} />)}
          {result.nodes?.filter((n: any) => n.node_type === "review" && n.detail).map((n: any) => (
            <details key={n.node_id}><summary>Review details (builders only)</summary>
              <p className="small"><b>Draft:</b> {n.detail.draft}</p>
              {n.detail.review.issues.map((i: any, k: number) => <div key={k} className={"issue" + (i.blocking ? " blocking" : "")} style={{ marginBottom: 6 }}>
                <code>{i.code}</code> ({i.source}) — {i.explanation}</div>)}
              {n.detail.review.suggested_action && <p className="small">Suggested: {n.detail.review.suggested_action}</p>}
            </details>))}
        </div>
      )}
    </div>
  );
}
