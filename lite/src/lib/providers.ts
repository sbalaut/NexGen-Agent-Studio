// Model provider adapters that run in the browser: OpenAI (and OpenAI-compatible), Anthropic, Google Gemini.
// One neutral message format so agents can use tools with any provider (same design as the server edition).
import type { Connection, ProviderKind } from "./types";

export type ToolCall = { id: string; name: string; arguments: Record<string, any> };
export type Message =
  | { role: "system" | "user"; content: string }
  | { role: "assistant"; content: string; tool_calls?: ToolCall[] }
  | { role: "tool"; content: string; tool_call_id: string; name: string };
export type ToolDef = { name: string; description: string; parameters: any };
export type Turn = { text: string; toolCalls: ToolCall[]; usage: { input: number; output: number } };
export type ChatOptions = { temperature?: number; maxTokens?: number; json?: boolean; tools?: ToolDef[]; signal?: AbortSignal };

export const PRESETS: Record<Exclude<ProviderKind, "openai-compatible">, string> = {
  openai: "https://api.openai.com",
  anthropic: "https://api.anthropic.com",
  gemini: "https://generativelanguage.googleapis.com",
};
export const PROVIDER_LABEL: Record<ProviderKind, string> = {
  openai: "OpenAI", anthropic: "Anthropic (Claude)", gemini: "Google Gemini", "openai-compatible": "OpenAI-compatible",
};
export const canEmbed = (k: ProviderKind) => k !== "anthropic";

export class ProviderError extends Error {
  constructor(message: string, public status?: number) { super(message); }
}

const tid = () => "call_" + Math.random().toString(36).slice(2, 14);
function parseArgs(v: unknown): Record<string, any> {
  if (v && typeof v === "object") return v as Record<string, any>;
  try { const o = JSON.parse(String(v || "{}")); return o && typeof o === "object" ? o : { value: o }; } catch { return { _raw: String(v) }; }
}

export function baseUrl(c: Connection): string {
  return (c.kind === "openai-compatible" ? c.baseUrl : PRESETS[c.kind]).replace(/\/+$/, "").replace(/\/v1$/, "");
}

export function headers(c: Connection): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (c.kind === "anthropic") {
    h["x-api-key"] = c.apiKey;
    h["anthropic-version"] = "2023-06-01";
    h["anthropic-dangerous-direct-browser-access"] = "true";   // required for calls straight from a browser
  } else if (c.kind === "gemini") h["x-goog-api-key"] = c.apiKey;
  else if (c.apiKey) h["Authorization"] = "Bearer " + c.apiKey;
  return h;
}

async function call(c: Connection, path: string, init: RequestInit & { signal?: AbortSignal }): Promise<any> {
  const provider = c.kind === "openai-compatible" ? c.name : PROVIDER_LABEL[c.kind];
  let r: Response;
  try {
    r = await fetch(baseUrl(c) + path, { ...init, headers: { ...headers(c), ...(init.headers as any) }, referrerPolicy: "no-referrer" });
  } catch (e: any) {
    if (e?.name === "AbortError") throw e;
    throw new ProviderError(`${provider} could not be reached from this browser (network error or the service does not allow browser calls / CORS).`);
  }
  const text = await r.text();
  if (r.status === 401 || r.status === 403) throw new ProviderError(`${provider} rejected the API key (HTTP ${r.status}). Check it under Settings.`, r.status);
  if (r.status === 429) throw new ProviderError(`${provider}: rate limit or quota exceeded (HTTP 429).`, 429);
  if (r.status === 404) throw new ProviderError(`${provider}: model or endpoint not found (HTTP 404). ${text.slice(0, 200)}`, 404);
  if (!r.ok) throw new ProviderError(`${provider} returned HTTP ${r.status}: ${text.slice(0, 300)}`, r.status);
  try { return JSON.parse(text); } catch { throw new ProviderError(`${provider} returned a response that is not JSON.`); }
}

/** JSON-schema subset every provider accepts (Gemini is strictest). */
export function cleanSchema(schema: any, kind: ProviderKind): any {
  const keep = kind === "gemini" ? new Set(["type", "properties", "required", "description", "items", "enum", "format", "nullable", "minimum", "maximum", "anyOf"]) : null;
  const walk = (s: any): any => {
    if (!s || typeof s !== "object" || Array.isArray(s)) return s;
    const out: any = {};
    for (const [k, v] of Object.entries(s)) {
      if (keep && !keep.has(k)) continue;
      if (k === "properties" && v && typeof v === "object") out[k] = Object.fromEntries(Object.entries(v).map(([pk, pv]) => [pk, walk(pv)]));
      else if (k === "items") out[k] = walk(v);
      else if (k === "anyOf" && Array.isArray(v)) out[k] = v.map(walk);
      else out[k] = v;
    }
    if (Array.isArray(out.type)) { const t = out.type.filter((x: string) => x !== "null"); out.type = t[0] || "string"; }
    return out;
  };
  const s = walk(schema && typeof schema === "object" ? schema : {});
  if (!s.type) s.type = "object";
  if (s.type === "object" && !s.properties) s.properties = {};
  return s;
}

