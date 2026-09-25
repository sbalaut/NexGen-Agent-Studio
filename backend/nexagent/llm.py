"""Model policy gateway. Every model-facing request (chat, review, embeddings)
goes through `Gateway`, which:

1. resolves the connection (local Ollama by default, admin-approved endpoints otherwise),
2. blocks the call *before any payload bytes are sent* when the content
   classification exceeds what the connection may receive,
3. only contacts hosts on the NEXAGENT_ALLOWED_MODEL_HOSTS allow-list, with
   redirects disabled (SSRF protection),
4. reports missing services explicitly — there is no silent fallback or mock.
"""
from __future__ import annotations

import ipaddress
import re
import json
import os
import socket
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import db
from .config import settings
from .policy import LEVEL
from .providers import (PRESETS, ProviderError, anthropic_chat, anthropic_models, auth_headers, gemini_chat,
                        gemini_embed, gemini_models, ollama_chat, openai_chat, openai_embed, openai_models)


_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_thinking(text: str) -> str:
    """Qwen3-family models may emit a <think> block; it is private reasoning and never shown or validated."""
    text = _THINK.sub("", text or "")
    return text.split("</think>", 1)[1].lstrip() if "</think>" in text else text


def query_for_embedding(model: str, question: str) -> str:
    """Qwen3-Embedding is trained with an instruction prefix on queries (documents are embedded as-is)."""
    if "qwen3-embedding" in model.lower():
        return ("Instruct: Given a question from a refinery engineer, retrieve passages from plant manuals "
                "that answer it\nQuery: " + question)
    return question


class GatewayBlocked(Exception):
    """Policy refused the call; nothing was sent."""


class ModelUnavailable(Exception):
    """The endpoint or model is not reachable / not installed."""


# ---------------------------------------------------------------- secrets
def _fernet():
    from cryptography.fernet import Fernet
    path: Path = settings().data_dir / "connections.key"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(Fernet.generate_key())
    return Fernet(path.read_bytes())


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    return _fernet().decrypt(value.encode()).decode() if value else None


