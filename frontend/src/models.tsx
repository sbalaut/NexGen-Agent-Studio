import { useEffect, useId, useState } from "react";
import { api } from "./api";

// Models offered by each connection, fetched once per connection and shared by all pages.
const modelCache = new Map<string, Promise<string[]>>();
export function modelsFor(conn: string): Promise<string[]> {
  if (!conn) return Promise.resolve([]);
  if (!modelCache.has(conn)) modelCache.set(conn, api(`/models/available?connection=${encodeURIComponent(conn)}`)
    .then((r) => r.models || []).catch(() => []));
  return modelCache.get(conn)!;
}

export type ConnInfo = { name: string; kind: string; personal: number; enabled: number; max_classification: string };
let connCache: Promise<ConnInfo[]> | null = null;
export function connections(refresh = false): Promise<ConnInfo[]> {
  if (!connCache || refresh) connCache = api("/models/connections").then((c) => c.connections.filter((x: ConnInfo) => x.enabled)).catch(() => []);
  return connCache;
}
export function useConnections(): ConnInfo[] {
  const [c, setC] = useState<ConnInfo[]>([]);
  useEffect(() => { connections().then(setC); }, []);
  return c;
}

/** A free-text model field with suggestions from the chosen connection. */
export function ModelInput({ conn, value, onChange, placeholder, name, kind }: { conn: string; value?: string; onChange?: (v: string) => void;
  placeholder?: string; name?: string; kind?: "chat" | "embed" }) {
  const [list, setList] = useState<string[]>([]);
  const id = "m" + useId().replace(/:/g, "");
  useEffect(() => { let live = true; modelsFor(conn).then((m) => live && setList(m)); return () => { live = false; }; }, [conn]);
  const shown = kind === "embed" ? list.filter((m) => /embed/i.test(m)).concat(list.filter((m) => !/embed/i.test(m)))
    : kind === "chat" ? list.filter((m) => !/embed/i.test(m)) : list;
  return <>
    <input list={id} name={name} value={onChange ? value || "" : undefined} defaultValue={onChange ? undefined : value}
      placeholder={placeholder} onChange={onChange ? (e) => onChange(e.target.value) : undefined} />
    <datalist id={id}>{shown.map((m) => <option key={m} value={m} />)}</datalist>
  </>;
}

export const connLabel = (c: ConnInfo) => `${c.name}${c.personal ? " (your key)" : ""} · ${c.kind}`;
