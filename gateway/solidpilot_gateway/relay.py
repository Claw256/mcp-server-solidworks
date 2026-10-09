"""Relay between the MCP-facing server and the one PC agent, over a Redis job queue.

Vercel functions are stateless and can run as many instances, so nothing lives in process memory.
The PC agent only ever makes OUTBOUND HTTPS calls:

  POST /agent/catalog   agent -> gateway   the tool/resource/prompt lists (pushed on start and hourly)
  POST /agent/poll      agent -> gateway   long-poll (<= ~25 s): 200 {"job": {...}} or 204 No Content
  POST /agent/result    agent -> gateway   {"id": ..., "result": {...}} or {"id": ..., "error": {...}}

A job is {"id", "method", "params"} with method one of tools/call, resources/read, prompts/get.
Results are the JSON (by-alias) form of the matching MCP result types. Presence ("is the PC there?")
is a Redis key the poll refreshes, so a PC that stops polling reads as offline within ~60 s.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("gateway.relay")

PROTOCOL = 1
QUEUE = "sp:relay:jobs"
PRESENCE = "sp:relay:agent"
CATALOG = "sp:relay:catalog"
PRESENCE_TTL = 60
RESULT_TTL = 300
SLICE = 10   # seconds per BLPOP slice while a caller waits, so a vanished PC is noticed promptly


class AgentOffline(Exception):
    """No agent is polling (or it vanished mid-call)."""


class AgentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AgentRelay:
    def __init__(self, redis: "aioredis.Redis", call_timeout: float):
        self.redis = redis
        self.call_timeout = call_timeout

    # ----- presence + catalogue ---------------------------------------------------------------
    async def touch(self, agent_version: str = "") -> None:
        await self.redis.set(PRESENCE, agent_version or "1", ex=PRESENCE_TTL)

    async def connected(self) -> bool:
        return bool(await self.redis.exists(PRESENCE))

    async def set_catalog(self, catalog: dict[str, Any]) -> None:
        await self.redis.set(CATALOG, json.dumps(catalog))

    async def catalog(self) -> dict[str, Any]:
        raw = await self.redis.get(CATALOG)
        return json.loads(raw) if raw else {}

    # ----- agent side -------------------------------------------------------------------------
    async def next_job(self, wait: int, agent_version: str = "") -> dict[str, Any] | None:
        """Hold the agent's poll open until a job arrives (or `wait` seconds pass)."""
        await self.touch(agent_version)
        item = await self.redis.blpop([QUEUE], timeout=wait)
        await self.touch(agent_version)
        return json.loads(item[1]) if item else None

    async def submit_result(self, frame: dict[str, Any]) -> None:
        key = f"sp:relay:res:{frame['id']}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lpush(key, json.dumps(frame))
            pipe.expire(key, RESULT_TTL)
            await pipe.execute()

    # ----- MCP side ---------------------------------------------------------------------------
    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if not await self.connected():
            raise AgentOffline("The SolidWorks PC agent is not connected.")
        job = json.dumps({"id": uuid.uuid4().hex, "method": method, "params": params})
        job_id = json.loads(job)["id"]
        res_key = f"sp:relay:res:{job_id}"
        await self.redis.rpush(QUEUE, job)  # FIFO with BLPOP on the head
        deadline = asyncio.get_running_loop().time() + (timeout or self.call_timeout)
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                item = await self.redis.blpop([res_key], timeout=max(1, int(min(SLICE, remaining))))
                if item:
                    frame = json.loads(item[1])
                    break
                if not await self.connected():
                    raise AgentOffline("The SolidWorks PC agent disconnected.")
        except BaseException:
            await self.redis.lrem(QUEUE, 1, job)  # withdraw it if the PC never picked it up
            raise
        finally:
            await self.redis.delete(res_key)
        if "error" in frame:
            err = frame["error"] or {}
            raise AgentError(str(err.get("code", "AGENT_ERROR")), str(err.get("message", "agent error")))
        return frame.get("result")
