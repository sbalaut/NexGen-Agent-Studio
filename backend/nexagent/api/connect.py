"""Personal model connections (user-owned API keys) and MCP servers/tools."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..llm import Gateway, GatewayBlocked, ModelUnavailable, check_url, decrypt_secret, encrypt_secret
from ..mcp_client import MCPError, list_tools
from ..policy import Actor, require
from ..providers import PRESETS
from ..security import CurrentActor

router = APIRouter()
NAME = r"^[a-z0-9][a-z0-9._-]{1,40}$"


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class MyConnectionIn(Strict):
    name: str = Field(pattern=NAME)
    provider: str = Field(pattern="^(openai|anthropic|gemini|openai-compatible)$")
    api_key: str = Field(min_length=8, max_length=500)
    base_url: str | None = Field(default=None, max_length=300)
    max_classification: str = Field(default="Internal", pattern="^(Public|Internal)$")


class MCPServerIn(Strict):
    name: str = Field(pattern=NAME)
    transport: str = Field(pattern="^(http|stdio)$")
    url: str | None = Field(default=None, max_length=500)
    headers: dict[str, str] = {}
    command: str | None = Field(default=None, max_length=300)
    args: list[str] = Field(default=[], max_length=30)
    env: dict[str, str] = {}
    max_classification: str = Field(default="Public", pattern="^(Public|Internal|Restricted)$")
    shared: bool = False


class ToolToggle(Strict):
    enabled: bool


def _personal_allowed(actor: Actor) -> None:
    require(settings().public_mode or actor.is_admin,
            "Personal API keys are only available on public servers; ask an administrator to add a shared connection")


# ------------------------------------------------------------------ personal model connections
@router.get("/me/connections")
def my_connections(actor: Actor = CurrentActor):
    with db.read() as conn:
        rows = db.all_rows(conn, """SELECT id,name,kind,base_url,max_classification,enabled,created_at FROM model_connections
            WHERE owner_user_id=? ORDER BY name""", (actor.id,))
        shared = db.all_rows(conn, """SELECT name,kind,max_classification FROM model_connections
            WHERE owner_user_id IS NULL AND enabled=1 ORDER BY name""")
    return {"mine": rows, "shared": shared, "providers": {k: v for k, v in PRESETS.items()},
            "notice": "Keys are encrypted on the server and never shown again. The server operator can technically "
                      "use them to make calls on your behalf; only add keys you are comfortable storing here."}


@router.post("/me/connections", status_code=201)
def add_my_connection(body: MyConnectionIn, actor: Actor = CurrentActor):
    _personal_allowed(actor)
    kind = "openai" if body.provider == "openai-compatible" else body.provider
    base = (body.base_url or "").rstrip("/") if body.provider == "openai-compatible" else PRESETS[body.provider]
    if body.provider == "openai-compatible":
        if not base:
            raise HTTPException(422, "base_url is required for an OpenAI-compatible endpoint")
        base = re.sub(r"/v1$", "", base)
    try:
        check_url(base, personal=True)
    except (GatewayBlocked, ModelUnavailable) as exc:
        raise HTTPException(400, str(exc)) from None
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM model_connections WHERE owner_user_id=? AND name=?", (actor.id, body.name)).fetchone():
            raise HTTPException(409, "You already have a connection with that name")
        if conn.execute("SELECT count(*) FROM model_connections WHERE owner_user_id=?", (actor.id,)).fetchone()[0] >= 20:
            raise HTTPException(400, "At most 20 personal connections")
        cid = db.new_id()
        conn.execute("""INSERT INTO model_connections(id,name,kind,base_url,max_classification,external,enabled,api_key_enc,
            owner_user_id,created_at) VALUES(?,?,?,?,?,1,1,?,?,?)""",
                     (cid, body.name, kind, base, body.max_classification, encrypt_secret(body.api_key), actor.id, db.now()))
        db.audit(conn, actor.id, "connection.personal_added", "model_connection", cid, {"provider": body.provider})
    return {"id": cid}


@router.delete("/me/connections/{cid}")
def delete_my_connection(cid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        n = conn.execute("DELETE FROM model_connections WHERE id=? AND owner_user_id=?", (cid, actor.id)).rowcount
        if not n:
            raise HTTPException(404, "Connection not found")
        db.audit(conn, actor.id, "connection.personal_deleted", "model_connection", cid, {})
    return {"deleted": True}


@router.get("/me/connections/{name}/models")
def my_models(name: str, actor: Actor = CurrentActor):
    try:
        return {"models": Gateway(name, owner=actor.id).list_models(), "reachable": True}
    except (ModelUnavailable, GatewayBlocked) as exc:
        return {"models": [], "reachable": False, "message": str(exc)}


# ------------------------------------------------------------------ MCP servers
def _server_for(conn, actor: Actor, sid: str, manage: bool = True) -> dict:
    row = db.one(conn, "SELECT * FROM mcp_servers WHERE id=?", (sid,))
    if not row:
        raise HTTPException(404, "MCP server not found")
    if row["owner_user_id"]:
        if row["owner_user_id"] != actor.id:
            raise HTTPException(404, "MCP server not found")
    elif manage:
        require(actor.is_admin, "Only an administrator can change a shared MCP server")
    return row


def _public(row: dict, tools: list[dict]) -> dict:
    out = {k: row[k] for k in ("id", "name", "transport", "url", "command", "max_classification", "enabled",
                               "last_error", "checked_at", "created_at")}
    out["shared"] = row["owner_user_id"] is None
    out["args"] = json.loads(row["args_json"] or "[]")
    out["has_headers"] = bool(row["headers_enc"])
    out["tools"] = tools
    return out


@router.get("/mcp/servers")
def mcp_servers(actor: Actor = CurrentActor):
    require(settings().has("mcp"), "MCP is not enabled on this server")
    with db.read() as conn:
        rows = db.all_rows(conn, "SELECT * FROM mcp_servers WHERE owner_user_id IS NULL OR owner_user_id=? ORDER BY name",
                           (actor.id,))
        out = []
        for r in rows:
            tools = db.all_rows(conn, "SELECT name,description,enabled,schema_json FROM mcp_tools WHERE server_id=? ORDER BY name",
                                (r["id"],))
            if r["owner_user_id"] is None and not actor.is_admin:
                if not r["enabled"]:
                    continue
                tools = [t for t in tools if t["enabled"]]
                r = dict(r, command=None, url=None)          # non-admins do not see internal addresses
            for t in tools:
                t["id"] = f"{r['id']}/{t['name']}"
                t["schema"] = json.loads(t.pop("schema_json") or "{}")
            out.append(_public(r, tools))
    return {"servers": out, "stdio_allowed": settings().allow_stdio_mcp and actor.is_admin}


@router.post("/mcp/servers", status_code=201)
def add_mcp_server(body: MCPServerIn, actor: Actor = CurrentActor):
    require(settings().has("mcp"), "MCP is not enabled on this server")
    shared = body.shared or not settings().public_mode
    if shared:
        require(actor.is_admin, "Only an administrator can add shared MCP servers")
    if body.transport == "stdio":
        require(actor.is_admin and settings().allow_stdio_mcp and shared,
                "Local (command) MCP servers can only be added by an administrator, and only when NEXAGENT_ALLOW_STDIO_MCP=1")
        if not body.command:
            raise HTTPException(422, "command is required for a local MCP server")
    else:
        if not body.url:
            raise HTTPException(422, "url is required for a remote MCP server")
        try:
            if shared:
                _check_shared_mcp_url(body.url)
            else:
                check_url(body.url, personal=True)
        except (GatewayBlocked, ModelUnavailable) as exc:
            raise HTTPException(400, str(exc)) from None
    if not shared and body.max_classification == "Restricted":
        raise HTTPException(400, "Personal remote MCP servers may not receive Restricted content")
    sid = db.new_id()
    with db.tx() as conn:
        owner = None if shared else actor.id
        dup = conn.execute("SELECT 1 FROM mcp_servers WHERE name=? AND owner_user_id IS ?", (body.name, owner)).fetchone()
        if dup:
            raise HTTPException(409, "An MCP server with that name already exists")
        conn.execute("""INSERT INTO mcp_servers(id,name,transport,url,command,args_json,env_enc,headers_enc,max_classification,
            owner_user_id,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
                     (sid, body.name, body.transport, body.url, body.command, json.dumps(body.args),
                      encrypt_secret(json.dumps(body.env)) if body.env else None,
                      encrypt_secret(json.dumps(body.headers)) if body.headers else None,
                      body.max_classification, owner, db.now()))
        db.audit(conn, actor.id, "mcp.server_added", "mcp_server", sid,
                 {"name": body.name, "transport": body.transport, "shared": shared})
    refresh_mcp_server(sid, actor)
    return {"id": sid}


