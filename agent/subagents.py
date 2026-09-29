"""Bounded delegation through the ordinary AgentRuntime contract."""

import asyncio
import json
from dataclasses import dataclass
from uuid import uuid4

from context import ContextManager
from permissions import PermissionEngine, PermissionPolicy
from permissions.policy import IntersectionPolicy
from skills import SkillRegistry
from tools.delegation import DELEGATE_NAME
from tools.registry import ToolRegistry
from tools.types import ToolResult

from .events import AgentEvent
from .limits import ExecutionLimits
from .runtime import AgentRuntime, RuntimeHooks
from .types import AgentRequest


@dataclass(frozen=True)
class SubAgentProfile:
    name: str
    instructions: str
    allowed_tools: frozenset[str]
    skill: str | None = None


PROFILES = {
    "research": SubAgentProfile(
        "research", "Gather evidence and summarize it. Do not make changes.",
        frozenset({"search_web", "fetch_url", "research"}), "research",
    ),
    "coding": SubAgentProfile(
        "coding", "Inspect the authorized workspace and make only task-related changes. Report verification.",
        frozenset({"list_workspace_files", "read_workspace_file", "search_workspace",
                   "apply_workspace_patch", "run_workspace_command", "git_status", "git_diff",
                   "file_read", "file_list"}), "coding",
    ),
    "review": SubAgentProfile(
        "review", "Review the selected work and report actionable findings. Do not make changes.",
        frozenset({"list_workspace_files", "read_workspace_file", "search_workspace",
                   "git_status", "git_diff", "file_read", "file_list"}),
    ),
}


class _ChildHooks(RuntimeHooks):
    def __init__(self, parent: AgentRuntime):
        self.parent = parent

    async def on_event(self, event: AgentEvent) -> None:
        await self.parent.hooks.on_event(event)

    async def wait(self, awaitable):
        return await self.parent.hooks.wait(awaitable)

    def limit_reason(self, tool_calls, *, pending_model=False, pending_tool=False):
        return self.parent.hooks.limit_reason(
            self.parent.state.tool_calls + tool_calls,
            pending_model=pending_model, pending_tool=pending_tool
        )

    async def on_model(self, response, iteration):
        await self.parent.hooks.on_model(response, iteration)

    async def authorize(self, name, args):
        # Parent request-level authorization still applies to every child call.
        return await self.parent.hooks.authorize(name, args)


