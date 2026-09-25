"""Provider adapters: Ollama, OpenAI (and OpenAI-compatible), Anthropic, Google Gemini.

All adapters speak one neutral message format so agents can use tools with any provider:

    {"role": "system" | "user" | "assistant" | "tool",
     "content": str,
     "tool_calls": [{"id", "name", "arguments": dict}],   # assistant only
     "tool_call_id": str, "name": str}                      # tool only

and return a `Turn(text, tool_calls, usage)`.
Only the Gateway (llm.py) calls these, after the classification / SSRF checks.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

import httpx

PRESETS = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}
ANTHROPIC_VERSION = "2023-06-01"


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Turn:
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def _tid() -> str:
    return "call_" + uuid.uuid4().hex[:16]


def _args(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value or "{}")
        return out if isinstance(out, dict) else {"value": out}
    except json.JSONDecodeError:
        return {"_raw": str(value)}


def _check(r: httpx.Response, provider: str) -> dict:
    if r.status_code in (401, 403):
        raise ProviderError(f"{provider} rejected the API key (HTTP {r.status_code}). Check the key in Settings → My API keys.",
                            r.status_code)
    if r.status_code == 429:
        raise ProviderError(f"{provider} rate limit or quota exceeded (HTTP 429). Try again later or check your plan.", 429)
    if r.status_code == 404:
        raise ProviderError(f"{provider}: model or endpoint not found (HTTP 404): {r.text[:200]}", 404)
    if r.status_code >= 300:
        raise ProviderError(f"{provider} returned HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    return r.json()


def clean_schema(schema: dict | None, provider: str) -> dict:
    """JSON-schema subset every provider accepts (Gemini is strictest)."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    keep = {"type", "properties", "required", "description", "items", "enum", "format", "nullable",
            "minimum", "maximum", "anyOf"} if provider == "gemini" else None

    def walk(s):
        if not isinstance(s, dict):
            return s
        out = {}
        for k, v in s.items():
            if keep is not None and k not in keep:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: walk(pv) for pk, pv in v.items()}
            elif k == "items":
                out[k] = walk(v)
            elif k == "anyOf" and isinstance(v, list):
                out[k] = [walk(x) for x in v]
            else:
                out[k] = v
        if isinstance(out.get("type"), list):                  # ["string","null"] → Gemini wants a single type
            types = [t for t in out["type"] if t != "null"]
            out["type"] = types[0] if types else "string"
        return out
    s = walk(schema)
    s.setdefault("type", "object")
    if s["type"] == "object":
        s.setdefault("properties", {})
    return s


# ------------------------------------------------------------------ OpenAI / compatible
def _openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None,
                        "tool_calls": [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                       for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def openai_chat(client: httpx.Client, model: str, messages, tools, temperature, max_tokens, json_mode,
                official: bool = True) -> Turn:
    body = {"model": model, "messages": _openai_messages(messages)}
    if official:
        # api.openai.com: reasoning models reject `max_tokens`/custom temperature
        body["max_completion_tokens"] = max_tokens
        if temperature is not None and not model.startswith(("o1", "o3", "o4", "gpt-5")):
            body["temperature"] = temperature
    else:                                   # vLLM, Groq, LM Studio, ... use the classic names
        body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                            "parameters": clean_schema(t.get("parameters"), "openai")}}
                         for t in tools]
    if json_mode and not tools:
        body["response_format"] = {"type": "json_object"}
    data = _check(client.post("/v1/chat/completions", json=body), "OpenAI")
    msg = data["choices"][0]["message"]
    calls = [{"id": c.get("id") or _tid(), "name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}
             for c in msg.get("tool_calls") or []]
    return Turn(msg.get("content") or "", calls, data.get("usage") or {})


def openai_embed(client: httpx.Client, model: str, texts: list[str]) -> list[list[float]]:
    data = _check(client.post("/v1/embeddings", json={"model": model, "input": texts}), "OpenAI")
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]


def openai_models(client: httpx.Client) -> list[str]:
    return sorted(m["id"] for m in _check(client.get("/v1/models"), "OpenAI").get("data", []))


