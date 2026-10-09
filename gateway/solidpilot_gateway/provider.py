"""OAuth authorization server for the single owner, plus the machine token for the local agent.

Two token kinds live in one keyspace, told apart by ``claims["kind"]``:
  * ``user``  - issued to Claude via authorization-code + PKCE, audience ``<public>/mcp``
  * ``agent`` - issued to the PC agent via client_credentials, audience ``<public>/agent``
Each verifier refuses the other kind, so a stolen user token cannot drive the relay and an agent
token cannot call tools.

All state is in Redis (see store.py) so any serverless instance can serve any step of a flow.
"""

import hmac
import secrets
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import AGENT_SCOPE, USER_SCOPE, Settings
from .store import Store, digest

CODE_TTL = 300
PENDING_TTL = 600
CLIENT_TTL = 60 * 60 * 24 * 180   # bound DCR growth; Claude re-registers after an invalid_client
LOCK_AFTER = 5


class OwnerProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    # ----- brute-force limits (shared across instances) ---------------------------------------
    async def _failed(self, scope: str) -> None:
        n = await self.store.bump(f"fail:{scope}", window=900)
        if n >= LOCK_AFTER:
            await self.store.lock(scope, min(900, 30 * 2 ** (n - LOCK_AFTER)))

    async def login_locked(self) -> bool:
        return await self.store.is_locked("login")

    async def agent_locked(self) -> bool:
        return await self.store.is_locked("agent")

    # ----- dynamic client registration -------------------------------------------------------
    def _redirect_allowed(self, uri: str) -> bool:
        if uri in self.settings.redirect_uris:
            return True
        if self.settings.is_local:  # local development only: loopback redirects
            return urlsplit(uri).hostname in ("localhost", "127.0.0.1", "::1")
        return False

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not self._redirect_allowed(str(uri)):
                raise RegistrationError("invalid_redirect_uri", f"redirect_uri not allowed: {uri}")
        assert client_info.client_id is not None
        await self.store.put("clients", client_info.client_id, client_info.model_dump(mode="json"), ttl=CLIENT_TTL)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = await self.store.get("clients", client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    # ----- authorization (owner login) -------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self.settings.mcp_url:
            raise AuthorizeError("invalid_target", "resource must be this server's /mcp URL")
        req_id = secrets.token_urlsafe(32)
        assert client.client_id is not None
        await self.store.put("pending", req_id,
                             {"client_id": client.client_id, "params": params.model_dump(mode="json")},
                             ttl=PENDING_TTL)
        return f"{self.settings.public_url}/login?req={req_id}"

    async def pending(self, req_id: str) -> dict | None:
        return await self.store.get("pending", req_id)

    async def complete_login(self, req_id: str, secret: str) -> str | None:
        """Check the owner secret; return the redirect back to the client carrying the code."""
        if await self.login_locked():
            return None
        pending = await self.store.get("pending", req_id)
        if pending is None:
            return None
        if not hmac.compare_digest(secret.encode(), self.settings.owner_secret.encode()):
            await self._failed("login")  # global on purpose: one owner, so one lockout
            return None
        await self.store.clear("fail:login")
        pending = await self.store.pop("pending", req_id)  # single use, race-safe
        if pending is None:
            return None
        params = AuthorizationParams.model_validate(pending["params"])
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [USER_SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.settings.mcp_url,
            subject="owner",
        )
        await self.store.put("codes", digest(code.code), code.model_dump(mode="json"), ttl=CODE_TTL)
        return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    async def deny(self, req_id: str) -> str | None:
        pending = await self.store.pop("pending", req_id)
        if pending is None:
            return None
        params = AuthorizationParams.model_validate(pending["params"])
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    # ----- code / token exchange ------------------------------------------------------------
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        raw = await self.store.get("codes", digest(authorization_code))
        if raw is None:
            return None
        code = AuthorizationCode.model_validate(raw)
        return code if code.client_id == client.client_id else None

    async def _mint(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None) -> OAuthToken:
        s = self.settings
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        await self.store.put("tokens", digest(access), AccessToken(
            token="", client_id=client_id, scopes=scopes, expires_at=now + s.access_token_ttl,
            resource=resource, subject=subject, claims={"kind": "user"}).model_dump(mode="json"),
            ttl=s.access_token_ttl)
        await self.store.put("refresh", digest(refresh), RefreshToken(
            token="", client_id=client_id, scopes=scopes, expires_at=now + s.refresh_token_ttl,
            resource=resource, subject=subject).model_dump(mode="json"), ttl=s.refresh_token_ttl)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=s.access_token_ttl,
                          scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        # Atomic redemption: two instances racing on one code cannot both mint tokens.
        if await self.store.pop("codes", digest(authorization_code.code)) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        assert client.client_id is not None
        return await self._mint(client.client_id, authorization_code.scopes, authorization_code.resource,
                                authorization_code.subject)

    async def load_refresh_token(self, client, refresh_token: str):
        raw = await self.store.get("refresh", digest(refresh_token))
        if raw is None:
            return None
        rt = RefreshToken.model_validate(raw)
        if rt.client_id != client.client_id:
            return None
        return rt.model_copy(update={"token": refresh_token})

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        granted = scopes or refresh_token.scopes
        if not set(granted) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "cannot widen scope on refresh")
        if await self.store.pop("refresh", digest(refresh_token.token)) is None:  # rotate; replay fails
            raise TokenError("invalid_grant", "refresh token already used")
        assert client.client_id is not None
        return await self._mint(client.client_id, granted, refresh_token.resource, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = await self.store.get("tokens", digest(token))
        if raw is None:
            return None
        at = AccessToken.model_validate(raw)
        if (at.claims or {}).get("kind") != "user":
            return None
        return at.model_copy(update={"token": token})

    async def revoke_token(self, token) -> None:
        await self.store.delete("tokens", digest(token.token))
        await self.store.delete("refresh", digest(token.token))

    # ----- agent (client_credentials) -------------------------------------------------------
    async def authenticate_agent(self, client_id: str, client_secret: str) -> bool:
        """Constant-time credential check; repeated failures lock only this endpoint, not owner login."""
        ok_id = hmac.compare_digest(client_id.encode(), self.settings.agent_client_id.encode())
        ok_secret = hmac.compare_digest(client_secret.encode(), self.settings.agent_client_secret.encode())
        if ok_id and ok_secret:
            await self.store.clear("fail:agent")
            return True
        await self._failed("agent")
        return False

    async def issue_agent_token(self) -> OAuthToken:
        s = self.settings
        token = secrets.token_urlsafe(32)
        await self.store.put("tokens", digest(token), AccessToken(
            token="", client_id=s.agent_client_id, scopes=[AGENT_SCOPE],
            expires_at=int(time.time()) + s.agent_token_ttl, resource=s.agent_resource,
            claims={"kind": "agent"}).model_dump(mode="json"), ttl=s.agent_token_ttl)
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=s.agent_token_ttl, scope=AGENT_SCOPE)

    async def verify_agent_token(self, token: str) -> bool:
        raw = await self.store.get("tokens", digest(token))
        if raw is None:
            return False
        at = AccessToken.model_validate(raw)
        return (at.claims or {}).get("kind") == "agent" and at.resource == self.settings.agent_resource