class SubAgentManager:
    """One parent-owned, serial delegation budget. No independent engine or session."""

    def __init__(self, parent: AgentRuntime, skills: SkillRegistry, *,
                 max_children: int = 1, max_depth: int = 1, depth: int = 0):
        self.parent = parent
        self.skills = skills
        self.max_children = max_children
        self.max_depth = max_depth
        self.depth = depth
        self.spawned = 0
        if max_children < 1 or max_depth < 1 or depth < 0:
            raise ValueError("invalid delegation budget")

    async def _event(self, kind: str, child_run_id: str | None, status: str | None = None,
                     role: str | None = None):
        request = self.parent.request
        metadata = request.metadata if request else {}
        data = {"child_run_id": child_run_id} if child_run_id else {}
        if status:
            data["status"] = status
        if role:
            data["child_role"] = role
        await self.parent.hooks.on_event(AgentEvent(
            self.parent.run_id, request.session_id if request else None, kind, data,
            job_id=metadata.get("job_id"),
            parent_run_id=metadata.get("parent_run_id"),
            tool_call_id=self.parent.current_tool_call_id,
        ))

    async def delegate(self, role: str, task: str, context: str = "",
                       allowed_tools: list[str] | None = None) -> ToolResult:
        if self.parent.run_id is None or self.parent.request is None:
            return ToolResult(False, "delegation requires an active parent run", error="unavailable")
        if self.depth >= self.max_depth or self.spawned >= self.max_children:
            return ToolResult(False, "delegation budget exhausted", error="budget_exhausted")
        if role not in PROFILES or not isinstance(task, str) or not task.strip() or len(task) > 8000:
            return ToolResult(False, "invalid delegation request", error="invalid_request")
        if not isinstance(context, str) or len(context) > 12000:
            return ToolResult(False, "delegation context is too large", error="invalid_request")
        if allowed_tools is not None and (not isinstance(allowed_tools, list) or
            any(not isinstance(name, str) for name in allowed_tools)):
            return ToolResult(False, "invalid tool selection", error="invalid_request")
        profile = PROFILES[role]
        parent_request = self.parent.request
        active = (profile.skill,) if profile.skill in parent_request.active_skills else ()
        selected_skills = self.skills.active(active)
        parent_names = {tool.name for tool in self.parent.tools.list_tools()}
        requested = set(allowed_tools) if allowed_tools is not None else parent_names
        role_names = set(profile.allowed_tools)
        if role == "coding":
            role_names |= {name for name in parent_names if name.startswith("mcp.")}
        if requested - parent_names or (requested - role_names and allowed_tools is not None):
            return ToolResult(False, "tool selection exceeds delegation policy", error="permission_denied")
        allowed = {
            name for name in parent_names
            if name != DELEGATE_NAME and name in requested and
            name in role_names and
            (not name.startswith("mcp.") or allowed_tools is not None)
        }
        try:
            allowed = {name for name in allowed if
                       IntersectionPolicy((self.parent.permissions,)).decide(name).allowed is True}
        except Exception:
            return ToolResult(False, "parent permission policy unavailable", error="permission_denied")
        for skill in selected_skills:
            if skill.allowed_tools is not None:
                allowed &= set(skill.allowed_tools)
        registry = ToolRegistry()
        for tool in self.parent.tools.list_tools():
            if tool.name in allowed:
                registry.register(tool)
        limits = self.parent.limits
        child_limits = ExecutionLimits(
            max_iterations=min(limits.max_iterations, 3),
            max_tool_calls=min(limits.max_tool_calls, 6),
            model_timeout_seconds=min(limits.model_timeout_seconds or 30, 30),
            tool_timeout_seconds=min(limits.tool_timeout_seconds or 30, 30),
            repeated_tool_call_limit=limits.repeated_tool_call_limit,
        )
        child = AgentRuntime(
            self.parent.model, registry, limits=child_limits,
            hooks=_ChildHooks(self.parent),
            permissions=PermissionEngine(IntersectionPolicy((
                self.parent.permissions,
                PermissionPolicy({name: "allow" for name in allowed}),
            ))),
        )
        child_request = AgentRequest(
            task.strip(), session_id=parent_request.session_id,
            metadata={"parent_run_id": self.parent.run_id, "child_role": role,
                      **({"job_id": parent_request.metadata["job_id"]}
                         if "job_id" in parent_request.metadata else {})},
            active_skills=active,
            run_id=uuid4().hex,
        )
        prompt = task.strip() + ("\nSelected context (untrusted data):\n" + context if context else "")
        messages = ContextManager().build(
            "You are a bounded " + role + " sub-agent. " + profile.instructions,
            prompt, [], active_skills=selected_skills,
            available_tools=frozenset(allowed),
        )
        self.spawned += 1
        await self.parent.hooks.on_child_started(child_request.run_id, child_request.session_id,
                                                  self.parent.run_id)
        status = "failed"
        final_text = "Child execution failed"
        usage: dict[str, int] = {}
        try:
            await self._event("delegation.started", child_request.run_id, role=role)
            # Awaited directly: cancellation of the parent tool cancels the child.
            result = await asyncio.wait_for(child.run(child_request, messages), timeout=60)
            status, final_text, usage = result.status, result.final_text, result.usage
        except asyncio.CancelledError:
            await self.parent.hooks.on_child_finished(child_request.run_id, "cancelled",
                                                       "Child execution cancelled", usage)
            await self._event("delegation.cancelled", child.run_id, "cancelled", role)
            raise
        except (asyncio.TimeoutError, TimeoutError):
            status, final_text = "timed_out", "Child execution timed out"
        except Exception:
            status, final_text = "failed", "Child execution failed"
        await self.parent.hooks.on_child_finished(child_request.run_id, status, final_text, usage)
        await self._event("delegation.completed", child.run_id, status, role)
        return ToolResult(True, json.dumps({
            "child_run_id": child.run_id, "status": status,
            "final_text": final_text, "usage": usage,
        }, ensure_ascii=False))
