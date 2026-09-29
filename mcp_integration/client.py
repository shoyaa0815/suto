"""The stdio transport and MCP SDK stay behind this client boundary."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import MCPServerConfig


# The SDK includes malformed server stdout in exception logs. That output can
# contain environment-derived secrets, so transport errors stay inside our
# sanitized connection/tool error boundary.
logging.getLogger("mcp.client.stdio").disabled = True


class StdioMCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        try:
            async with asyncio.timeout(15):
                # Server stderr can contain configuration or private data.
                errlog = self._stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=self.config.resolved_env(),
                )
                read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
                self._session = await self._stack.enter_async_context(ClientSession(read, write))
                initialized = await self._session.initialize()
                if initialized.capabilities.tools is None:
                    raise ValueError("MCP server does not advertise tools")
        except BaseException:
            await self.close()
            raise

    async def list_tools(self) -> list[Any]:
        if self._session is None:
            raise RuntimeError("MCP server is not connected")
        found = []
        cursor = None
        seen = set()
        async with asyncio.timeout(15):
            while True:
                page = await self._session.list_tools(cursor=cursor)
                found.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    return found
                if cursor in seen or len(seen) >= 100:
                    raise ValueError("invalid MCP tool pagination")
                seen.add(cursor)

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("MCP server is not connected")
        return await self._session.call_tool(name, arguments=args, read_timeout_seconds=timedelta(seconds=120))

    async def close(self) -> None:
        self._session = None
        await self._stack.aclose()
