import { useEffect, useState } from "react";
import { api, fmtTime, post, Project, put, User } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Card, Classification, Empty, Field, Modal, PageHead, Status, Tabs, useAction } from "../ui";

export default function ImprovePage({ user, pid, tab, nav }: { user: User; pid?: string; tab?: string; nav: Nav }) {
  const [projects, setProjects] = useState<Project[]>([]);
  useEffect(() => { api<Project[]>("/projects").then((p) => { setProjects(p); if (!pid && p.length) nav(`improve/${p[0].id}/examples`); }); }, []);
  const isOp = user.roles.includes("TrainingOperator");
  const t = tab || "examples";
  return (
    <>
      <PageHead title="Model improvement" sub="Engineer-approved corrections → approved dataset → approved training → evaluation → approved deployment. No step authorises the next one automatically."
        actions={<select value={pid || ""} onChange={(e) => nav(`improve/${e.target.value}/${t}`)} aria-label="Project" style={{ width: 260 }}>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>} />
      <div className="notice" style={{ marginBottom: 16 }}>Fine-tuning teaches style, format and terminology. Plant facts that change (values, procedures) should stay in the knowledge bases, where access rules apply.</div>
      <Tabs value={t} onChange={(x) => nav(`improve/${pid}/${x}`)} items={[["examples", "1. Examples"], ["datasets", "2. Dataset versions"], ["training", "3. Training & evaluation"], ["deploy", "4. Deployment"]]} />
      {!pid ? <Empty title="Join a project first" /> : !isOp && t !== "deploy" ? <Empty title="Training Operator role required" /> : <>
        {t === "examples" && <Examples pid={pid} />}
        {t === "datasets" && <Datasets pid={pid} />}
        {t === "training" && <Training pid={pid} user={user} />}
        {t === "deploy" && <Deploy user={user} />}
      </>}
    </>
  );
}

function Examples({ pid }: { pid: string }) {
  const [d, setD] = useState<any>(null);
  const [edit, setEdit] = useState<any>(null);
  const { busy, run } = useAction();
  const load = () => api(`/projects/${pid}/dataset/candidates`).then(setD);
  useEffect(() => { load(); }, [pid]);
  if (!d) return <p className="muted">Loading…</p>;
  const flagged = (id: string) => d.proposals.filter((p: any) => p.candidate_ids.includes(id));
  return (
    <div className="stack">
      {d.proposals.length > 0 && <Card title={<span>Preparation suggestions <span className="muted small">({d.proposal_method})</span></span>}>
        <div className="stack" style={{ gap: 6 }}>{d.proposals.map((p: any, i: number) => <div key={i} className="issue"><code>{p.kind}</code> {p.message}
          {p.candidate_ids.length > 0 && <span className="muted small"> — {p.candidate_ids.length} example(s)</span>}</div>)}</div>
      </Card>}
      <Card title="Candidate examples from engineer decisions">
        {d.candidates.length === 0 ? <Empty title="No candidates yet">They appear when an engineer approves or corrects an answer in the review queue.</Empty> :
          <table><thead><tr><th>Question & approved answer</th><th>Engineer</th><th>Group</th><th>Status</th><th /></tr></thead>
            <tbody>{d.candidates.map((c: any) => (
              <tr key={c.id}>
                <td><b>{c.question}</b><div className="small" style={{ marginTop: 3 }}>{c.answer}</div>
                  <div className="row" style={{ marginTop: 4, gap: 4 }}>{flagged(c.id).map((p: any, i: number) => <Badge key={i} tone="warn">{p.kind}</Badge>)}
                    <Classification c={c.classification} /><span className="muted small">{c.source_chunk_ids.length} source(s)</span></div>
                  {c.approvals.map((a: any, i: number) => <div key={i} className="small muted">{a.decision} by @{a.username} {fmtTime(a.created_at)}{!a.current && " (for an earlier text)"}</div>)}</td>
                <td className="small">@{c.reviewer_username}</td>
                <td className="mono small" title={c.group_id}>{c.group_id.slice(0, 8)}</td>
                <td><Status s={c.status} /></td>
                <td><div className="stack" style={{ gap: 4 }}>
                  <Button kind="primary" disabled={busy} onClick={async () => { await run(() => post(`/dataset/candidates/${c.id}/decision`, { decision: "include" }), "Approved for datasets"); load(); }}>Include</Button>
                  <Button disabled={busy} onClick={async () => { const note = prompt("Reason for excluding (optional)") ?? ""; await run(() => post(`/dataset/candidates/${c.id}/decision`, { decision: "exclude", note }), "Excluded"); load(); }}>Exclude</Button>
                  <Button kind="ghost" onClick={() => setEdit(c)}>Edit / redact</Button></div></td>
              </tr>))}</tbody></table>}
        <p className="muted small" style={{ marginTop: 8 }}>You cannot include an example whose answer you approved yourself as an engineer — a second person must do it.</p>
      </Card>
      {edit && <Modal title="Edit example" onClose={() => setEdit(null)}>
        <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget);
          if (await run(() => put(`/dataset/candidates/${edit.id}`, { question: f.get("q"), answer: f.get("a") }), "Saved — needs approval again")) { setEdit(null); load(); } }}>
          <Field label="Question"><textarea name="q" defaultValue={edit.question} /></Field>
          <Field label="Answer" hint="Editing removes existing approvals for this example."><textarea name="a" rows={6} defaultValue={edit.answer} /></Field>
          <div className="modal-foot"><Button type="button" onClick={() => setEdit(null)}>Cancel</Button><Button kind="primary" disabled={busy}>Save</Button></div>
        </form></Modal>}
    </div>
  );
}

