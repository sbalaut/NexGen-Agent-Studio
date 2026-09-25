// Tiny promise wrapper around IndexedDB plus key storage. No data ever leaves the browser except the
// calls the user configures (model providers and MCP servers).
import type { Chunk, Community, Connection, Doc, Entity, KB, McpServer, Relation, RunRecord, Workflow } from "./types";

const DB_NAME = "nexagent-lite";
const DB_VERSION = 1;
type Stores = { kbs: KB; docs: Doc; chunks: Chunk; entities: Entity; relations: Relation; communities: Community;
  workflows: Workflow; mcp: McpServer; runs: RunRecord };
export type StoreName = keyof Stores;
const BY_KB: StoreName[] = ["docs", "chunks", "entities", "relations", "communities"];

let dbp: Promise<IDBDatabase> | null = null;
function open(): Promise<IDBDatabase> {
  if (!dbp) dbp = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const name of ["kbs", "docs", "chunks", "entities", "relations", "communities", "workflows", "mcp", "runs"] as StoreName[]) {
        if (!db.objectStoreNames.contains(name)) {
          const s = db.createObjectStore(name, { keyPath: "id" });
          if (BY_KB.includes(name)) s.createIndex("kbId", "kbId");
          if (name === "chunks") s.createIndex("docId", "docId");
          if (name === "runs") s.createIndex("workflowId", "workflowId");
        }
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbp;
}

function done<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
}

export async function all<K extends StoreName>(store: K): Promise<Stores[K][]> {
  const db = await open();
  return done(db.transaction(store).objectStore(store).getAll()) as Promise<Stores[K][]>;
}
export async function byIndex<K extends StoreName>(store: K, index: string, value: string): Promise<Stores[K][]> {
  const db = await open();
  return done(db.transaction(store).objectStore(store).index(index).getAll(value)) as Promise<Stores[K][]>;
}
export async function get<K extends StoreName>(store: K, id: string): Promise<Stores[K] | undefined> {
  const db = await open();
  return done(db.transaction(store).objectStore(store).get(id)) as Promise<Stores[K] | undefined>;
}
export async function put<K extends StoreName>(store: K, ...items: Stores[K][]): Promise<void> {
  const db = await open();
  const tx = db.transaction(store, "readwrite");
  for (const it of items) tx.objectStore(store).put(it);
  await new Promise<void>((res, rej) => { tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error); tx.onabort = () => rej(tx.error); });
}
export async function remove(store: StoreName, id: string): Promise<void> {
  const db = await open();
  await done(db.transaction(store, "readwrite").objectStore(store).delete(id));
}
/** Delete every row of `store` whose index `index` equals `value`. */
export async function removeWhere(store: StoreName, index: string, value: string): Promise<void> {
  const db = await open();
  const tx = db.transaction(store, "readwrite");
  const idx = tx.objectStore(store).index(index);
  await new Promise<void>((resolve, reject) => {
    const cur = idx.openKeyCursor(IDBKeyRange.only(value));
    cur.onsuccess = () => { const c = cur.result; if (c) { tx.objectStore(store).delete(c.primaryKey); c.continue(); } };
    tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error);
  });
}
export async function clearKbIndex(kbId: string, keepDocs = true) {
  for (const s of BY_KB) if (!(keepDocs && (s === "docs" || s === "chunks"))) await removeWhere(s, "kbId", kbId);
}
export async function deleteKb(kbId: string) {
  for (const s of BY_KB) await removeWhere(s, "kbId", kbId);
  await remove("kbs", kbId);
}
export async function wipeAll() {
  const db = await open();
  db.close(); dbp = null;
  await new Promise<void>((res, rej) => { const r = indexedDB.deleteDatabase(DB_NAME); r.onsuccess = () => res(); r.onerror = () => rej(r.error); });
  try { localStorage.removeItem(KEY_LS); sessionStorage.removeItem(KEY_LS); localStorage.removeItem(REMEMBER_LS); } catch { /* ignore */ }
}

export const newId = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(36).slice(2) + Date.now().toString(36));

// ------------------------------------------------------------------ API keys
// Keys stay in this browser. By default in sessionStorage (gone when the tab closes); "remember" moves them to localStorage.
const KEY_LS = "nexagent-lite.connections";
const REMEMBER_LS = "nexagent-lite.remember-keys";
function safe<T>(fn: () => T, fallback: T): T { try { return fn(); } catch { return fallback; } }
export const rememberKeys = () => safe(() => localStorage.getItem(REMEMBER_LS) === "1", false);
export function loadConnections(): Connection[] {
  const raw = safe(() => (rememberKeys() ? localStorage : sessionStorage).getItem(KEY_LS), null);
  return safe(() => (raw ? JSON.parse(raw) : []), []);
}
export function saveConnections(list: Connection[], remember = rememberKeys()) {
  safe(() => {
    localStorage.setItem(REMEMBER_LS, remember ? "1" : "0");
    (remember ? localStorage : sessionStorage).setItem(KEY_LS, JSON.stringify(list));
    (remember ? sessionStorage : localStorage).removeItem(KEY_LS);
  }, undefined);
}

// ------------------------------------------------------------------ export / import (never includes API keys)
export async function exportWorkspace(): Promise<string> {
  const out: Record<string, unknown> = { format: "nexagent-lite", version: 1, exportedAt: new Date().toISOString() };
  for (const s of ["kbs", "docs", "chunks", "entities", "relations", "communities", "workflows", "mcp"] as StoreName[]) {
    let rows: any[] = await all(s);
    if (s === "mcp") rows = rows.map((m) => ({ ...m, headers: {} }));   // auth headers are secrets
    out[s] = rows;
  }
  return JSON.stringify(out);
}
export async function importWorkspace(json: string): Promise<number> {
  const data = JSON.parse(json);
  if (data?.format !== "nexagent-lite") throw new Error("This is not a NexAgent Lite export file.");
  let n = 0;
  for (const s of ["kbs", "docs", "chunks", "entities", "relations", "communities", "workflows", "mcp"] as StoreName[]) {
    const rows = Array.isArray(data[s]) ? data[s] : [];
    if (rows.length) { await put(s, ...rows); n += rows.length; }
  }
  return n;
}
