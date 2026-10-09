"""Real agent (adapters/claude/agent.py) <-> real gateway: deliver_document end to end, storage faked, CAD stubbed."""
import asyncio
import hashlib
import os
import sys

import httpx
import pytest

from .conftest import AGENT_ID, AGENT_SECRET
from .test_gateway import mcp_call, parse_rpc, user_token

ADAPTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "adapters", "claude"))
sys.path.insert(0, ADAPTER_DIR)

agent = pytest.importorskip("agent")


@pytest.fixture
async def live_agent(gateway_blob, fake_blob, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "GATEWAY_URL", gateway_blob.url)
    monkeypatch.setattr(agent, "CLIENT_ID", AGENT_ID)
    monkeypatch.setattr(agent, "CLIENT_SECRET", AGENT_SECRET)
    monkeypatch.setattr(agent, "STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setattr(agent, "POLL_WAIT", 1)
    monkeypatch.setattr(agent, "_call_lock", asyncio.Lock())
    os.makedirs(agent.STAGING_DIR, exist_ok=True)

    real_put = httpx.AsyncClient.put

    async def put(self, url, **kw):          # the "storage" side of a presigned PUT
        if str(url).startswith("https://blob.fake/put/"):
            data = kw["content"]
            assert kw["headers"]["x-content-type"]
            fake_blob.upload(str(url)[len("https://blob.fake/put/"):], data)
            return httpx.Response(200, request=httpx.Request("PUT", url))
        return await real_put(self, url, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "put", put)
    client = httpx.AsyncClient()
    task = asyncio.create_task(agent.poll_forever(agent.Gateway(client)))
    for _ in range(200):
        if await gateway_blob.agent_connected():
            break
        await asyncio.sleep(0.05)
    assert await gateway_blob.agent_connected(), "agent never connected"
    yield agent
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client.aclose()


async def call(gw, access, name, arguments):
    return parse_rpc(await mcp_call(gw, access, "tools/call", {"name": name, "arguments": arguments}))["result"]


async def test_deliver_existing_file_over_the_body_limit(gateway_blob, fake_blob, live_agent):
    _, tok = await user_token(gateway_blob)
    access = tok["access_token"]
    listed = parse_rpc(await mcp_call(gateway_blob, access, "tools/list"))["result"]["tools"]
    tool = next(t for t in listed if t["name"] == "deliver_document")
    assert "delivery_slot" not in tool["inputSchema"]["properties"]      # the model never sees the slot

    payload = os.urandom(12 * 1024 * 1024)                               # a "12 MB .sldprt"
    path = os.path.join(agent.STAGING_DIR, "bracket.sldprt")
    with open(path, "wb") as fh:
        fh.write(payload)
    res = await call(gateway_blob, access, "deliver_document", {"file_path": path})
    assert not res.get("isError"), res
    text = res["content"][0]["text"]
    assert hashlib.sha256(payload).hexdigest() in text
    link = next(w for w in text.split() if w.startswith(f"{gateway_blob.url}/dl/"))
    stored = next(iter(fake_blob.objects.values()))
    assert stored == payload                                             # bytes arrived intact, off the gateway path
    async with httpx.AsyncClient(follow_redirects=False) as c:
        assert (await c.get(link)).status_code == 302
        assert (await c.get(link)).status_code == 404                    # one time only


async def test_deliver_export_of_active_document_cleans_up(gateway_blob, fake_blob, live_agent, monkeypatch):
    _, tok = await user_token(gateway_blob)
    calls = []

    def fake_call_raw(tool, params):
        calls.append((tool, dict(params)))
        if tool == "verify_state":
            return {"status": "COMPLETED", "cadState": {"activeDocument": "Bracket.SLDPRT"}}
        with open(params["file_path"], "wb") as fh:
            fh.write(b"ISO-10303-21;" * 1000)
        return {"status": "COMPLETED"}

    monkeypatch.setattr(agent.sw_server, "_call_raw", fake_call_raw)
    res = await call(gateway_blob, tok["access_token"], "deliver_document", {"format": "step"})
    assert not res.get("isError"), res
    assert "Bracket.step" in res["content"][0]["text"]
    assert [c[0] for c in calls] == ["verify_state", "export_document"] and calls[1][1]["format"] == "STEP"
    assert os.listdir(agent.STAGING_DIR) == []                           # the temporary export is removed


async def test_deliver_save_in_place_uses_path_from_solidworks(gateway_blob, fake_blob, live_agent, monkeypatch, tmp_path):
    _, tok = await user_token(gateway_blob)
    doc = tmp_path / "elsewhere" / "Frame.SLDASM"          # outside the staging folder: allowed because SolidWorks says so
    doc.parent.mkdir()
    doc.write_bytes(b"asm" * 100)
    monkeypatch.setattr(agent.sw_server, "_call_raw", lambda tool, params: {
        "status": "COMPLETED", "cadState": {"features": [str(doc)]}})
    res = await call(gateway_blob, tok["access_token"], "deliver_document", {})
    assert not res.get("isError"), res
    assert "Frame.SLDASM" in res["content"][0]["text"]


async def test_deliver_refuses_paths_outside_allowed_roots_and_oversize(gateway_blob, live_agent, monkeypatch, tmp_path):
    _, tok = await user_token(gateway_blob)
    outside = tmp_path / "secret.step"
    outside.write_bytes(b"x")
    res = await call(gateway_blob, tok["access_token"], "deliver_document", {"file_path": str(outside)})
    assert res["isError"] and "FORBIDDEN_PATH" in res["content"][0]["text"]
    monkeypatch.setattr(agent, "DELIVER_MAX_BYTES", 10)
    big = os.path.join(agent.STAGING_DIR, "big.step")
    with open(big, "wb") as fh:
        fh.write(b"y" * 100)
    res = await call(gateway_blob, tok["access_token"], "deliver_document", {"file_path": big})
    assert res["isError"] and "FILE_TOO_LARGE" in res["content"][0]["text"]
    res = await call(gateway_blob, tok["access_token"], "deliver_document", {"format": "EXE"})
    assert res["isError"] and "INVALID_PARAMETER" in res["content"][0]["text"]