function Datasets({ pid }: { pid: string }) {
  const [versions, setVersions] = useState<any[]>([]);
  const [cands, setCands] = useState<any>(null);
  const [split, setSplit] = useState<Record<string, string>>({});
  const [detail, setDetail] = useState<any>(null);
  const { busy, run } = useAction();
  const load = () => { api<any[]>(`/projects/${pid}/dataset/versions`).then(setVersions);
    api(`/projects/${pid}/dataset/candidates`).then((c) => { setCands(c); setSplit(c.proposed_splits); }); };
  useEffect(() => { load(); }, [pid]);
  const approved = cands?.candidates.filter((c: any) => c.status === "approved") || [];
  const groups = Object.keys(split);
  return (
    <div className="grid2" style={{ alignItems: "start" }}>
      <Card title="Create an immutable dataset version">
        {approved.length === 0 ? <p className="muted">No approved examples yet.</p> : <form onSubmit={async (e) => {
          e.preventDefault(); const f = new FormData(e.currentTarget);
          const r = await run(() => post(`/projects/${pid}/dataset/versions`, { name: f.get("name"), version: f.get("v"), split_by_group: split }), "Dataset version created");
          if (r) load();
        }}>
          <div className="grid2" style={{ gap: 8 }}><Field label="Dataset name"><input name="name" required defaultValue="cdu-answers" /></Field>
            <Field label="Version"><input name="v" required placeholder="1" /></Field></div>
          <div className="field"><span className="field-label">Split by source-document group</span>
            <span className="field-hint">Examples from the same document family stay in one split, so held-out tests are not leaked into training.
              The proposal is deterministic; change it if needed. Held-out answers are never given to the training job.</span></div>
          <table><thead><tr><th>Group</th><th>Examples</th><th>Split</th></tr></thead><tbody>{groups.map((g) => (
            <tr key={g}><td className="mono small">{g.slice(0, 16)}</td><td>{approved.filter((c: any) => c.group_id === g).length}</td>
              <td><div className="pill-select">{["train", "validation", "heldout"].map((s) => <button type="button" key={s} className={split[g] === s ? "on" : ""} onClick={() => setSplit({ ...split, [g]: s })}>{s}</button>)}</div></td></tr>))}</tbody></table>
          <div style={{ marginTop: 12 }}><Button kind="primary" disabled={busy}>Create version</Button></div>
        </form>}
      </Card>
      <Card title="Versions">
        {versions.length === 0 ? <p className="muted">None yet.</p> : <table><thead><tr><th>Version</th><th>Counts</th><th>Hash</th><th /></tr></thead>
          <tbody>{versions.map((v) => (
            <tr key={v.id}><td><b>{v.name}</b> v{v.version}<div className="small muted">by @{v.created_by} · {fmtTime(v.created_at)}</div>
              {v.restricted_reason && <Badge tone="error">restricted: {v.restricted_reason}</Badge>}</td>
              <td className="small">{Object.entries(v.counts).map(([k, n]) => `${k} ${n}`).join(" · ")}</td>
              <td className="mono small" title={v.sha256}>{v.sha256.slice(0, 12)}…</td>
              <td><Button kind="ghost" onClick={() => api(`/dataset/versions/${v.id}`).then(setDetail)}>Details</Button></td></tr>))}</tbody></table>}
      </Card>
      {detail && <Modal title="Dataset version" onClose={() => setDetail(null)} wide>
        <dl className="kv"><dt>SHA-256</dt><dd className="mono">{detail.sha256}</dd><dt>Classification</dt><dd><Classification c={detail.classification} /></dd>
          <dt>Split method</dt><dd>{detail.split_definition.method}</dd><dt>Excluded</dt><dd>{detail.exclusions.length} ({[...new Set(detail.exclusions.map((x: any) => x.reason))].join(", ") || "none"})</dd></dl>
        <table style={{ marginTop: 12 }}><thead><tr><th>Split</th><th>Question</th></tr></thead><tbody>{detail.examples.map((e: any) => <tr key={e.id}><td><Badge>{e.split}</Badge></td><td>{e.question}</td></tr>)}</tbody></table>
      </Modal>}
    </div>
  );
}

