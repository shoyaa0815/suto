from datetime import datetime

# PROMPT is this tool's slice of the AI's system prompt: it tells the AI
# when/why to call get_current_datetime. harness/__init__.py collects every
# tool's PROMPT into one block that ai.py appends to its base system prompt.
# Keep it here (not in ai.py) so the usage rule always travels with the tool
# it governs — delete this file and its rule disappears with it.
PROMPT = """- You do not know the current date or time on your own — your training data
  has no awareness of "now". Any question involving today's date, the current
  time, day of the week, or how long ago/until something is, always call
  get_current_datetime first. Never guess it."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": "Get the current local date and time.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def get_current_datetime() -> str:
    return datetime.now().strftime("%A %d %B %Y, %H:%M:%S")
