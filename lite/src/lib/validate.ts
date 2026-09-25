// Answer checks (a compact port of the server's deterministic Answer Review):
// cited sources must exist, every number+unit and equipment tag in a sentence must appear in the passages that
// sentence cites, factual sentences need a citation, and generated topic summaries are never a primary source.
import { TAG_RE } from "./ingest";
import type { Evidence, Issue } from "./types";

const UNITS = ["°c", "ºc", "degc", "deg c", "°f", "bar", "bara", "barg", "bar(g)", "kg/cm2", "kg/cm²", "kg/cm2g", "psi", "psig", "psia",
  "kpa", "mpa", "pa", "mmhg", "mmh2o", "mmwc", "%", "wt%", "vol%", "mol%", "ppm", "ppmw", "ppmv", "ppb", "m3/h", "m³/h", "nm3/h",
  "t/h", "tph", "kg/h", "bpsd", "bpd", "mm", "cm", "m", "km", "rpm", "kw", "mw", "kv", "hz", "cp", "cst", "mg/l",
  "s", "sec", "min", "h", "hr", "hrs", "hours", "days", "kg", "g", "t", "ml", "usd", "eur"];
const UNIT_RE = UNITS.map((u) => u.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length).join("|");
const NUM = String.raw`[-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-−]?\d+(?:\.\d+)?`;
const QTY = new RegExp(String.raw`(?<![\w.])(${NUM})\s*(${UNIT_RE})(?![A-Za-z0-9/])`, "gi");
export const CITE_RE = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

export type Qty = { value: number; unit: string };
const canon = (u: string) => u.toLowerCase().replace("º", "°").replace("deg c", "°c").replace("degc", "°c").replace("kg/cm²", "kg/cm2")
  .replace("m³/h", "m3/h").replace(/^(hr|hrs|hours)$/, "h").replace(/^sec$/, "s");

export function quantities(text: string): Qty[] {
  const clean = text.replace(TAG_RE, (m) => " ".repeat(m.length)).replace(CITE_RE, (m) => " ".repeat(m.length));
  const out: Qty[] = [];
  for (const m of clean.matchAll(QTY)) out.push({ value: parseFloat(m[1].replace("−", "-").replace(/,/g, "")), unit: canon(m[2]) });
  return out;
}

export const NOT_FOUND = /not (?:found|available) in the (?:accessible )?sources|could not find|no (?:relevant )?information/i;

function sentences(answer: string): string[] {
  return answer.split(/\n+/).flatMap((line) => line.split(/(?<=[.!?])\s+(?=[A-Z0-9\[])/)).map((s) => s.trim())
    .filter((s) => s && !/^#{1,6}\s/.test(s) && !/^\*\*[^*]+\*\*:?$/.test(s) && !(s.endsWith(":") && s.split(/\s+/).length <= 8));
}

export function checkAnswer(answer: string, evidence: Evidence[]): Issue[] {
  const issues: Issue[] = [];
  if (!answer.trim() || NOT_FOUND.test(answer) && !CITE_RE.test(answer)) return issues;
  CITE_RE.lastIndex = 0;
  const byRef = new Map(evidence.map((e) => [e.ref, e]));
  const allTags = new Set(evidence.flatMap((e) => [...e.tags, ...(e.text.match(TAG_RE) || [])]));
  for (const s of sentences(answer)) {
    const refs = [...s.matchAll(CITE_RE)].flatMap((m) => m[1].split(",").map((x) => parseInt(x.trim())));
    const unknown = refs.filter((r) => !byRef.has(r));
    if (unknown.length) issues.push({ code: "unknown_source", claim: s, blocking: true,
      explanation: `Cites source ${unknown.join(", ")}, which was not retrieved.` });
    const cited = refs.map((r) => byRef.get(r)).filter(Boolean) as Evidence[];
    const qs = quantities(s);
    const tags = [...new Set(s.replace(CITE_RE, " ").match(TAG_RE) || [])];
    if (!refs.length) {
      if (qs.length || tags.length) issues.push({ code: "missing_citation", claim: s, blocking: true,
        explanation: "States a value or equipment tag without citing a source." });
      for (const t of tags) if (!allTags.has(t)) issues.push({ code: "tag_not_in_sources", claim: s, blocking: true,
        explanation: `Equipment tag ${t} does not appear in any retrieved source.` });
      continue;
    }
    const primary = cited.filter((e) => e.kind !== "summary");
    if (cited.length && !primary.length && (qs.length || tags.length)) issues.push({ code: "summary_not_primary_source", claim: s, blocking: true,
      explanation: "Only a generated topic summary is cited for a specific value; cite the original passage." });
    const pool = primary.map((e) => e.text + " " + e.section).join("\n");
    const poolQ = quantities(pool);
    for (const q of qs) {
      if (!poolQ.some((p) => p.value === q.value && p.unit === q.unit)) issues.push({ code: "value_not_in_cited_source", claim: s, blocking: true,
        explanation: `${q.value} ${q.unit} does not appear in the cited source(s) ${refs.join(", ")}.` });
    }
    for (const t of tags) if (primary.length && !pool.includes(t)) issues.push({ code: "tag_not_in_cited_source", claim: s, blocking: true,
      explanation: `${t} is not mentioned in the cited source(s) ${refs.join(", ")}.` });
  }
  return issues;
}
