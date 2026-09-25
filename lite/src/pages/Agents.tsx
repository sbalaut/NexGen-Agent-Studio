import { useEffect, useMemo, useState } from "react";
import { Background, Edge, MarkerType, Node, ReactFlow } from "@xyflow/react";
import type { Nav } from "../App";
import { ModelInput, SUGGESTED, useData } from "../data";
import { newAgent, newWorkflow, validateWorkflow } from "../lib/agents";
import { PROVIDER_LABEL } from "../lib/providers";
import { newId, put, remove } from "../lib/store";
import type { AgentSpec, Workflow } from "../lib/types";
import { Badge, Button, Card, Empty, Field, Modal, PageHead, useToast } from "../ui";

export default function AgentsPage({ wid, nav }: { wid?: string; nav: Nav }) {
  const { workflows, connections, reload } = useData();
  const [create, setCreate] = useState(false);
  const toast = useToast();
  const wf = workflows.find((w) => w.id === wid);
  if (wid && wf) return <Editor key={wf.id} initial={wf} nav={nav} />;
  return (
    <>
      <PageHead title="Agent builder" sub="Build a tool-using agent, or a team where a leader delegates to specialist agents. No code needed."
        actions={<>
          <label className="btn" style={{ cursor: "pointer" }}>Import agent…<input type="file" hidden accept=".json,application/json" onChange={async (e) => {
            const f = e.target.files?.[0]; e.target.value = ""; if (!f) return;
            try {
              const data = JSON.parse(await f.text());
              if (data?.format !== "nexagent-lite-agent" || !data.agent?.leader) throw new Error("Not a NexAgent Lite agent file.");
              const w: Workflow = { ...data.agent, id: newId(), name: data.agent.name + " (imported)", createdAt: Date.now(), updatedAt: Date.now() };
              await put("workflows", w); await reload(); toast("ok", "Imported — pick your own model connections, then save."); nav(`agents/${w.id}`);
            } catch (err: any) { toast("error", err.message); }
          }} /></label>
          <Button kind="primary" onClick={() => setCreate(true)}>New agent</Button></>} />
      {workflows.length === 0 ? <Empty title="No agents yet">Create an agent, give it knowledge bases and MCP tools, then chat with it.</Empty> :
        <div className="grid3">{workflows.map((w) => (
          <button key={w.id} className="tile" onClick={() => nav(`agents/${w.id}`)}>
            <span className="row between"><span className="tile-title">{w.name}</span><Badge tone={w.kind === "team" ? "info" : "neutral"}>{w.kind === "team" ? `team of ${w.members.length + 1}` : "agent"}</Badge></span>
            <span className="small muted">{w.description || w.leader.instructions.slice(0, 90)}</span>
          </button>))}</div>}
      {create && (
        <Modal title="New agent" onClose={() => setCreate(false)}>
          <p className="small muted">Pick a starting point. You can change everything afterwards.</p>
          <div className="stack">
            {([["agent", "Single agent", "One model that can search your knowledge bases and call MCP tools."],
               ["team", "Agent team", "A leader agent that splits the task and delegates to specialist member agents."]] as const).map(([k, t, d]) => (
              <button key={k} className="tile" disabled={!connections.length} onClick={async () => {
                const c = connections[0];
                const w = newWorkflow(k, c?.id || "", c ? SUGGESTED[c.kind]?.chat || "" : "");
                await put("workflows", w); await reload(); setCreate(false); nav(`agents/${w.id}`);
              }}><span className="tile-title">{t}</span><span className="small muted">{d}</span></button>))}
            {!connections.length && <div className="notice warn">Add an API key first (API keys &amp; data).</div>}
          </div>
        </Modal>)}
    </>
  );
}

