import asyncio
import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from .conftest import AGENT_ID, AGENT_SECRET, OWNER_SECRET

REDIRECT = "https://claude.ai/api/mcp/auth_callback"

TOOL = {"name": "ensure_ready", "description": "Bring SolidWorks up.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
CATALOG = {"tools": [TOOL], "resources": [], "templates": [], "prompts": []}


async def agent_token(gw, secret=AGENT_SECRET, resource=None):
    async with httpx.AsyncClient() as c:
        return await c.post(
            f"{gw.url}/agent/token", auth=(AGENT_ID, secret),
            data={"grant_type": "client_credentials", "scope": "agent", "resource": resource or f"{gw.url}/agent"})


async def user_token(gw, scope="solidworks"):
    """Full DCR + authorization-code + PKCE dance against the gateway, as Claude would do it."""
    async with httpx.AsyncClient(follow_redirects=False) as c:
        reg = await c.post(f"{gw.url}/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = await c.get(f"{gw.url}/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "xyz", "scope": scope, "resource": f"{gw.url}/mcp"})
        assert auth.status_code == 302, auth.text
        login = auth.headers["location"]
        assert urlsplit(login).path == "/login"
        req = parse_qs(urlsplit(login).query)["req"][0]
        page = await c.get(login)
        assert page.status_code == 200 and "Owner secret" in page.text
        bad = await c.post(f"{gw.url}/login", data={"req": req, "secret": "nope", "action": "approve"})
        assert bad.status_code == 401
        ok = await c.post(f"{gw.url}/login", data={"req": req, "secret": OWNER_SECRET, "action": "approve"})
        assert ok.status_code == 302
        q = parse_qs(urlsplit(ok.headers["location"]).query)
        assert q["state"] == ["xyz"]
        tok = await c.post(f"{gw.url}/token", data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier, "resource": f"{gw.url}/mcp"})
        assert tok.status_code == 200, tok.text
        return client_id, tok.json()


class FakeAgent:
    """Speaks the relay protocol the way adapters/claude/agent.py does: push catalogue, long-poll, post results."""

    def __init__(self, gw, handler=None, catalog=CATALOG):
        self.gw, self.handler, self.catalog, self.seen = gw, handler or self._default, catalog, []
        self.tasks = set()

    async def _default(self, method, params):
        if method == "tools/call":
            return {"content": [{"type": "text", "text": f"ran {params['name']}"}], "isError": False}
        return {}

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "X-Agent-Protocol": "1", "X-Agent-Version": "test"}

    async def __aenter__(self):
        self.token = (await agent_token(self.gw)).json()["access_token"]
        self.http = httpx.AsyncClient(timeout=30)
        r = await self.http.post(f"{self.gw.url}/agent/catalog", json=self.catalog, headers=self._headers())
        assert r.status_code == 204, r.text
        self.task = asyncio.create_task(self._poll())
        return self

    async def _poll(self):
        while True:
            r = await self.http.post(f"{self.gw.url}/agent/poll", json={"wait": 1}, headers=self._headers())
            if r.status_code == 200:
                t = asyncio.create_task(self._run(r.json()["job"]))
                self.tasks.add(t)
                t.add_done_callback(self.tasks.discard)

    async def _run(self, job):
        self.seen.append((job["method"], job["params"]))
        try:
            out = {"id": job["id"], "result": await self.handler(job["method"], job["params"])}
        except Exception as e:
            out = {"id": job["id"], "error": {"code": "Boom", "message": str(e)}}
        await self.http.post(f"{self.gw.url}/agent/result", json=out, headers=self._headers())

    async def __aexit__(self, *a):
        self.task.cancel()
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(self.task, *self.tasks, return_exceptions=True)
        await self.http.aclose()


async def wait_for(predicate, seconds=10):
    for _ in range(int(seconds / 0.1)):
        if await predicate():
            return True
        await asyncio.sleep(0.1)
    return False


async def mcp_call(gw, token, method, params=None, id_=1):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{gw.url}/mcp", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
        return r