// ------------------------------------------------------------------ chat
export async function chat(c: Connection, model: string, messages: Message[], o: ChatOptions = {}): Promise<Turn> {
  if (!model) throw new ProviderError("No model selected for connection " + c.name);
  const maxTokens = o.maxTokens ?? 1200;
  if (c.kind === "anthropic") return anthropicChat(c, model, messages, o, maxTokens);
  if (c.kind === "gemini") return geminiChat(c, model, messages, o, maxTokens);
  return openaiChat(c, model, messages, o, maxTokens);
}

async function openaiChat(c: Connection, model: string, messages: Message[], o: ChatOptions, maxTokens: number): Promise<Turn> {
  const official = c.kind === "openai";
  const body: any = {
    model, messages: messages.map((m) => m.role === "assistant" && m.tool_calls?.length
      ? { role: "assistant", content: m.content || null, tool_calls: m.tool_calls.map((t) => ({ id: t.id, type: "function", function: { name: t.name, arguments: JSON.stringify(t.arguments) } })) }
      : m.role === "tool" ? { role: "tool", tool_call_id: m.tool_call_id, content: m.content } : { role: m.role, content: m.content }),
  };
  if (official) {
    body.max_completion_tokens = maxTokens;
    if (o.temperature != null && !/^(o1|o3|o4|gpt-5)/.test(model)) body.temperature = o.temperature;
  } else {
    body.max_tokens = maxTokens;
    if (o.temperature != null) body.temperature = o.temperature;
  }
  if (o.tools?.length) body.tools = o.tools.map((t) => ({ type: "function", function: { name: t.name, description: t.description, parameters: cleanSchema(t.parameters, c.kind) } }));
  if (o.json && !o.tools?.length) body.response_format = { type: "json_object" };
  const d = await call(c, "/v1/chat/completions", { method: "POST", body: JSON.stringify(body), signal: o.signal });
  const msg = d.choices?.[0]?.message || {};
  return {
    text: msg.content || "",
    toolCalls: (msg.tool_calls || []).map((t: any) => ({ id: t.id || tid(), name: t.function?.name, arguments: parseArgs(t.function?.arguments) })),
    usage: { input: d.usage?.prompt_tokens || 0, output: d.usage?.completion_tokens || 0 },
  };
}

async function anthropicChat(c: Connection, model: string, messages: Message[], o: ChatOptions, maxTokens: number): Promise<Turn> {
  let system = messages.filter((m) => m.role === "system").map((m) => m.content).join("\n\n");
  if (o.json) system += "\n\nReply with a single JSON object only, no other text.";
  const out: any[] = [];
  for (const m of messages) {
    if (m.role === "system") continue;
    const last = out[out.length - 1];
    if (m.role === "tool") {
      const block = { type: "tool_result", tool_use_id: m.tool_call_id, content: m.content };
      if (last?.role === "user" && Array.isArray(last.content) && last.content.every((b: any) => b.type === "tool_result")) last.content.push(block);
      else out.push({ role: "user", content: [block] });
    } else if (m.role === "assistant") {
      const blocks: any[] = m.content ? [{ type: "text", text: m.content }] : [];
      for (const t of m.tool_calls || []) blocks.push({ type: "tool_use", id: t.id, name: t.name, input: t.arguments });
      out.push({ role: "assistant", content: blocks.length ? blocks : [{ type: "text", text: "" }] });
    } else if (last?.role === "user" && typeof last.content === "string") last.content += "\n\n" + m.content;
    else out.push({ role: "user", content: m.content });
  }
  const body: any = { model, max_tokens: maxTokens, messages: out };
  if (system.trim()) body.system = system.trim();
  if (o.temperature != null) body.temperature = Math.min(1, o.temperature);
  if (o.tools?.length) body.tools = o.tools.map((t) => ({ name: t.name, description: t.description, input_schema: cleanSchema(t.parameters, c.kind) }));
  const d = await call(c, "/v1/messages", { method: "POST", body: JSON.stringify(body), signal: o.signal });
  const content: any[] = d.content || [];
  return {
    text: content.filter((b) => b.type === "text").map((b) => b.text).join(""),
    toolCalls: content.filter((b) => b.type === "tool_use").map((b) => ({ id: b.id, name: b.name, arguments: b.input || {} })),
    usage: { input: d.usage?.input_tokens || 0, output: d.usage?.output_tokens || 0 },
  };
}

