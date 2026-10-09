"""Outbound relay agent: lets a remote gateway drive this PC's SolidWorks tools.

Runs the SAME tool surface as `server.py` (it imports its `mcp` object), but instead of speaking
stdio to a local host it makes only OUTBOUND HTTPS calls to the gateway, authenticating with an OAuth
`client_credentials` token: it pushes its tool catalogue, then long-polls for jobs and posts results
back (protocol documented in gateway/solidpilot_gateway/relay.py). Nothing listens on this machine; the
C# execution layer is still reached over http://localhost:5000 exactly as before.

    python agent.py          # needs GATEWAY_URL, AGENT_CLIENT_ID, AGENT_CLIENT_SECRET (see .env)
"""
import asyncio
import base64
import json
import mimetypes
import os
import random
import re
import sys
import time
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.shared.exceptions import MCPError
from mcp_types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent

import adapter_log
import config  # noqa: F401  (loads adapters/claude/.env)
import server as sw_server

PROTOCOL = 1
AGENT_VERSION = "2"
POLL_WAIT = 25                      # seconds the gateway holds a poll open
CATALOG_REFRESH = 3600.0            # re-push the tool catalogue this often

GATEWAY_URL = os.getenv("GATEWAY_URL", "").rstrip("/")
CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AGENT_CLIENT_SECRET", "")
# Largest file the agent will accept from, or hand back to, the phone.
MAX_RESULT_BYTES = 4 * 1024 * 1024     # stay under the gateway host's 4.5 MB request-body cap
MAX_FILE_BYTES = int(os.getenv("AGENT_MAX_FILE_MB", "20")) * 1024 * 1024
STAGING_DIR = os.path.abspath(os.getenv("AGENT_STAGING_DIR", os.path.join(os.path.expanduser("~"), "SolidPilotStaging")))
STAGING_TTL = float(os.getenv("AGENT_STAGING_TTL_HOURS", "72")) * 3600
_EXTRA_ROOTS = [os.path.abspath(p) for p in os.getenv("AGENT_FILE_ROOTS", "").split(os.pathsep) if p.strip()]
ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".dxf", ".dwg", ".sldprt", ".sldasm",
                ".slddrw", ".step", ".stp", ".iges", ".igs", ".x_t", ".stl", ".json"}

mcp = sw_server.mcp
_call_lock: asyncio.Lock | None = None   # one CAD call at a time: SolidWorks COM is single-threaded


def log(msg: str) -> None:
    adapter_log.write(f"[agent] {msg}")
    print(f"[agent] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------
# File staging: the phone cannot reach this disk, so it hands files over as base64.
# ---------------------------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(name.replace("\\", "/"))).strip(". ")
    if not base:
        raise ToolError("INVALID_FILENAME: filename is empty after sanitising")
    return base


def _inside_allowed_root(path: str) -> bool:
    real = os.path.realpath(path)
    for root in [STAGING_DIR, *_EXTRA_ROOTS]:
        root_real = os.path.realpath(root)
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    return False


def _sweep_staging() -> None:
    cutoff = time.time() - STAGING_TTL
    try:
        for entry in os.scandir(STAGING_DIR):
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
    except OSError:
        pass


@mcp.tool(structured_output=False)
def stage_file(filename: str, content_base64: str) -> str:
    """Put a file (PDF, image, DXF, CAD file) on the SolidWorks PC so other tools can open it.

    Use this when the user attaches a drawing or model from a device that is not the PC. Returns the
    absolute path on the PC to pass as `file_path` to open_document, prepare_drawing, analyze_drawing, etc.
    Also write exports you want to send back into the staging folder, then fetch them with get_file.
    """
    name = _safe_name(filename)
    if os.path.splitext(name)[1].lower() not in ALLOWED_EXTS:
        raise ToolError(f"UNSUPPORTED_TYPE: {os.path.splitext(name)[1] or name!r} is not an accepted file type")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except ValueError as e:
        raise ToolError("INVALID_BASE64: content_base64 is not valid base64") from e
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"FILE_TOO_LARGE: {len(data)} bytes exceeds the {MAX_FILE_BYTES} byte limit")
    os.makedirs(STAGING_DIR, exist_ok=True)
    _sweep_staging()
    path = os.path.join(STAGING_DIR, name)
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(path):   # never overwrite an earlier upload
        path = os.path.join(STAGING_DIR, f"{stem}-{n}{ext}")
        n += 1
    with open(path, "wb") as fh:
        fh.write(data)
    return f"Staged {len(data)} bytes at {path}"


