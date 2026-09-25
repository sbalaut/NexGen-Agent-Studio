"""FastAPI application: API under /api, pre-built UI served from ./static."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, jobs
from . import pipeline, executor  # noqa: F401  (registers job handlers)
from .training import coordinator  # noqa: F401
from .api import admin, connect, improve, projects, review, workflows
from .config import settings
from .llm import ensure_default_connection

log = logging.getLogger("nexagent")
STATIC = Path(__file__).resolve().parent / "static"
VERSION = "0.3.0"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def init_state() -> None:
    s = settings()
    s.validate()
    s.secret()
    db.migrate()
    with db.tx() as conn:
        ensure_default_connection(conn)
        for key, value in (("generation_model", s.default_generation_model), ("review_model", s.default_review_model),
                           ("embedding_model", s.default_embedding_model)):
            if conn.execute("SELECT 1 FROM app_settings WHERE key=?", (key,)).fetchone() is None:
                db.set_setting(conn, key, value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_state()
    pool = None
    if settings().workers > 0:
        pool = jobs.WorkerPool(settings().workers)
        pool.start()
    yield
    if pool:
        pool.shutdown()


app = FastAPI(title="NexAgent Studio", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


class BasePath:
    """Serve under a URL prefix (JupyterHub services forward the full /services/<name>/ path)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        base = settings().base_path
        if base and scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == base and scope["type"] == "http":        # relative asset URLs need the trailing slash
                await send({"type": "http.response.start", "status": 308,
                            "headers": [(b"location", (base + "/").encode()), (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
            if path.startswith(base + "/"):
                scope = dict(scope, path=path[len(base):], root_path=scope.get("root_path", "") + base)
        await self.inner(scope, receive, send)


@app.middleware("http")
async def headers(request: Request, call_next):
    request.state.request_id = secrets.token_hex(8)
    response = await call_next(request)
    h = response.headers
    h["X-Request-ID"] = request.state.request_id
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Content-Security-Policy"] = CSP
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings().secure_cookie:
        h["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith("/api/"):
        h["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def invalid(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "; ".join(
        f"{'.'.join(str(x) for x in e['loc'][1:]) or 'request'}: {e['msg']}" for e in exc.errors())})


@app.exception_handler(Exception)
async def crashed(request: Request, exc: Exception):
    log.exception("unhandled error request_id=%s", getattr(request.state, "request_id", "?"))
    return JSONResponse(status_code=500, content={"detail": "Internal error. Ask the administrator to check the "
                                                            "server log.", "request_id": getattr(request.state, "request_id", "")})


@app.get("/api/health")
def health():
    return {"status": "alive", "version": VERSION}


def _feature(name: str):
    def check():
        if not settings().has(name):
            raise HTTPException(404, f"The '{name}' workspace is not enabled on this server")
    return check


for r in (admin.router, projects.router, workflows.router, connect.router):
    app.include_router(r, prefix="/api")
app.include_router(review.router, prefix="/api", dependencies=[Depends(_feature("review"))])
app.include_router(improve.router, prefix="/api", dependencies=[Depends(_feature("training"))])


if (STATIC / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Not found")
    candidate = (STATIC / path).resolve()
    if path and candidate.is_file() and STATIC in candidate.parents:
        return FileResponse(candidate)
    index = STATIC / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "The user interface has not been built. See README (frontend build)."}, 503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


asgi = BasePath(app)       # what the server runs
