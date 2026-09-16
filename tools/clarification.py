"""Model-requested clarification prompts for interactive interfaces."""


PROMPT = """- When information needed to safely complete the user's request is
  missing or ambiguous, call ask_user instead of guessing or taking a partial
  action. Ask one concise question and offer 2 or 3 concrete choices.
- The user may choose an option or provide their own answer. Do not call this
  tool after performing the action that depends on the answer."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "Ask the user to resolve an ambiguity before continuing.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 3,
                    "uniqueItems": True,
                },
            },
            "required": ["question", "options"],
            "additionalProperties": False,
        },
    },
}


def parse_request(arguments: dict) -> dict[str, list[str] | str]:
    """Validate untrusted model arguments at the interface boundary."""
    question = arguments.get("question")
    options = arguments.get("options")
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValueError("question must be a non-empty string up to 500 characters")
    if (
        not isinstance(options, list)
        or not 2 <= len(options) <= 3
        or any(not isinstance(option, str) or not option.strip() or len(option) > 200
               for option in options)
        or len({option.casefold() for option in options}) != len(options)
    ):
        raise ValueError("options must contain 2 or 3 distinct non-empty strings")
    return {"question": question.strip(), "options": [option.strip() for option in options]}