@mcp.tool(structured_output=False)
def get_file(file_path: str) -> list:
    """Fetch a file from the SolidWorks PC's staging folder (for example an exported PDF, STEP or image).

    Only files inside the staging folder (plus any folders the PC owner allows) can be read. Export
    there first with export_document / export_image, then call this to receive the bytes.
    """
    path = os.path.abspath(file_path)
    if not _inside_allowed_root(path):
        raise ToolError(f"FORBIDDEN_PATH: only files under {STAGING_DIR} can be fetched")
    if not os.path.isfile(path):
        raise ToolError(f"NOT_FOUND: {path}")
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ToolError(f"FILE_TOO_LARGE: {size} bytes exceeds the {MAX_FILE_BYTES} byte limit")
    with open(path, "rb") as fh:
        data = fh.read()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    note = TextContent(type="text", text=f"{os.path.basename(path)} ({size} bytes, {mime})")
    if mime in ("image/png", "image/jpeg"):
        return [note, Image(data=data, format=mime.split("/")[1])]
    uri = "file:///" + os.path.basename(path).replace(" ", "%20")
    blob = BlobResourceContents(uri=uri, mime_type=mime, blob=base64.b64encode(data).decode())
    return [note, EmbeddedResource(type="resource", resource=blob)]


sw_server._normalize_tool_surface()   # re-run so the two tools above get the same schema cleanup


# ---------------------------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------------------------
def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _tools_call(params: dict[str, Any]) -> dict[str, Any]:
    assert _call_lock is not None
    name, args = params["name"], params.get("arguments") or {}
    async with _call_lock:
        try:
            result = await mcp.call_tool(name, args)
        except MCPError:
            raise
        except ToolError as exc:
            result = CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
        except Exception as exc:   # mirror the SDK: never leak internals, but keep the stack in the log
            log(f"tool {name} crashed: {exc!r}")
            result = CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
    if not isinstance(result, CallToolResult):
        raise ToolError(f"{name} needs interactive input, which the remote gateway cannot relay")
    return _dump(result)


async def _resources_read(params: dict[str, Any]) -> dict[str, Any]:
    uri = params["uri"]
    items = await mcp.read_resource(uri)
    if not isinstance(items, (list, tuple)):
        raise ToolError("resource needs interactive input, which the remote gateway cannot relay")
    contents = []
    for item in items:
        entry: dict[str, Any] = {"uri": uri}
        if item.mime_type:
            entry["mimeType"] = item.mime_type
        if isinstance(item.content, bytes):
            entry["blob"] = base64.b64encode(item.content).decode()
        else:
            entry["text"] = item.content
        contents.append(entry)
    return {"contents": contents}


async def handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "tools/list":
        return {"tools": [_dump(t) for t in await mcp.list_tools()]}
    if method == "tools/call":
        return await _tools_call(params)
    if method == "resources/list":
        return {"resources": [_dump(r) for r in await mcp.list_resources()]}
    if method == "resources/templates/list":
        return {"templates": [_dump(r) for r in await mcp.list_resource_templates()]}
    if method == "resources/read":
        return await _resources_read(params)
    if method == "prompts/list":
        return {"prompts": [_dump(p) for p in await mcp.list_prompts()]}
    if method == "prompts/get":
        result = await mcp.get_prompt(params["name"], params.get("arguments") or {})
        if not hasattr(result, "messages"):
            raise ToolError("prompt needs interactive input, which the remote gateway cannot relay")
        return _dump(result)
    raise ToolError(f"unknown method {method}")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Agent-Protocol": str(PROTOCOL), "X-Agent-Version": AGENT_VERSION}


