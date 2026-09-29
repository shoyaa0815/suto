"""Connect configured servers, discover tools, and own their lifetime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .adapter import MCPTool, adapt_tool
from .client import StdioMCPClient
from .config import MCPConfig, MCPServerConfig


class MCPManager:
    def __init__(
        self,
        config: MCPConfig,
        client_factory: Callable[[MCPServerConfig], Any] = StdioMCPClient,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.tools: dict[str, MCPTool] = {}
        self.failures: dict[str, str] = {}
        self._clients: list[Any] = []

    async def __aenter__(self) -> "MCPManager":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        try:
            for server in self.config.servers:
                if not server.allow_tools:
                    continue
                client = None
                try:
                    client = self.client_factory(server)
                    await client.connect()
                    definitions = await client.list_tools()
                    discovered: dict[str, MCPTool] = {}
                    for definition in definitions:
                        tool = adapt_tool(server.name, definition, client)
                        if tool.name in discovered or tool.name in self.tools:
                            raise ValueError("duplicate MCP tool name")
                        discovered[tool.name] = tool
                    self.tools.update(discovered)
                    self._clients.append(client)
                except Exception as error:
                    self.failures[server.name] = type(error).__name__
                    if client is not None:
                        try:
                            await client.close()
                        except Exception:
                            self.failures[server.name] = "MCP cleanup failed"
                except BaseException:
                    try:
                        if client is not None:
                            await client.close()
                    finally:
                        raise
        except BaseException:
            await self.close()
            raise

    def allowed_tools(self) -> dict[str, MCPTool]:
        allowed = {
            f"mcp.{server.name}.{name}"
            for server in self.config.servers for name in server.allow_tools
        }
        return {name: tool for name, tool in self.tools.items() if name in allowed}

    async def close(self) -> None:
        clients, self._clients = self._clients, []
        self.tools.clear()
        for client in reversed(clients):
            try:
                await client.close()
            except Exception:
                self.failures[client.config.name] = "MCP cleanup failed"
