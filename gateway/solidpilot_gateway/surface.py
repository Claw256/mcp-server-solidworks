"""The MCP server Claude talks to. It owns no tools: the lists come from the catalogue the PC agent
pushed (so they work while the PC is off), and every call is forwarded to the PC through the relay."""

import asyncio
import base64
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp_types import (
    CallToolResult,
    GetPromptResult,
    Prompt,
    Resource,
    ResourceTemplate,
    Tool,
)

from .deliveries import Deliveries
from .relay import AgentError, AgentOffline, AgentRelay

DELIVERY_TOOL = "deliver_document"
SLOT_ARG = "delivery_slot"   # injected by the gateway; hidden from the model's schema

OFFLINE_MESSAGE = (
    "AGENT_OFFLINE: the SolidWorks PC is not connected to the gateway. Ask the user to check that the PC is on "
    "and the SolidPilot agent is running, then retry."
)


class RelayServer(MCPServer):
    """MCPServer whose tool/resource/prompt surface is whatever the PC agent last reported."""

    def __init__(self, relay: AgentRelay, deliveries: Deliveries | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._relay = relay
        self._deliveries = deliveries
        self._schemas: dict[str, Any] = {}

    # Argument validation is the agent's job (it holds the real schemas). The SDK only uses this
    # hook for optional header validation, and treats "unknown" as "skip".
    def _tool_input_schema(self, name: str) -> dict[str, Any] | None:
        return self._schemas.get(name)

    async def list_tools(self) -> list[Tool]:
        tools = (await self._relay.catalog()).get("tools", [])
        self._schemas = {t["name"]: t.get("inputSchema") for t in tools}
        return [Tool.model_validate(self._hide_slot(t)) for t in tools]

    @staticmethod
    def _hide_slot(tool: dict[str, Any]) -> dict[str, Any]:
        """The delivery slot is the gateway's business; the model must neither see nor choose it."""
        if tool.get("name") != DELIVERY_TOOL:
            return tool
        schema = dict(tool.get("inputSchema") or {})
        schema["properties"] = {k: v for k, v in (schema.get("properties") or {}).items() if k != SLOT_ARG}
        if "required" in schema:
            schema["required"] = [k for k in schema["required"] if k != SLOT_ARG]
        return {**tool, "inputSchema": schema}

    async def call_tool(self, name, arguments, context=None):  # type: ignore[override]
        if name == DELIVERY_TOOL:
            if self._deliveries is None:
                raise ToolError("DELIVERY_UNAVAILABLE: file delivery is not configured on this gateway "
                                "(BLOB_READ_WRITE_TOKEN of a private Vercel Blob store is missing).")
            access = get_access_token()
            if access is None:
                raise ToolError("UNAUTHORIZED: no authenticated session for this call.")
            # Bind the delivery to THIS session; any model-supplied slot is discarded.
            arguments = {**(arguments or {}), SLOT_ARG: await self._deliveries.open_slot(access.client_id)}
        try:
            result = await self._relay.request("tools/call", {"name": name, "arguments": arguments})
        except AgentOffline as e:
            raise ToolError(OFFLINE_MESSAGE) from e
        except asyncio.TimeoutError as e:
            raise ToolError(f"TIMEOUT: the PC agent did not answer {name} within the gateway's call timeout.") from e
        except AgentError as e:
            raise ToolError(f"{e.code}: {e}") from e
        return CallToolResult.model_validate(result)

    async def list_resources(self) -> list[Resource]:
        return [Resource.model_validate(r) for r in (await self._relay.catalog()).get("resources", [])]

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        return [ResourceTemplate.model_validate(r) for r in (await self._relay.catalog()).get("templates", [])]

    async def read_resource(self, uri, context=None):  # type: ignore[override]
        try:
            result = await self._relay.request("resources/read", {"uri": str(uri)})
        except AgentOffline as e:
            raise ResourceError(OFFLINE_MESSAGE) from e
        except (AgentError, asyncio.TimeoutError) as e:
            raise ResourceError(str(e) or "resource read failed") from e
        out = []
        for item in result.get("contents", []):
            content: str | bytes = base64.b64decode(item["blob"]) if "blob" in item else item.get("text", "")
            out.append(ReadResourceContents(content=content, mime_type=item.get("mimeType")))
        return out

    async def list_prompts(self) -> list[Prompt]:
        return [Prompt.model_validate(p) for p in (await self._relay.catalog()).get("prompts", [])]

    async def get_prompt(self, name, arguments=None, context=None):  # type: ignore[override]
        try:
            result = await self._relay.request("prompts/get", {"name": name, "arguments": arguments or {}})
        except AgentOffline as e:
            raise ValueError(OFFLINE_MESSAGE) from e
        except (AgentError, asyncio.TimeoutError) as e:
            raise ValueError(str(e) or "prompt failed") from e
        return GetPromptResult.model_validate(result)


__all__ = ["RelayServer", "OFFLINE_MESSAGE"]