function Editor({ initial, nav }: { initial: Workflow; nav: Nav }) {
  const { connections, kbs, servers, reload } = useData();
  const [wf, setWf] = useState<Workflow>(initial);
  const [dirty, setDirty] = useState(false);
  const [sel, setSel] = useState<number>(-1);          // -1 = leader, else member index
  const toast = useToast();
  const errors = useMemo(() => validateWorkflow(wf, { connections, kbs, servers }), [wf, connections, kbs, servers]);
  const change = (p: Partial<Workflow>) => { setWf({ ...wf, ...p }); setDirty(true); };
  const setSpec = (i: number, p: Partial<AgentSpec>) => i < 0 ? change({ leader: { ...wf.leader, ...p } })
    : change({ members: wf.members.map((m, k) => (k === i ? { ...m, ...p } : m)) });
  async function save() { await put("workflows", { ...wf, updatedAt: Date.now() }); await reload(); setDirty(false); toast("ok", "Saved"); }
  useEffect(() => {
    const h = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault(); };
    window.addEventListener("beforeunload", h); return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);
  const spec = sel < 0 ? wf.leader : wf.members[sel];
  return (
    <>
      <div className="crumbs"><button onClick={() => nav("agents")}>Agents</button> / {wf.name}</div>
      <PageHead title={<span className="row">{wf.name}{dirty && <Badge>unsaved</Badge>}{errors.length ? <Badge tone="error">{errors.length} problem(s)</Badge> : <Badge tone="ok">ready</Badge>}</span>}
        actions={<>
          <Button kind="primary" disabled={!dirty} onClick={save}>Save</Button>
          <Button disabled={dirty || errors.length > 0} title={dirty ? "Save first" : ""} onClick={() => nav(`chat/${wf.id}`)}>Chat with it →</Button>
          <Button onClick={() => { const blob = new Blob([JSON.stringify({ format: "nexagent-lite-agent", agent: wf }, null, 1)], { type: "application/json" });
            const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = wf.name.replace(/\W+/g, "-") + ".agent.json"; a.click(); }}>Download</Button>
          <Button kind="danger" onClick={async () => { if (confirm(`Delete ${wf.name}?`)) { await remove("workflows", wf.id); await reload(); nav("agents"); } }}>Delete</Button>
        </>} />
      {errors.length > 0 && <div className="notice error" style={{ marginBottom: 12 }}><b>Fix before chatting:</b>
        <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{errors.map((e) => <li key={e}>{e}</li>)}</ul></div>}
      <div className="grid2" style={{ gridTemplateColumns: "minmax(0,1fr) minmax(0,1.2fr)", alignItems: "start" }}>
        <div className="stack">
          <Card title="About">
            <Field label="Name"><input value={wf.name} maxLength={80} onChange={(e) => change({ name: e.target.value })} /></Field>
            <Field label="Description"><textarea rows={2} value={wf.description} onChange={(e) => change({ description: e.target.value })} /></Field>
            <label className="row small" style={{ flexWrap: "nowrap", alignItems: "flex-start" }}><input type="checkbox" checked={wf.checkAnswers} onChange={(e) => change({ checkAnswers: e.target.checked })} />
              Check answers against the sources (citations, numbers and equipment tags) and flag anything unsupported</label>
          </Card>
          <Card title="How it fits together">
            <TeamDiagram wf={wf} selected={sel} onSelect={setSel} />
            {wf.kind === "team" && <div className="row" style={{ marginTop: 10 }}>
              <Button disabled={wf.members.length >= 6} onClick={() => { change({ members: [...wf.members, newAgent({ name: "member" + (wf.members.length + 1), role: "", instructions: "" })] }); setSel(wf.members.length); }}>Add member</Button>
              <span className="muted small">Click an agent in the diagram to edit it.</span></div>}
          </Card>
        </div>
        <Card title={sel < 0 ? (wf.kind === "team" ? "Team leader" : "Agent") : `Member: ${spec.name}`}
          actions={sel >= 0 && <Button kind="danger" disabled={wf.members.length <= 1} onClick={() => { change({ members: wf.members.filter((_, k) => k !== sel) }); setSel(-1); }}>Remove member</Button>}>
          <AgentForm spec={spec} member={sel >= 0} onChange={(p) => setSpec(sel, p)} />
        </Card>
      </div>
    </>
  );
}

function AgentForm({ spec, member, onChange }: { spec: AgentSpec; member: boolean; onChange: (p: Partial<AgentSpec>) => void }) {
  const { connections, kbs, servers } = useData();
  const conn = connections.find((c) => c.id === spec.connection);
  const tools = servers.flatMap((s) => s.tools.filter((t) => t.enabled).map((t) => ({ id: `${s.id}/${t.name}`, server: s.name, ...t })));
  const toggle = (list: string[], v: string, on: boolean) => (on ? [...list, v] : list.filter((x) => x !== v));
  return <>
    <div className="grid2" style={{ gap: 8 }}>
      <Field label="Name" hint="letters, digits, _ or -"><input value={spec.name} onChange={(e) => onChange({ name: e.target.value.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 32) })} /></Field>
      {member && <Field label="Role (what the leader sees)"><input value={spec.role} onChange={(e) => onChange({ role: e.target.value })} placeholder="e.g. Checks prices on the web" /></Field>}
    </div>
    <Field label="Instructions"><textarea rows={5} value={spec.instructions} onChange={(e) => onChange({ instructions: e.target.value })} /></Field>
    <div className="grid2" style={{ gap: 8 }}>
      <Field label="Model connection"><select value={spec.connection} onChange={(e) => { const c = connections.find((x) => x.id === e.target.value);
        onChange({ connection: e.target.value, model: c ? SUGGESTED[c.kind]?.chat || "" : "" }); }}>
        {member ? <option value="">Same as leader</option> : <option value="">Choose…</option>}
        {connections.map((c) => <option key={c.id} value={c.id}>{c.name} · {PROVIDER_LABEL[c.kind]}</option>)}</select></Field>
      <Field label="Model"><ModelInput conn={conn} kind="chat" value={spec.model} onChange={(m) => onChange({ model: m })} placeholder={member && !spec.connection ? "Same as leader" : "e.g. gpt-4.1-mini"} /></Field>
    </div>
    <div className="grid2" style={{ gap: 8 }}>
      <Field label="Max tool steps"><input type="number" min={1} max={15} value={spec.maxSteps} onChange={(e) => onChange({ maxSteps: parseInt(e.target.value || "6") })} /></Field>
      <Field label="Temperature"><input type="number" min={0} max={1.5} step={0.1} value={spec.temperature} onChange={(e) => onChange({ temperature: parseFloat(e.target.value || "0") })} /></Field>
    </div>
    <div className="field"><span className="field-label">Knowledge bases it may search</span>
      {kbs.length === 0 ? <span className="muted small">None yet — create one under Knowledge &amp; GraphRAG.</span> :
        <div className="checks" style={{ flexDirection: "column" }}>{kbs.map((k) => <label key={k.id}><input type="checkbox" checked={spec.kbIds.includes(k.id)}
          onChange={(e) => onChange({ kbIds: toggle(spec.kbIds, k.id, e.target.checked) })} />{k.name} {k.status !== "ready" && <span className="muted small">({k.status})</span>}</label>)}</div>}</div>
    {spec.kbIds.length > 0 && <Field label="Knowledge graph (GraphRAG)" hint="Local follows linked equipment/topics; global adds topic summaries for broad questions.">
      <select value={spec.graph} onChange={(e) => onChange({ graph: e.target.value as AgentSpec["graph"] })}>
        <option value="off">Off</option><option value="local">Local</option><option value="global">Global</option><option value="both">Both</option></select></Field>}
    <div className="field"><span className="field-label">MCP tools</span>
      {tools.length === 0 ? <span className="muted small">No enabled MCP tools — add a server under MCP tools.</span> :
        <div className="checks" style={{ flexDirection: "column" }}>{tools.map((t) => <label key={t.id} title={t.description}><input type="checkbox" checked={spec.mcpTools.includes(t.id)}
          onChange={(e) => onChange({ mcpTools: toggle(spec.mcpTools, t.id, e.target.checked) })} /><span><b>{t.server}</b> · {t.name}</span></label>)}</div>}</div>
  </>;
}

