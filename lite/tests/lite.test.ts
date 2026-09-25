import "fake-indexeddb/auto";
import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { chunkBlocks, parseText, tagsIn } from "../src/lib/ingest";
import { bm25, search } from "../src/lib/search";
import { checkAnswer, quantities } from "../src/lib/validate";
import { parseSse, resultText } from "../src/lib/mcp";
import { chat, cleanSchema, embed, headers } from "../src/lib/providers";
import { extractGraph, globalSummaries, localRanking } from "../src/lib/graph";
import { runWorkflow, newWorkflow, validateWorkflow } from "../src/lib/agents";
import { buildIndex } from "../src/lib/knowledge";
import { newId, put } from "../src/lib/store";
import type { Chunk, Connection, Evidence, KB, McpServer } from "../src/lib/types";

const MANUAL = readFileSync(new URL("../../samples/fictional-unit-manual.md", import.meta.url), "utf8");
const conn = (kind: Connection["kind"] = "openai"): Connection => ({ id: "c-" + kind, name: "my-" + kind, kind, baseUrl: kind === "openai-compatible" ? "https://llm.example.com/v1" : "", apiKey: "sk-test-key" });

type Call = { url: string; init: any; body: any };
function mockFetch(handler: (url: string, body: any, init: any) => any) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: any = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url, init, body });
    const out = await handler(url, body, init);
    if (out instanceof Response) return out;
    return new Response(JSON.stringify(out), { status: 200, headers: { "content-type": "application/json" } });
  }));
  return calls;
}
const vec = (t: string) => { const v = new Array(16).fill(0); for (const w of t.toLowerCase().split(/\W+/)) if (w) v[[...w].reduce((a, c) => a + c.charCodeAt(0), 0) % 16] += 1; return v; };

beforeEach(() => vi.unstubAllGlobals());

describe("ingest", () => {
  it("reads markdown sections and table rows with headers", () => {
    const blocks = parseText(MANUAL, true);
    const rows = blocks.filter((b) => b.kind === "table_row");
    expect(rows[0].text).toContain("Equipment: 10-P-101A | Property: Discharge pressure");
    expect(rows[0].section).toBe("Equipment specifications");
    const pieces = chunkBlocks(blocks);
    expect(pieces.every((p) => p.text.length <= 1800)).toBe(true);
    expect(pieces.some((p) => p.kind === "table_row" && p.tags.includes("10-P-101A"))).toBe(true);
  });
  it("finds equipment tags", () => {
    expect(tagsIn("Pump 10-P-101A trips on FIC-1203 low flow; see P-101.")).toEqual(["10-P-101A", "FIC-1203", "P-101"]);
  });
  it("detects headings in plain text", () => {
    const b = parseText("STARTUP PROCEDURE\nOpen the valve slowly.\n\n3.2 Shutdown\nClose it.", false);
    expect(b.filter((x) => x.kind === "heading").map((x) => x.text)).toEqual(["STARTUP PROCEDURE", "3.2 Shutdown"]);
  });
});

