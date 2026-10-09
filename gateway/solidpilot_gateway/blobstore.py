"""Private Vercel Blob access for file deliveries, without the (JS-only) signed-URL SDK.

Vercel's Python SDK has no signed-URL support, so this follows the documented scheme of
`@vercel/blob` (`issueSignedToken` + `presignUrl`): the gateway asks the Blob control API for a short-lived
delegation (`POST /signed-token`, authenticated with the store's read-write token), then HMAC-signs
individual URLs locally. The PC agent PUTs straight to Blob with a presigned URL, and the browser is
redirected to a presigned GET, so file bytes never pass through a Vercel Function (4.5 MB body limit).

`tests/test_blobstore.py` pins the signing to golden vectors produced by the real JS SDK.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import httpx

API_URL = "https://vercel.com/api/blob"
API_VERSION = "12"
MAX_PATHNAME = 950

# Order and names of the query parameters that take part in the signature (from @vercel/blob).
_Q_VALID_UNTIL = "vercel-blob-valid-until"
_Q_MAX_SIZE = "vercel-blob-maximum-size-in-bytes"
_Q_CONTENT_TYPES = "vercel-blob-allowed-content-types"
_Q_RANDOM_SUFFIX = "vercel-blob-add-random-suffix"
_Q_OVERWRITE = "vercel-blob-allow-overwrite"
_CANONICAL_KEYS = (_Q_RANDOM_SUFFIX, _Q_OVERWRITE, _Q_CONTENT_TYPES, "vercel-blob-cache-control-max-age",
                   "vercel-blob-callback-token-payload", "vercel-blob-callback-url", "vercel-blob-if-match",
                   _Q_MAX_SIZE, _Q_VALID_UNTIL)


class BlobError(Exception):
    pass


class BlobBackend(Protocol):
    """What the gateway needs from file storage. `VercelBlob` in production, `FakeBlob` in tests."""

    async def presign_put(self, pathname: str, size: int, content_type: str, ttl: int) -> dict[str, Any]:
        """-> {"url": ..., "headers": {...}} for a single PUT of exactly `size` bytes."""

    async def head(self, pathname: str) -> dict[str, Any] | None:
        """-> {"size": int, "contentType": str} or None when the blob does not exist."""

    async def presign_get(self, pathname: str, ttl: int) -> str: ...

    async def delete(self, pathname: str) -> None: ...


# ----- pure signing helpers (ported from @vercel/blob; pinned by golden-vector tests) -----------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def delegation_payload(delegation_token: str) -> dict[str, Any]:
    head, dot, _ = delegation_token.partition(".")
    if not dot:
        raise BlobError("invalid delegation token format")
    try:
        return json.loads(_b64url_decode(head))
    except ValueError as e:
        raise BlobError("invalid delegation token payload") from e


def store_id_of(delegation_token: str) -> str:
    sid = str(delegation_payload(delegation_token).get("storeId", ""))
    if not sid:
        raise BlobError("delegation token has no storeId")
    return sid[len("store_"):] if sid.startswith("store_") else sid


def _canonical(operation: str, pathname: str, entries: dict[str, str]) -> str:
    lines = [f"operation={operation}", f"pathname={pathname}"]
    lines += [f"{k}={entries[k]}" for k in _CANONICAL_KEYS if entries.get(k)]
    return "\n".join(sorted(lines, key=lambda s: s.encode("utf-8")))


def presign(delegation_token: str, signing_token: str, *, operation: str, pathname: str, valid_until_ms: int,
            max_size: int | None = None, content_types: list[str] | None = None,
            add_random_suffix: bool | None = None, allow_overwrite: bool | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
    """-> {"delegationToken", "signature", "params"} (the same payload `presignUrl` builds)."""
    scope = delegation_payload(delegation_token)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if scope.get("pathname") not in (None, "", "*") and scope["pathname"] != pathname:
        raise BlobError(f"pathname does not match the delegation scope ({scope['pathname']})")
    if operation not in (scope.get("operations") or []):
        raise BlobError(f"delegation token does not allow {operation!r}")
    deleg_until = int(scope["validUntil"]) if scope.get("validUntil") is not None else None
    if deleg_until is not None and now > deleg_until:
        raise BlobError("the signed delegation has expired")
    until = valid_until_ms if deleg_until is None else min(valid_until_ms, deleg_until)
    if until <= now:
        raise BlobError("resolved URL expiry is not after the current time")
    entries: dict[str, str] = {}
    if deleg_until is None or until < deleg_until:
        entries[_Q_VALID_UNTIL] = str(until)
    if operation == "put":
        if content_types is not None:
            entries[_Q_CONTENT_TYPES] = ",".join(sorted(content_types))
        if max_size is not None:
            entries[_Q_MAX_SIZE] = str(int(max_size))
        if add_random_suffix is not None:
            entries[_Q_RANDOM_SUFFIX] = "true" if add_random_suffix else "false"
        if allow_overwrite is not None:
            entries[_Q_OVERWRITE] = "true" if allow_overwrite else "false"
    signature = _b64url(hmac.new(signing_token.encode(), _canonical(operation, pathname, entries).encode(),
                                 hashlib.sha256).digest())
    return {"delegationToken": delegation_token, "signature": signature, "params": entries}


def add_presigned_params(url: str, payload: dict[str, Any], extra: dict[str, str] | None = None) -> str:
    q = {**payload["params"], **(extra or {}), "vercel-blob-delegation": payload["delegationToken"],
         "vercel-blob-signature": payload["signature"]}
    return f"{url}{'&' if '?' in url else '?'}{urlencode(q)}"


def _check_pathname(pathname: str) -> None:
    if not pathname or len(pathname) > MAX_PATHNAME or "//" in pathname or pathname.startswith("/"):
        raise BlobError("invalid blob pathname")


# ----- production backend ------------------------------------------------------------------------
class VercelBlob:
    """Private-store backend. `token` is the store's read-write token (`BLOB_READ_WRITE_TOKEN`)."""

    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        self._token = token
        parts = token.split("_")           # vercel_blob_rw_<storeId>_<secret>
        self.store_id = parts[3] if len(parts) > 3 else ""
        if not self.store_id:
            raise BlobError("BLOB_READ_WRITE_TOKEN is not a valid read-write token")
        self._http = http or httpx.AsyncClient(timeout=20)

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}", "x-vercel-blob-store-id": self.store_id,
                "x-api-version": API_VERSION}

    async def _delegation(self, pathname: str, operation: str, ttl: int, **limits: Any) -> dict[str, Any]:
        body = {"pathname": pathname, "operations": [operation],
                "validUntil": int((time.time() + ttl) * 1000), **limits}
        r = await self._http.post(f"{API_URL}/signed-token", json=body, headers=self._headers())
        if r.status_code >= 300:
            raise BlobError(f"signed-token request failed: HTTP {r.status_code}")
        return r.json()

    async def presign_put(self, pathname, size, content_type, ttl):
        _check_pathname(pathname)
        d = await self._delegation(pathname, "put", ttl, maximumSizeInBytes=size, allowedContentTypes=[content_type])
        p = presign(d["delegationToken"], d["clientSigningToken"], operation="put", pathname=pathname,
                    valid_until_ms=int((time.time() + ttl) * 1000), max_size=size, content_types=[content_type],
                    add_random_suffix=False, allow_overwrite=False)
        url = add_presigned_params(f"{API_URL}/?{urlencode({'pathname': pathname})}", p)
        return {"url": url, "headers": {
            "x-vercel-blob-store-id": store_id_of(d["delegationToken"]), "x-api-version": API_VERSION,
            "x-vercel-blob-access": "private", "x-content-type": content_type,
            "x-add-random-suffix": "0", "x-allow-overwrite": "0", "content-type": content_type}}

    async def presign_get(self, pathname, ttl):
        _check_pathname(pathname)
        d = await self._delegation(pathname, "get", ttl)
        p = presign(d["delegationToken"], d["clientSigningToken"], operation="get", pathname=pathname,
                    valid_until_ms=int((time.time() + ttl) * 1000))
        base = f"https://{store_id_of(d['delegationToken'])}.private.blob.vercel-storage.com/{quote(pathname)}"
        return add_presigned_params(base, p, {"download": "1"})   # download=1 -> Content-Disposition: attachment

    async def head(self, pathname):
        _check_pathname(pathname)
        url = f"https://{self.store_id}.private.blob.vercel-storage.com/{pathname}"
        r = await self._http.get(f"{API_URL}/?{urlencode({'url': url})}", headers=self._headers())
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            raise BlobError(f"head failed: HTTP {r.status_code}")
        j = r.json()
        return {"size": int(j["size"]), "contentType": j.get("contentType", "")}

    async def delete(self, pathname):
        _check_pathname(pathname)
        url = f"https://{self.store_id}.private.blob.vercel-storage.com/{pathname}"
        r = await self._http.post(f"{API_URL}/delete", json={"urls": [url]},
                                  headers={**self._headers(), "content-type": "application/json"})
        if r.status_code >= 300 and r.status_code != 404:
            raise BlobError(f"delete failed: HTTP {r.status_code}")


# ----- test double ---------------------------------------------------------------------------------
class FakeBlob:
    """In-memory backend. `uploads` holds what the 'agent' PUT; the URLs are opaque fakes."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.put_limits: dict[str, dict[str, Any]] = {}

    async def presign_put(self, pathname, size, content_type, ttl):
        _check_pathname(pathname)
        self.put_limits[pathname] = {"size": size, "content_type": content_type, "ttl": ttl}
        return {"url": f"https://blob.fake/put/{pathname}", "headers": {"x-content-type": content_type}}

    def upload(self, pathname: str, data: bytes) -> None:
        lim = self.put_limits[pathname]
        assert len(data) <= lim["size"], "presigned maximum size exceeded"
        self.objects[pathname] = data

    async def head(self, pathname):
        o = self.objects.get(pathname)
        return None if o is None else {"size": len(o), "contentType": self.put_limits[pathname]["content_type"]}

    async def presign_get(self, pathname, ttl):
        return f"https://blob.fake/get/{pathname}?ttl={ttl}"

    async def delete(self, pathname):
        self.objects.pop(pathname, None)
        self.deleted.append(pathname)
