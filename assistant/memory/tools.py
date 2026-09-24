"""Tools for saving, searching, and managing long-term memories."""

from typing import Any

from assistant.context import AssistantContext


def build_memory_tools(context: AssistantContext) -> dict[str, Any]:
    """Assemble memory tools bound to the active assistant context."""
    user_id = context.user_id
    store = context.store

    def save_memory(content: str, category: str = "general") -> str:
        try:
            item = store.save_memory(user_id, content=content, category=category)
            return f"Successfully saved to memory (ID: {item.id}, category: {item.category}): {item.content}"
        except Exception as exc:
            return f"Error saving to memory: {exc}"

    def search_memory(query: str, limit: int = 5) -> str:
        try:
            items = store.search_memories(user_id, query=query, limit=limit)
            if not items:
                return f"No memories found matching '{query}'."
            lines = [f"- [{m.category}] ({m.id}): {m.content}" for m in items]
            return "Found relevant memories:\n" + "\n".join(lines)
        except Exception as exc:
            return f"Error searching memory: {exc}"

    def list_memories(category: str | None = None) -> str:
        try:
            items = store.list_memories(user_id, category=category, limit=20)
            if not items:
                return "No saved memories found."
            lines = [f"- [{m.category}] ({m.id}): {m.content}" for m in items]
            return "Saved memories:\n" + "\n".join(lines)
        except Exception as exc:
            return f"Error listing memories: {exc}"

    def delete_memory(memory_id: str) -> str:
        try:
            deleted = store.delete_memory(user_id, memory_id)
            if deleted:
                return f"Successfully deleted memory with ID: {memory_id}"
            return f"Memory with ID '{memory_id}' not found."
        except Exception as exc:
            return f"Error deleting memory: {exc}"

    return {
        "memory.save": save_memory,
        "save_memory": save_memory,
        "memory.search": search_memory,
        "search_memory": search_memory,
        "memory.list": list_memories,
        "list_memories": list_memories,
        "memory.delete": delete_memory,
        "delete_memory": delete_memory,
    }


SAVE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory.save",
        "description": "Save important user facts, project rules, or preferences to permanent memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact or preference to remember permanently.",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category (e.g. 'preference', 'project', 'personal', 'rule').",
                },
            },
            "required": ["content"],
        },
    },
}

SAVE_MEMORY_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": "Save important user facts, project rules, or preferences to permanent memory.",
        "parameters": SAVE_MEMORY_SCHEMA["function"]["parameters"],
    },
}

SEARCH_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory.search",
        "description": "Search permanent memory for previously saved facts or preferences.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or phrase to search in memory.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_MEMORY_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "Search permanent memory for previously saved facts or preferences.",
        "parameters": SEARCH_MEMORY_SCHEMA["function"]["parameters"],
    },
}

LIST_MEMORIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory.list",
        "description": "List saved permanent memories.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional category filter.",
                },
            },
        },
    },
}

LIST_MEMORIES_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_memories",
        "description": "List saved permanent memories.",
        "parameters": LIST_MEMORIES_SCHEMA["function"]["parameters"],
    },
}

DELETE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory.delete",
        "description": "Delete a previously saved memory by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The unique ID of the memory to remove.",
                },
            },
            "required": ["memory_id"],
        },
    },
}

DELETE_MEMORY_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_memory",
        "description": "Delete a previously saved memory by its ID.",
        "parameters": DELETE_MEMORY_SCHEMA["function"]["parameters"],
    },
}

MEMORY_SCHEMAS = [
    SAVE_MEMORY_SCHEMA,
    SAVE_MEMORY_ALIAS_SCHEMA,
    SEARCH_MEMORY_SCHEMA,
    SEARCH_MEMORY_ALIAS_SCHEMA,
    LIST_MEMORIES_SCHEMA,
    LIST_MEMORIES_ALIAS_SCHEMA,
    DELETE_MEMORY_SCHEMA,
    DELETE_MEMORY_ALIAS_SCHEMA,
]

MEMORY_TOOL_NAMES = frozenset(
    {
        "memory.save",
        "save_memory",
        "memory.search",
        "search_memory",
        "memory.list",
        "list_memories",
        "memory.delete",
        "delete_memory",
    }
)

MEMORY_PROMPT = (
    "Use memory.save (or save_memory) when the user shares persistent facts, "
    "preferences, or project rules they want remembered across sessions. "
    "Use memory.search (or search_memory) when you need to recall past user facts."
)

__all__ = [
    "DELETE_MEMORY_ALIAS_SCHEMA",
    "DELETE_MEMORY_SCHEMA",
    "LIST_MEMORIES_ALIAS_SCHEMA",
    "LIST_MEMORIES_SCHEMA",
    "MEMORY_PROMPT",
    "MEMORY_SCHEMAS",
    "MEMORY_TOOL_NAMES",
    "SAVE_MEMORY_ALIAS_SCHEMA",
    "SAVE_MEMORY_SCHEMA",
    "SEARCH_MEMORY_ALIAS_SCHEMA",
    "SEARCH_MEMORY_SCHEMA",
    "build_memory_tools",
]
