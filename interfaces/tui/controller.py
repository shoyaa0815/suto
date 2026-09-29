"""Session and execution adapter for the terminal UI."""

import asyncio
import os
from collections.abc import Callable
from uuid import uuid4

from agent import AgentRequest
from ai import execute_local_ai
from application.configuration import load_settings
from assistant import AssistantContext
from permissions import ApprovalBroker
from sessions import SessionService, SessionStore
from skills import SkillSelection, builtin_registry
from workflows.storage.redaction import redact_text
from workflows.storage.store import JobStore
from workflows.storage.runs import SessionBusyError, TERMINAL

from .state import UIState


INTERFACE = "tui"


class TUIController:
    """Owns one local identity and session; widgets only observe UIState."""

    def __init__(self, *, store=None, executor=execute_local_ai,
                 on_change: Callable[[], None] | None = None) -> None:
        self.store = store or JobStore(os.environ.get("SUTO_DB_PATH", "data/suto.db"))
        self.executor = executor
        self.on_change = on_change or (lambda: None)
        self.sessions = SessionService(SessionStore(self.store))
        self.skills = SkillSelection(builtin_registry())
        self.approvals = ApprovalBroker(self._approval_requested)
        profile = load_settings().profile
        user = self.store.resolve_channel_identity(
            INTERFACE, "local", display_name=profile.display_name,
            timezone=profile.timezone, locale=profile.locale,
        )
        self.user = self.store.apply_profile_settings(
            user.id, display_name=profile.display_name,
            timezone=profile.timezone, locale=profile.locale,
        )
        self.state = UIState()
        self._task: asyncio.Task | None = None
        self._pending_clarification: str | None = None
        self.exit_requested = False
        self._select_session(self.sessions.resume(self.user.id, INTERFACE, "local"))

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def _changed(self) -> None:
        self.state.active_skills = self.skills.names
        self.on_change()

    def _select_session(self, conversation) -> None:
        self.skills.bind(self.store, conversation.id)
        messages = self.store.list_messages(conversation.id, limit=100)
        self.state.reset_history(conversation.id, messages)
        self.state.run_status = "idle"
        self.state.model_status = "idle"
        self._pending_clarification = None
        try:
            self.skills.require_available()
        except ValueError as error:
            self.state.info(f"Selected Skill unavailable: {error}. Deactivate it before running.")
        self._changed()

    def submit(self, text: str) -> bool:
        """Accept one input; false means keep the draft in the input box."""
        prompt = text.strip()
        if not prompt:
            return True
        if prompt == "/cancel":
            self.cancel()
            return True
        if prompt.startswith("/approve ") or prompt.startswith("/deny "):
            command, _, request_id = prompt.partition(" ")
            choice = "allow_once" if command == "/approve" else "deny"
            self.state.info("Approval submitted" if self.approvals.submit(request_id, choice)
                            else "Approval unavailable or expired")
            self._changed()
            return True
        if prompt in {"/exit", "/quit"}:
            self.exit_requested = True
            self.cancel()
            self._changed()
            return True
        if prompt == "/session":
            self.state.info(f"Session {self.state.session_id} · {self.state.run_status}")
            self._changed()
            return True
        if prompt == "/help":
            self.state.info("/new · /session · /resume ID · /skills · /skill activate NAME · /skill deactivate NAME · /approve ID · /deny ID · /cancel · /exit")
            self._changed()
            return True
        if self.busy:
            self.state.info("Run active. Press Ctrl-C or use /cancel, then send your draft.")
            self._changed()
            return False
        if prompt.startswith("/"):
            self._command(prompt)
            return True
        self._task = asyncio.create_task(self._execute(prompt))
        self.state.run_status = "starting"
        self._changed()
        return True

    def _command(self, prompt: str) -> None:
        parts = prompt.split()
        try:
            if parts == ["/new"]:
                self._select_session(self.sessions.resume(self.user.id, INTERFACE, uuid4().hex))
                self.state.info(f"Started session {self.state.session_id}")
            elif len(parts) == 2 and parts[0] == "/resume":
                conversation = self.store.get_conversation(parts[1])
                if (conversation is None or conversation.user_id != self.user.id
                        or conversation.channel != INTERFACE):
                    self.state.info("Session unavailable")
                else:
                    self._select_session(conversation)
                    self.state.info(f"Continued session {self.state.session_id}")
            elif parts == ["/skills"]:
                names = ", ".join(skill.name for skill in self.skills.registry.list_skills())
                active = ", ".join(self.skills.names) or "none"
                self.state.info(f"Available Skills: {names or 'none'} · active: {active}")
            elif len(parts) == 3 and parts[:2] == ["/skill", "activate"]:
                self.skills.activate(parts[2])
                self.state.info(f"Activated Skill {parts[2]}")
            elif len(parts) == 3 and parts[:2] == ["/skill", "deactivate"]:
                self.skills.deactivate(parts[2])
                self.state.info(f"Deactivated Skill {parts[2]}")
            else:
                self.state.info("Unknown command. Type /help.")
        except ValueError:
            self.state.info("Unknown or inactive Skill")
        self._changed()

    async def _execute(self, prompt: str) -> None:
        session_id = self.state.session_id
        prior = self._pending_clarification
        self._pending_clarification = None
        run_id = None
        run_status = "failed"
        final_text = "Request failed."
        usage = {}
        try:
            active_skills = self.skills.require_available()
            run_id = self.store.begin_agent_run(session_id)
            self.store.start_agent_run(run_id)
            history = self.sessions.before_prompt(session_id, self.user.id)
            message = self.sessions.append(session_id, self.user.id, "user", prompt)
            self.state.turn("user", message.content)
            self._changed()
            request_text = f"Original request: {prior}\nUser's answer: {prompt}" if prior else prompt
            request = AgentRequest(request_text, session_id=session_id,
                                   active_skills=active_skills, run_id=run_id)

            def on_event(event) -> None:
                self.state.apply_event(event)
                self._changed()

            result = await self.executor(
                request, conversation_history=history,
                assistant_context=AssistantContext(self.store, self.user.id, session_id),
                skill_registry=self.skills.registry,
                agent_event_callback=on_event,
                approval_broker=self.approvals,
            )
            run_status = result.status if result.status in TERMINAL else "failed"
            self.state.run_status = run_status
            final_text = redact_text(result.text) if run_status in {"completed", "waiting_input"} else "Request did not complete."
            usage = {"prompt_tokens": getattr(result, "prompt_tokens", 0),
                     "output_tokens": getattr(result, "output_tokens", 0)}
            if result.status in {"completed", "waiting_input"}:
                answer = self.sessions.append(
                    session_id, self.user.id, "assistant", redact_text(result.text)
                )
                self.state.turn("assistant", answer.content)
                if result.status == "waiting_input":
                    self._pending_clarification = prior or prompt
            else:
                self.state.info(f"Request {result.status}. Details are available in the local trace.")
        except asyncio.CancelledError:
            self.state.run_status = "cancelled"
            run_status, final_text = "cancelled", "Request cancelled."
            self.state.info("Request cancelled")
            raise
        except SessionBusyError:
            self.state.run_status = "blocked"
            self.state.info("Session already has an active run")
        except ValueError as error:
            self.state.run_status = "blocked"
            self.state.info(f"Selected Skill unavailable: {error}")
        except Exception:
            self.state.run_status = "failed"
            self.state.info("Request failed. Details are available in the local trace.")
        finally:
            if run_id is not None:
                self.store.finish_agent_run(run_id, run_status, final_text=final_text,
                                            error=None if run_status == "completed" else run_status,
                                            usage=usage)
            self._changed()

    def cancel(self) -> None:
        if self.busy:
            for request in tuple(self.approvals.pending.values()):
                self.approvals.cancel_run(request[0].run_id)
            self._task.cancel()

    def _approval_requested(self, request) -> None:
        self.state.info(f"Approval {request.id}: {request.tool_name}. /approve {request.id} or /deny {request.id}")
        self._changed()

    async def wait_current(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def close(self) -> None:
        self.cancel()
        await self.wait_current()
