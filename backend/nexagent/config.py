"""Runtime settings. Everything comes from environment variables (or a .env file
next to the data directory) so the same code runs on a JupyterHub/GPU server
without Docker or root access.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get("NEXAGENT_" + name, default)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8600
    origin: str = "http://127.0.0.1:8600"
    trusted_proxy: bool = False
    session_hours: int = 8
    cookie_name: str = "nexagent_session"
    max_upload_mb: int = 40
    ollama_url: str = "http://127.0.0.1:11434"
    allowed_model_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    default_generation_model: str = "qwen3-next:80b-a3b-instruct-q4_K_M"
    default_review_model: str = "qwen3-next:80b-a3b-instruct-q4_K_M"
    default_embedding_model: str = "qwen3-embedding:8b"
    llm_timeout_s: float = 600.0
    num_ctx: int = 16384            # Ollama context window; the default (2-4k) would silently cut off sources
    keep_alive: str = "30m"         # keep models loaded in VRAM between questions
    workers: int = 2
    train_python: str = ""          # python executable of the training venv
    models_dir: Path = field(default_factory=Path)  # allow-listed base-model folder
    train_gpu: str = ""             # CUDA_VISIBLE_DEVICES for training jobs
    train_max_hours: float = 12.0
    ollama_cli: str = "ollama"
    public_mode: bool = False       # open sign-up, personal API keys, no plant-only workspaces
    allow_signup: bool = False
    features: tuple[str, ...] = ("review", "training", "mcp", "agents", "graphrag")
    quota_runs_per_hour: int = 60
    quota_docs_per_user: int = 100
    quota_upload_mb_per_user: int = 500
    signups_per_ip_per_day: int = 5
    allow_stdio_mcp: bool = True    # admin-registered local MCP servers (never available to public users)
    base_path: str = ""             # e.g. /services/nexagent when published as a JupyterHub service
    tls_cert: str = ""              # serve HTTPS directly with these files (optional)
    tls_key: str = ""

    # ---- derived paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "nexagent.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def training_dir(self) -> Path:
        return self.data_dir / "training"

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.origin).scheme == "https"

    def secret(self) -> str:
        """Application secret kept in a 0600 file outside the database."""
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_urlsafe(48))
        return path.read_text().strip()

    def has(self, feature: str) -> bool:
        return feature in self.features

    def validate(self) -> None:
        u = urlsplit(self.origin)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise RuntimeError("NEXAGENT_ORIGIN must look like https://server.example:8600")
        if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1") and _env("ALLOW_HTTP") != "1":
            raise RuntimeError("Plain HTTP is only allowed on loopback. Use HTTPS (e.g. behind the JupyterHub proxy) "
                               "or set NEXAGENT_ALLOW_HTTP=1 for an isolated plant LAN you have approved.")
        if not 1 <= self.session_hours <= 24:
            raise RuntimeError("Session lifetime must be 1-24 hours")
        for d in (self.data_dir, self.uploads_dir, self.training_dir):
            d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass


PROJECT_ROOT = Path(__file__).resolve().parents[2]      # folder holding .env, backend/, frontend/


def _path(value: str) -> Path:
    """Relative paths in settings are relative to the project folder, not the current directory."""
    p = Path(value).expanduser()
    return (p if p.is_absolute() else PROJECT_ROOT / p).resolve()


def build_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    data_dir = _path(_env("DATA_DIR", "data"))
    hosts = tuple(h.strip() for h in (_env("ALLOWED_MODEL_HOSTS", "127.0.0.1,localhost,::1") or "").split(",") if h.strip())
    return Settings(
        data_dir=data_dir,
        host=_env("HOST", "127.0.0.1"),
        port=int(_env("PORT", "8600")),
        origin=_env("ORIGIN", "http://127.0.0.1:8600").rstrip("/"),
        trusted_proxy=_env("TRUSTED_PROXY", "0") == "1",
        session_hours=int(_env("SESSION_HOURS", "8")),
        max_upload_mb=int(_env("MAX_UPLOAD_MB", "40")),
        ollama_url=_env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
        allowed_model_hosts=hosts,
        default_generation_model=_env("GENERATION_MODEL", "qwen3-next:80b-a3b-instruct-q4_K_M"),
        default_review_model=_env("REVIEW_MODEL", "qwen3-next:80b-a3b-instruct-q4_K_M"),
        default_embedding_model=_env("EMBEDDING_MODEL", "qwen3-embedding:8b"),
        llm_timeout_s=float(_env("LLM_TIMEOUT_S", "600")),
        num_ctx=int(_env("NUM_CTX", "16384")),
        keep_alive=_env("KEEP_ALIVE", "30m"),
        workers=int(_env("WORKERS", "2")),
        train_python=_env("TRAIN_PYTHON", "") or "",
        models_dir=_path(_env("MODELS_DIR", str(data_dir / "base-models"))),
        train_gpu=_env("TRAIN_GPU", "") or "",
        train_max_hours=float(_env("TRAIN_MAX_HOURS", "12")),
        ollama_cli=_env("OLLAMA_CLI", "ollama"),
        public_mode=_env("PUBLIC_MODE", "0") == "1",
        allow_signup=_env("ALLOW_SIGNUP", _env("PUBLIC_MODE", "0")) == "1",   # public servers allow sign-up unless turned off
        features=tuple(f.strip() for f in (_env("FEATURES") or (
            "mcp,agents,graphrag" if _env("PUBLIC_MODE", "0") == "1" else "review,training,mcp,agents,graphrag")).split(",")
            if f.strip()),
        quota_runs_per_hour=int(_env("QUOTA_RUNS_PER_HOUR", "60")),
        quota_docs_per_user=int(_env("QUOTA_DOCS_PER_USER", "100")),
        quota_upload_mb_per_user=int(_env("QUOTA_UPLOAD_MB_PER_USER", "500")),
        signups_per_ip_per_day=int(_env("SIGNUPS_PER_IP_PER_DAY", "5")),
        allow_stdio_mcp=_env("ALLOW_STDIO_MCP", "0" if _env("PUBLIC_MODE", "0") == "1" else "1") == "1",
        base_path=("/" + (_env("BASE_PATH", "") or "").strip("/")).rstrip("/") if (_env("BASE_PATH", "") or "").strip("/") else "",
        tls_cert=str(_path(_env("TLS_CERT"))) if _env("TLS_CERT") else "",
        tls_key=str(_path(_env("TLS_KEY"))) if _env("TLS_KEY") else "",
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = build_settings()
    return _settings


def override_settings(value: Settings) -> None:
    """Used by tests and the CLI."""
    global _settings
    _settings = value
