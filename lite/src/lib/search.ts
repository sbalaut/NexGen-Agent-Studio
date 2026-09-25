// Hybrid search over the passages of the selected knowledge bases: BM25 keywords + embeddings (when the knowledge
// base has an embedding model), fused with reciprocal-rank fusion, plus optional GraphRAG boosts and summaries.
import { byIndex } from "./store";
import { embed } from "./providers";
import { globalSummaries, localRanking } from "./graph";
import { cosine } from "./vec";
import type { Chunk, Connection, Evidence, GraphMode, KB } from "./types";

export const tokenize = (s: string) => (s.toLowerCase().match(/[\p{L}\p{N}][\p{L}\p{N}\-.]*/gu) || []).map((t) => t.replace(/[.\-]+$/, ""))
  .filter((t) => t.length > 1 && !STOP.has(t));
const STOP = new Set("the a an and or of to in on for with by is are was were be been at as it this that from what which how when where who why do does did can could should would will shall may might must not no yes there their its into about than then also any all per".split(" "));

export function bm25(chunks: Chunk[], query: string, k1 = 1.4, b = 0.75): Map<string, number> {
  const q = [...new Set(tokenize(query))];
  const docs = chunks.map((c) => tokenize(c.section + " " + c.text));
  const avg = docs.reduce((n, d) => n + d.length, 0) / (docs.length || 1);
  const df = new Map<string, number>();
  for (const d of docs) for (const t of new Set(d)) df.set(t, (df.get(t) || 0) + 1);
  const out = new Map<string, number>();
  docs.forEach((d, i) => {
    let s = 0;
    const tf = new Map<string, number>();
    for (const t of d) tf.set(t, (tf.get(t) || 0) + 1);
    for (const t of q) {
      const f = tf.get(t); if (!f) continue;
      const idf = Math.log(1 + (docs.length - (df.get(t) || 0) + 0.5) / ((df.get(t) || 0) + 0.5));
      s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * d.length / avg));
    }
    // exact equipment-tag matches matter most in technical documents
    for (const tag of query.match(/\b(?:\d{1,3}-)?[A-Z]{1,4}-\d{2,5}[A-Z]?\b/g) || []) if (chunks[i].tags.includes(tag)) s += 3;
    if (s > 0) out.set(chunks[i].id, s);
  });
  return out;
}

export { cosine };

const rrf = (ranked: string[], k = 60) => new Map(ranked.map((id, i) => [id, 1 / (k + i + 1)]));

export type SearchOptions = { topK?: number; graph?: GraphMode; signal?: AbortSignal };

export async function search(kbs: KB[], connections: Connection[], query: string, o: SearchOptions = {}): Promise<Evidence[]> {
  const topK = o.topK ?? 6;
  const scored = new Map<string, number>();
  const byId = new Map<string, Chunk>();
  const summaries: Evidence[] = [];
  for (const kb of kbs) {
    const chunks = await byIndex("chunks", "kbId", kb.id);
    if (!chunks.length) continue;
    chunks.forEach((c) => byId.set(c.id, c));
    const lists: Map<string, number>[] = [];
    const kw = bm25(chunks, query);
    lists.push(rrf([...kw.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id).slice(0, 50)));
    let qv: number[] | null = null;
    const conn = connections.find((c) => c.id === kb.embedConnection);
    if (conn && kb.embedModel && chunks.some((c) => c.vector)) {
      try {
        [qv] = await embed(conn, kb.embedModel, [query], o.signal);
        const sims = chunks.filter((c) => c.vector).map((c) => [c.id, cosine(qv!, c.vector!)] as [string, number]);
        lists.push(rrf(sims.sort((a, b) => b[1] - a[1]).map(([id]) => id).slice(0, 50)));
      } catch { /* keyword search still works */ }
    }
    if (o.graph && o.graph !== "off" && kb.graphMode !== "off") {
      if (o.graph === "local" || o.graph === "both") {
        const ranked = await localRanking(kb.id, query);
        if (ranked.length) lists.push(rrf(ranked, 30));
      }
      if (o.graph === "global" || o.graph === "both") {
        for (const s of await globalSummaries(kb.id, query, qv)) summaries.push({ ref: 0, kind: "summary", chunkId: s.id, kbId: kb.id,
          filename: kb.name + " (knowledge graph)", section: "Topic summary: " + s.title, location: `community ${s.community + 1}`, text: s.summary, tags: [] });
      }
    }
    for (const l of lists) for (const [id, v] of l) scored.set(id, (scored.get(id) || 0) + v);
  }
  const top = [...scored.entries()].sort((a, b) => b[1] - a[1]).slice(0, topK).map(([id]) => byId.get(id)!).filter(Boolean);
  const out: Evidence[] = top.map((c) => ({ ref: 0, kind: "passage", chunkId: c.id, kbId: c.kbId, filename: c.filename, section: c.section,
    location: c.location, text: c.text, tags: c.tags }));
  return [...out, ...summaries.slice(0, 2)];
}