# ------------------------------------------------------------------ Ollama
def ollama_chat(client: httpx.Client, model: str, messages, tools, temperature, max_tokens, json_mode,
                num_ctx: int, keep_alive: str) -> Turn:
    msgs = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            msgs.append({"role": "assistant", "content": m.get("content") or "",
                         "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            msgs.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    body = {"model": model, "messages": msgs, "stream": False, "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": num_ctx}}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                            "parameters": clean_schema(t.get("parameters"), "ollama")}}
                         for t in tools]
    elif json_mode:
        body["format"] = "json"
    r = client.post("/api/chat", json=body)
    if r.status_code == 404:
        raise ProviderError(f"Model '{model}' is not installed in Ollama. Run: ollama pull {model}", 404)
    data = _check(r, "Ollama")
    msg = data.get("message") or {}
    calls = [{"id": _tid(), "name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}
             for c in msg.get("tool_calls") or []]
    return Turn(msg.get("content") or "", calls,
                {"prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count")})


# ------------------------------------------------------------------ Anthropic
def anthropic_chat(client: httpx.Client, model: str, messages, tools, temperature, max_tokens, json_mode) -> Turn:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    if json_mode:
        system += "\n\nReply with a single JSON object only, no other text."
    out: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                       for c in m.get("tool_calls") or []]
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], str):
                out[-1]["content"] += "\n\n" + m["content"]
            else:
                out.append({"role": "user", "content": m["content"]})
    body = {"model": model, "max_tokens": max_tokens, "messages": out}
    if system.strip():
        body["system"] = system.strip()
    if temperature is not None:
        body["temperature"] = min(1.0, float(temperature))
    if tools:
        body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                          "input_schema": clean_schema(t.get("parameters"), "anthropic")} for t in tools]
    data = _check(client.post("/v1/messages", json=body), "Anthropic")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    calls = [{"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}}
             for b in data.get("content", []) if b.get("type") == "tool_use"]
    return Turn(text, calls, data.get("usage") or {})


def anthropic_models(client: httpx.Client) -> list[str]:
    return sorted(m["id"] for m in _check(client.get("/v1/models", params={"limit": 100}), "Anthropic").get("data", []))


# ------------------------------------------------------------------ Gemini
def gemini_chat(client: httpx.Client, model: str, messages, tools, temperature, max_tokens, json_mode) -> Turn:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents: list[dict] = []
    names: dict[str, str] = {}
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant":
            parts = [{"text": m["content"]}] if m.get("content") else []
            for c in m.get("tool_calls") or []:
                names[c["id"]] = c["name"]
                parts.append({"functionCall": {"name": c["name"], "args": c["arguments"]}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif m["role"] == "tool":
            part = {"functionResponse": {"name": m.get("name") or names.get(m["tool_call_id"], "tool"),
                                         "response": {"result": m["content"]}}}
            if contents and contents[-1]["role"] == "user" and all("functionResponse" in p for p in contents[-1]["parts"]):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
        else:
            contents.append({"role": "user", "parts": [{"text": m["content"]}]})
    body: dict = {"contents": contents,
                  "generationConfig": {"maxOutputTokens": max_tokens}}
    if temperature is not None:
        body["generationConfig"]["temperature"] = temperature
    if system.strip():
        body["systemInstruction"] = {"parts": [{"text": system.strip()}]}
    if tools:
        body["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t.get("description", ""),
                                                    "parameters": clean_schema(t.get("parameters"), "gemini")}
                                                   for t in tools]}]
    elif json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    name = model if model.startswith("models/") else "models/" + model
    data = _check(client.post(f"/v1beta/{name}:generateContent", json=body), "Gemini")
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise ProviderError(f"Gemini returned no answer ({reason})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
    calls = [{"id": p["functionCall"].get("id") or _tid(), "name": p["functionCall"]["name"],
              "arguments": p["functionCall"].get("args") or {}} for p in parts if "functionCall" in p]
    return Turn(text, calls, data.get("usageMetadata") or {})


def gemini_embed(client: httpx.Client, model: str, texts: list[str]) -> list[list[float]]:
    name = model if model.startswith("models/") else "models/" + model
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        body = {"requests": [{"model": name, "content": {"parts": [{"text": t}]}} for t in texts[i:i + 100]]}
        data = _check(client.post(f"/v1beta/{name}:batchEmbedContents", json=body), "Gemini")
        out.extend(e["values"] for e in data.get("embeddings", []))
    return out


def gemini_models(client: httpx.Client) -> list[str]:
    data = _check(client.get("/v1beta/models", params={"pageSize": 200}), "Gemini")
    return sorted(m["name"].split("/", 1)[-1] for m in data.get("models", [])
                  if {"generateContent", "embedContent"} & set(m.get("supportedGenerationMethods", [])))


def auth_headers(kind: str, key: str | None) -> dict:
    if not key:
        return {}
    if kind == "anthropic":
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if kind == "gemini":
        return {"x-goog-api-key": key}
    return {"Authorization": "Bearer " + key}
