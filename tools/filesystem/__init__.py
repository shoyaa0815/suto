"""Filesystem tool module exposing schemas and operations."""

from typing import Any

from .operations import FilesystemOperations

_default_fs = FilesystemOperations()


def read(path: str, offset: int = 0, limit: int | None = None) -> str:
    """Read contents of a file."""
    return _default_fs.read(path, offset=offset, limit=limit)


def write(path: str, content: str, append: bool = False) -> str:
    """Write or append content to a file."""
    return _default_fs.write(path, content=content, append=append)


def list_files(
    path: str = ".",
    recursive: bool = False,
    max_entries: int = 100,
) -> str:
    """List entries in a directory."""
    return _default_fs.list(path=path, recursive=recursive, max_entries=max_entries)


file_read = read
file_write = write
file_list = list_files

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file.read",
        "description": "Read file contents with optional offset and line/character limit.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Byte offset to start reading from.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of characters to read.",
                },
            },
            "required": ["path"],
        },
    },
}

READ_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_read",
        "description": "Read file contents with optional offset and line/character limit.",
        "parameters": READ_SCHEMA["function"]["parameters"],
    },
}

WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file.write",
        "description": "Write or append text content to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the destination file.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write.",
                },
                "append": {
                    "type": "boolean",
                    "description": "Whether to append to the file instead of overwriting.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

WRITE_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_write",
        "description": "Write or append text content to a file.",
        "parameters": WRITE_SCHEMA["function"]["parameters"],
    },
}

LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file.list",
        "description": "List files and directories within a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (defaults to current directory).",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to list recursively.",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (default 100).",
                },
            },
        },
    },
}

LIST_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_list",
        "description": "List files and directories within a directory.",
        "parameters": LIST_SCHEMA["function"]["parameters"],
    },
}

PROMPT = (
    "Use file.read (or file_read) to view files, file.write (or file_write) to create or edit files, "
    "and file.list (or file_list) to explore directory contents."
)

FILESYSTEM_SCHEMAS = [
    READ_SCHEMA,
    READ_ALIAS_SCHEMA,
    WRITE_SCHEMA,
    WRITE_ALIAS_SCHEMA,
    LIST_SCHEMA,
    LIST_ALIAS_SCHEMA,
]

FILESYSTEM_TOOLS: dict[str, Any] = {
    "file.read": read,
    "file_read": read,
    "file.write": write,
    "file_write": write,
    "file.list": list_files,
    "file_list": list_files,
}

__all__ = [
    "FILESYSTEM_SCHEMAS",
    "FILESYSTEM_TOOLS",
    "FilesystemOperations",
    "LIST_ALIAS_SCHEMA",
    "LIST_SCHEMA",
    "PROMPT",
    "READ_ALIAS_SCHEMA",
    "READ_SCHEMA",
    "WRITE_ALIAS_SCHEMA",
    "WRITE_SCHEMA",
    "file_list",
    "file_read",
    "file_write",
    "list_files",
    "read",
    "write",
]
