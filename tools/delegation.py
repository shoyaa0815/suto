"""Model-callable delegation tool schema."""

DELEGATE_NAME = "agent.delegate"
DELEGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["research", "coding", "review"]},
        "task": {"type": "string"},
        "context": {"type": "string"},
        "allowed_tools": {"type": "array", "items": {"type": "string"}, "maxItems": 20, "uniqueItems": True},
    },
    "required": ["role", "task"],
    "additionalProperties": False,
}

