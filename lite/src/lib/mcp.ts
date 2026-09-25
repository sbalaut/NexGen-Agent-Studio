// Minimal Model Context Protocol client for the browser ("Streamable HTTP" transport, JSON-RPC 2.0).
// Only initialize, tools/list and tools/call are used. Each operation opens a short session and closes it.
// The MCP server must allow this site's origin (CORS) and expose the Mcp-Session-Id header.

export const PROTOCOL_VERSION = "2025-06-18";
const CLIENT_INFO = { name: "nexagent-lite", version: "0.3" };
const MAX_RESULT = 20000;

export class MCPError extends Error {}
export type ToolResult = { text: string; isError: boolean };
export type ListedTool = { name: string; description: string; inputSchema: any };

/** Parse a Server-Sent-Events body and return the JSON-RPC message with the given id (or the last one). */
export function parseSse(body: string, id: number | string): any {
  let last: any = null;
  for (const event of body.split(/\r?\n\r?\n/)) {
    const data = event.split(/\r?\n/).filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
    if (!data) continue;
    try {
      const msg = JSON.parse(data);
      if (msg?.id === id) return msg;
      last = msg;
    } catch { /* ignore keep-alives */ }
  }
  return last;
}

export function resultText(result: any): ToolResult {
  const parts: string[] = [];
  for (const c of result?.content || []) {
    if (c.type === "text") parts.push(c.text || "");
    else if (c.type === "resource") parts.push(c.resource?.text || `[resource ${c.resource?.uri || ""}]`);
    else parts.push(`[${c.type || "content"} omitted]`);
  }
  let text = parts.join("\n");
  if (!text && result?.structuredContent != null) text = JSON.stringify(result.structuredContent);
  return { text: text.slice(0, MAX_RESULT), isError: !!result?.isError };
}

class Session {
  private sessionId: string | null = null;
  private nextId = 1;
  constructor(private url: string, private extra: Record<string, string>, private signal?: AbortSignal) {}

  private async post(payload: any): Promise<any> {
    const h: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": PROTOCOL_VERSION, ...this.extra };
    if (this.sessionId) h["Mcp-Session-Id"] = this.sessionId;
    let r: Response;
    try {
      r = await fetch(this.url, { method: "POST", headers: h, body: JSON.stringify(payload), signal: this.signal, referrerPolicy: "no-referrer" });
    } catch (e: any) {
      if (e?.name === "AbortError") throw e;
      throw new MCPError("The MCP server could not be reached from this browser. It must be online and allow this site (CORS).");
    }
    if (r.status === 401 || r.status === 403) throw new MCPError(`MCP server refused access (HTTP ${r.status}); check the auth header.`);
    if (r.status === 202) return null;
    const text = await r.text();
    if (!r.ok) throw new MCPError(`MCP server returned HTTP ${r.status}: ${text.slice(0, 200)}`);
    const sid = r.headers.get("mcp-session-id");
    if (sid) this.sessionId = sid;
    if (!text) return null;
    if ((r.headers.get("content-type") || "").includes("text/event-stream")) return parseSse(text, payload.id);
    return JSON.parse(text);
  }

  async request(method: string, params: any = {}): Promise<any> {
    const id = this.nextId++;
    const msg = await this.post({ jsonrpc: "2.0", id, method, params });
    if (!msg) throw new MCPError(`No reply to ${method}`);
    if (msg.error) throw new MCPError(`${method} failed: ${msg.error.message || JSON.stringify(msg.error)}`);
    return msg.result || {};
  }

  async open() {
    const init = await this.request("initialize", { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: CLIENT_INFO });
    await this.post({ jsonrpc: "2.0", method: "notifications/initialized" });
    return init;
  }

  async close() {
    if (!this.sessionId) return;
    try { await fetch(this.url, { method: "DELETE", headers: { "Mcp-Session-Id": this.sessionId, ...this.extra } }); } catch { /* ignore */ }
  }
}

async function withSession<T>(url: string, headers: Record<string, string>, fn: (s: Session) => Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!/^https?:\/\//i.test(url)) throw new MCPError("The MCP server URL must start with https:// (http:// only works for localhost).");
  const s = new Session(url, headers, signal);
  try {
    await s.open();
    return await fn(s);
  } finally { await s.close(); }
}

export async function listTools(url: string, headers: Record<string, string>): Promise<ListedTool[]> {
  return withSession(url, headers, async (s) => {
    const tools: ListedTool[] = [];
    let cursor: string | undefined;
    for (let page = 0; page < 20; page++) {
      const r = await s.request("tools/list", cursor ? { cursor } : {});
      for (const t of r.tools || []) tools.push({ name: t.name, description: t.description || "", inputSchema: t.inputSchema || {} });
      cursor = r.nextCursor;
      if (!cursor) break;
    }
    return tools;
  });
}

export async function callTool(url: string, headers: Record<string, string>, name: string, args: any, signal?: AbortSignal): Promise<ToolResult> {
  return withSession(url, headers, async (s) => resultText(await s.request("tools/call", { name, arguments: args || {} })), signal);
}
