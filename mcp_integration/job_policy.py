"""Operator-owned selection metadata. This module never grants MCP execution."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from workflows.errors import ErrorCode, WorkflowError


MAX_POLICY_BYTES = 65_536
MAX_SELECTION = 64
_NAME = re.compile(r"mcp\.[a-z][a-z0-9_-]{0,63}\.[a-z][a-z0-9_.-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
IDENTITIES = frozenset({
    "config_identity", "executable_identity", "dependency_identity",
    "schema_identity", "effect_metadata_identity", "review_identity",
    "mount_manifest_identity", "confinement_identity", "resource_limits_identity",
})


def _deny(reason: str) -> None:
    raise WorkflowError(ErrorCode.PERMISSION_DENIED, reason)


def identity(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def tool_names(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_SELECTION:
        _deny("MCP selection must be a bounded array of exact tool names")
    if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in value):
        _deny("MCP selection requires exact mcp.<server>.<tool> names")
    if len(set(value)) != len(value):
        _deny("duplicate MCP selection")
    return sorted(value)


def _unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate policy key")
        result[key] = value
    return result


def load_job_mcp_policy() -> dict:
    """Read fresh operator policy on each boundary; no model/config fallback."""
    configured = os.environ.get("SUTO_JOB_MCP_POLICY")
    if not configured:
        return {"format_version": 1, "tools": {}}
    source = Path(configured)
    try:
        if not source.is_absolute() or source.is_symlink() or source.resolve() != source:
            raise ValueError("invalid policy path")
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            details = os.fstat(stream.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_size > MAX_POLICY_BYTES
                    or (os.name == "posix" and
                        (details.st_uid != os.geteuid() or details.st_mode & 0o022))):
                raise ValueError("unsafe policy file")
            content = stream.read(MAX_POLICY_BYTES + 1)
        if len(content) > MAX_POLICY_BYTES:
            raise ValueError("oversized policy")
        policy = json.loads(content, object_pairs_hook=_unique_pairs)
        if (not isinstance(policy, dict) or set(policy) != {"format_version", "tools"}
                or type(policy["format_version"]) is not int or policy["format_version"] != 1
                or not isinstance(policy["tools"], dict)):
            raise ValueError("invalid policy")
        tool_names(list(policy["tools"]))
        for entry in policy["tools"].values():
            if not isinstance(entry, dict) or set(entry) != IDENTITIES | {"workspaces", "effect"}:
                raise ValueError("incomplete policy entry")
            if entry["effect"] != "read_only":
                raise ValueError("unreviewed effects")
            if any(not isinstance(entry[key], str) or not _DIGEST.fullmatch(entry[key])
                   for key in IDENTITIES):
                raise ValueError("invalid policy identity")
            roots = entry["workspaces"]
            if (not isinstance(roots, list) or not roots
                    or any(not isinstance(root, str) or not Path(root).is_absolute()
                           or str(Path(root).resolve()) != root for root in roots)
                    or len(set(roots)) != len(roots)):
                raise ValueError("invalid policy workspaces")
            if any(source.resolve().is_relative_to(root) for root in roots):
                raise ValueError("operator policy must be outside eligible workspaces")
        return policy
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        _deny("invalid operator Job MCP policy")


def pin_selection(selection: object, workspace: str) -> dict | None:
    names = tool_names(selection)
    if not names:
        return None
    policy = load_job_mcp_policy()
    if any(name not in policy["tools"] or workspace not in policy["tools"][name]["workspaces"]
           for name in names):
        _deny("MCP selection exceeds operator policy")
    pin = {"tools": names, "workspace": workspace, "policy_identity": identity(policy)}
    return {**pin, "selection_identity": identity(pin)}


def validate_pin(pin: object) -> dict:
    if not isinstance(pin, dict) or set(pin) != {
        "tools", "workspace", "policy_identity", "selection_identity",
    }:
        _deny("invalid pinned MCP selection")
    names = tool_names(pin["tools"])
    if not names or names != pin["tools"] or not isinstance(pin["workspace"], str):
        _deny("invalid pinned MCP selection")
    if any(not isinstance(pin[key], str) or not _DIGEST.fullmatch(pin[key])
           for key in ("policy_identity", "selection_identity")):
        _deny("invalid pinned MCP identity")
    body = {key: value for key, value in pin.items() if key != "selection_identity"}
    if identity(body) != pin["selection_identity"]:
        _deny("pinned MCP selection changed")
    return pin


def revalidate_selection(options: dict, workspace: str, *, child: bool = False) -> None:
    if "mcp_selection" not in options:
        return
    pin = validate_pin(options["mcp_selection"])
    if child:
        _deny("child jobs cannot receive MCP selections")
    if pin["workspace"] != workspace or pin_selection(pin["tools"], workspace) != pin:
        _deny("MCP selection revoked or policy identity changed")