describe("search + checks", () => {
  const chunks: Chunk[] = chunkBlocks(parseText(MANUAL, true)).map((p, i) => ({ ...p, id: "k" + i, kbId: "kb", docId: "d", seq: i, filename: "manual.md" }));
  it("bm25 ranks the tagged table row first", () => {
    const s = bm25(chunks, "discharge pressure of 10-P-101A");
    const best = [...s.entries()].sort((a, b) => b[1] - a[1])[0][0];
    expect(chunks.find((c) => c.id === best)!.text).toContain("10-P-101A | Property: Discharge pressure");
  });
  const ev = (text: string, ref = 1, kind: Evidence["kind"] = "passage"): Evidence => ({ ref, kind, chunkId: "x" + ref, kbId: "kb", filename: "f", section: "s", location: "l", text, tags: tagsIn(text) });
  it("accepts a correctly cited value", () => {
    expect(checkAnswer("The discharge pressure of 10-P-101A is 5 bar [1].", [ev("Equipment: 10-P-101A | Fictional value: 5 bar")])).toEqual([]);
  });
  it("flags wrong values, wrong tags, missing citations, unknown refs and summary-only citations", () => {
    const e = [ev("Equipment: 10-P-101A | Fictional value: 5 bar"), ev("10-C-101 reference temperature 120 °C", 2), ev("Topic summary: pumps", 3, "summary")];
    const codes = (a: string) => checkAnswer(a, e).map((i) => i.code);
    expect(codes("The pressure of 10-P-101A is 7 bar [1].")).toContain("value_not_in_cited_source");
    expect(codes("The pressure of 10-C-101 is 5 bar [1].")).toContain("tag_not_in_cited_source");
    expect(codes("The pressure of 10-P-101A is 5 bar.")).toContain("missing_citation");
    expect(codes("It is 5 bar [9].")).toContain("unknown_source");
    expect(codes("10-P-101A runs at 5 bar [3].")).toContain("summary_not_primary_source");
    expect(codes("The sources do not contain this; it was not found in the accessible sources.")).toEqual([]);
  });
  it("does not read tag digits or citation numbers as values", () => {
    expect(quantities("10-P-101A at 5 bar [2]")).toEqual([{ value: 5, unit: "bar" }]);
  });
});

describe("providers (request shape)", () => {
  it("OpenAI: tools, tool results and auth header", async () => {
    const calls = mockFetch(() => ({ choices: [{ message: { content: null, tool_calls: [{ id: "t1", type: "function", function: { name: "f", arguments: "{\"a\":1}" } }] } }], usage: { prompt_tokens: 3, completion_tokens: 2 } }));
    const t = await chat(conn(), "gpt-x", [{ role: "system", content: "s" }, { role: "user", content: "u" },
      { role: "assistant", content: "", tool_calls: [{ id: "t0", name: "f", arguments: {} }] }, { role: "tool", tool_call_id: "t0", name: "f", content: "r" }],
      { tools: [{ name: "f", description: "d", parameters: { type: "object", properties: { a: { type: "integer" } } } }], maxTokens: 50 });
    expect(calls[0].url).toBe("https://api.openai.com/v1/chat/completions");
    expect(calls[0].init.headers.Authorization).toBe("Bearer sk-test-key");
    expect(calls[0].body.max_completion_tokens).toBe(50);
    expect(calls[0].body.messages[3]).toEqual({ role: "tool", tool_call_id: "t0", content: "r" });
    expect(t.toolCalls).toEqual([{ id: "t1", name: "f", arguments: { a: 1 } }]);
    expect(t.usage).toEqual({ input: 3, output: 2 });
  });
  it("OpenAI-compatible uses max_tokens and its own base URL", async () => {
    const calls = mockFetch(() => ({ choices: [{ message: { content: "hi" } }] }));
    await chat(conn("openai-compatible"), "llama", [{ role: "user", content: "u" }], { maxTokens: 10 });
    expect(calls[0].url).toBe("https://llm.example.com/v1/chat/completions");
    expect(calls[0].body.max_tokens).toBe(10);
  });
  it("Anthropic: browser header, system split, tool_result blocks", async () => {
    const calls = mockFetch(() => ({ content: [{ type: "text", text: "ok" }, { type: "tool_use", id: "tu", name: "f", input: { q: 1 } }], usage: { input_tokens: 1, output_tokens: 1 } }));
    const t = await chat(conn("anthropic"), "claude-x", [{ role: "system", content: "sys" }, { role: "user", content: "u" },
      { role: "assistant", content: "", tool_calls: [{ id: "a", name: "f", arguments: {} }] }, { role: "tool", tool_call_id: "a", name: "f", content: "r" }]);
    expect(calls[0].init.headers["anthropic-dangerous-direct-browser-access"]).toBe("true");
    expect(calls[0].body.system).toBe("sys");
    expect(calls[0].body.messages[2]).toEqual({ role: "user", content: [{ type: "tool_result", tool_use_id: "a", content: "r" }] });
    expect(t.toolCalls[0]).toEqual({ id: "tu", name: "f", arguments: { q: 1 } });
  });
  it("Gemini: functionCall / functionResponse, schema cleaning, embeddings", async () => {
    const calls = mockFetch((url) => url.includes("batchEmbed") ? { embeddings: [{ values: [1, 2] }] }
      : { candidates: [{ content: { parts: [{ functionCall: { name: "f", args: { x: 1 } } }] } }] });
    const t = await chat(conn("gemini"), "gemini-x", [{ role: "user", content: "u" }], { tools: [{ name: "f", description: "", parameters: { type: "object", additionalProperties: false, properties: { x: { type: ["integer", "null"] } } } }] });
    expect(calls[0].url).toContain("/v1beta/models/gemini-x:generateContent");
    expect(calls[0].body.tools[0].functionDeclarations[0].parameters).toEqual({ type: "object", properties: { x: { type: "integer" } } });
    expect(t.toolCalls[0].name).toBe("f");
    expect(await embed(conn("gemini"), "emb", ["a"])).toEqual([[1, 2]]);
    expect(headers(conn("gemini"))["x-goog-api-key"]).toBe("sk-test-key");
  });
  it("reports a rejected key clearly", async () => {
    mockFetch(() => new Response("{}", { status: 401 }));
    await expect(chat(conn(), "m", [{ role: "user", content: "u" }])).rejects.toThrow(/rejected the API key/);
  });
  it("cleanSchema keeps full JSON schema for OpenAI", () => {
    expect(cleanSchema({ type: "object", additionalProperties: false }, "openai")).toEqual({ type: "object", additionalProperties: false, properties: {} });
  });
});

