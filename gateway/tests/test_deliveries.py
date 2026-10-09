"""deliver_document plumbing in the gateway: slots bound to the session, presigned upload, one-time links."""
import asyncio
import re
import time

import httpx

from solidpilot_gateway.store import digest

from .conftest import AGENT_ID, AGENT_SECRET
from .test_gateway import FakeAgent, mcp_call, parse_rpc, user_token

DELIVER = {"name": "deliver_document", "description": "d",
           "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string"},
                                                            "delivery_slot": {"type": "string"}},
                           "required": ["delivery_slot"]}}
CATALOG = {"tools": [DELIVER], "resources": [], "templates": [], "prompts": []}


async def agent_headers(gw):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{gw.url}/agent/token", auth=(AGENT_ID, AGENT_SECRET),
                         data={"grant_type": "client_credentials", "scope": "agent", "resource": f"{gw.url}/agent"})
    return {"Authorization": f"Bearer {r.json()['access_token']}", "X-Agent-Protocol": "1"}


async def open_slot_via_chat(gw, token, agent):
    """A deliver_document call from the chat; returns (slot the gateway injected, the pending call)."""
    before = len(agent.seen)
    task = asyncio.create_task(mcp_call(gw, token, "tools/call", {
        "name": "deliver_document", "arguments": {"file_path": "x", "delivery_slot": "attacker-chosen"}}))
    for _ in range(100):
        if len(agent.seen) > before:
            break
        await asyncio.sleep(0.05)
    return agent.seen[-1][1]["arguments"]["delivery_slot"], task


async def test_tool_schema_hides_slot_and_gateway_injects_its_own(gateway_blob):
    _, tok = await user_token(gateway_blob)
    access = tok["access_token"]
    async with FakeAgent(gateway_blob, catalog=CATALOG) as agent:
        listed = parse_rpc(await mcp_call(gateway_blob, access, "tools/list"))["result"]["tools"][0]
        assert "delivery_slot" not in listed["inputSchema"]["properties"]
        assert "delivery_slot" not in listed["inputSchema"].get("required", [])
        slot, task = await open_slot_via_chat(gateway_blob, access, agent)
        assert re.fullmatch(r"[0-9a-f]{32}", slot) and slot != "attacker-chosen"
        task.cancel()


async def test_delivery_unavailable_without_blob(gateway):
    _, tok = await user_token(gateway)
    async with FakeAgent(gateway, catalog=CATALOG):
        res = parse_rpc(await mcp_call(gateway, tok["access_token"], "tools/call",
                                       {"name": "deliver_document", "arguments": {}}))
        assert res["result"]["isError"] and "DELIVERY_UNAVAILABLE" in res["result"]["content"][0]["text"]


async def test_full_flow_one_time_link(gateway_blob, fake_blob):
    _, tok = await user_token(gateway_blob)
    async with FakeAgent(gateway_blob, catalog=CATALOG) as agent:
        slot, task = await open_slot_via_chat(gateway_blob, tok["access_token"], agent)
        task.cancel()
    h = await agent_headers(gateway_blob)
    data = b"S" * 5_000_000          # > the 4.5 MB function body limit: never passes through the gateway
    async with httpx.AsyncClient(follow_redirects=False) as c:
        up = (await c.post(f"{gateway_blob.url}/agent/upload-url", headers=h,
                           json={"slot": slot, "filename": "bracket.SLDPRT", "size": len(data)})).json()
        assert up["pathname"] == f"deliveries/{slot}/bracket.SLDPRT" and up["method"] == "PUT"
        again = await c.post(f"{gateway_blob.url}/agent/upload-url", headers=h,
                             json={"slot": slot, "filename": "bracket.SLDPRT", "size": len(data)})
        assert again.status_code == 400 and again.json()["error"] == "INVALID_SLOT"   # one upload per slot
        fake_blob.upload(up["pathname"], data)
        done = (await c.post(f"{gateway_blob.url}/agent/upload-complete", headers=h,
                             json={"slot": slot, "size": len(data)})).json()
        assert done["size"] == len(data) and done["filename"] == "bracket.SLDPRT" and done["expires_in"] == 3600
        link = done["download_url"]
        assert link.startswith(f"{gateway_blob.url}/dl/")

        first = await c.get(link)
        assert first.status_code == 302 and first.headers["location"].startswith("https://blob.fake/get/deliveries/")
        assert "no-store" in first.headers["cache-control"] and first.headers["referrer-policy"] == "no-referrer"
        assert (await c.get(link)).status_code == 404                                  # burnt after first use
        assert (await c.get(f"{gateway_blob.url}/dl/{'a' * 43}")).status_code == 404   # unknown token
        redo = await c.post(f"{gateway_blob.url}/agent/upload-complete", headers=h,
                            json={"slot": slot, "size": len(data)})
        assert redo.status_code == 400                                                  # slot cannot complete twice


