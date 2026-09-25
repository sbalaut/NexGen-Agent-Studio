// Same-origin API client. Paths are relative so the app also works behind a proxy prefix.
export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

let csrf = "";
export function setCsrf(v: string) { csrf = v; }

export async function api<T = any>(path: string, options: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = options;
  const headers: Record<string, string> = { ...(rest.headers as Record<string, string> || {}) };
  if (json !== undefined) headers["Content-Type"] = "application/json";
  if (csrf && (rest.method || "GET") !== "GET") headers["X-CSRF-Token"] = csrf;
  let res: Response;
  try {
    res = await fetch("api" + path, { ...rest, headers, credentials: "same-origin", cache: "no-store",
      body: json !== undefined ? JSON.stringify(json) : rest.body });
  } catch {
    throw new ApiError(0, "The NexAgent server could not be reached. Check that it is running.");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data?.detail;
    throw new ApiError(res.status, typeof d === "string" ? d : Array.isArray(d) ? d.map((x: any) => x.msg).join("; ")
      : `The server returned HTTP ${res.status}.`);
  }
  return data as T;
}

export const post = <T = any>(path: string, json: unknown = {}) => api<T>(path, { method: "POST", json });
export const put = <T = any>(path: string, json: unknown = {}) => api<T>(path, { method: "PUT", json });
export const del = <T = any>(path: string) => api<T>(path, { method: "DELETE" });

export type User = { id: string; username: string; display_name: string; roles: string[]; permissions: string[] };
export type Project = { id: string; name: string; description: string; classification_floor: string;
  membership: "owner" | "editor" | "viewer"; knowledge_bases: number; workflows: number; assistants: number; created_at: number;
  require_review?: number };
export type KB = { id: string; name: string; description: string; classification: string; can_write: number;
  documents: number; chunks: number | null; last_index_status: string | null; last_index_error: string | null; active_generation: string | null;
  embedding_connection?: string | null; embedding_model?: string | null; graph_mode?: string; graph_connection?: string | null; graph_model?: string | null };

export function fmtTime(t?: number | null) {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// Subscribe to a run's server-sent events. Returns a close function.
export function followRun(runId: string, onEvent: (type: string, data: any) => void): () => void {
  const es = new EventSource(`api/runs/${runId}/events`, { withCredentials: true });
  for (const t of ["status", "node", "final"]) {
    es.addEventListener(t, (e) => {
      onEvent(t, JSON.parse((e as MessageEvent).data));
      if (t === "final") es.close();
    });
  }
  es.onerror = () => { /* browser retries automatically with Last-Event-ID */ };
  return () => es.close();
}

export type AppConfig = { public_mode: boolean; allow_signup: boolean; features: string[]; min_password: number };
export let appConfig: AppConfig = { public_mode: false, allow_signup: false, features: [], min_password: 12 };
export async function loadConfig() {
  try { appConfig = await api<AppConfig>("/config"); } catch { /* keep defaults */ }
  return appConfig;
}
export const hasFeature = (f: string) => appConfig.features.includes(f);