describe("MCP client helpers", () => {
  it("parses SSE replies and tool results", () => {
    const body = 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\nevent: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":1}}\n\n';
    expect(parseSse(body, 2)).toEqual({ jsonrpc: "2.0", id: 2, result: { ok: 1 } });
    expect(resultText({ content: [{ type: "text", text: "42" }, { type: "image" }], isError: false })).toEqual({ text: "42\n[image omitted]", isError: false });
  });
});

describe("GraphRAG + agents (end to end with mocked providers)", () => {
  const openai = conn();
  const kb: KB = { id: "kb1", name: "Manual", description: "", embedConnection: openai.id, embedModel: "emb", graphMode: "rules", graphConnection: "", graphModel: "",
    status: "empty", note: "", builtAt: null, createdAt: 1 };

  it("builds a rules graph with seeded, reproducible communities", async () => {
    const chunks = chunkBlocks(parseText(MANUAL, true)).map((p, i) => ({ ...p, id: "g" + i, kbId: "kb", docId: "d", seq: i, filename: "m.md" }));
    const a = await extractGraph({ kb, chunks }), b = await extractGraph({ kb, chunks });
    expect(a.entities.has("10-p-101a")).toBe(true);
    expect(a.comm).toEqual(b.comm);
  });

  it("indexes, then an agent uses knowledge search, an MCP tool and a team member", async () => {
    const chunks = chunkBlocks(parseText(MANUAL, true)).map((p, i) => ({ ...p, id: newId(), kbId: kb.id, docId: "d1", seq: i, filename: "manual.md" }));
    await put("kbs", kb); await put("chunks", ...chunks);
    const replies: any[] = [
      { tool_calls: [{ name: "delegate_to_researcher", arguments: { task: "find the discharge pressure of 10-P-101A" } }] },
      { tool_calls: [{ name: "search_knowledge", arguments: { query: "10-P-101A discharge pressure" } }] },   // member
      { tool_calls: [{ name: "mcp_plant__status", arguments: { unit: "CDU-1" } }] },                           // member
      (b: any) => `10-P-101A: 5 bar [${refFor(b)}]. CDU-1 is running.`,                                          // member answer
      (b: any) => `The design discharge pressure of 10-P-101A is 5 bar [${refFor(b)}]. CDU-1 is running normally.`,  // leader answer
    ];
    const mcpCalls: any[] = [];
    // the scripted "model" cites whichever numbered passage really holds the value
    const refFor = (b: any) => JSON.stringify(b.messages).match(/\[(\d+)\] manual\.md[^\[]*?5 bar/)?.[1]
      ?? JSON.stringify(b.messages).match(/5 bar \[(\d+)\]/)?.[1] ?? "?";
    const calls = mockFetch((url, body, init) => {
      if (url.endsWith("/v1/embeddings")) return { data: body.input.map((t: string, i: number) => ({ index: i, embedding: vec(t) })) };
      if (url.startsWith("https://mcp.example.com")) {
        if (init.method === "DELETE") return new Response(null, { status: 204 });
        mcpCalls.push(body);
        if (!body.id) return new Response(null, { status: 202 });
        const result = body.method === "initialize" ? { protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "t" } }
          : { content: [{ type: "text", text: `Unit ${body.params.arguments.unit} is running normally.` }] };
        return new Response(`event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: body.id, result })}\n\n`,
          { status: 200, headers: { "content-type": "text/event-stream", "mcp-session-id": "s1" } });
      }
      let r = replies.shift();
      if (typeof r === "function") r = r(body);
      const msg = typeof r === "string" ? { content: r } : { content: null, tool_calls: r.tool_calls.map((c: any, i: number) => ({ id: "c" + i, type: "function", function: { name: c.name, arguments: JSON.stringify(c.arguments) } })) };
      return { choices: [{ message: msg }], usage: { prompt_tokens: 10, completion_tokens: 5 } };
    });
    const built = await buildIndex(kb.id, [openai], () => {});
    expect(built.status).toBe("ready");
    expect(built.note).toMatch(/graph: \d+ entities/);
    expect((await localRanking(kb.id, "What is the discharge pressure of 10-P-101A?")).length).toBeGreaterThan(0);
    expect(Array.isArray(await globalSummaries(kb.id, "pumps", null))).toBe(true);
    const direct = await search([built], [openai], "discharge pressure 10-P-101A", { graph: "both" });
    expect(direct[0].text).toContain("10-P-101A");

    const server: McpServer = { id: "srv", name: "plant", url: "https://mcp.example.com/mcp", headers: {}, lastError: null, checkedAt: 1,
      tools: [{ name: "status", description: "Unit status", schema: { type: "object", properties: { unit: { type: "string" } } }, enabled: true }] };
    const wf = newWorkflow("team", openai.id, "gpt-x");
    wf.members[0].kbIds = [kb.id];
    wf.members[0].mcpTools = ["srv/status"];
    expect(validateWorkflow(wf, { connections: [openai], kbs: [built], servers: [server] })).toEqual([]);
    const rec = await runWorkflow(wf, "Is CDU-1 running and what is the discharge pressure of 10-P-101A?", { connections: [openai], kbs: [built], servers: [server] });
    expect(rec.error).toBeUndefined();
    expect({ status: rec.status, issues: rec.issues, ev: rec.evidence.map((e) => e.text.slice(0, 60)) }).toMatchObject({ status: "completed", issues: [] });
    expect(rec.evidence.some((e) => e.text.includes("5 bar"))).toBe(true);
    expect(rec.trace.map((t) => t.tool).filter(Boolean)).toEqual(["search_knowledge", "mcp_plant__status", "delegate_to_researcher"]);
    expect(mcpCalls.map((m) => m.method)).toEqual(["initialize", "notifications/initialized", "tools/call"]);
    const toolMsgs = calls.filter((c) => c.url.endsWith("/chat/completions")).flatMap((c) => c.body.messages).filter((m: any) => m.role === "tool");
    expect(toolMsgs.some((m: any) => m.content.includes("running normally"))).toBe(true);
    expect(rec.usage.input).toBe(50);
  });

  it("flags an unsupported number in the final answer", async () => {
    mockFetch(() => ({ choices: [{ message: { content: "The pressure of 10-P-101A is 9 bar." } }] }));
    const wf = newWorkflow("agent", openai.id, "gpt-x");
    const rec = await runWorkflow(wf, "pressure?", { connections: [openai], kbs: [], servers: [] });
    expect(rec.status).toBe("unverified");
    expect(rec.issues.map((i) => i.code)).toContain("missing_citation");
  });
});