async def test_upload_rules(gateway_blob, fake_blob):
    _, tok = await user_token(gateway_blob)
    h = await agent_headers(gateway_blob)
    async with FakeAgent(gateway_blob, catalog=CATALOG) as agent, httpx.AsyncClient() as c:
        async def fresh():
            slot, task = await open_slot_via_chat(gateway_blob, tok["access_token"], agent)
            task.cancel()
            return slot
        up = f"{gateway_blob.url}/agent/upload-url"
        bad_ext = await c.post(up, headers=h, json={"slot": await fresh(), "filename": "evil.exe", "size": 10})
        assert bad_ext.json()["error"] == "UNSUPPORTED_TYPE"
        too_big = await c.post(up, headers=h, json={"slot": await fresh(), "filename": "a.step",
                                                    "size": 50 * 1024 * 1024 + 1})
        assert too_big.json()["error"] == "FILE_TOO_LARGE"
        zero = await c.post(up, headers=h, json={"slot": await fresh(), "filename": "a.step", "size": 0})
        assert zero.json()["error"] == "FILE_TOO_LARGE"
        nosuch = await c.post(up, headers=h, json={"slot": "0" * 32, "filename": "a.step", "size": 5})
        assert nosuch.json()["error"] == "INVALID_SLOT"
        slot = await fresh()      # path tricks are flattened into the slot's own folder
        ok = (await c.post(up, headers=h, json={"slot": slot, "filename": "..\\..\\etc/passwd.step", "size": 5})).json()
        assert ok["pathname"] == f"deliveries/{slot}/passwd.step"
        fake_blob.upload(ok["pathname"], b"12345")      # size mismatch at completion deletes the blob
        mism = await c.post(f"{gateway_blob.url}/agent/upload-complete", headers=h, json={"slot": slot, "size": 6})
        assert mism.json()["error"] == "SIZE_MISMATCH" and ok["pathname"] in fake_blob.deleted
        slot2 = await fresh()     # completing without uploading fails
        up2 = (await c.post(up, headers=h, json={"slot": slot2, "filename": "b.step", "size": 5})).json()
        miss = await c.post(f"{gateway_blob.url}/agent/upload-complete", headers=h, json={"slot": slot2, "size": 5})
        assert miss.json()["error"] == "UPLOAD_MISSING" and up2["pathname"] not in fake_blob.objects


async def test_agent_endpoints_need_the_agent_token(gateway_blob):
    _, tok = await user_token(gateway_blob)
    async with httpx.AsyncClient() as c:
        for path in ("upload-url", "upload-complete"):
            assert (await c.post(f"{gateway_blob.url}/agent/{path}", json={})).status_code == 401
            user = await c.post(f"{gateway_blob.url}/agent/{path}", json={},
                                headers={"Authorization": f"Bearer {tok['access_token']}", "X-Agent-Protocol": "1"})
            assert user.status_code == 401        # a chat token can never mint deliveries


async def test_expired_link_and_sweep(gateway_blob, fake_blob):
    store = gateway_blob.app.state.store
    deliveries = gateway_blob.app.state.deliveries
    fake_blob.put_limits["deliveries/s/a.step"] = {"size": 1, "content_type": "model/step", "ttl": 1}
    fake_blob.objects["deliveries/s/a.step"] = b"x"
    token = "t" * 43
    await store.put("dl", digest(token), {"pathname": "deliveries/s/a.step", "filename": "a.step", "size": 1,
                                         "client_id": "c", "expires": time.time() - 1})
    await deliveries.redis.zadd("sp:deliveries:index", {"deliveries/s/a.step": time.time() - 1})
    async with httpx.AsyncClient(follow_redirects=False) as c:
        assert (await c.get(f"{gateway_blob.url}/dl/{token}")).status_code == 404     # expired
        assert (await c.get(f"{gateway_blob.url}/internal/sweep")).status_code == 404  # no cron secret configured
    assert await deliveries.sweep() == 1 and "deliveries/s/a.step" in fake_blob.deleted


async def test_repeated_guessing_is_rate_limited(gateway_blob):
    async with httpx.AsyncClient(follow_redirects=False) as c:
        codes = [(await c.get(f"{gateway_blob.url}/dl/{'g' * 43}")).status_code for _ in range(22)]
    assert codes[0] == 404 and codes[-1] == 429
