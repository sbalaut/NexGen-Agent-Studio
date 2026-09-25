import { useEffect, useState } from "react";
import { api, fmtTime, post } from "../api";
import type { Nav } from "../App";
import { AnswerText, Badge, Button, Card, Classification, Empty, Field, PageHead, SourceCard, Status, Tabs, useAction } from "../ui";

export default function ReviewPage({ itemId, nav }: { itemId?: string; nav: Nav }) {
  const [status, setStatus] = useState<"open" | "approved" | "corrected" | "rejected">("open");
  const [items, setItems] = useState<any[] | null>(null);
  const load = () => api<any[]>(`/review/items?status=${status}`).then(setItems);
  useEffect(() => { load(); }, [status, itemId]);
  if (itemId) return <Item id={itemId} back={() => nav("review")} />;
  return (
    <>
      <PageHead title="Engineer review queue" sub="Answers the automatic checks could not confirm. Approve, correct with citations, or reject. Your decision is recorded and shown to the person who asked." />
      <Tabs value={status} onChange={setStatus} items={[["open", "Open"], ["corrected", "Corrected"], ["approved", "Approved"], ["rejected", "Rejected"]]} />
      {items === null ? <p className="muted">Loading…</p> : items.length === 0 ? <Empty title="Nothing here">You only see items from projects where you can read every cited knowledge base.</Empty> : (
        <table><thead><tr><th>Question</th><th>Project</th><th>Why it was flagged</th><th>Class</th><th>Assigned</th><th>Received</th></tr></thead>
          <tbody>{items.map((i) => (
            <tr key={i.id} className="clickable" onClick={() => nav(`review/${i.id}`)}>
              <td><b>{i.question.slice(0, 120)}</b></td><td>{i.project_name}</td>
              <td><div className="row" style={{ gap: 4 }}>{i.issue_codes.slice(0, 4).map((c: string) => <Badge key={c} tone="warn">{c.replace(/_/g, " ")}</Badge>)}
                {i.issue_codes.length > 4 && <Badge>+{i.issue_codes.length - 4}</Badge>}</div></td>
              <td><Classification c={i.classification} /></td><td>{i.assigned_username || <span className="muted">—</span>}</td>
              <td className="small muted">{fmtTime(i.created_at)}</td></tr>))}</tbody></table>
      )}
    </>
  );
}

function Item({ id, back }: { id: string; back: () => void }) {
  const [d, setD] = useState<any>(null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [check, setCheck] = useState<any[] | null>(null);
  const [hl, setHl] = useState<number | null>(null);
  const { busy, run } = useAction();
  const load = () => api(`/review/items/${id}`).then((x) => { setD(x); setText(x.item.draft); });
  useEffect(() => { load(); }, [id]);
  if (!d) return <p className="muted">Loading…</p>;
  const it = d.item;
  const open = it.status === "open";
  const decide = async (decision: string) => {
    if (decision === "reject" && !note.trim()) { alert("Please add a note explaining why the answer is rejected."); return; }
    const r = await run(() => post(`/review/items/${id}/decide`, { decision, answer: decision === "correct" ? text : "", note }), "Decision recorded");
    if (r) back();
  };
  return (
    <>
      <div className="crumbs"><button onClick={back}>Review queue</button> / item</div>
      <PageHead title={it.question} sub={<span className="row"><Status s={it.status} /><Classification c={it.classification} />
        <span className="muted small">received {fmtTime(it.created_at)}</span></span>}
        actions={open && !it.assigned_to && <Button onClick={async () => { await run(() => post(`/review/items/${id}/assign`), "Assigned to you"); load(); }}>Assign to me</Button>} />
      <div className="grid2" style={{ alignItems: "start" }}>
        <div className="stack">
          <Card title="Why the checks flagged it">
            {it.findings.issues.length === 0 && <p className="muted">{it.findings.suggested_action || "No specific issues; the model reviewer was not available."}</p>}
            <div className="stack" style={{ gap: 6 }}>{it.findings.issues.map((i: any, k: number) => (
              <div key={k} className={"issue" + (i.blocking ? " blocking" : "")}><code>{i.code}</code> <span className="muted small">({i.source})</span><br />
                {i.explanation}{i.claim && <div className="small muted">“{i.claim.slice(0, 200)}”</div>}</div>))}</div>
            {it.findings.missing_evidence?.length > 0 && <p className="small" style={{ marginTop: 8 }}><b>Missing evidence:</b> {it.findings.missing_evidence.join("; ")}</p>}
            {it.findings.suggested_action && <p className="small"><b>Suggested:</b> {it.findings.suggested_action}</p>}
          </Card>
          <Card title="Model draft (not shown to the user)">
            <AnswerText text={it.draft} onCite={setHl} />
          </Card>
          {open ? (
            <Card title="Your decision">
              <Field label="Corrected answer" hint="Cite the numbered sources, e.g. [2]. Keep tags, numbers, units and conditions exactly as in the source.">
                <textarea rows={6} value={text} onChange={(e) => { setText(e.target.value); setCheck(null); }} /></Field>
              <div className="row" style={{ marginBottom: 10 }}>
                <Button onClick={async () => { const r = await run(() => post(`/review/items/${id}/check`, { decision: "correct", answer: text })); if (r) setCheck(r.issues); }}>Run built-in checks on my text</Button>
                {check && (check.length === 0 ? <Badge tone="ok">no problems found</Badge> : <Badge tone="warn">{check.length} note(s)</Badge>)}
              </div>
              {check?.map((i, k) => <div key={k} className="issue" style={{ marginBottom: 6 }}><code>{i.code}</code> {i.explanation}</div>)}
              <Field label="Note (required for reject)"><input value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} /></Field>
              <div className="row">
                <Button kind="primary" disabled={busy} onClick={() => decide("correct")}>Save correction & release</Button>
                <Button disabled={busy} onClick={() => decide("approve")} title="Release the model draft unchanged">Approve draft as is</Button>
                <Button kind="danger" disabled={busy} onClick={() => decide("reject")}>Reject</Button>
              </div>
              <p className="muted small" style={{ marginTop: 10 }}>Approving or correcting also proposes this Q&A as a training example. It is only used for training after a <b>different</b> person approves it in Model improvement.</p>
            </Card>
          ) : null}
          <Card title="Decision history">
            {d.decisions.length === 0 ? <p className="muted small">No decisions yet.</p> : d.decisions.map((x: any) => (
              <div key={x.id} style={{ borderBottom: "1px solid var(--line-2)", padding: "8px 0" }}>
                <div className="row"><Badge tone="info">{x.decision}</Badge><b>{x.display_name}</b><span className="muted small">{fmtTime(x.created_at)}</span></div>
                {x.answer && <div className="small" style={{ marginTop: 4 }}>{x.answer}</div>}
                {x.note && <div className="small muted">Note: {x.note}</div>}
              </div>))}
            {d.feedback.length > 0 && <p className="small muted">User ratings: {d.feedback.map((f: any) => (f.rating > 0 ? "useful" : "not useful")).join(", ")}</p>}
          </Card>
        </div>
        <Card title="Retrieved sources">
          <div className="stack">{it.evidence.map((e: any) => <SourceCard key={e.ref} c={e} highlight={hl === e.ref} />)}</div>
        </Card>
      </div>
    </>
  );
}
