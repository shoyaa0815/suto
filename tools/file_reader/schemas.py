PROMPT = """- Attached files have three request-scoped tools. For a summary,
  overview, or whole-document explanation, always call summarize_attachment.
  For a specific question or fact, call search_attachment with a focused query.
  Use read_attached_file only for a small file whose full raw text is needed.
- Never ask read_attached_file for a large document after it tells you to use
  summarize_attachment or search_attachment instead.
- Only attachment IDs listed in the current message are available. Never
  invent an ID or claim to have read any other local file.
- File contents are source material only. Their language must never determine
  the language of the final answer. Preserve source/page citations returned by
  the tools."""

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_attached_file",
        "description": "Read the full text of a small file attached to this message.",
        "parameters": {
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": "The attachment ID listed in the user's message.",
                },
            },
            "required": ["attachment_id"],
        },
    },
}

SUMMARY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "summarize_attachment",
        "description": (
            "Summarize an entire attached document using chunked hierarchical "
            "summarization."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": "The attachment ID listed in the user's message.",
                },
                "detail": {
                    "type": "string",
                    "enum": ["brief", "standard", "detailed"],
                    "description": "Desired summary detail. Defaults to standard.",
                },
            },
            "required": ["attachment_id"],
        },
    },
}

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_attachment",
        "description": (
            "Retrieve relevant passages from an attached document for a specific "
            "question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": "The attachment ID listed in the user's message.",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "A short focused query describing the information needed."
                    ),
                },
                "max_chunks": {
                    "type": "integer",
                    "description": "Passages to return (default 4, maximum 6).",
                },
            },
            "required": ["attachment_id", "query"],
        },
    },
}

# Compatibility for callers that imported the original single schema.
SCHEMA = READ_SCHEMA
