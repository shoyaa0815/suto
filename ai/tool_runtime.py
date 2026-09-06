import hashlib
import inspect
import json

from . import config
from .models import ToolEventCallback


def tool_detail(name: str, args: dict) -> str:
    visible = []
    for key in ("query", "url", "path", "attachment_id", "detail"):
        if key not in args:
            continue
        value = str(args[key]).replace("\n", " ")
        if len(value) > 80:
            value = value[:77] + "..."
        visible.append(f"{key}={value}")
    suffix = f" ({', '.join(visible)})" if visible else ""
    return f"{name}{suffix}"


def audit_tool_arguments(name: str, args: dict) -> dict:
    """Keep tool audits useful without storing entire file contents."""
    audited = dict(args)
    if name == "apply_workspace_patch" and "content" in audited:
        content = str(audited.pop("content"))
        encoded = content.encode("utf-8")
        audited["content_size"] = len(encoded)
        audited["content_sha256"] = hashlib.sha256(encoded).hexdigest()
    return audited


def tool_call_signature(name: str, args: dict) -> str:
    audited = audit_tool_arguments(name, args)
    return f"{name}:{json.dumps(audited, ensure_ascii=False, sort_keys=True)}"


async def emit_tool_event(
    callback: ToolEventCallback | None,
    event: dict,
) -> None:
    if callback is None:
        return
    try:
        result = callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception as error:
        config.debug(f"[tool_audit] callback failed: {error!r}")
