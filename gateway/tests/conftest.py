import os
import socket
import sys
import threading
import time

import fakeredis
import httpx
import pytest
import uvicorn

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from solidpilot_gateway import relay as relay_module  # noqa: E402
from solidpilot_gateway.app import create_app  # noqa: E402
from solidpilot_gateway.config import Settings  # noqa: E402

OWNER_SECRET = "owner-secret-for-tests"
AGENT_ID = "pc-agent"
AGENT_SECRET = "agent-secret-for-tests"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_settings(port: int) -> Settings:
    return Settings(
        public_url=f"http://127.0.0.1:{port}", owner_secret=OWNER_SECRET, agent_client_id=AGENT_ID,
        agent_client_secret=AGENT_SECRET, call_timeout=5.0, poll_wait=2,
        redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
    )


class Running:
    def __init__(self, url, app, settings):
        self.url, self.app, self.settings = url, app, settings

    async def agent_connected(self) -> bool:
        async with httpx.AsyncClient() as c:
            return (await c.get(f"{self.url}/health")).json()["agent_connected"]


def _serve(monkeypatch, blob=None):
    monkeypatch.setattr(relay_module, "PRESENCE_TTL", 2)   # so "PC went away" is observable in seconds
    port = _free_port()
    settings = make_settings(port)
    app = create_app(settings, redis=fakeredis.FakeAsyncRedis(decode_responses=True), blob=blob)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "gateway failed to start"
    yield Running(settings.public_url, app, settings)
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def gateway(monkeypatch):
    yield from _serve(monkeypatch)


@pytest.fixture
def fake_blob():
    from solidpilot_gateway.blobstore import FakeBlob
    return FakeBlob()


@pytest.fixture
def gateway_blob(monkeypatch, fake_blob):
    """A gateway with file deliveries enabled, backed by the in-memory blob store."""
    yield from _serve(monkeypatch, fake_blob)
