"""Real agent (adapters/claude/agent.py, real tool surface) <-> real gateway, C# layer stubbed."""
import asyncio
import base64
import os
import sys

import httpx
import pytest

from .conftest import AGENT_ID, AGENT_SECRET
from .test_gateway import mcp_call, parse_rpc, user_token

ADAPTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "adapters", "claude"))
sys.path.insert(0, ADAPTER_DIR)

agent = pytest.importorskip("agent")  # needs websockets + the adapter's own dependencies


@pytest.fixture
async def live_agent(gateway, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "GATEWAY_URL", gateway.url)
    monkeypatch.setattr(agent, "CLIENT_ID", AGENT_ID)
    monkeypatch.setattr(agent, "CLIENT_SECRET", AGENT_SECRET)
    monkeypatch.setattr(agent, "STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setattr(agent, "POLL_WAIT", 1)
    monkeypatch.setattr(agent, "_call_lock", asyncio.Lock())
    monkeypatch.setattr(agent.sw_server, "_ensure_ready", lambda: {
        "comAttached": True, "swLaunched": False, "activeDocument": None, "swVersion": "test", "stateVersion": 7})
    client = httpx.AsyncClient()
    task = asyncio.create_task(agent.poll_forever(agent.Gateway(client)))
    for _ in range(200):
        if await gateway.agent_connected():
            break
        await asyncio.sleep(0.05)
    assert await gateway.agent_connected(), "agent never connected"
    yield agent
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client.aclose()


async def test_real_tool_surface_and_call(gateway, live_agent):
    _, tok = await user_token(gateway)
    access = tok["access_token"]
    listed = parse_rpc(await mcp_call(gateway, access, "tools/list"))["result"]["tools"]
    names = {t["name"] for t in listed}
    assert {"ensure_ready", "open_document", "export_image", "prepare_drawing", "stage_file", "get_file"} <= names

    res = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "ensure_ready", "arguments": {}}))
    assert not res["result"].get("isError"), res
    assert "READY" in res["result"]["content"][0]["text"]


async def test_tool_error_text_reaches_the_model(gateway, live_agent):
    _, tok = await user_token(gateway)
    res = parse_rpc(await mcp_call(gateway, tok["access_token"], "tools/call",
                                   {"name": "stage_file", "arguments": {"filename": "x.exe", "content_base64": "AA=="}}))
    assert res["result"]["isError"] and "UNSUPPORTED_TYPE" in res["result"]["content"][0]["text"]


async def test_stage_then_fetch_roundtrip_and_path_jail(gateway, live_agent, tmp_path):
    _, tok = await user_token(gateway)
    access = tok["access_token"]
    payload = b"%PDF-1.4 fake drawing"
    staged = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "stage_file", "arguments": {
        "filename": "..\\..\\evil name.pdf", "content_base64": base64.b64encode(payload).decode()}}))
    text = staged["result"]["content"][0]["text"]
    path = text.split(" at ", 1)[1]
    assert os.path.dirname(path) == agent.STAGING_DIR and os.path.basename(path) == "evil name.pdf"

    got = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "get_file", "arguments": {"file_path": path}}))
    blob = [c for c in got["result"]["content"] if c["type"] == "resource"][0]["resource"]
    assert base64.b64decode(blob["blob"]) == payload

    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"nope")
    denied = parse_rpc(await mcp_call(gateway, access, "tools/call",
                                      {"name": "get_file", "arguments": {"file_path": str(outside)}}))
    assert denied["result"]["isError"] and "FORBIDDEN_PATH" in denied["result"]["content"][0]["text"]


async def test_resources_and_prompt_relay(gateway, live_agent):
    _, tok = await user_token(gateway)
    access = tok["access_token"]
    res = parse_rpc(await mcp_call(gateway, access, "resources/list"))["result"]["resources"]
    assert any(r["uri"].startswith("schema://") for r in res)
    read = parse_rpc(await mcp_call(gateway, access, "resources/read", {"uri": "schema://feature-graph"}))
    assert read["result"]["contents"][0]["text"]
    prompts = parse_rpc(await mcp_call(gateway, access, "prompts/list"))["result"]["prompts"]
    assert "pdf_drawing_to_part" in {p["name"] for p in prompts}


async def test_image_result_survives_relay(gateway, live_agent):
    _, tok = await user_token(gateway)
    access = tok["access_token"]
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    staged = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "stage_file", "arguments": {
        "filename": "pic.png", "content_base64": base64.b64encode(png).decode()}}))
    path = staged["result"]["content"][0]["text"].split(" at ", 1)[1]
    got = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "get_file", "arguments": {"file_path": path}}))
    img = [c for c in got["result"]["content"] if c["type"] == "image"][0]
    assert img["mimeType"] == "image/png" and base64.b64decode(img["data"]) == png
