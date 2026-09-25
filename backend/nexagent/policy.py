"""Roles, classifications and the single place where project/KB access is decided.

Every API route and every worker calls these helpers with the *current* database
state, so a revoked grant takes effect on the next check (including the final
re-check performed just before an answer is released).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from .compat import StrEnum

from fastapi import HTTPException


class Role(StrEnum):
    ADMIN = "Admin"
    BUILDER = "Builder"
    REVIEWER = "Reviewer"
    TRAINING_OPERATOR = "TrainingOperator"
    VIEWER = "Viewer"


ALL_ROLES = tuple(r.value for r in Role)
PERMISSIONS = ("model.promote",)


class Classification(StrEnum):
    PUBLIC = "Public"
    INTERNAL = "Internal"
    RESTRICTED = "Restricted"


LEVEL = {"Public": 0, "Internal": 1, "Restricted": 2}


def max_class(*values: str) -> str:
    vals = [v for v in values if v]
    return max(vals, key=LEVEL.__getitem__) if vals else "Internal"


def class_allows(ceiling: str, value: str) -> bool:
    return LEVEL[value] <= LEVEL[ceiling]


@dataclass(frozen=True)
class Actor:
    id: str
    username: str
    display_name: str
    roles: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles

    @property
    def can_build(self) -> bool:
        return self.has(Role.ADMIN, Role.BUILDER)

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "display_name": self.display_name,
                "roles": sorted(self.roles), "permissions": sorted(self.permissions)}


def load_actor(conn: sqlite3.Connection, user_id: str) -> Actor | None:
    row = conn.execute("SELECT id,username,display_name FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    if not row:
        return None
    roles = frozenset(r[0] for r in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (user_id,)))
    perms = frozenset(r[0] for r in conn.execute("SELECT permission FROM user_permissions WHERE user_id=?", (user_id,)))
    return Actor(row["id"], row["username"], row["display_name"], roles, perms)


def require(cond: bool, message: str, status: int = 403) -> None:
    if not cond:
        raise HTTPException(status, message)


def require_admin(actor: Actor) -> None:
    require(actor.is_admin, "An administrator account is required")


def membership(conn: sqlite3.Connection, project_id: str, user_id: str) -> str | None:
    row = conn.execute("SELECT membership FROM project_members WHERE project_id=? AND user_id=?",
                       (project_id, user_id)).fetchone()
    return row[0] if row else None


def project_for(conn: sqlite3.Connection, actor: Actor, project_id: str, need: str = "read") -> dict:
    """need: read | write | manage | review | train"""
    row = conn.execute("""SELECT p.*, m.membership FROM projects p JOIN project_members m
        ON m.project_id=p.id AND m.user_id=? WHERE p.id=?""", (actor.id, project_id)).fetchone()
    if not row:
        raise HTTPException(404, "Project not found or not accessible")
    m = row["membership"]
    if need == "write":
        require(actor.can_build and m in ("owner", "editor"), "Project editing permission is required")
    elif need == "manage":
        require(actor.can_build and m == "owner", "Project owner permission is required")
    elif need == "review":
        require(actor.has(Role.REVIEWER), "The Engineer Reviewer role is required")
    elif need == "train":
        require(actor.has(Role.TRAINING_OPERATOR), "The Training Operator role is required")
    return dict(row)


READABLE_KB_SQL = """SELECT b.id FROM knowledge_bases b
  JOIN kb_grants g ON g.kb_id=b.id AND g.user_id=:uid AND g.can_read=1
  JOIN project_members m ON m.project_id=b.project_id AND m.user_id=:uid
  JOIN users u ON u.id=:uid AND u.active=1"""


def readable_kbs(conn: sqlite3.Connection, user_id: str, project_id: str | None = None) -> set[str]:
    sql = READABLE_KB_SQL + (" WHERE b.project_id=:pid" if project_id else "")
    return {r[0] for r in conn.execute(sql, {"uid": user_id, "pid": project_id})}


def kb_for(conn: sqlite3.Connection, actor: Actor, kb_id: str, need: str = "read") -> dict:
    row = conn.execute("""SELECT b.*, g.can_read, g.can_write, m.membership FROM knowledge_bases b
        JOIN project_members m ON m.project_id=b.project_id AND m.user_id=?
        LEFT JOIN kb_grants g ON g.kb_id=b.id AND g.user_id=m.user_id
        WHERE b.id=?""", (actor.id, kb_id)).fetchone()
    if not row or not row["can_read"]:
        raise HTTPException(404, "Knowledge base not found or not accessible")
    if need == "write":
        require(bool(row["can_write"]) and actor.can_build and row["membership"] in ("owner", "editor"),
                "Write access to this knowledge base is required")
    if need == "manage":
        require(actor.can_build and row["membership"] == "owner", "Project owner permission is required")
    return dict(row)
