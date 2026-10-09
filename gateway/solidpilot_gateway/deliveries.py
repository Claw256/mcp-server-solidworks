"""File deliveries: the PC agent uploads a finished document straight to private Blob storage and the gateway
hands the chat a one-time download link.

Flow (all state in Redis, so any serverless instance can serve any step):

  1. open_slot()        a `deliver_document` tool call arrives; a slot bound to the caller's OAuth client is
                        created and its id injected into the job (the model never chooses it).
  2. request_upload()   agent (agent token) asks for a presigned PUT for exactly `size` bytes -> one per slot.
  3. complete()         agent says it is done; the gateway HEADs the blob, checks the size and mints the
                        download token. Only a SHA-256 digest of the token is stored.
  4. redeem()           GET /dl/<token>: atomic get-and-delete (single use) -> 60 s presigned GET.

Abandoned or used blobs are listed in a sorted set by expiry and deleted by sweep().
"""

import re
import secrets
import time
from typing import Any

from .blobstore import BlobBackend, BlobError
from .store import Store, digest

SLOT_TTL = 900               # an upload must start and finish within 15 minutes of the tool call
UPLOAD_URL_TTL = 600         # presigned PUT lifetime
GET_URL_TTL = 60             # presigned GET lifetime after the link is redeemed
SWEEP_GRACE = 120            # keep a redeemed blob this long so the redirect can finish
INDEX = "sp:deliveries:index"

# Extensions that may be delivered, with the content type the upload is pinned to.
CONTENT_TYPES = {
    ".sldprt": "application/octet-stream", ".sldasm": "application/octet-stream",
    ".slddrw": "application/octet-stream", ".step": "model/step", ".stp": "model/step",
    ".iges": "model/iges", ".igs": "model/iges", ".stl": "model/stl", ".pdf": "application/pdf",
    ".dxf": "application/dxf", ".dwg": "application/acad", ".png": "image/png",
}


class DeliveryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def safe_filename(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", name.replace("\\", "/").rsplit("/", 1)[-1]).strip(". ")
    if not base or "." not in base:
        raise DeliveryError("INVALID_FILENAME", "filename is empty or has no extension")
    return base[:120]


class Deliveries:
    def __init__(self, store: Store, blob: BlobBackend, public_url: str, ttl: int, max_bytes: int):
        self.store, self.blob, self.public_url, self.ttl, self.max_bytes = store, blob, public_url, ttl, max_bytes

    @property
    def redis(self):
        return self.store.redis

    # ----- 1. tool call ----------------------------------------------------------------------------
    async def open_slot(self, client_id: str) -> str:
        await self.sweep()   # best effort housekeeping on every new delivery
        slot = secrets.token_hex(16)
        await self.store.put("slot", slot, {"client_id": client_id, "state": "open"}, ttl=SLOT_TTL)
        return slot

    # ----- 2. presigned upload -----------------------------------------------------------------------
    async def request_upload(self, slot: str, filename: str, size: int) -> dict[str, Any]:
        rec = await self.store.get("slot", slot) if re.fullmatch(r"[0-9a-f]{32}", slot or "") else None
        if rec is None or rec.get("state") != "open":
            raise DeliveryError("INVALID_SLOT", "unknown, expired or already used delivery slot")
        name = safe_filename(filename)
        ext = "." + name.rsplit(".", 1)[-1].lower()
        if ext not in CONTENT_TYPES:
            raise DeliveryError("UNSUPPORTED_TYPE", f"{ext} files cannot be delivered")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= self.max_bytes:
            raise DeliveryError("FILE_TOO_LARGE", f"size must be 1..{self.max_bytes} bytes")
        # one upload per slot, race-safe across instances
        if not await self.redis.set(self.store._k("slotuse", slot), "1", nx=True, ex=SLOT_TTL):
            raise DeliveryError("INVALID_SLOT", "unknown, expired or already used delivery slot")
        pathname = f"deliveries/{slot}/{name}"
        ctype = CONTENT_TYPES[ext]
        try:
            put = await self.blob.presign_put(pathname, size, ctype, UPLOAD_URL_TTL)
        except BlobError as e:
            raise DeliveryError("STORAGE_ERROR", str(e)) from e
        await self.store.put("slot", slot, {**rec, "state": "uploading", "pathname": pathname, "filename": name,
                                            "size": size}, ttl=SLOT_TTL)
        await self.redis.zadd(INDEX, {pathname: time.time() + SLOT_TTL})   # cleaned up if never completed
        return {"url": put["url"], "headers": put["headers"], "method": "PUT", "pathname": pathname}

    # ----- 3. done -------------------------------------------------------------------------------------
    async def complete(self, slot: str, size: int) -> dict[str, Any]:
        rec = await self.store.pop("slot", slot) if re.fullmatch(r"[0-9a-f]{32}", slot or "") else None
        if rec is None or rec.get("state") != "uploading":
            raise DeliveryError("INVALID_SLOT", "unknown, expired or already completed delivery slot")
        try:
            info = await self.blob.head(rec["pathname"])
        except BlobError as e:
            raise DeliveryError("STORAGE_ERROR", str(e)) from e
        if info is None:
            raise DeliveryError("UPLOAD_MISSING", "the file was not found in storage; upload it first")
        if info["size"] != rec["size"] or size != rec["size"]:
            await self._drop(rec["pathname"])
            raise DeliveryError("SIZE_MISMATCH", "uploaded size does not match the announced size")
        token = secrets.token_urlsafe(32)
        expires = time.time() + self.ttl
        await self.store.put("dl", digest(token), {
            "pathname": rec["pathname"], "filename": rec["filename"], "size": rec["size"],
            "client_id": rec["client_id"], "expires": expires}, ttl=self.ttl)
        await self.redis.zadd(INDEX, {rec["pathname"]: expires})
        return {"download_url": f"{self.public_url}/dl/{token}", "filename": rec["filename"], "size": rec["size"],
                "expires_in": self.ttl}

    # ----- 4. the link ---------------------------------------------------------------------------------
    async def redeem(self, token: str) -> str | None:
        """-> a 60 s presigned GET URL, or None. The token dies on first use."""
        rec = await self.store.pop("dl", digest(token)) if 20 <= len(token) <= 100 else None
        if rec is None or rec["expires"] < time.time():
            return None
        await self.redis.zadd(INDEX, {rec["pathname"]: time.time() + SWEEP_GRACE})
        try:
            return await self.blob.presign_get(rec["pathname"], GET_URL_TTL)
        except BlobError:
            return None

    # ----- housekeeping -------------------------------------------------------------------------------
    async def _drop(self, pathname: str) -> None:
        try:
            await self.blob.delete(pathname)
        except BlobError:
            return          # leave it in the index; the next sweep retries
        await self.redis.zrem(INDEX, pathname)

    async def sweep(self) -> int:
        due = await self.redis.zrangebyscore(INDEX, "-inf", time.time())
        for pathname in due:
            await self._drop(pathname)
        return len(due)
