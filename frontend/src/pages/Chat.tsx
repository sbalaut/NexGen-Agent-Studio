import { useEffect, useRef, useState } from "react";
import { api, fmtTime, followRun, post, User } from "../api";
import { AnswerText, Button, Empty, PageHead, SourceCard, Status, useAction } from "../ui";

type Turn = { runId: string; question: string; status: string; progress: string[]; result?: any };

function Answer({ t, onFeedback }: { t: Turn; onFeedback: (rating: number) => void }) {
  const [hl, setHl] = useState<number | null>(null);
  const a = t.result?.answer;
  const [rated, setRated] = useState<number | null>(null);
  return (
    <div className="bubble-a">
      {!t.result && <ul className="progress-list">{t.progress.map((p, i) => <li key={i}><span className={"dot " + (i === t.progress.length - 1 ? "running" : "completed")} />{p}</li>)}</ul>}
      {t.result && <>
        <div className="row" style={{ marginBottom: 8 }}><Status s={t.result.status} />
          {a?.review?.verdict === "pass" && <span className="small muted">checked against the sources{a.review.repaired ? " (after one correction)" : ""}</span>}
          {String(a?.review?.verdict || "").startsWith("engineer_") && <span className="small muted">engineer-reviewed</span>}</div>
        {t.result.error && <div className="notice error">{t.result.error}</div>}
        {a?.answer && <AnswerText text={a.answer} onCite={(r) => { setHl(r); document.getElementById("src-" + r)?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }} />}
        {a?.notice && <div className="notice" style={{ marginTop: 8 }}>{a.notice}</div>}
        {a?.citations?.length > 0 && <div className="stack" style={{ marginTop: 12 }}><b className="small">Sources</b>
          {a.citations.map((c: any) => <SourceCard key={c.ref} c={c} highlight={hl === c.ref} />)}</div>}
        {a?.extracts?.length > 0 && <div className="stack" style={{ marginTop: 12 }}><b className="small">Relevant extracts (quoted, not a generated answer)</b>
          {a.extracts.map((c: any) => <SourceCard key={c.ref} c={c} />)}</div>}
        {["completed", "not_found", "awaiting_review"].includes(t.result.status) && (
          <div className="row small muted" style={{ marginTop: 10 }}>Was this useful?
            <Button kind="ghost" aria-pressed={rated === 1} onClick={() => { setRated(1); onFeedback(1); }}>Yes</Button>
            <Button kind="ghost" aria-pressed={rated === -1} onClick={() => { setRated(-1); onFeedback(-1); }}>No</Button>
            {rated !== null && <span>Thanks — ratings help engineers see weak spots; they are never used for training on their own.</span>}
          </div>)}
      </>}
    </div>
  );
}

export default function ChatPage({ user, assistantId }: { user: User; assistantId?: string }) {
  const [assistants, setAssistants] = useState<any[] | null>(null);
  const [current, setCurrent] = useState<string | undefined>(assistantId);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [history, setHistory] = useState<any[]>([]);
  const [q, setQ] = useState("");
  const { busy, run } = useAction();
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    api<any[]>("/assistants").then((a) => { setAssistants(a); if (!current && a.length) setCurrent(a[0].id); });
    api<any[]>("/me/history").then(setHistory);
  }, []);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [turns]);

  async function ask() {
    if (!current || !q.trim()) return;
    const question = q.trim();
    const r = await run(() => post(`/assistants/${current}/ask`, { question }));
    if (!r) return;
    setQ("");
    const turn: Turn = { runId: r.run_id, question, status: "queued", progress: ["Waiting to start…"] };
    setTurns((t) => [...t, turn]);
    const upd = (f: (t: Turn) => Turn) => setTurns((ts) => ts.map((x) => (x.runId === r.run_id ? f(x) : x)));
    followRun(r.run_id, (type, data) => {
      if (type === "node" && data.status === "running") {
        const label: Record<string, string> = { retrieval: "Searching your documents…", prompt: "Preparing sources…", llm: "Drafting an answer…",
          review: "Checking the answer against the sources…", output: "Finishing…", condition: "Deciding next step…", input: "Reading the question…" };
        upd((t) => ({ ...t, progress: [...t.progress.slice(-3), label[data.node_type] || "Working…"] }));
      }
      if (type === "final") api(`/runs/${r.run_id}`).then((res) => upd((t) => ({ ...t, status: res.status, result: res })));
    });
  }
  const asst = assistants?.find((a) => a.id === current);
  return (
    <>
      <PageHead title="Assistants" sub="Answers come only from documents you are allowed to read. Anything the checks cannot confirm goes to an engineer first." />
      {assistants === null ? <p className="muted">Loading…</p> : assistants.length === 0 ? (
        <Empty title="No assistants shared with you yet">A project owner publishes assistants and adds you to the project.</Empty>
      ) : (
        <div className="chat">
          <aside className="chat-list" aria-label="Assistants and history">
            {assistants.map((a) => <button key={a.id} className={"chat-item" + (a.id === current ? " active" : "")} onClick={() => { setCurrent(a.id); setTurns([]); }}>
              <b>{a.name}</b><div className="small muted">{a.project_name}</div></button>)}
            <div className="nav-group">Your recent questions</div>
            {history.filter((h) => h.assistant_id === current).slice(0, 15).map((h) => (
              <button key={h.id} className="chat-item small" onClick={() => setTurns((t) => t.some((x) => x.runId === h.id) ? t :
                [...t, { runId: h.id, question: h.question, status: h.status, progress: [], result: h }])}>
                {h.question.slice(0, 70)}<div className="row" style={{ marginTop: 3 }}><Status s={h.status} /><span className="muted">{fmtTime(h.created_at)}</span></div></button>))}
          </aside>
          <section className="stack" aria-label="Conversation">
            <h2>{asst?.name}</h2>
            <div className="thread">
              {turns.length === 0 && <Empty title="Ask a question">For example: "What is the design discharge pressure of the feed pump?" You can ask in English or Hindi.</Empty>}
              {turns.map((t) => <div key={t.runId} className="stack"><div className="bubble-q">{t.question}</div>
                <Answer t={t} onFeedback={(rating) => post(`/runs/${t.runId}/feedback`, { rating }).catch(() => {})} /></div>)}
              <div ref={end} />
            </div>
            <div className="composer">
              <textarea value={q} onChange={(e) => setQ(e.target.value)} placeholder={`Ask ${asst?.name || "the assistant"}…`} aria-label="Your question"
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } }} maxLength={4000} />
              <Button kind="primary" onClick={ask} disabled={busy || !q.trim()}>Ask</Button>
            </div>
            <p className="muted small">Signed in as {user.display_name}. Not for plant control: the assistant never operates equipment.</p>
          </section>
        </div>
      )}
    </>
  );
}
