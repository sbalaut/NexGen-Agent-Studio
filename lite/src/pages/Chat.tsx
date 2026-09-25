import { useEffect, useRef, useState } from "react";
import type { Nav } from "../App";
import { useData } from "../data";
import { runWorkflow, validateWorkflow } from "../lib/agents";
import { byIndex, put, remove } from "../lib/store";
import type { RunRecord, TraceStep } from "../lib/types";
import { AnswerText, Badge, Button, Empty, PageHead, SourceCard } from "../ui";

export default function ChatPage({ wid, nav }: { wid?: string; nav: Nav }) {
  const { workflows, connections, kbs, servers } = useData();
  const wf = workflows.find((w) => w.id === wid) || (!wid ? workflows[0] : undefined);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [q, setQ] = useState("");
  const [live, setLive] = useState<TraceStep[] | null>(null);
  const [pending, setPending] = useState("");
  const [hl, setHl] = useState<Record<string, number | null>>({});
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { if (wf) byIndex("runs", "workflowId", wf.id).then((r) => setRuns(r.sort((a, b) => a.createdAt - b.createdAt))); }, [wf?.id]);
  useEffect(() => { end.current?.scrollIntoView?.({ block: "end" }); }, [runs.length, live?.length]);
  if (!workflows.length) return <><PageHead title="Chat with agents" /><Empty title="No agents yet">Create one in the <button className="linklike" onClick={() => nav("agents")}>agent builder</button>.</Empty></>;
  if (!wf) return <p className="muted">Agent not found.</p>;
  const errors = validateWorkflow(wf, { connections, kbs, servers });

  async function ask() {
    const question = q.trim(); if (!question || !wf) return;
    setQ(""); setLive([]); setPending(question);
    abort.current = new AbortController();
    const rec = await runWorkflow(wf, question, { connections, kbs, servers, signal: abort.current.signal, onTrace: (s) => setLive((l) => [...(l || []), s]) });
    abort.current = null; setLive(null);
    await put("runs", rec);
    setRuns((r) => [...r, rec]);
  }
  return (
    <>
      <PageHead title="Chat with agents" actions={<>
        <select aria-label="Agent" value={wf.id} onChange={(e) => nav("chat/" + e.target.value)} style={{ width: 240 }}>
          {workflows.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}</select>
        <Button onClick={() => nav("agents/" + wf.id)}>Edit agent</Button>
        {runs.length > 0 && <Button kind="ghost" onClick={async () => { if (confirm("Clear this conversation history?")) { for (const r of runs) await remove("runs", r.id); setRuns([]); } }}>Clear history</Button>}
      </>} />
      {errors.length > 0 && <div className="notice error" style={{ marginBottom: 12 }}>This agent is not ready: {errors.join(" ")}</div>}
      <div className="stack" style={{ maxWidth: 980 }}>
        {runs.length === 0 && !live && <p className="muted">Ask {wf.name} something. Each question is answered independently; sources appear under the answer.</p>}
        {runs.map((r) => (
          <div key={r.id} className="stack" style={{ gap: 8 }}>
            <div className="bubble-q">{r.question}</div>
            <div className="bubble-a stack" style={{ gap: 10 }}>
              {r.status === "failed" || r.status === "cancelled" ? <div className="notice error">{r.error}</div> : <AnswerText text={r.answer} onCite={(n) => setHl({ ...hl, [r.id]: n })} />}
              <div className="row small muted">
                <Badge tone={r.status === "completed" ? "ok" : r.status === "unverified" ? "warn" : "error"}>{r.status === "completed" ? (wf.checkAnswers ? "checks passed" : "answered") : r.status}</Badge>
                {(r.durationMs / 1000).toFixed(1)} s · {r.usage.input + r.usage.output} tokens · {r.trace.filter((t) => t.type === "tool").length} tool call(s)
              </div>
              {r.issues.length > 0 && <div className="stack" style={{ gap: 6 }}>
                <b className="small">Not supported by the sources — check before relying on it:</b>
                {r.issues.map((i, k) => <div key={k} className={"issue" + (i.blocking ? " blocking" : "")}><code>{i.code}</code> — {i.explanation}<div className="muted">“{i.claim.slice(0, 200)}”</div></div>)}</div>}
              {r.evidence.filter((e) => new RegExp(`\\[(?:\\d+\\s*,\\s*)*${e.ref}(?:\\s*,\\s*\\d+)*\\]`).test(r.answer)).map((e) => <SourceCard key={e.ref} c={e} highlight={hl[r.id] === e.ref} />)}
              {r.trace.length > 0 && <details><summary className="small">How it worked ({r.trace.length} steps)</summary><Trace steps={r.trace} /></details>}
            </div>
          </div>))}
        {live && <div className="stack" style={{ gap: 8 }}>
          <div className="bubble-q">{pending}</div>
          <div className="bubble-a"><div className="row"><span className="dot running" /> Working…
            <Button kind="ghost" onClick={() => abort.current?.abort()}>Stop</Button></div>{live.length > 0 && <Trace steps={live} />}</div></div>}
        <div ref={end} />
        <form className="row" style={{ alignItems: "flex-end" }} onSubmit={(e) => { e.preventDefault(); ask(); }}>
          <textarea aria-label="Your question" rows={2} value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1, minWidth: 240 }}
            placeholder="Ask a question…" onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } }} />
          <Button kind="primary" disabled={!!live || !q.trim() || errors.length > 0}>Ask</Button>
        </form>
      </div>
    </>
  );
}

function Trace({ steps }: { steps: TraceStep[] }) {
  return <div className="trace" style={{ marginTop: 8 }}>{steps.map((s, i) => (
    <div key={i}><b>{s.agent}</b> · step {s.step} · {s.type === "tool" ? <>called <code>{s.tool}</code> <span className="muted">{s.arguments}</span>
      <div className="muted" style={{ whiteSpace: "pre-wrap" }}>{(s.result || "").slice(0, 240)}</div></> : "answered"}</div>))}</div>;
}