class Gateway:
    """Authenticated HTTPS client for the gateway's /agent endpoints; refreshes its token as needed."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._token = ""
        self._expires = 0.0

    async def token(self, force: bool = False) -> str:
        if force or not self._token or time.monotonic() > self._expires - 60:
            resp = await self.client.post(
                f"{GATEWAY_URL}/agent/token",
                data={"grant_type": "client_credentials", "resource": f"{GATEWAY_URL}/agent", "scope": "agent"},
                auth=(CLIENT_ID, CLIENT_SECRET), timeout=20)
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._expires = time.monotonic() + float(body.get("expires_in", 900))
        return self._token

    async def post(self, path: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
        """POST with the bearer token; one retry with a fresh token if the gateway says it expired."""
        for attempt in (0, 1):
            resp = await self.client.post(f"{GATEWAY_URL}{path}", json=payload, headers=_headers(await self.token(attempt == 1)),
                                          timeout=timeout)
            if resp.status_code != 401 or attempt == 1:
                return resp
        raise AssertionError("unreachable")


async def build_catalog() -> dict[str, Any]:
    return {
        "tools": [_dump(t) for t in await mcp.list_tools()],
        "resources": [_dump(r) for r in await mcp.list_resources()],
        "templates": [_dump(r) for r in await mcp.list_resource_templates()],
        "prompts": [_dump(p) for p in await mcp.list_prompts()],
    }


async def _respond(gw: Gateway, job: dict[str, Any]) -> None:
    out: dict[str, Any] = {"id": job["id"]}
    try:
        out["result"] = await handle(job["method"], job.get("params") or {})
    except Exception as exc:
        out["error"] = {"code": type(exc).__name__, "message": str(exc)}
    if len(json.dumps(out)) > MAX_RESULT_BYTES:   # the gateway's host caps request bodies at ~4.5 MB
        out = {"id": job["id"], "error": {
            "code": "RESULT_TOO_LARGE", "message": "result exceeds the relay size limit; export a smaller view"}}
    for attempt in range(3):
        try:
            resp = await gw.post("/agent/result", out, timeout=30)
            if resp.status_code < 300:
                return
            log(f"result {job['id']} rejected: HTTP {resp.status_code}")
            if resp.status_code < 500:
                return
        except httpx.HTTPError as exc:
            log(f"result {job['id']} not delivered (attempt {attempt + 1}): {exc!r}")
        await asyncio.sleep(1 + attempt)


async def poll_forever(gw: Gateway) -> None:
    """Push the catalogue, then long-poll. Returns only by raising, so main() can back off and retry."""
    resp = await gw.post("/agent/catalog", await build_catalog(), timeout=60)
    resp.raise_for_status()
    log("connected to gateway; catalogue pushed")
    last_catalog = time.monotonic()
    tasks: set[asyncio.Task] = set()
    try:
        while True:
            resp = await gw.post("/agent/poll", {"wait": POLL_WAIT}, timeout=POLL_WAIT + 15)
            if resp.status_code == 426:
                raise SystemExit("Gateway speaks a different agent protocol; update agent.py.")
            resp.raise_for_status()
            if resp.status_code == 200:
                task = asyncio.create_task(_respond(gw, resp.json()["job"]))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if time.monotonic() - last_catalog > CATALOG_REFRESH:
                (await gw.post("/agent/catalog", await build_catalog(), timeout=60)).raise_for_status()
                last_catalog = time.monotonic()
    finally:
        for task in tasks:
            task.cancel()


async def main() -> None:
    global _call_lock
    if not (GATEWAY_URL and CLIENT_ID and CLIENT_SECRET):
        raise SystemExit("Set GATEWAY_URL, AGENT_CLIENT_ID and AGENT_CLIENT_SECRET (adapters/claude/.env).")
    _call_lock = asyncio.Lock()
    delay = 1.0
    async with httpx.AsyncClient() as client:
        gw = Gateway(client)
        while True:
            started = time.monotonic()
            try:
                await poll_forever(gw)
            except (OSError, httpx.HTTPError) as exc:
                log(f"gateway connection failed: {exc!r}")
                gw._token = ""   # re-authenticate from scratch after any failure
            if time.monotonic() - started > 60:   # a healthy stretch resets the backoff
                delay = 1.0
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 60.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