# ---------------------------------------------------------------- connections
def ensure_default_connection(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM model_connections WHERE name='local-ollama' AND owner_user_id IS NULL").fetchone():
        # In public mode a local Ollama is usually absent: create it disabled unless explicitly configured.
        enabled = 0 if settings().public_mode and not os.environ.get("NEXAGENT_OLLAMA_URL") else 1
        conn.execute("""INSERT INTO model_connections(id,name,kind,base_url,max_classification,external,enabled,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (db.new_id(), "local-ollama", "ollama", settings().ollama_url,
                                          "Restricted", 0, enabled, db.now()))


def _resolve_ips(host: str, port: int) -> list:
    try:
        return [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, port)]
    except socket.gaierror as exc:
        raise ModelUnavailable(f"Cannot resolve host '{host}'") from exc


def check_url(url: str, *, personal: bool = False) -> None:
    """SSRF guard.
    Shared (admin) endpoints: host must be on NEXAGENT_ALLOWED_MODEL_HOSTS (public provider hosts are added
    automatically in public mode). Personal (user-supplied) endpoints: https only, and every resolved address
    must be a public internet address — users can never make the server call internal systems."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise GatewayBlocked("Endpoint URL must be http(s)://host[:port] without credentials")
    port = u.port or (443 if u.scheme == "https" else 80)
    if personal:
        if u.scheme != "https":
            raise GatewayBlocked("Personal endpoints must use https://")
        for ip in _resolve_ips(u.hostname, port):
            if not ip.is_global:
                raise GatewayBlocked(f"'{u.hostname}' resolves to a non-public address ({ip}); not allowed")
        return
    allowed = set(settings().allowed_model_hosts)
    if settings().public_mode:
        allowed |= {urlsplit(v).hostname for v in PRESETS.values()}
    if u.hostname not in allowed:
        raise GatewayBlocked(f"Host '{u.hostname}' is not on NEXAGENT_ALLOWED_MODEL_HOSTS")
    for ip in _resolve_ips(u.hostname, port):
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise GatewayBlocked(f"Host '{u.hostname}' resolves to a forbidden address {ip}")


@dataclass
class ChatResult:
    text: str
    model: str
    connection: str
    latency_ms: int
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def resolve_model(conn: sqlite3.Connection, model: str) -> str:
    """'promoted:<alias>' → the Ollama model of the alias' active deployment (pinned per run)."""
    if not model.startswith("promoted:"):
        return model
    alias = model.split(":", 1)[1]
    row = conn.execute("""SELECT d.ollama_model FROM model_aliases a JOIN deployments d ON d.id=a.deployment_id
        WHERE a.alias=? AND d.status='active'""", (alias,)).fetchone()
    if not row or not row[0]:
        raise ModelUnavailable(f"No active deployment for promoted model alias '{alias}'")
    return row[0]


def find_connection(conn: sqlite3.Connection, name: str, owner: str | None) -> dict | None:
    """A user's own connection with that name wins over a shared one."""
    if owner:
        row = db.one(conn, "SELECT * FROM model_connections WHERE name=? AND owner_user_id=? AND enabled=1", (name, owner))
        if row:
            return row
    return db.one(conn, "SELECT * FROM model_connections WHERE name=? AND owner_user_id IS NULL AND enabled=1", (name,))


def usable_connection_names(conn: sqlite3.Connection, owner: str | None) -> set[str]:
    rows = conn.execute("SELECT name FROM model_connections WHERE enabled=1 AND (owner_user_id IS NULL OR owner_user_id=?)",
                        (owner or "",)).fetchall()
    return {r[0] for r in rows}


class Gateway:
    def __init__(self, connection_name: str | None = None, owner: str | None = None, row: dict | None = None):
        if row is None:
            with db.read() as conn:
                row = find_connection(conn, connection_name or "local-ollama", owner)
        if not row:
            raise ModelUnavailable(f"Model connection '{connection_name or 'local-ollama'}' is not configured, "
                                   "not yours, or disabled")
        self.conn_row = row
        self.kind = row["kind"]
        self.personal = bool(row.get("owner_user_id"))
        self.timeout = settings().llm_timeout_s

    # -- policy -------------------------------------------------------------
    def _authorize(self, classification: str) -> None:
        ceiling = self.conn_row["max_classification"]
        if LEVEL[classification] > LEVEL[ceiling]:
            raise GatewayBlocked(f"{classification} content may not be sent to connection "
                                 f"'{self.conn_row['name']}' (allowed up to {ceiling}). Nothing was sent.")
        check_url(self.conn_row["base_url"], personal=self.personal)

    def _client(self) -> httpx.Client:
        key = decrypt_secret(self.conn_row.get("api_key_enc"))
        if self.kind in ("anthropic", "gemini") and not key:
            raise ModelUnavailable(f"Connection '{self.conn_row['name']}' has no API key")
        return httpx.Client(base_url=self.conn_row["base_url"], timeout=self.timeout, follow_redirects=False,
                            headers=auth_headers(self.kind, key), trust_env=False)

    def _call(self, fn):
        try:
            with self._client() as c:
                return fn(c)
        except httpx.ConnectError as exc:
            hint = " Start it (e.g. `ollama serve`) and retry." if self.kind == "ollama" else ""
            raise ModelUnavailable(f"Model service at {self.conn_row['base_url']} is not reachable.{hint}") from exc
        except httpx.TimeoutException as exc:
            raise ModelUnavailable("The model did not answer within the time limit") from exc
        except ProviderError as exc:
            raise ModelUnavailable(str(exc)) from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ModelUnavailable(f"Unexpected reply from {self.kind} ({type(exc).__name__})") from exc

    # -- operations ---------------------------------------------------------
    def chat(self, messages: list[dict], model: str, classification: str, *, temperature: float = 0.1,
             max_tokens: int = 800, json_mode: bool = False, tools: list[dict] | None = None) -> ChatResult:
        """messages/tools in the neutral format of providers.py; returns text and any tool calls."""
        self._authorize(classification)
        start = time.monotonic()
        official = urlsplit(self.conn_row["base_url"]).hostname == "api.openai.com"

        def run(c):
            if self.kind == "ollama":
                return ollama_chat(c, model, messages, tools, temperature, max_tokens, json_mode,
                                   settings().num_ctx, settings().keep_alive)
            if self.kind == "anthropic":
                return anthropic_chat(c, model, messages, tools, temperature, max_tokens, json_mode)
            if self.kind == "gemini":
                return gemini_chat(c, model, messages, tools, temperature, max_tokens, json_mode)
            return openai_chat(c, model, messages, tools, temperature, max_tokens, json_mode, official)
        turn = self._call(run)
        return ChatResult(text=strip_thinking(turn.text), model=model, connection=self.conn_row["name"],
                          latency_ms=int((time.monotonic() - start) * 1000), tool_calls=turn.tool_calls,
                          usage=turn.usage)

    def embed(self, texts: list[str], model: str, classification: str) -> list[list[float]]:
        self._authorize(classification)
        if not texts:
            return []

        def run(c):
            if self.kind == "ollama":
                r = c.post("/api/embed", json={"model": model, "input": texts, "keep_alive": settings().keep_alive})
                if r.status_code == 404:
                    raise ProviderError(f"Model '{model}' is not installed in Ollama. Run: ollama pull {model}")
                if r.status_code >= 300:
                    raise ProviderError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")
                return r.json().get("embeddings") or []
            if self.kind == "gemini":
                return gemini_embed(c, model, texts)
            if self.kind == "anthropic":
                raise ProviderError("Anthropic has no embedding API. Choose an OpenAI, Gemini or Ollama "
                                    "connection for the knowledge base's search embeddings.")
            return openai_embed(c, model, texts)
        vectors = self._call(run)
        if len(vectors) != len(texts):
            raise ModelUnavailable("Embedding service returned an unexpected number of vectors")
        return vectors

    def list_models(self) -> list[str]:
        check_url(self.conn_row["base_url"], personal=self.personal)

        def run(c):
            if self.kind == "ollama":
                return sorted(m["name"] for m in c.get("/api/tags").json().get("models", []))
            if self.kind == "anthropic":
                return anthropic_models(c)
            if self.kind == "gemini":
                return gemini_models(c)
            return openai_models(c)
        try:
            return self._call(run)
        except ModelUnavailable as exc:
            if self.kind == "ollama" and "not reachable" in str(exc):
                raise ModelUnavailable(f"The model service at {self.conn_row['base_url']} is not reachable. "
                                       "Start it (e.g. `ollama serve`) or fix NEXAGENT_OLLAMA_URL.") from exc
            raise
