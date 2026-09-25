import { createContext, ReactNode, useContext, useEffect, useId, useState } from "react";
import { all, loadConnections, saveConnections } from "./lib/store";
import { listModels } from "./lib/providers";
import type { Connection, KB, McpServer, Workflow } from "./lib/types";

type Data = { connections: Connection[]; setConnections: (c: Connection[], remember?: boolean) => void;
  kbs: KB[]; workflows: Workflow[]; servers: McpServer[]; reload: () => Promise<void>; loaded: boolean };
const Ctx = createContext<Data>(null as any);
export const useData = () => useContext(Ctx);

export function DataProvider({ children }: { children: ReactNode }) {
  const [connections, setConns] = useState<Connection[]>(loadConnections());
  const [kbs, setKbs] = useState<KB[]>([]);
  const [workflows, setWfs] = useState<Workflow[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loaded, setLoaded] = useState(false);
  const reload = async () => {
    const [k, w, s] = await Promise.all([all("kbs"), all("workflows"), all("mcp")]);
    setKbs(k.sort((a, b) => a.createdAt - b.createdAt)); setWfs(w.sort((a, b) => b.updatedAt - a.updatedAt)); setServers(s.sort((a, b) => a.name.localeCompare(b.name)));
    setLoaded(true);
  };
  useEffect(() => { reload().catch(() => setLoaded(true)); }, []);
  const setConnections = (c: Connection[], remember?: boolean) => { saveConnections(c, remember); setConns(c); };
  return <Ctx.Provider value={{ connections, setConnections, kbs, workflows, servers, reload, loaded }}>{children}</Ctx.Provider>;
}

const modelCache = new Map<string, Promise<string[]>>();
export function modelsFor(c?: Connection): Promise<string[]> {
  if (!c) return Promise.resolve([]);
  const key = c.id + ":" + c.apiKey.slice(-6);
  if (!modelCache.has(key)) modelCache.set(key, listModels(c).catch(() => { modelCache.delete(key); return []; }));
  return modelCache.get(key)!;
}

export function ModelInput({ conn, value, onChange, kind, placeholder }: { conn?: Connection; value: string; onChange: (v: string) => void;
  kind?: "chat" | "embed"; placeholder?: string }) {
  const [list, setList] = useState<string[]>([]);
  const id = "m" + useId().replace(/:/g, "");
  useEffect(() => { let live = true; modelsFor(conn).then((m) => live && setList(m)); return () => { live = false; }; }, [conn?.id]);
  const shown = kind === "embed" ? list.filter((m) => /embed/i.test(m)) : kind === "chat" ? list.filter((m) => !/embed|tts|whisper|dall-e|image|audio|moderation/i.test(m)) : list;
  return <>
    <input list={id} value={value} placeholder={placeholder || "Model name"} onChange={(e) => onChange(e.target.value)} />
    <datalist id={id}>{shown.map((m) => <option key={m} value={m} />)}</datalist>
  </>;
}

export const SUGGESTED: Record<string, { chat: string; embed?: string }> = {
  openai: { chat: "gpt-4.1-mini", embed: "text-embedding-3-small" },
  anthropic: { chat: "claude-sonnet-4-5" },
  gemini: { chat: "gemini-2.5-flash", embed: "gemini-embedding-001" },
  "openai-compatible": { chat: "" },
};
