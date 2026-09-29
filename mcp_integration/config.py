"""Separate MCP configuration; secret values are resolved only at connection time."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml


MAX_MCP_CONFIG_BYTES = 32_768
_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_TOOL = re.compile(r"[a-z][a-z0-9_.-]*\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class _UniqueKeysLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueKeysLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError("duplicate MCP configuration key")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str = field(repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    allow_tools: frozenset[str] = frozenset()

    def resolved_env(self) -> dict[str, str]:
        missing = [source for source in self.env.values() if source not in os.environ]
        if missing:
            raise ValueError("MCP server environment variable is unavailable")
        return {target: os.environ[source] for target, source in self.env.items()}


@dataclass(frozen=True)
class MCPConfig:
    servers: tuple[MCPServerConfig, ...] = ()


def parse_mcp_config(raw: object) -> MCPConfig:
    if raw is None:
        return MCPConfig()
    if not isinstance(raw, dict) or set(raw) != {"servers"}:
        raise ValueError("MCP config requires a servers mapping")
    servers = raw["servers"]
    if not isinstance(servers, dict):
        raise ValueError("MCP servers must be a mapping")
    parsed = []
    for name, value in servers.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("invalid MCP server name")
        if not isinstance(value, dict) or set(value) - {"transport", "command", "args", "env", "allow_tools"}:
            raise ValueError(f"invalid MCP server configuration: {name}")
        if value.get("transport") != "stdio":
            raise ValueError(f"MCP server {name} requires stdio transport")
        command = value.get("command")
        args = value.get("args", [])
        env = value.get("env", {})
        allowed = value.get("allow_tools", [])
        if (
            not isinstance(command, str) or not command.strip()
            or "\x00" in command or not Path(command).is_absolute()
        ):
            raise ValueError(f"invalid MCP command for {name}")
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise ValueError(f"invalid MCP arguments for {name}")
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not _ENV.fullmatch(key)
            or not isinstance(source, str) or not _ENV.fullmatch(source)
            for key, source in env.items()
        ):
            raise ValueError(f"invalid MCP environment references for {name}")
        if not isinstance(allowed, list) or any(
            not isinstance(tool, str) or not _TOOL.fullmatch(tool) for tool in allowed
        ) or len(set(allowed)) != len(allowed):
            raise ValueError(f"invalid MCP allow_tools for {name}")
        parsed.append(MCPServerConfig(name, command, tuple(args), dict(env), frozenset(allowed)))
    return MCPConfig(tuple(parsed))


def load_mcp_config(path: str | Path | None = None) -> MCPConfig:
    configured = path if path is not None else os.environ.get("SUTO_MCP_CONFIG")
    if configured is None or configured == "":
        return MCPConfig()
    source = Path(configured)
    if not source.is_absolute() or source.is_symlink():
        raise ValueError("MCP configuration requires an absolute, regular file path")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_MCP_CONFIG_BYTES:
                raise ValueError("invalid MCP configuration file")
            if os.name == "posix" and details.st_mode & 0o022:
                raise ValueError("MCP configuration file must not be group or world writable")
            content = stream.read(MAX_MCP_CONFIG_BYTES + 1)
        if len(content) > MAX_MCP_CONFIG_BYTES:
            raise ValueError("MCP configuration file is too large")
        raw = yaml.load(content.decode("utf-8"), Loader=_UniqueKeysLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as error:
        raise ValueError("cannot read MCP configuration") from error
    return parse_mcp_config(raw)