def parse_rpc(resp):
    body = resp.text
    if "data:" in body:
        body = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")][-1]
    return json.loads(body)


# ----------------------------------------------------------------------------------------------
async def test_health_and_discovery(gateway):
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{gateway.url}/health")).json() == {"ok": True, "agent_connected": False}
        prm = await c.get(f"{gateway.url}/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200 and prm.json()["resource"] == f"{gateway.url}/mcp"
        assert prm.json()["authorization_servers"][0].rstrip("/") == gateway.url
        asm = (await c.get(f"{gateway.url}/.well-known/oauth-authorization-server")).json()
        assert "S256" in asm["code_challenge_methods_supported"]
        assert asm["registration_endpoint"].endswith("/register")
        unauth = await c.post(f"{gateway.url}/mcp", json={})
        assert unauth.status_code == 401 and "resource_metadata" in unauth.headers["www-authenticate"]


async def test_dcr_rejects_foreign_redirect(gateway):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{gateway.url}/register", json={
            "redirect_uris": ["https://evil.example/cb"], "token_endpoint_auth_method": "none"})
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


async def test_agent_token_endpoint(gateway):
    ok = await agent_token(gateway)
    assert ok.status_code == 200 and ok.json()["token_type"].lower() == "bearer"
    assert (await agent_token(gateway, secret="wrong")).status_code == 401
    assert (await agent_token(gateway, resource="https://other.example/agent")).status_code == 400


async def test_full_flow_user_to_agent(gateway):
    async with FakeAgent(gateway) as agent:
        _, tok = await user_token(gateway)
        access = tok["access_token"]
        listed = parse_rpc(await mcp_call(gateway, access, "tools/list"))
        assert [t["name"] for t in listed["result"]["tools"]] == ["ensure_ready"]
        called = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "ensure_ready", "arguments": {}}))
        assert called["result"]["content"][0]["text"] == "ran ensure_ready"
        assert ("tools/call", {"name": "ensure_ready", "arguments": {}}) in agent.seen
        assert await gateway.agent_connected()


async def test_sdk_client_end_to_end(gateway):
    """A real MCP client (initialize/negotiation included), not hand-rolled JSON-RPC."""
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    async with FakeAgent(gateway):
        _, tok = await user_token(gateway)
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok['access_token']}"})
        async with Client(streamable_http_client(f"{gateway.url}/mcp", http_client=http)) as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == ["ensure_ready"]
            result = await client.call_tool("ensure_ready", {})
            assert not result.is_error and result.content[0].text == "ran ensure_ready"


async def test_refresh_rotates_and_replay_fails(gateway):
    client_id, tok = await user_token(gateway)
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{gateway.url}/token", data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id})
        assert r.status_code == 200, r.text
        assert r.json()["refresh_token"] != tok["refresh_token"]
        replay = await c.post(f"{gateway.url}/token", data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id})
        assert replay.status_code == 400


async def test_token_kinds_do_not_cross(gateway):
    _, tok = await user_token(gateway)
    agent_tok = (await agent_token(gateway)).json()["access_token"]
    assert (await mcp_call(gateway, agent_tok, "tools/list")).status_code == 401   # agent token cannot call tools
    async with httpx.AsyncClient() as c:
        for bearer in (tok["access_token"], "garbage", None):                      # nor can anyone else poll
            headers = {"X-Agent-Protocol": "1", **({"Authorization": f"Bearer {bearer}"} if bearer else {})}
            for path in ("/agent/poll", "/agent/result", "/agent/catalog"):
                r = await c.post(f"{gateway.url}{path}", json={"id": "a", "result": {}}, headers=headers)
                assert r.status_code == 401, (path, bearer)


