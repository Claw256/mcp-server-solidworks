"""Serverless means any instance can serve any step: two providers on one Redis must interoperate."""
import fakeredis
import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from solidpilot_gateway.config import Settings
from solidpilot_gateway.provider import OwnerProvider
from solidpilot_gateway.relay import AgentOffline, AgentRelay
from solidpilot_gateway.store import Store

from .conftest import OWNER_SECRET, make_settings

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def two_instances():
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    settings = make_settings(8123)
    return OwnerProvider(settings, Store(redis)), OwnerProvider(settings, Store(redis)), redis, settings


async def test_flow_can_hop_between_instances_and_codes_are_single_use(two_instances):
    a, b, _, settings = two_instances
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")
    await a.register_client(client)                                           # instance A registers
    url = await b.authorize(await a.get_client("c1"), AuthorizationParams(    # B starts authorization
        state="s", scopes=None, code_challenge="x" * 43, redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True, resource=settings.mcp_url))
    req = url.split("req=")[1]
    redirect = await a.complete_login(req, OWNER_SECRET)                      # A takes the login
    code = redirect.split("code=")[1].split("&")[0]
    loaded = await b.load_authorization_code(client, code)                    # B loads the code
    tokens = await a.exchange_authorization_code(client, loaded)              # A redeems it
    with pytest.raises(TokenError):                                           # replay on any instance fails
        await b.exchange_authorization_code(client, loaded)
    assert await b.load_access_token(tokens.access_token) is not None         # B accepts A's token
    assert await b.load_access_token("nope") is None


async def test_lockout_is_shared(two_instances):
    a, b, _, _ = two_instances
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")
    await a.register_client(client)
    url = await a.authorize(client, AuthorizationParams(
        state=None, scopes=None, code_challenge="x" * 43, redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True))
    req = url.split("req=")[1]
    for _ in range(5):
        assert await a.complete_login(req, "bad") is None
    assert await b.login_locked()                                             # B sees A's failures
    assert await b.complete_login(req, OWNER_SECRET) is None


async def test_relay_queue_across_instances(two_instances):
    _, _, redis, _ = two_instances
    pc, caller = AgentRelay(redis, 5), AgentRelay(redis, 5)
    with pytest.raises(AgentOffline):
        await caller.request("tools/call", {"name": "x", "arguments": {}})

    await pc.touch("t")
    import asyncio

    async def agent():
        job = await pc.next_job(3)
        await pc.submit_result({"id": job["id"], "result": {"echo": job["params"]}})

    task = asyncio.create_task(agent())
    assert await caller.request("tools/call", {"name": "x", "arguments": {"n": 1}}) == {
        "echo": {"name": "x", "arguments": {"n": 1}}}
    await task
    assert await redis.llen("sp:relay:jobs") == 0


async def test_unclaimed_job_is_withdrawn_on_timeout(two_instances):
    _, _, redis, _ = two_instances
    relay = AgentRelay(redis, 1)
    await relay.touch("t")           # PC "present" but never takes the job
    import asyncio
    with pytest.raises(asyncio.TimeoutError):
        await relay.request("tools/call", {"name": "x", "arguments": {}}, timeout=1)
    assert await redis.llen("sp:relay:jobs") == 0   # no stale work left for the PC to execute later
