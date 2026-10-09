"""ASGI app: Claude-facing MCP (+ its OAuth endpoints), owner login, and the PC agent's HTTPS endpoints."""

import base64
import contextlib
import hmac
import html
import logging
from urllib.parse import unquote

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .blobstore import BlobBackend, VercelBlob
from .config import AGENT_SCOPE, USER_SCOPE, Settings
from .deliveries import Deliveries, DeliveryError
from .provider import OwnerProvider
from .relay import PROTOCOL, AgentRelay
from .store import Store, connect
from .surface import RelayServer

log = logging.getLogger("gateway")

_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>SolidPilot sign-in</title>
<style>:root{{color-scheme:light dark}}body{{font:16px system-ui;max-width:26rem;margin:12vh auto;padding:0 1rem}}
input,button{{font:inherit;padding:.6rem;width:100%;box-sizing:border-box;margin:.4rem 0}}
.err{{color:#c00}}</style>
<h1>SolidPilot</h1><p>Approve access to your SolidWorks session for <b>{client}</b>.</p>{error}
<form method=post action=/login><input type=hidden name=req value="{req}">
<input type=password name=secret placeholder="Owner secret" autocomplete=current-password autofocus required>
<button name=action value=approve>Approve</button><button name=action value=deny>Deny</button></form>"""

_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer"}
_NOSTORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_CATALOG_KEYS = ("tools", "resources", "templates", "prompts")


def create_app(settings: Settings, redis=None, blob: BlobBackend | None = None) -> Starlette:
    store = Store(redis if redis is not None else connect(settings.redis_url))
    provider = OwnerProvider(settings, store)
    relay = AgentRelay(store.redis, settings.call_timeout)
    if blob is None and settings.blob_token:
        blob = VercelBlob(settings.blob_token)
    deliveries = (Deliveries(store, blob, settings.public_url, settings.delivery_ttl, settings.delivery_max_bytes)
                  if blob is not None else None)

    if settings.is_local:
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"] + settings.allowed_origins
    else:
        hosts, origins = settings.allowed_hosts, settings.allowed_origins
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                         allowed_origins=origins)

    mcp = RelayServer(
        relay,
        deliveries,
        name="solidpilot-gateway",
        instructions="SolidWorks CAD tools, executed on the user's own PC through a connected agent.",
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.mcp_url,
            required_scopes=[USER_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[USER_SCOPE], default_scopes=[USER_SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=True,
        ),
        auth_server_provider=provider,
    )
    # Stateless + plain JSON responses: no session or SSE stream has to survive between serverless
    # invocations. Streamable HTTP lives at /mcp; the OAuth and well-known routes at the root.
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                      transport_security=security, host=settings.host.split(":")[0])

    # ----- owner login ------------------------------------------------------------------------
    async def login_get(request: Request) -> HTMLResponse:
        req = request.query_params.get("req", "")
        pending = await provider.pending(req)
        if pending is None:
            return HTMLResponse("Sign-in request expired. Start again from Claude.", status_code=400, headers=_HEADERS)
        client = await provider.get_client(pending.get("client_id", ""))
        name = client.client_name if client and client.client_name else "Claude"
        return HTMLResponse(_PAGE.format(client=html.escape(name), req=html.escape(req), error=""), headers=_HEADERS)

    async def login_post(request: Request):
        form = await request.form()
        req, secret, action = str(form.get("req", "")), str(form.get("secret", "")), str(form.get("action", ""))
        if await provider.login_locked():
            return HTMLResponse("Too many attempts. Try again later.", status_code=429, headers=_HEADERS)
        target = await (provider.deny(req) if action == "deny" else provider.complete_login(req, secret))
        if target is None:
            if await provider.pending(req) is None:
                return HTMLResponse("Sign-in request expired. Start again from Claude.", status_code=400,
                                    headers=_HEADERS)
            body = _PAGE.format(client="Claude", req=html.escape(req), error='<p class=err>Wrong secret.</p>')
            return HTMLResponse(body, status_code=401, headers=_HEADERS)
        return RedirectResponse(target, status_code=302, headers=_HEADERS)

    # ----- agent token (OAuth client_credentials) ---------------------------------------------
    async def agent_token(request: Request) -> JSONResponse:
        if await provider.agent_locked():
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=429, headers=_NOSTORE)
        form = await request.form()
        if form.get("grant_type") != "client_credentials":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=_NOSTORE)
        client_id, client_secret = "", ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                raw = base64.b64decode(auth[6:]).decode()
                client_id, _, client_secret = raw.partition(":")
                client_id, client_secret = unquote(client_id), unquote(client_secret)  # RFC 6749 §2.3.1
            except ValueError:
                pass
        if not await provider.authenticate_agent(client_id, client_secret):
            return JSONResponse({"error": "invalid_client"}, status_code=401,
                                headers={**_NOSTORE, "WWW-Authenticate": "Basic"})
        resource = form.get("resource")
        if resource is not None and str(resource).rstrip("/") != settings.agent_resource:
            return JSONResponse({"error": "invalid_target"}, status_code=400, headers=_NOSTORE)
        scope = form.get("scope")
        if scope is not None and set(str(scope).split()) - {AGENT_SCOPE}:
            return JSONResponse({"error": "invalid_scope"}, status_code=400, headers=_NOSTORE)
        return JSONResponse((await provider.issue_agent_token()).model_dump(exclude_none=True), headers=_NOSTORE)

    # ----- agent endpoints (all outbound-initiated by the PC) ---------------------------------
    async def _agent_authorized(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        return bool(token) and await provider.verify_agent_token(token)

    def _unauthorized() -> Response:
        return JSONResponse({"error": "invalid_token"}, status_code=401,
                            headers={**_NOSTORE, "WWW-Authenticate": 'Bearer error="invalid_token"'})

    def _protocol_ok(request: Request) -> bool:
        return request.headers.get("x-agent-protocol") == str(PROTOCOL)

    async def agent_poll(request: Request) -> Response:
        if not await _agent_authorized(request):
            return _unauthorized()
        if not _protocol_ok(request):
            return JSONResponse({"error": "unsupported_protocol", "supported": PROTOCOL}, status_code=426)
        try:
            wait = int((await request.json()).get("wait", settings.poll_wait))
        except (ValueError, AttributeError):
            wait = settings.poll_wait
        job = await relay.next_job(max(1, min(wait, settings.poll_wait)), request.headers.get("x-agent-version", ""))
        if job is None:
            return Response(status_code=204, headers=_NOSTORE)
        return JSONResponse({"job": job}, headers=_NOSTORE)

    async def agent_result(request: Request) -> Response:
        if not await _agent_authorized(request):
            return _unauthorized()
        try:
            frame = await request.json()
            job_id = frame["id"]
            if not isinstance(job_id, str) or not job_id.isalnum() or ("result" not in frame and "error" not in frame):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        await relay.submit_result({k: frame[k] for k in ("id", "result", "error") if k in frame})
        return Response(status_code=204, headers=_NOSTORE)

    async def agent_catalog(request: Request) -> Response:
        if not await _agent_authorized(request):
            return _unauthorized()
        try:
            body = await request.json()
            catalog = {k: body[k] for k in _CATALOG_KEYS if isinstance(body.get(k), list)}
        except (ValueError, AttributeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        await relay.set_catalog(catalog)
        await relay.touch(request.headers.get("x-agent-version", ""))
        return Response(status_code=204, headers=_NOSTORE)

    # ----- file deliveries ----------------------------------------------------------------------
    async def agent_upload_url(request: Request) -> Response:
        if not await _agent_authorized(request):
            return _unauthorized()
        if deliveries is None:
            return JSONResponse({"error": "DELIVERY_UNAVAILABLE"}, status_code=501, headers=_NOSTORE)
        try:
            body = await request.json()
            result = await deliveries.request_upload(str(body.get("slot", "")), str(body.get("filename", "")),
                                                     body.get("size"))
        except DeliveryError as e:
            return JSONResponse({"error": e.code, "message": str(e)}, status_code=400, headers=_NOSTORE)
        except (ValueError, AttributeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=_NOSTORE)
        return JSONResponse(result, headers=_NOSTORE)

    async def agent_upload_complete(request: Request) -> Response:
        if not await _agent_authorized(request):
            return _unauthorized()
        if deliveries is None:
            return JSONResponse({"error": "DELIVERY_UNAVAILABLE"}, status_code=501, headers=_NOSTORE)
        try:
            body = await request.json()
            size = body.get("size")
            if not isinstance(size, int) or isinstance(size, bool):
                raise ValueError
            result = await deliveries.complete(str(body.get("slot", "")), size)
        except DeliveryError as e:
            return JSONResponse({"error": e.code, "message": str(e)}, status_code=400, headers=_NOSTORE)
        except (ValueError, AttributeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=_NOSTORE)
        return JSONResponse(result, headers=_NOSTORE)

    def _client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")) or "?"

    async def download(request: Request) -> Response:
        """The link itself is the credential: 256-bit, single use, 1 h. Every miss looks the same."""
        miss = Response("Not found", status_code=404, headers=_HEADERS)
        if deliveries is None:
            return miss
        ip = _client_ip(request)
        if await store.is_locked(f"dl:{ip}"):
            return Response("Too many attempts.", status_code=429, headers=_HEADERS)
        url = await deliveries.redeem(request.path_params["token"])
        if url is None:
            if await store.bump(f"dlfail:{ip}", window=600) >= 20:
                await store.lock(f"dl:{ip}", 600)
            return miss
        return Response(status_code=302, headers={**_HEADERS, "Location": url})

    async def internal_sweep(request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        ok = bool(settings.cron_secret) and hmac.compare_digest(auth.encode(), f"Bearer {settings.cron_secret}".encode())
        if not ok or deliveries is None:
            return Response("Not found", status_code=404)
        return JSONResponse({"deleted": await deliveries.sweep()}, headers=_NOSTORE)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "agent_connected": await relay.connected()})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():  # Starlette does not run a mounted app's lifespan
            yield

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/login", login_get, methods=["GET"]),
            Route("/login", login_post, methods=["POST"]),
            Route("/agent/token", agent_token, methods=["POST"]),
            Route("/agent/poll", agent_poll, methods=["POST"]),
            Route("/agent/result", agent_result, methods=["POST"]),
            Route("/agent/catalog", agent_catalog, methods=["POST"]),
            Route("/agent/upload-url", agent_upload_url, methods=["POST"]),
            Route("/agent/upload-complete", agent_upload_complete, methods=["POST"]),
            Route("/dl/{token}", download, methods=["GET"]),
            Route("/internal/sweep", internal_sweep, methods=["GET"]),
            Mount("/", app=mcp_app),  # must stay last: it matches every path
        ],
        lifespan=lifespan,
    )
    app.state.relay = relay
    app.state.provider = provider
    app.state.store = store
    app.state.deliveries = deliveries
    return app