async def test_agent_endpoint_validation(gateway):
    token = (await agent_token(gateway)).json()["access_token"]
    h = {"Authorization": f"Bearer {token}", "X-Agent-Protocol": "1"}
    async with httpx.AsyncClient() as c:
        assert (await c.post(f"{gateway.url}/agent/poll", json={}, headers={"Authorization": f"Bearer {token}"})
                ).status_code == 426                                        # missing/old protocol is refused
        assert (await c.post(f"{gateway.url}/agent/result", json={"id": "../x", "result": {}}, headers=h)
                ).status_code == 400                                        # ids are validated
        assert (await c.post(f"{gateway.url}/agent/result", json={"id": "abc"}, headers=h)).status_code == 400
        assert (await c.post(f"{gateway.url}/agent/poll", json={"wait": 1}, headers=h)).status_code == 204


async def test_catalog_served_while_pc_off_and_calls_say_offline(gateway):
    _, tok = await user_token(gateway)
    access = tok["access_token"]
    async with FakeAgent(gateway):
        pass
    assert await wait_for(lambda: _is_offline(gateway), seconds=8)
    listed = parse_rpc(await mcp_call(gateway, access, "tools/list"))
    assert [t["name"] for t in listed["result"]["tools"]] == ["ensure_ready"]      # from the pushed catalogue
    called = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "ensure_ready", "arguments": {}}))
    assert called["result"]["isError"] is True
    assert "AGENT_OFFLINE" in called["result"]["content"][0]["text"]


async def _is_offline(gw):
    return not await gw.agent_connected()


async def test_agent_error_and_timeout_surface_as_tool_errors(gateway):
    async def handler(method, params):
        if params["name"] == "slow":
            await asyncio.sleep(30)
        raise RuntimeError("COM exploded")

    async with FakeAgent(gateway, handler):
        _, tok = await user_token(gateway)
        access = tok["access_token"]
        err = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "ensure_ready", "arguments": {}}))
        assert err["result"]["isError"] and "COM exploded" in err["result"]["content"][0]["text"]
        slow = parse_rpc(await mcp_call(gateway, access, "tools/call", {"name": "slow", "arguments": {}}))
        assert slow["result"]["isError"] and "TIMEOUT" in slow["result"]["content"][0]["text"]


async def test_concurrent_calls_are_correlated(gateway):
    async def handler(method, params):
        await asyncio.sleep(0.3 if params["arguments"]["n"] == 1 else 0.05)
        return {"content": [{"type": "text", "text": f"n={params['arguments']['n']}"}], "isError": False}

    async with FakeAgent(gateway, handler):
        _, tok = await user_token(gateway)
        access = tok["access_token"]
        rs = await asyncio.gather(*[
            mcp_call(gateway, access, "tools/call", {"name": "ensure_ready", "arguments": {"n": n}}, id_=n)
            for n in (1, 2, 3)])
        assert [parse_rpc(r)["result"]["content"][0]["text"] for r in rs] == ["n=1", "n=2", "n=3"]


async def test_owner_login_lockout(gateway):
    async with httpx.AsyncClient(follow_redirects=False) as c:
        reg = await c.post(f"{gateway.url}/register", json={
            "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
        cid = reg.json()["client_id"]
        auth = await c.get(f"{gateway.url}/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": "x" * 43,
            "code_challenge_method": "S256"})
        req = parse_qs(urlsplit(auth.headers["location"]).query)["req"][0]
        codes = []
        for _ in range(7):
            r = await c.post(f"{gateway.url}/login", data={"req": req, "secret": "wrong", "action": "approve"})
            codes.append(r.status_code)
        assert codes[:5] == [401] * 5 and codes[-1] == 429
        # even the right secret is refused while locked
        r = await c.post(f"{gateway.url}/login", data={"req": req, "secret": OWNER_SECRET, "action": "approve"})
        assert r.status_code == 429


async def test_foreign_resource_rejected_at_authorize(gateway):
    async with httpx.AsyncClient(follow_redirects=False) as c:
        reg = await c.post(f"{gateway.url}/register", json={
            "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
        cid = reg.json()["client_id"]
        r = await c.get(f"{gateway.url}/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": "x" * 43,
            "code_challenge_method": "S256", "resource": "https://other.example/mcp"})
        assert r.status_code in (302, 400)
        if r.status_code == 302:
            assert "error=invalid_target" in r.headers["location"]
