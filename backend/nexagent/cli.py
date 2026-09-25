"""Command line: `python -m nexagent.cli <command>`

  serve          start the web app (uvicorn) on NEXAGENT_HOST:NEXAGENT_PORT
  create-admin   interactive first administrator (no default credentials)
  backup DIR     consistent online backup of the database + uploads + secrets
  restore DIR    restore a backup into an empty data directory
  check          show configuration and whether Ollama / models are reachable
"""
from __future__ import annotations

import getpass
import json
import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

from . import db
from .config import settings


def create_admin() -> None:
    from .main import init_state
    from .security import MIN_PASSWORD, hash_password
    init_state()
    with db.read() as conn:
        if conn.execute("SELECT 1 FROM user_roles r JOIN users u ON u.id=r.user_id WHERE r.role='Admin' AND u.active=1").fetchone():
            sys.exit("An active administrator already exists. Use the Admin screen to add accounts.")
    username = input("Administrator username (lowercase): ").strip().lower()
    display = input("Display name: ").strip() or username
    pw = getpass.getpass(f"Password (at least {MIN_PASSWORD} characters): ")
    if len(pw) < MIN_PASSWORD or pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords must match and be long enough")
    uid = db.new_id()
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,active,created_at) VALUES(?,?,?,?,1,?)",
                     (uid, username, display, hash_password(pw), db.now()))
        conn.executemany("INSERT INTO user_roles VALUES(?,?)", [(uid, "Admin"), (uid, "Builder")])
        db.audit(conn, uid, "admin.bootstrap", "user", uid)
    print("Administrator created. Open", settings().origin)


def bootstrap_admin_from_env() -> None:
    """Hosting platforms without a console: NEXAGENT_BOOTSTRAP_ADMIN=<username> and
    NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD=<password> create the first administrator once, at start-up.
    Nothing happens if an administrator already exists. Remove the password variable afterwards."""
    import os
    from .main import init_state
    from .security import MIN_PASSWORD, hash_password
    username = (os.environ.get("NEXAGENT_BOOTSTRAP_ADMIN") or "").strip().lower()
    password = os.environ.get("NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD") or ""
    if not username:
        return
    init_state()
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM user_roles r JOIN users u ON u.id=r.user_id WHERE r.role='Admin' AND u.active=1").fetchone():
            if password:
                print("NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD is still set but an administrator exists — remove the variable.")
            return
        if len(password) < MIN_PASSWORD:
            sys.exit(f"NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD must be at least {MIN_PASSWORD} characters")
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            sys.exit(f"User {username} already exists but is not an administrator; choose another NEXAGENT_BOOTSTRAP_ADMIN")
        uid = db.new_id()
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,active,created_at) VALUES(?,?,?,?,1,?)",
                     (uid, username, username, hash_password(password), db.now()))
        conn.executemany("INSERT INTO user_roles VALUES(?,?)", [(uid, "Admin"), (uid, "Builder")])
        db.audit(conn, uid, "admin.bootstrap", "user", uid, {"source": "environment"})
    print(f"Administrator '{username}' created from environment variables. Remove NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD now.")


def backup(target: str) -> None:
    s = settings()
    out = Path(target).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp_db = out / f"nexagent-{stamp}.sqlite3"
    src = sqlite3.connect(s.db_path)
    dst = sqlite3.connect(tmp_db)
    src.backup(dst)                      # consistent online copy
    dst.close(); src.close()
    archive = out / f"nexagent-backup-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_db, arcname="nexagent.sqlite3")
        for name in ("secret.key", "connections.key"):
            if (s.data_dir / name).exists():
                tar.add(s.data_dir / name, arcname=name)
        if s.uploads_dir.exists():
            tar.add(s.uploads_dir, arcname="uploads")
    tmp_db.unlink()
    print(f"Backup written: {archive}\nIt contains secrets — store it with the same care as the server.")
    print("Training folders (checkpoints, adapters) are not included; copy", s.training_dir, "separately if needed.")


def restore(source: str) -> None:
    s = settings()
    archive = Path(source).expanduser().resolve()
    if s.db_path.exists():
        sys.exit(f"{s.db_path} already exists. Restore only into an empty NEXAGENT_DATA_DIR.")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(s.data_dir, filter="data")
        except TypeError:                      # Python < 3.10.12 has no extraction filters
            for m in tar.getmembers():
                if m.name.startswith(("/", "..")) or ".." in m.name.split("/") or m.issym() or m.islnk():
                    sys.exit(f"Unsafe path in backup archive: {m.name}")
            tar.extractall(s.data_dir)
    with sqlite3.connect(s.db_path) as conn:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print("Restored. Integrity check:", ok)


def check() -> None:
    from .main import init_state
    from .llm import Gateway, ModelUnavailable, GatewayBlocked
    init_state()
    s = settings()
    print(json.dumps({"origin": s.origin, "data_dir": str(s.data_dir), "ollama_url": s.ollama_url,
                      "train_python": s.train_python or "(same as app)", "models_dir": str(s.models_dir)}, indent=1))
    try:
        print("Ollama models:", ", ".join(Gateway().list_models()) or "(none pulled)")
    except (ModelUnavailable, GatewayBlocked) as exc:
        print("Ollama:", exc)


def serve() -> None:
    import uvicorn
    s = settings()
    bootstrap_admin_from_env()
    tls = {"ssl_certfile": s.tls_cert, "ssl_keyfile": s.tls_key} if s.tls_cert and s.tls_key else {}
    uvicorn.run("nexagent.main:asgi", host=s.host, port=s.port, proxy_headers=s.trusted_proxy,
                forwarded_allow_ips="*" if s.trusted_proxy else "127.0.0.1", log_level="info", **tls)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "create-admin":
        create_admin()
    elif cmd == "backup":
        backup(sys.argv[2] if len(sys.argv) > 2 else "backups")
    elif cmd == "restore":
        restore(sys.argv[2])
    elif cmd == "check":
        check()
    else:
        print(__doc__); sys.exit(2)


if __name__ == "__main__":
    main()
