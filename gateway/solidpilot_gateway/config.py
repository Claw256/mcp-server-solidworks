"""Gateway settings, read from the environment (a `.env` in the gateway folder is honoured for local runs)."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

# Redirect URIs for Claude's hosted apps (web, desktop, mobile, Cowork). Anthropic's connector docs
# require exactly the first one; the claude.com variant is allowed as a forward-looking extra.
# https://claude.com/docs/connectors/building/authentication#callback-urls
CLAUDE_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)

USER_SCOPE = "solidworks"
AGENT_SCOPE = "agent"
# Names under which Vercel's Redis/Upstash integration may inject the TCP connection string.
_REDIS_ENV_NAMES = ("GATEWAY_REDIS_URL", "REDIS_URL", "KV_URL", "UPSTASH_KV_URL")


def _env(name: str, default: str = "") -> str:
    """Read an env var, dropping a stray BOM / surrounding whitespace (Windows shells like to add them)."""
    return os.getenv(name, default).lstrip("\ufeff").strip()


def _csv(name: str) -> list[str]:
    return [v.strip() for v in _env(name).split(",") if v.strip()]


def redis_url_from_env() -> str | None:
    for name in _REDIS_ENV_NAMES:
        value = _env(name)
        if value and value.startswith(("redis://", "rediss://")):
            return value
    return None


@dataclass(frozen=True)
class Settings:
    public_url: str                      # e.g. https://solidpilot.vercel.app  (no trailing slash)
    owner_secret: str                    # the one password that approves a Claude connector login
    agent_client_id: str
    agent_client_secret: str
    redis_url: str = ""
    redirect_uris: tuple[str, ...] = CLAUDE_REDIRECT_URIS
    # Claude gives a tool call 240 s; stay under it so our own error reaches the model first.
    call_timeout: float = 200.0
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 60 * 60 * 24 * 90
    agent_token_ttl: int = 900
    poll_wait: int = 25                  # seconds the agent's long-poll is held open
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=lambda: ["https://claude.ai", "https://claude.com"])

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def agent_resource(self) -> str:
        return f"{self.public_url}/agent"

    @property
    def host(self) -> str:
        return urlsplit(self.public_url).netloc

    @property
    def is_local(self) -> bool:
        return urlsplit(self.public_url).hostname in ("localhost", "127.0.0.1", "::1")

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [n for n in ("GATEWAY_PUBLIC_URL", "GATEWAY_OWNER_SECRET", "AGENT_CLIENT_ID", "AGENT_CLIENT_SECRET")
                   if not _env(n)]
        redis_url = redis_url_from_env()
        if redis_url is None:
            missing.append("GATEWAY_REDIS_URL (or REDIS_URL / KV_URL from the Redis integration)")
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        public_url = _env("GATEWAY_PUBLIC_URL").rstrip("/")
        host = urlsplit(public_url).netloc
        return cls(
            public_url=public_url,
            owner_secret=_env("GATEWAY_OWNER_SECRET"),
            agent_client_id=_env("AGENT_CLIENT_ID"),
            agent_client_secret=_env("AGENT_CLIENT_SECRET"),
            redis_url=redis_url or "",
            redirect_uris=CLAUDE_REDIRECT_URIS + tuple(_csv("GATEWAY_EXTRA_REDIRECT_URIS")),
            call_timeout=float(_env("GATEWAY_CALL_TIMEOUT", "200")),
            allowed_hosts=[host, f"{host}:*"] if host else [],
            allowed_origins=_csv("GATEWAY_ALLOWED_ORIGINS") or ["https://claude.ai", "https://claude.com"],
        )
