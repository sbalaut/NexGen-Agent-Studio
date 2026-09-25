import { ReactElement, ReactNode, cloneElement, createContext, isValidElement, useContext, useEffect, useId, useRef, useState } from "react";

// ------------------------------------------------------------------ toasts
type Toast = { id: number; kind: "ok" | "error" | "info"; text: string };
const ToastCtx = createContext<(kind: Toast["kind"], text: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = (kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x.slice(-2), { id, kind, text }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), kind === "error" ? 9000 : 4000);
  };
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => <div key={t.id} className={"toast " + t.kind}>{t.text}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}

// Run an async action with busy state and error toast.
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  async function run<T>(fn: () => Promise<T>, ok?: string): Promise<T | undefined> {
    setBusy(true);
    try {
      const r = await fn();
      if (ok) toast("ok", ok);
      return r;
    } catch (e: any) {
      toast("error", e?.message || "Something went wrong");
      return undefined;
    } finally { setBusy(false); }
  }
  return { busy, run };
}

// ------------------------------------------------------------------ primitives
export function Button({ kind = "secondary", children, ...p }:
  { kind?: "primary" | "secondary" | "danger" | "ghost" } & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={"btn " + kind} {...p}>{children}</button>;
}

export function Badge({ tone = "neutral", children, title }: { tone?: string; children: ReactNode; title?: string }) {
  return <span className={"badge " + tone} title={title}>{children}</span>;
}

const STATUS_TONE: Record<string, string> = {
  completed: "ok", pass: "ok", ready: "ok", confirmed: "ok", active: "ok", approved: "ok", corrected: "ok",
  published: "ok", registered: "ok", running: "info", queued: "info", building: "info", leased: "info",
  preview_ready: "info", pending_approval: "warn", awaiting_review: "warn", needs_human_review: "warn", revise: "warn",
  open: "warn", cancelling: "warn", pending_export: "info", scanned_ocr_deferred: "warn", blocked: "error",
  failed: "error", export_failed: "error", rejected: "error", cancelled: "neutral", not_found: "neutral",
  skipped: "neutral", superseded: "neutral", rolled_back: "neutral", restricted: "error", unpublished: "neutral",
  uploaded: "info", excluded: "neutral", proposed: "info",
};
const STATUS_LABEL: Record<string, string> = {
  awaiting_review: "engineer review", needs_human_review: "needs engineer", not_found: "not found",
  preview_ready: "preview ready", scanned_ocr_deferred: "scanned — OCR needed", pending_approval: "awaiting approval",
  export_failed: "export failed", pending_export: "exporting", rolled_back: "rolled back",
};
export function Status({ s }: { s: string | null | undefined }) {
  if (!s) return <Badge>—</Badge>;
  return <Badge tone={STATUS_TONE[s] || "neutral"}>{STATUS_LABEL[s] || s.replace(/_/g, " ")}</Badge>;
}

export function Classification({ c }: { c: string }) {
  return <Badge tone={c === "Restricted" ? "error" : c === "Internal" ? "info" : "neutral"} title="Classification">{c}</Badge>;
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  const uid = useId();
  // A single form control gets a proper <label for> and its hint as a description (not part of its accessible name).
  if (isValidElement(children) && ["input", "select", "textarea"].includes(children.type as string)) {
    const el = children as ReactElement<any>;
    const id = el.props.id || "f" + uid.replace(/:/g, "");
    return (
      <div className="field">
        <label className="field-label" htmlFor={id}>{label}</label>
        {cloneElement(el, { id, "aria-describedby": hint ? id + "-hint" : undefined })}
        {hint && <span id={id + "-hint"} className="field-hint">{hint}</span>}
      </div>
    );
  }
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("input,select,textarea,button")?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => { window.removeEventListener("keydown", esc); prev?.focus(); };
  }, []);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={"modal" + (wide ? " wide" : "")} role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <div className="modal-head"><h2>{title}</h2><button className="icon-btn" onClick={onClose} aria-label="Close">×</button></div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><div className="empty-title">{title}</div>{children && <div className="empty-text">{children}</div>}</div>;
}

export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: [T, string][] }) {
  return (
    <div className="tabs" role="tablist">
      {items.map(([k, label]) => (
        <button key={k} role="tab" aria-selected={value === k} className={"tab" + (value === k ? " active" : "")}
          onClick={() => onChange(k)}>{label}</button>
      ))}
    </div>
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={"card " + (className || "")}>
      {(title || actions) && <div className="card-head"><h3>{title}</h3><div className="card-actions">{actions}</div></div>}
      {children}
    </section>
  );
}

export function PageHead({ title, sub, actions }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-head">
      <div><h1>{title}</h1>{sub && <p className="sub">{sub}</p>}</div>
      <div className="page-actions">{actions}</div>
    </header>
  );
}

// Answer text with [n] markers rendered as citation chips (plain text only — never HTML).
export function AnswerText({ text, onCite }: { text: string; onCite?: (ref: number) => void }) {
  const inline = (line: string, key: number) => line.split(/(\[\d+(?:\s*,\s*\d+)*\])/g).map((p, i) => {
    const m = p.match(/^\[(.+)\]$/);
    if (!m) return <span key={key + "-" + i}>{p.replace(/\*\*/g, "")}</span>;
    return <span key={key + "-" + i}>{m[1].split(",").map((r) => (
      <button key={r} className="cite" onClick={() => onCite?.(parseInt(r))} title="Show source">{r.trim()}</button>))}</span>;
  });
  return (
    <div className="answer-text">
      {text.split("\n").map((line, i) => {
        const h = line.match(/^#{1,6}\s+(.*)$/);
        return h ? <div key={i} className="answer-heading">{h[1].replace(/\*\*/g, "")}</div> : <div key={i}>{inline(line, i)}</div>;
      })}
    </div>
  );
}

export function SourceCard({ c, highlight }: { c: any; highlight?: boolean }) {
  return (
    <div className={"source" + (highlight ? " hl" : "")} id={"src-" + c.ref}>
      <div className="source-head">
        <span className="source-ref">{c.ref}</span>
        <span className="source-file">{c.filename}</span>
        <span className="muted">{c.section} · {c.location}</span>
        {c.kind === "summary" && <Badge tone="warn" title="Generated by a model — not a primary source">graph summary</Badge>}
        {c.kind === "tool" && <Badge tone="info">tool result</Badge>}
      </div>
      <blockquote>{c.text}</blockquote>
    </div>
  );
}