function LossChart({ metrics }: { metrics: any[] }) {
  const pts = metrics.filter((m) => m.loss != null);
  const evals = metrics.filter((m) => m.eval_loss != null);
  if (pts.length < 2) return <p className="muted small">{pts.length ? "One logged step so far." : "No metrics logged yet."}</p>;
  const W = 520, H = 180, P = 32;
  const xs = metrics.map((m) => m.step), all = [...pts.map((m) => m.loss), ...evals.map((m) => m.eval_loss)];
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...all), Math.max(...all)];
  const X = (s: number) => P + ((s - x0) / Math.max(1, x1 - x0)) * (W - P - 8);
  const Y = (v: number) => 8 + (1 - (v - y0) / Math.max(1e-9, y1 - y0)) * (H - P);
  const path = (arr: any[], k: string) => arr.map((m, i) => `${i ? "L" : "M"}${X(m.step).toFixed(1)},${Y(m[k]).toFixed(1)}`).join("");
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Training loss from ${pts[0].loss.toFixed(3)} to ${pts[pts.length - 1].loss.toFixed(3)} over ${x1} steps`}>
      <line className="axis" x1={P} y1={H - P + 8} x2={W} y2={H - P + 8} /><line className="axis" x1={P} y1={8} x2={P} y2={H - P + 8} />
      <path className="l1" d={path(pts, "loss")} />{evals.length > 1 && <path className="l2" d={path(evals, "eval_loss")} />}
      {evals.map((m) => <circle key={m.step} cx={X(m.step)} cy={Y(m.eval_loss)} r={3} fill="#c2410c" />)}
      <text x={P} y={H - 4}>step {x0}</text><text x={W - 60} y={H - 4}>step {x1}</text>
      <text x={2} y={14}>{y1.toFixed(2)}</text><text x={2} y={H - P + 8}>{y0.toFixed(2)}</text>
      <text x={W - 170} y={16}>— train loss {evals.length ? "  - - validation" : ""}</text>
    </svg>
  );
}

function Training({ pid, user }: { pid: string; user: User }) {
  const [jobs, setJobs] = useState<any[]>([]);
  const [bm, setBm] = useState<any>({ models: [], recipes: {} });
  const [versions, setVersions] = useState<any[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [recipe, setRecipe] = useState("lora-sft");
  const { busy, run } = useAction();
  const load = () => api<any[]>(`/projects/${pid}/training/jobs`).then(setJobs);
  useEffect(() => { load(); api("/training/base-models").then(setBm); api<any[]>(`/projects/${pid}/dataset/versions`).then(setVersions);
    const t = setInterval(load, 5000); return () => clearInterval(t); }, [pid]);
  return (
    <div className="stack">
      <Card title="Request a training job">
        {bm.models.length === 0 ? <p className="muted">No base model registered. An administrator copies a model folder into the approved models directory and registers it under Administration → Models. Nothing is downloaded automatically.</p> :
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            const params: any = {}; for (const k of ["epochs", "lr", "r", "max_len"]) { const v = f.get(k); if (v) params[k] = Number(v); }
            if (await run(() => post(`/projects/${pid}/training/jobs`, { dataset_version_id: f.get("ds"), base_model_id: f.get("bm"), recipe, params, seed: Number(f.get("seed")) }),
              "Requested — another operator must approve it")) load();
          }}>
            <div className="grid2" style={{ gap: 10 }}>
              <Field label="Dataset version"><select name="ds" required>{versions.filter((v) => !v.restricted_reason).map((v) => <option key={v.id} value={v.id}>{v.name} v{v.version} ({v.sha256.slice(0, 8)})</option>)}</select></Field>
              <Field label="Base model"><select name="bm">{bm.models.map((m: any) => <option key={m.id} value={m.id}>{m.name} — {m.license}</option>)}</select></Field>
              <Field label="Recipe (allow-listed)"><select value={recipe} onChange={(e) => setRecipe(e.target.value)}>{Object.keys(bm.recipes).map((r) => <option key={r}>{r}</option>)}</select></Field>
              <Field label="Seed"><input name="seed" type="number" defaultValue={42} /></Field>
            </div>
            <p className="small muted">Defaults: {JSON.stringify(bm.recipes[recipe] || {})}</p>
            <div className="grid2" style={{ gap: 10 }}>
              <Field label="Epochs (optional)"><input name="epochs" type="number" min={1} max={20} /></Field>
              <Field label="Learning rate (optional)"><input name="lr" type="number" step="any" /></Field>
              <Field label="LoRA rank r (optional)"><input name="r" type="number" min={2} max={128} /></Field>
              <Field label="Max sequence length (optional)"><input name="max_len" type="number" min={128} max={8192} /></Field>
            </div>
            <Button kind="primary" disabled={busy || !versions.length}>Request training</Button>
          </form>}
      </Card>
      <Card title="Training jobs">
        {jobs.length === 0 ? <p className="muted">No jobs yet.</p> : <table><thead><tr><th>Job</th><th>Status</th><th>Requested / approved</th><th>Started</th><th /></tr></thead>
          <tbody>{jobs.map((j) => (
            <tr key={j.id}><td><b>{j.base_model}</b> · {j.recipe}<div className="small muted">{j.dataset_name} v{j.dataset_version} · seed {j.seed}</div>
              {j.error && <div className="small" style={{ color: "var(--err)" }}>{j.error.slice(0, 200)}</div>}</td>
              <td><Status s={j.status} /></td><td className="small">@{j.requested_by_name}{j.approved_by_name ? ` / @${j.approved_by_name}` : ""}</td>
              <td className="small muted">{fmtTime(j.started_at)}</td>
              <td><div className="row">
                {j.status === "pending_approval" && j.requested_by !== user.id && <Button kind="primary" onClick={async () => { await run(() => post(`/training/jobs/${j.id}/approve`), "Approved — running preflight checks"); load(); }}>Approve</Button>}
                {["pending_approval", "blocked"].includes(j.status) && j.requested_by !== user.id && <Button onClick={async () => { await run(() => post(`/training/jobs/${j.id}/reject`), "Rejected"); load(); }}>Reject</Button>}
                {["pending_approval", "blocked", "queued", "running"].includes(j.status) && <Button kind="danger" onClick={async () => { await run(() => post(`/training/jobs/${j.id}/cancel`), "Cancel requested"); load(); }}>Cancel</Button>}
                <Button kind="ghost" onClick={() => setOpen(j.id)}>Details</Button></div></td></tr>))}</tbody></table>}
      </Card>
      {open && <JobDetail jid={open} user={user} onClose={() => { setOpen(null); load(); }} />}
    </div>
  );
}

function Scores({ r }: { r: any }) {
  const rows: [string, string, boolean][] = [
    ["raw_token_f1_mean", "Raw answer quality (token F1 vs engineer answer)", true],
    ["passes_answer_review_checks", "Passes the built-in answer checks", true],
    ["useful_answer_rate_answerable", "Useful answers (answerable questions)", true],
    ["abstention_rate_answerable", "Abstained although answerable", false],
    ["false_answer_rate_unanswerable", "Answered although unanswerable", false],
    ["numeric_tag_preservation", "Numbers & tags preserved", true],
    ["scope_leakage_rate", "Scope leakage", false], ["hindi_token_f1_mean", "Hindi questions (token F1)", true],
    ["latency_ms_mean", "Mean latency (ms)", false]];
  const fmt = (v: any) => (v == null ? "n/a" : typeof v === "number" && v <= 1 && v >= 0 && !Number.isInteger(v) ? (v * 100).toFixed(1) + "%" : String(v));
  return (
    <table><thead><tr><th>Measure</th><th>Base model</th><th>Candidate</th></tr></thead><tbody>
      {rows.map(([k, label, higher]) => {
        const b = r.base[k], c = r.candidate[k];
        const better = b != null && c != null && b !== c && ((c > b) === higher);
        return <tr key={k}><td>{label}</td><td>{fmt(b)}</td><td style={{ fontWeight: better ? 700 : 400, color: better ? "var(--ok)" : undefined }}>{fmt(c)}</td></tr>;
      })}</tbody></table>
  );
}

function JobDetail({ jid, user, onClose }: { jid: string; user: User; onClose: () => void }) {
  const [d, setD] = useState<any>(null);
  const [promote, setPromote] = useState<string | null>(null);
  const { busy, run } = useAction();
  const load = () => api(`/training/jobs/${jid}`).then(setD);
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, [jid]);
  if (!d) return <Modal title="Training job" onClose={onClose} wide><p className="muted">Loading…</p></Modal>;
  const j = d.job;
  return (
    <Modal title={`Training job — ${d.base_model.name} · ${j.recipe}`} onClose={onClose} wide>
      <div className="row" style={{ marginBottom: 12 }}><Status s={j.status} />{j.error && <span className="small" style={{ color: "var(--err)" }}>{j.error}</span>}</div>
      <div className="grid2">
        <dl className="kv">
          <dt>Dataset</dt><dd>{d.dataset.name} v{d.dataset.version} <Classification c={d.dataset.classification} /></dd>
          <dt>Dataset SHA-256</dt><dd className="mono">{d.dataset.sha256}</dd>
          <dt>Base model</dt><dd>{d.base_model.name} — {d.base_model.license}<div className="mono small">{d.base_model.path}</div></dd>
          <dt>Parameters</dt><dd className="mono">{JSON.stringify(j.params)} · seed {j.seed}</dd>
          <dt>Started / finished</dt><dd>{fmtTime(j.started_at)} / {fmtTime(j.finished_at)}</dd>
          {j.software && <><dt>Software</dt><dd className="mono small">{Object.entries(j.software).map(([k, v]) => `${k} ${v}`).join(", ")}</dd></>}
          {j.hardware && <><dt>Hardware</dt><dd className="small">{j.hardware.gpus?.length ? j.hardware.gpus.map((g: any) => g.name).join(", ") : `CPU only (${j.hardware.cpu_count} cores)`}</dd></>}
        </dl>
        <div><b className="small">Loss (from the runner's log)</b><LossChart metrics={d.metrics} /><p className="muted small">{d.metrics_note}</p></div>
      </div>
      {j.preflight && <details open={j.status === "blocked"} style={{ marginTop: 12 }}><summary><b>Preflight checks</b> {j.preflight.ok ? <Badge tone="ok">passed</Badge> : <Badge tone="error">blocked</Badge>}</summary>
        <table><tbody>{j.preflight.checks.map((c: any) => <tr key={c.name}><td>{c.ok ? "✓" : c.blocking ? "✗" : "!"}</td><td className="mono small">{c.name}</td><td className="small">{c.detail}</td></tr>)}</tbody></table>
        {j.preflight.estimate?.label && <p className="small muted">{j.preflight.estimate.label}: ≈{(j.preflight.estimate.memory_needed_bytes_estimated / 2 ** 30).toFixed(1)} GiB for ≈{(j.preflight.estimate.parameters_estimated / 1e9).toFixed(2)} B parameters.</p>}
      </details>}
      {d.artifacts.map((a: any) => (
        <Card key={a.id} title={<span className="row">Adapter artifact <Status s={a.status} /><Classification c={a.classification} /></span>}
          actions={a.status === "registered" && user.roles.includes("TrainingOperator") && <Button onClick={async () => { await run(() => post(`/artifacts/${a.id}/evaluate`), "Evaluation queued"); load(); }}>Evaluate on held-out set</Button>}>
          <p className="mono small">{a.path}</p>
          <details><summary className="small">File hashes</summary>{Object.entries(a.files).map(([f, h]: any) => <div key={f} className="mono small">{f} {h.slice(0, 16)}…</div>)}</details>
        </Card>))}
      {d.evaluations.map((e: any) => (
        <Card key={e.id} title={<span className="row">Evaluation <Status s={e.status} /><span className="muted small">{fmtTime(e.created_at)}</span></span>}
          actions={e.status === "completed" && user.permissions.includes("model.promote") && <Button kind="primary" onClick={() => setPromote(e.id)}>Approve deployment…</Button>}>
          {e.error && <div className="notice error">{e.error}</div>}
          {e.results && <><p className="small muted">{e.results.method}. {e.results.comparison}.</p><Scores r={e.results} />
            <details><summary className="small">Sample outputs</summary>{e.results.samples.map((s: any) => <div key={s.id} className="small" style={{ margin: "8px 0" }}>
              <b>{s.question}</b><div>Base: {s.base}</div><div>Candidate: {s.candidate}</div></div>)}</details></>}
        </Card>))}
      {promote && <Modal title="Approve deployment" onClose={() => setPromote(null)}>
        <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget);
          if (await run(() => post("/deployments", { evaluation_id: promote, alias: f.get("alias"), note: f.get("note") }), "Deployment approved — exporting")) setPromote(null); }}>
          <p className="small">The adapter is merged and imported into Ollama. Builders then choose <code>promoted:&lt;name&gt;</code> in an LLM node — nothing changes for existing agents automatically. Requests already running keep their model.</p>
          <Field label="Deployment name"><input name="alias" required pattern="[a-z0-9][a-z0-9\-]+" placeholder="cdu-style" /></Field>
          <Field label="Reason / evidence reviewed"><textarea name="note" /></Field>
          <div className="modal-foot"><Button type="button" onClick={() => setPromote(null)}>Cancel</Button><Button kind="primary" disabled={busy}>Approve</Button></div>
        </form></Modal>}
    </Modal>
  );
}

function Deploy({ user }: { user: User }) {
  const [d, setD] = useState<any>(null);
  const { run } = useAction();
  const load = () => api("/deployments").then(setD).catch(() => setD({ deployments: [], aliases: [] }));
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  if (!d) return <p className="muted">Loading…</p>;
  const canPromote = user.permissions.includes("model.promote");
  return (
    <div className="stack">
      <Card title="Deployed model names">
        {d.aliases.length === 0 ? <p className="muted">No promoted models.</p> : <table><thead><tr><th>Name used in agents</th><th>Active deployment</th><th>Previous</th><th /></tr></thead>
          <tbody>{d.aliases.map((a: any) => <tr key={a.alias}><td className="mono">promoted:{a.alias}</td><td className="mono small">{a.deployment_id.slice(0, 8)}</td>
            <td className="mono small">{a.previous_deployment_id?.slice(0, 8) || "—"}</td>
            <td>{canPromote && a.previous_deployment_id && <Button kind="danger" onClick={async () => { if (confirm("Switch back to the previous deployment?")) { await run(() => post(`/deployments/${a.alias}/rollback`), "Rolled back"); load(); } }}>Roll back</Button>}</td></tr>)}</tbody></table>}
      </Card>
      <Card title="Deployment history">
        {d.deployments.length === 0 ? <p className="muted">None.</p> : <table><thead><tr><th>Name</th><th>Status</th><th>Ollama model</th><th>Approved by</th><th>When</th></tr></thead>
          <tbody>{d.deployments.map((x: any) => <tr key={x.id}><td>{x.alias}</td><td><Status s={x.status} />{x.error && <div className="small" style={{ color: "var(--err)" }}>{x.error.slice(0, 300)}</div>}</td>
            <td className="mono small">{x.ollama_model || "—"}</td><td>@{x.approver}</td><td className="small muted">{fmtTime(x.created_at)}</td></tr>)}</tbody></table>}
      </Card>
    </div>
  );
}
