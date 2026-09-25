// Data model of NexAgent Lite. Everything lives in this browser (IndexedDB + local/session storage).
export type ProviderKind = "openai" | "anthropic" | "gemini" | "openai-compatible";

export type Connection = { id: string; name: string; kind: ProviderKind; baseUrl: string; apiKey: string };

export type GraphMode = "off" | "local" | "global" | "both";

export type AgentSpec = {
  name: string;               // letters, digits, _ -
  role: string;               // shown to a team leader
  instructions: string;
  connection: string;         // Connection.id ("" = same as leader, members only)
  model: string;
  temperature: number;
  maxSteps: number;
  kbIds: string[];
  graph: GraphMode;
  mcpTools: string[];         // "<serverId>/<toolName>"
};

export type Workflow = {
  id: string; name: string; description: string;
  kind: "agent" | "team";
  leader: AgentSpec;
  members: AgentSpec[];
  checkAnswers: boolean;      // run the citation / number checks on every answer
  createdAt: number; updatedAt: number;
};

export type KB = {
  id: string; name: string; description: string;
  embedConnection: string; embedModel: string;          // "" = keyword search only
  graphMode: "off" | "rules" | "llm"; graphConnection: string; graphModel: string;
  status: "empty" | "indexing" | "ready" | "failed"; note: string; builtAt: number | null; createdAt: number;
};

export type Doc = { id: string; kbId: string; filename: string; size: number; chunks: number; addedAt: number; warning?: string };

export type Chunk = {
  id: string; kbId: string; docId: string; seq: number; filename: string; section: string; location: string;
  kind: "text" | "table_row"; text: string; tags: string[]; vector?: number[]; vectorKey?: string;
};

export type Entity = { id: string; kbId: string; name: string; norm: string; type: string; description: string;
  community: number; degree: number; chunkIds: string[] };
export type Relation = { id: string; kbId: string; src: string; dst: string; relation: string; weight: number; chunkId: string };
export type Community = { id: string; kbId: string; community: number; title: string; summary: string; size: number; vector?: number[] };

export type McpTool = { name: string; description: string; schema: any; enabled: boolean };
export type McpServer = { id: string; name: string; url: string; headers: Record<string, string>; tools: McpTool[];
  lastError: string | null; checkedAt: number | null };

export type Evidence = { ref: number; kind: "passage" | "summary" | "tool"; chunkId: string; kbId: string; filename: string;
  section: string; location: string; text: string; tags: string[] };

export type TraceStep = { agent: string; step: number; type: "tool" | "answer" | "error"; tool?: string; arguments?: string; result?: string };

export type Issue = { code: string; claim: string; explanation: string; blocking: boolean };

export type RunRecord = { id: string; workflowId: string; question: string; answer: string; evidence: Evidence[]; trace: TraceStep[];
  issues: Issue[]; status: "completed" | "unverified" | "failed" | "cancelled"; error?: string; usage: { input: number; output: number };
  createdAt: number; durationMs: number };
