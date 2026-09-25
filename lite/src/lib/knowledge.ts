// Knowledge bases: add documents, build the search index (embeddings + optional knowledge graph).
import { readFile } from "./ingest";
import { embed } from "./providers";
import { buildGraph } from "./graph";
import { byIndex, get, newId, put, remove, removeWhere } from "./store";
import type { Chunk, Connection, Doc, KB } from "./types";

export async function addDocument(kb: KB, file: File): Promise<Doc> {
  const { pieces, warning } = await readFile(file);
  const existing = (await byIndex("docs", "kbId", kb.id)).find((d) => d.filename === file.name);
  if (existing) await removeDocument(existing);           // re-upload replaces the old version
  const doc: Doc = { id: newId(), kbId: kb.id, filename: file.name, size: file.size, chunks: pieces.length, addedAt: Date.now(), warning };
  const chunks: Chunk[] = pieces.map((p, i) => ({ id: newId(), kbId: kb.id, docId: doc.id, seq: i, filename: file.name, ...p }));
  await put("chunks", ...chunks);
  await put("docs", doc);
  await put("kbs", { ...kb, status: kb.status === "empty" ? "empty" : kb.status, note: "Documents changed — rebuild the index to update the knowledge graph." });
  return doc;
}

export async function removeDocument(doc: Doc) {
  await removeWhere("chunks", "docId", doc.id);
  await remove("docs", doc.id);
}

export async function buildIndex(kbId: string, connections: Connection[], onProgress: (m: string) => void, signal?: AbortSignal): Promise<KB> {
  let kb = (await get("kbs", kbId))!;
  await put("kbs", { ...kb, status: "indexing", note: "Indexing…" });
  try {
    const chunks = await byIndex("chunks", "kbId", kb.id);
    if (!chunks.length) throw new Error("Add at least one document first.");
    const embedConn = connections.find((c) => c.id === kb.embedConnection);
    const notes: string[] = [`${chunks.length} passages`];
    if (kb.embedConnection && !embedConn) throw new Error("The embedding connection of this knowledge base no longer exists — check Settings.");
    if (embedConn && kb.embedModel) {
      const key = `${embedConn.id}:${kb.embedModel}`;
      const todo = chunks.filter((c) => c.vectorKey !== key || !c.vector);
      for (let i = 0; i < todo.length; i += 64) {
        signal?.throwIfAborted();
        onProgress(`Embedding passages ${i + 1}–${Math.min(i + 64, todo.length)} of ${todo.length}`);
        const batch = todo.slice(i, i + 64);
        const vecs = await embed(embedConn, kb.embedModel, batch.map((c) => `${c.section}\n${c.text}`), signal);
        batch.forEach((c, k) => { c.vector = vecs[k]; c.vectorKey = key; });
        await put("chunks", ...batch);
      }
      notes.push(`embeddings: ${kb.embedModel}`);
    } else notes.push("keyword search only (no embedding model)");
    onProgress("Building knowledge graph…");
    const graphNote = await buildGraph({ kb, chunks, graphConn: connections.find((c) => c.id === kb.graphConnection), embedConn, onProgress, signal });
    if (graphNote) notes.push("graph: " + graphNote);
    kb = { ...kb, status: "ready", note: notes.join(" · "), builtAt: Date.now() };
  } catch (e: any) {
    kb = { ...kb, status: "failed", note: e?.name === "AbortError" ? "Cancelled." : e.message };
  }
  await put("kbs", kb);
  return kb;
}
