"""Authentication, accounts, model connections, settings and audit."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from .. import db
from ..config import settings
from ..llm import Gateway, GatewayBlocked, ModelUnavailable, check_url, encrypt_secret
from ..policy import ALL_ROLES, PERMISSIONS, Actor, Role, load_actor, require, require_admin
from ..security import (ACCOUNT_ALLOWANCE, ADDRESS_ALLOWANCE, MIN_PASSWORD, CurrentActor, check_password,
                        clear_failures, client_address, csrf_for, hash_password, hasher, new_session,
                        record_failure, require_origin, throttle_state, token_digest)

router = APIRouter()


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class LoginIn(Strict):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def valid_username(v: str) -> str:
    v = v.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,63}", v):
        raise ValueError("Use 3-64 lowercase letters, digits, '.', '_' or '-', starting with a letter")
    return v


class UserIn(Strict):
    username: str
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)
    roles: list[str] = Field(min_length=1)
    permissions: list[str] = []

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        return valid_username(v)


class UserUpdate(Strict):
    display_name: str = Field(min_length=1, max_length=120)
    active: bool
    roles: list[str] = Field(min_length=1)
    permissions: list[str] = []


class PasswordIn(Strict):
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class OwnPasswordIn(Strict):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class ConnectionIn(Strict):
    name: str = Field(min_length=2, max_length=60, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    kind: str = Field(pattern="^(ollama|openai|anthropic|gemini)$")
    base_url: str = Field(min_length=8, max_length=300)
    max_classification: str = Field(pattern="^(Public|Internal|Restricted)$")
    external: bool = False
    api_key: str | None = Field(default=None, max_length=400)


class SettingsIn(Strict):
    generation_model: str = Field(max_length=120)
    review_model: str = Field(max_length=120)
    embedding_model: str = Field(max_length=120)


class SignupIn(Strict):
    username: str
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)
    email: str | None = Field(default=None, max_length=200)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        return valid_username(v)


@router.get("/config")
def public_config():
    """Unauthenticated: what the sign-in page should offer."""
    s = settings()
    return {"public_mode": s.public_mode, "allow_signup": s.allow_signup, "features": list(s.features),
            "min_password": MIN_PASSWORD}


@router.post("/auth/signup", status_code=201)
def signup(body: SignupIn, request: Request, response: Response):
    s = settings()
    if not s.allow_signup:
        raise HTTPException(403, "Self sign-up is not enabled on this server")
    require_origin(request)
    ip_key = "signup:" + token_digest(client_address(request))
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) FROM usage_events WHERE kind=? AND created_at>?",
                         (ip_key, db.now() - 86400)).fetchone()[0]
        if n >= s.signups_per_ip_per_day:
            raise HTTPException(429, "Too many sign-ups from this network today. Try again tomorrow.")
        if conn.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
            raise HTTPException(409, "That username is taken")
        uid, pid = db.new_id(), db.new_id()
        conn.execute("""INSERT INTO users(id,username,display_name,password_hash,active,created_at,email,self_registered)
            VALUES(?,?,?,?,1,?,?,1)""", (uid, body.username, body.display_name.strip(), hash_password(body.password),
                                         db.now(), (body.email or "").strip()[:200] or None))
        conn.executemany("INSERT INTO user_roles VALUES(?,?)", [(uid, "Builder"), (uid, "Viewer")])
        conn.execute("""INSERT INTO projects(id,name,description,classification_floor,created_by,created_at,require_review)
            VALUES(?,?,?,?,?,?,?)""", (pid, "My workspace", "Your private workspace", "Public", uid, db.now(),
                                       0 if s.public_mode else 1))
        conn.execute("INSERT INTO project_members VALUES(?,?,?)", (pid, uid, "owner"))
        conn.execute("INSERT INTO usage_events(user_id,kind,amount,created_at) VALUES(?,?,?,?)", (uid, ip_key, 1, db.now()))
        raw = new_session(conn, uid)
        db.audit(conn, uid, "user.signup", "user", uid, {})
        actor = load_actor(conn, uid)
    response.set_cookie(settings().cookie_name, raw, httponly=True, secure=settings().secure_cookie,
                        samesite="strict", max_age=settings().session_hours * 3600, path=settings().base_path or "/")
    return {"user": actor.public(), "csrf_token": csrf_for(raw)}


# ------------------------------------------------------------------ auth
@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    require_origin(request)
    username = body.username.strip().lower()
    keys = ["account:" + username, "address:" + client_address(request)]
    with db.tx() as conn:
        wait = throttle_state(conn, keys)
    if wait > 0:
        raise HTTPException(429, f"Too many failed sign-ins. Try again in {int(wait) + 1} seconds.",
                            headers={"Retry-After": str(int(wait) + 1)})
    with db.read() as conn:
        row = db.one(conn, "SELECT * FROM users WHERE username=?", (username,))
    ok = check_password(body.password, row["password_hash"] if row else None) and bool(row and row["active"])
    with db.tx() as conn:
        if not ok:
            record_failure(conn, keys[0], ACCOUNT_ALLOWANCE)
            record_failure(conn, keys[1], ADDRESS_ALLOWANCE)
            db.audit(conn, row["id"] if row else None, "auth.login_failed", "user", row["id"] if row else None,
                     {"username": username[:64]})
        else:
            clear_failures(conn, keys[0])
            old = request.cookies.get(settings().cookie_name)
            if old:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(old),))
            raw = new_session(conn, row["id"])
            if hasher.check_needs_rehash(row["password_hash"]):
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), row["id"]))
            db.audit(conn, row["id"], "auth.login", "user", row["id"])
            actor = load_actor(conn, row["id"])
    if not ok:
        raise HTTPException(401, "Username or password is incorrect")
    response.set_cookie(settings().cookie_name, raw, httponly=True, secure=settings().secure_cookie,
                        samesite="strict", max_age=settings().session_hours * 3600, path=settings().base_path or "/")
    return {"user": actor.public(), "csrf_token": csrf_for(raw)}


@router.get("/auth/me")
def me(request: Request, actor: Actor = CurrentActor):
    return {"user": actor.public(), "csrf_token": csrf_for(request.cookies[settings().cookie_name])}


@router.post("/auth/logout")
def logout(request: Request, response: Response, actor: Actor = CurrentActor):
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(request.cookies[settings().cookie_name]),))
        db.audit(conn, actor.id, "auth.logout", "user", actor.id)
    response.delete_cookie(settings().cookie_name, path=settings().base_path or "/", httponly=True, secure=settings().secure_cookie,
                           samesite="strict")
    return {"signed_out": True}


@router.post("/auth/password")
def change_own_password(body: OwnPasswordIn, request: Request, actor: Actor = CurrentActor):
    with db.tx() as conn:
        row = db.one(conn, "SELECT password_hash FROM users WHERE id=?", (actor.id,))
        if not check_password(body.current_password, row["password_hash"]):
            raise HTTPException(400, "Current password is incorrect")
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.new_password), actor.id))
        keep = token_digest(request.cookies[settings().cookie_name])
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (actor.id, keep))
        db.audit(conn, actor.id, "user.password_changed", "user", actor.id)
    return {"updated": True}


# ------------------------------------------------------------------ users
def _check_roles(roles: list[str], perms: list[str]) -> None:
    bad = [r for r in roles if r not in ALL_ROLES] + [p for p in perms if p not in PERMISSIONS]
    if bad:
        raise HTTPException(422, f"Unknown role/permission: {bad}")


def _user_rows(conn):
    users = db.all_rows(conn, "SELECT id,username,display_name,active,created_at FROM users ORDER BY username")
    for u in users:
        u["roles"] = sorted(r[0] for r in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (u["id"],)))
        u["permissions"] = sorted(r[0] for r in conn.execute("SELECT permission FROM user_permissions WHERE user_id=?",
                                                              (u["id"],)))
    return users


@router.get("/users")
def list_users(actor: Actor = CurrentActor):
    require_admin(actor)
    with db.read() as conn:
        return _user_rows(conn)


@router.get("/users/directory")
def directory(actor: Actor = CurrentActor):
    """Minimal list (username + display name) so project owners can add members."""
    require(actor.can_build, "A Builder or Admin account is required")
    if settings().public_mode and not actor.is_admin:
        return []                          # never list other people's accounts on a public server
    with db.read() as conn:
        return db.all_rows(conn, "SELECT id,username,display_name FROM users WHERE active=1 ORDER BY username")


@router.post("/users", status_code=201)
def create_user(body: UserIn, actor: Actor = CurrentActor):
    require_admin(actor)
    _check_roles(body.roles, body.permissions)
    uid = db.new_id()
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
            raise HTTPException(409, "That username already exists")
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,active,created_at) VALUES(?,?,?,?,1,?)",
                     (uid, body.username, body.display_name.strip(), hash_password(body.password), db.now()))
        conn.executemany("INSERT INTO user_roles VALUES(?,?)", [(uid, r) for r in set(body.roles)])
        conn.executemany("INSERT INTO user_permissions VALUES(?,?)", [(uid, p) for p in set(body.permissions)])
        db.audit(conn, actor.id, "user.create", "user", uid, {"roles": sorted(body.roles), "permissions": body.permissions})
    return {"id": uid}


@router.put("/users/{user_id}")
def update_user(user_id: str, body: UserUpdate, actor: Actor = CurrentActor):
    require_admin(actor)
    _check_roles(body.roles, body.permissions)
    if user_id == actor.id and (not body.active or Role.ADMIN not in body.roles):
        raise HTTPException(400, "Ask another administrator to change your own administrative access")
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "User not found")
        was_admin = conn.execute("SELECT 1 FROM user_roles r JOIN users u ON u.id=r.user_id WHERE r.user_id=? "
                                 "AND r.role='Admin' AND u.active=1", (user_id,)).fetchone()
        if was_admin and (not body.active or Role.ADMIN not in body.roles):
            n = conn.execute("SELECT count(*) FROM user_roles r JOIN users u ON u.id=r.user_id "
                             "WHERE r.role='Admin' AND u.active=1").fetchone()[0]
            if n <= 1:
                raise HTTPException(409, "Keep at least one active administrator")
        builder_after = any(r in body.roles for r in ("Admin", "Builder")) and body.active
        if not builder_after:
            sole = conn.execute("""SELECT p.name FROM project_members m JOIN projects p ON p.id=m.project_id
                WHERE m.user_id=? AND m.membership='owner' AND NOT EXISTS(SELECT 1 FROM project_members o
                JOIN users u ON u.id=o.user_id JOIN user_roles r ON r.user_id=o.user_id
                WHERE o.project_id=m.project_id AND o.user_id<>m.user_id AND o.membership='owner' AND u.active=1
                AND r.role IN ('Admin','Builder'))""", (user_id,)).fetchone()
            if sole:
                raise HTTPException(409, f"Transfer ownership of project '{sole[0]}' first")
        conn.execute("UPDATE users SET display_name=?, active=? WHERE id=?", (body.display_name.strip(),
                                                                            int(body.active), user_id))
        conn.execute("DELETE FROM user_roles WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_permissions WHERE user_id=?", (user_id,))
        conn.executemany("INSERT INTO user_roles VALUES(?,?)", [(user_id, r) for r in set(body.roles)])
        conn.executemany("INSERT INTO user_permissions VALUES(?,?)", [(user_id, p) for p in set(body.permissions)])
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.audit(conn, actor.id, "user.update", "user", user_id,
                 {"active": body.active, "roles": sorted(body.roles), "permissions": body.permissions})
    return {"updated": True}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, body: PasswordIn, actor: Actor = CurrentActor):
    require_admin(actor)
    with db.tx() as conn:
        if conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), user_id)).rowcount != 1:
            raise HTTPException(404, "User not found")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.audit(conn, actor.id, "user.password_reset", "user", user_id)
    return {"updated": True, "sessions_revoked": True}


# ------------------------------------------------------------------ models
@router.get("/models/connections")
def connections(actor: Actor = CurrentActor):
    with db.read() as conn:
        rows = db.all_rows(conn, """SELECT id,name,kind,base_url,max_classification,external,enabled,
            (api_key_enc IS NOT NULL) AS has_key, (owner_user_id IS NOT NULL) AS personal FROM model_connections
            WHERE owner_user_id IS NULL OR owner_user_id=? ORDER BY personal, name""", (actor.id,))
        cfg = {k: db.get_setting(conn, k, "") for k in ("generation_model", "review_model", "embedding_model")}
        aliases = db.all_rows(conn, """SELECT a.alias, d.ollama_model, d.id AS deployment_id FROM model_aliases a
            JOIN deployments d ON d.id=a.deployment_id WHERE d.status='active'""")
    if not actor.is_admin:
        for r in rows:
            r.pop("base_url")
    return {"connections": rows, "defaults": cfg, "promoted": aliases}


@router.get("/models/available")
def available_models(connection: str = "local-ollama", actor: Actor = CurrentActor):
    require(actor.can_build or actor.is_admin, "A Builder or Admin account is required")
    try:
        return {"models": Gateway(connection, owner=actor.id).list_models(), "reachable": True}
    except (ModelUnavailable, GatewayBlocked) as exc:
        return {"models": [], "reachable": False, "message": str(exc)}


@router.post("/models/connections", status_code=201)
def add_connection(body: ConnectionIn, actor: Actor = CurrentActor):
    require_admin(actor)
    try:
        check_url(body.base_url)
    except (GatewayBlocked, ModelUnavailable) as exc:
        raise HTTPException(400, str(exc)) from None
    if body.external and body.max_classification == "Restricted":
        raise HTTPException(400, "External endpoints may not receive Restricted content")
    with db.tx() as conn:
        cid = db.new_id()
        conn.execute("""INSERT INTO model_connections(id,name,kind,base_url,max_classification,external,enabled,api_key_enc,created_at)
            VALUES(?,?,?,?,?,?,1,?,?)""", (cid, body.name, body.kind, body.base_url.rstrip("/"), body.max_classification,
                                           int(body.external), encrypt_secret(body.api_key) if body.api_key else None,
                                           db.now()))
        db.audit(conn, actor.id, "model.connection_added", "model_connection", cid,
                 {"name": body.name, "external": body.external, "max_classification": body.max_classification})
    return {"id": cid}


@router.put("/models/connections/{cid}/enabled")
def toggle_connection(cid: str, enabled: bool, actor: Actor = CurrentActor):
    require_admin(actor)
    with db.tx() as conn:
        conn.execute("UPDATE model_connections SET enabled=? WHERE id=?", (int(enabled), cid))
        db.audit(conn, actor.id, "model.connection_" + ("enabled" if enabled else "disabled"), "model_connection", cid)
    return {"updated": True}


@router.put("/models/defaults")
def set_defaults(body: SettingsIn, actor: Actor = CurrentActor):
    require_admin(actor)
    with db.tx() as conn:
        for k, v in body.model_dump().items():
            db.set_setting(conn, k, v.strip())
        db.audit(conn, actor.id, "model.defaults_set", "settings", None, body.model_dump())
    return {"updated": True}


# ------------------------------------------------------------------ audit & status
@router.get("/audit")
def audit_log(limit: int = 300, actor: Actor = CurrentActor):
    require_admin(actor)
    with db.read() as conn:
        return db.all_rows(conn, """SELECT a.id,a.action,a.resource_type,a.resource_id,a.details,a.created_at,u.username
            FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT ?""", (min(limit, 2000),))


@router.get("/system/status")
def system_status(actor: Actor = CurrentActor):
    with db.read() as conn:
        cfg = {k: db.get_setting(conn, k, "") for k in ("generation_model", "review_model", "embedding_model")}
        jobs = db.all_rows(conn, "SELECT kind, status, count(*) n FROM jobs GROUP BY kind, status")
    try:
        models = Gateway().list_models()
        ollama = {"reachable": True, "models": models,
                  "missing": [m for m in cfg.values() if m and not any(x == m or x.split(":")[0] == m for x in models)]}
    except (ModelUnavailable, GatewayBlocked) as exc:
        ollama = {"reachable": False, "message": str(exc)}
    return {"ollama": ollama, "defaults": cfg, "jobs": jobs, "training_python": settings().train_python or "(app python)",
            "models_dir": str(settings().models_dir) if actor.is_admin else None}