def _check_shared_mcp_url(url: str) -> None:
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise GatewayBlocked("MCP URL must be http(s)://host[:port]/path without credentials")


@router.post("/mcp/servers/{sid}/refresh")
def refresh_mcp_server(sid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        row = _server_for(conn, actor, sid)
    secrets = {"headers": json.loads(decrypt_secret(row["headers_enc"]) or "{}"),
               "env": json.loads(decrypt_secret(row["env_enc"]) or "{}")}
    try:
        if row["transport"] == "http" and row["owner_user_id"]:
            check_url(row["url"], personal=True)
        tools = list_tools(row, secrets)
        error = None
    except (MCPError, OSError, ValueError, GatewayBlocked, ModelUnavailable) as exc:
        tools, error = [], str(exc)[:500]
    with db.tx() as conn:
        conn.execute("UPDATE mcp_servers SET last_error=?, checked_at=? WHERE id=?", (error, db.now(), sid))
        if error is None:
            names = sorted({t["name"] for t in tools})
            marks = ",".join("?" * len(names)) or "''"
            conn.execute(f"DELETE FROM mcp_tools WHERE server_id=? AND name NOT IN ({marks})", [sid, *names])
            for t in tools:
                conn.execute("""INSERT INTO mcp_tools(server_id,name,description,schema_json,enabled) VALUES(?,?,?,?,0)
                    ON CONFLICT(server_id,name) DO UPDATE SET description=excluded.description, schema_json=excluded.schema_json""",
                             (sid, t["name"], (t.get("description") or "")[:2000], json.dumps(t.get("inputSchema") or {})))
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "tools": len(tools),
            "notice": "New tools start disabled. Review each tool and enable the ones agents may use."}