function TeamDiagram({ wf, selected, onSelect }: { wf: Workflow; selected: number; onSelect: (i: number) => void }) {
  const { kbs, servers } = useData();
  const { nodes, edges } = useMemo(() => {
    const specs = [wf.leader, ...(wf.kind === "team" ? wf.members : [])];
    const nodes: Node[] = [{ id: "q", position: { x: 0, y: 0 }, data: { label: "Your question" }, style: pill("#475569") }];
    const edges: Edge[] = [];
    const arrow = { markerEnd: { type: MarkerType.ArrowClosed }, style: { stroke: "#94a3b8" } };
    const resources = new Map<string, string>();
    specs.forEach((s, i) => {
      const id = "a" + i;
      const x = i === 0 ? 0 : (i - 1 - (specs.length - 2) / 2) * 190;
      nodes.push({ id, position: { x, y: i === 0 ? 90 : 190 }, data: { label: (i === 0 && wf.kind === "team" ? "Leader: " : "") + s.name },
        style: { ...pill(i === 0 ? "#9333ea" : "#be185d"), outline: selected === i - 1 ? "3px solid #0f5c63" : undefined, cursor: "pointer" } });
      edges.push({ id: "e" + id, source: i === 0 ? "q" : "a0", target: id, ...arrow, label: i === 0 ? undefined : "delegates" });
      for (const k of s.kbIds) resources.set("kb:" + k, "📚 " + (kbs.find((b) => b.id === k)?.name || "?"));
      for (const t of s.mcpTools) resources.set("t:" + t, "🔧 " + t.slice(t.indexOf("/") + 1) + " (" + (servers.find((v) => v.id === t.slice(0, t.indexOf("/")))?.name || "?") + ")");
    });
    const res = [...resources.entries()];
    res.forEach(([rid, label], j) => nodes.push({ id: rid, position: { x: (j - (res.length - 1) / 2) * 170, y: wf.kind === "team" ? 300 : 200 }, data: { label }, style: pill("#0f766e", true) }));
    specs.forEach((s, i) => {
      for (const k of s.kbIds) edges.push({ id: `r${i}${k}`, source: "a" + i, target: "kb:" + k, style: { stroke: "#cbd5e1", strokeDasharray: "4 3" } });
      for (const t of s.mcpTools) edges.push({ id: `r${i}${t}`, source: "a" + i, target: "t:" + t, style: { stroke: "#cbd5e1", strokeDasharray: "4 3" } });
    });
    return { nodes, edges };
  }, [wf, selected, kbs, servers]);
  return <div className="team-diagram"><ReactFlow key={nodes.length + ":" + edges.length} nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.2, maxZoom: 1.2 }} nodesDraggable={false}
    nodesConnectable={false} proOptions={{ hideAttribution: true }} onNodeClick={(_, n) => n.id.startsWith("a") && onSelect(parseInt(n.id.slice(1)) - 1)}>
    <Background gap={18} size={1} /></ReactFlow></div>;
}

const pill = (bg: string, light = false): React.CSSProperties => ({ background: light ? "var(--panel)" : bg, color: light ? bg : "#fff", border: `1.5px solid ${bg}`,
  borderRadius: 16, fontSize: 12, padding: "5px 10px", width: "auto", minWidth: 90, textAlign: "center" });