const gname = (model: string) => (model.startsWith("models/") ? model : "models/" + model);

async function geminiChat(c: Connection, model: string, messages: Message[], o: ChatOptions, maxTokens: number): Promise<Turn> {
  const system = messages.filter((m) => m.role === "system").map((m) => m.content).join("\n\n");
  const contents: any[] = [];
  const names: Record<string, string> = {};
  for (const m of messages) {
    if (m.role === "system") continue;
    const last = contents[contents.length - 1];
    if (m.role === "assistant") {
      const parts: any[] = m.content ? [{ text: m.content }] : [];
      for (const t of m.tool_calls || []) { names[t.id] = t.name; parts.push({ functionCall: { name: t.name, args: t.arguments } }); }
      contents.push({ role: "model", parts: parts.length ? parts : [{ text: "" }] });
    } else if (m.role === "tool") {
      const part = { functionResponse: { name: m.name || names[m.tool_call_id] || "tool", response: { result: m.content } } };
      if (last?.role === "user" && last.parts.every((p: any) => p.functionResponse)) last.parts.push(part);
      else contents.push({ role: "user", parts: [part] });
    } else contents.push({ role: "user", parts: [{ text: m.content }] });
  }
  const body: any = { contents, generationConfig: { maxOutputTokens: maxTokens } };
  if (o.temperature != null) body.generationConfig.temperature = o.temperature;
  if (system.trim()) body.systemInstruction = { parts: [{ text: system.trim() }] };
  if (o.tools?.length) body.tools = [{ functionDeclarations: o.tools.map((t) => ({ name: t.name, description: t.description, parameters: cleanSchema(t.parameters, "gemini") })) }];
  else if (o.json) body.generationConfig.responseMimeType = "application/json";
  const d = await call(c, `/v1beta/${gname(model)}:generateContent`, { method: "POST", body: JSON.stringify(body), signal: o.signal });
  const cand = d.candidates?.[0];
  if (!cand) throw new ProviderError(`Gemini returned no answer (${d.promptFeedback?.blockReason || "no candidates"})`);
  const parts: any[] = cand.content?.parts || [];
  return {
    text: parts.filter((p) => "text" in p && !p.thought).map((p) => p.text).join(""),
    toolCalls: parts.filter((p) => p.functionCall).map((p) => ({ id: p.functionCall.id || tid(), name: p.functionCall.name, arguments: p.functionCall.args || {} })),
    usage: { input: d.usageMetadata?.promptTokenCount || 0, output: d.usageMetadata?.candidatesTokenCount || 0 },
  };
}

// ------------------------------------------------------------------ embeddings and model lists
export async function embed(c: Connection, model: string, texts: string[], signal?: AbortSignal): Promise<number[][]> {
  if (!canEmbed(c.kind)) throw new ProviderError("Anthropic has no embedding models; choose an OpenAI, Gemini or compatible connection.");
  const out: number[][] = [];
  if (c.kind === "gemini") {
    for (let i = 0; i < texts.length; i += 100) {
      const body = { requests: texts.slice(i, i + 100).map((t) => ({ model: gname(model), content: { parts: [{ text: t }] } })) };
      const d = await call(c, `/v1beta/${gname(model)}:batchEmbedContents`, { method: "POST", body: JSON.stringify(body), signal });
      out.push(...(d.embeddings || []).map((e: any) => e.values));
    }
    return out;
  }
  for (let i = 0; i < texts.length; i += 96) {
    const d = await call(c, "/v1/embeddings", { method: "POST", body: JSON.stringify({ model, input: texts.slice(i, i + 96) }), signal });
    out.push(...[...(d.data || [])].sort((a: any, b: any) => (a.index ?? 0) - (b.index ?? 0)).map((x: any) => x.embedding));
  }
  return out;
}

export async function listModels(c: Connection): Promise<string[]> {
  if (c.kind === "gemini") {
    const d = await call(c, "/v1beta/models?pageSize=200", { method: "GET" });
    return (d.models || []).filter((m: any) => (m.supportedGenerationMethods || []).some((x: string) => x === "generateContent" || x === "embedContent"))
      .map((m: any) => String(m.name).replace(/^models\//, "")).sort();
  }
  const d = await call(c, c.kind === "anthropic" ? "/v1/models?limit=100" : "/v1/models", { method: "GET" });
  return (d.data || []).map((m: any) => m.id).sort();
}