@router.put("/mcp/servers/{sid}/tools/{name}")
def toggle_tool(sid: str, name: str, body: ToolToggle, actor: Actor = CurrentActor):
    with db.tx() as conn:
        _server_for(conn, actor, sid)
        n = conn.execute("UPDATE mcp_tools SET enabled=? WHERE server_id=? AND name=?", (int(body.enabled), sid, name)).rowcount
        if not n:
            raise HTTPException(404, "Tool not found")
        db.audit(conn, actor.id, "mcp.tool_" + ("enabled" if body.enabled else "disabled"), "mcp_server", sid, {"tool": name})
    return {"updated": True}


@router.put("/mcp/servers/{sid}/enabled")
def toggle_server(sid: str, body: ToolToggle, actor: Actor = CurrentActor):
    with db.tx() as conn:
        _server_for(conn, actor, sid)
        conn.execute("UPDATE mcp_servers SET enabled=? WHERE id=?", (int(body.enabled), sid))
        db.audit(conn, actor.id, "mcp.server_" + ("enabled" if body.enabled else "disabled"), "mcp_server", sid, {})
    return {"updated": True}


@router.delete("/mcp/servers/{sid}")
def delete_server(sid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        _server_for(conn, actor, sid)
        conn.execute("DELETE FROM mcp_servers WHERE id=?", (sid,))
        db.audit(conn, actor.id, "mcp.server_deleted", "mcp_server", sid, {})
    return {"deleted": True}
