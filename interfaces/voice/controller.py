"""Audio-to-agent adapter with no speech dependencies in the runtime."""

import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

from agent import AgentRequest, AgentResult
from ai import execute_local_ai
from application.configuration import load_settings
from assistant import AssistantContext
from permissions import ApprovalBroker
from sessions import SessionService, SessionStore
from skills import SkillSelection, builtin_registry
from workflows.storage.runs import SessionBusyError, TERMINAL
from workflows.storage.store import JobStore

from .audio import AudioInput, AudioOutput
from .presentation import present
from .state import VoiceState
from .stt import SpeechToText
from .tts import TextToSpeech


INTERFACE = "voice"


@dataclass(frozen=True)
class VoiceTurn:
    run_id: str | None
    session_id: str
    status: str
    result: AgentResult | None = None
    spoken_text: str = ""


class VoiceController:
    """Owns one Voice identity; providers and executor are injected ports."""

    def __init__(self, audio_input: AudioInput, stt: SpeechToText,
                 tts: TextToSpeech, audio_output: AudioOutput, *,
                 store=None, executor=execute_local_ai, skill_registry=None,
                 approval_broker: ApprovalBroker | None = None, on_change=None) -> None:
        self.audio_input = audio_input
        self.stt = stt
        self.tts = tts
        self.audio_output = audio_output
        self.store = store or JobStore(os.environ.get("SUTO_DB_PATH", "data/suto.db"))
        self.executor = executor
        self.sessions = SessionService(SessionStore(self.store))
        self.skills = SkillSelection(skill_registry or builtin_registry())
        self.approvals = approval_broker or ApprovalBroker()
        self.approvals.on_request = self._approval_requested
        self.on_change = on_change or (lambda state: None)
        profile = load_settings().profile
        user = self.store.resolve_channel_identity(
            INTERFACE, "local", display_name=profile.display_name,
            timezone=profile.timezone, locale=profile.locale,
        )
        self.user = self.store.apply_profile_settings(
            user.id, display_name=profile.display_name,
            timezone=profile.timezone, locale=profile.locale,
        )
        self.state = VoiceState()
        self._task: asyncio.Task | None = None
        self._pending_clarification: str | None = None
        self._select_session(self.sessions.resume(self.user.id, INTERFACE, "local"))

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def _changed(self) -> None:
        self.state.active_skills = self.skills.names
        self.on_change(self.state)

    def _select_session(self, conversation) -> None:
        if self.busy:
            raise RuntimeError("Voice interaction is active")
        self.skills.bind(self.store, conversation.id)
        self.state = VoiceState(session_id=conversation.id, active_skills=self.skills.names)
        self._pending_clarification = None
        self._changed()

    def new_session(self) -> str:
        self._select_session(self.sessions.resume(self.user.id, INTERFACE, uuid4().hex))
        return self.state.session_id

    def resume_session(self, session_id: str) -> None:
        conversation = self.store.get_conversation(session_id)
        if conversation is None or conversation.user_id != self.user.id or conversation.channel != INTERFACE:
            raise ValueError("Voice session unavailable")
        self._select_session(conversation)

    def activate_skill(self, name: str) -> None:
        if self.busy:
            raise RuntimeError("Voice interaction is active")
        self.skills.activate(name)
        self._changed()

    def deactivate_skill(self, name: str) -> None:
        if self.busy:
            raise RuntimeError("Voice interaction is active")
        self.skills.deactivate(name)
        self._changed()

    def _approval_requested(self, request) -> None:
        self.state.pending_approval_id = request.id
        self.state.status = "waiting_approval"
        self._changed()

    def submit_approval(self, request_id: str, choice: str) -> bool:
        """Explicit local control only; transcription never calls this method."""
        accepted = self.approvals.submit(request_id, choice)
        if accepted:
            self.state.pending_approval_id = None
            self._changed()
        return accepted

    async def run_once(self) -> VoiceTurn:
        if self.busy:
            raise RuntimeError("Voice interaction is active")
        self._task = asyncio.current_task()
        run_id = None
        run_finished = False
        result = None
        try:
            try:
                selected = self.skills.require_available()
            except ValueError as error:
                self.state.status = "blocked"
                self.state.display_text = f"Selected Skill unavailable: {error}"
                self._changed()
                return VoiceTurn(None, self.state.session_id, "blocked")
            self.state.status = "listening"
            self.state.transcript = self.state.display_text = self.state.spoken_text = ""
            self.state.run_id = self.state.tool_name = self.state.pending_approval_id = None
            self.state.events.clear()
            self._changed()
            audio = await self.audio_input.capture()
            self.state.status = "transcribing"
            self._changed()
            transcript = await self.stt.transcribe(audio)
            if not isinstance(transcript, str) or len(transcript) > 8000:
                raise ValueError("invalid transcription")
            transcript = transcript.strip()
            if not transcript:
                self.state.status = "no_speech"
                self._changed()
                return VoiceTurn(None, self.state.session_id, "no_speech")
            self.state.transcript = transcript
            run_id = self.store.begin_agent_run(self.state.session_id)
            self.state.run_id = run_id
            self.store.start_agent_run(run_id)
            history = self.sessions.before_prompt(self.state.session_id, self.user.id)
            self.sessions.append(self.state.session_id, self.user.id, "user", transcript)
            self.state.status = "thinking"
            self._changed()
            prior = self._pending_clarification
            request_text = (
                f"Original request: {prior}\nUser's answer: {transcript}"
                if prior else transcript
            )
            self._pending_clarification = None
            request = AgentRequest(request_text, session_id=self.state.session_id,
                                   active_skills=selected, run_id=run_id)

            def on_event(event):
                self.state.apply_event(event)
                self._changed()

            outcome = await self.executor(
                request, conversation_history=history,
                assistant_context=AssistantContext(self.store, self.user.id, self.state.session_id),
                skill_registry=self.skills.registry, approval_broker=self.approvals,
                agent_event_callback=on_event,
            )
            status = outcome.status if outcome.status in TERMINAL else "failed"
            presentation = present(outcome.text, status)
            self.state.display_text = presentation.display_text
            self.state.spoken_text = presentation.spoken_text
            usage = {"prompt_tokens": getattr(outcome, "prompt_tokens", 0),
                     "output_tokens": getattr(outcome, "output_tokens", 0)}
            result = AgentResult(self.state.session_id, presentation.display_text,
                                 status, usage, error=None if status == "completed" else status)
            if status in {"completed", "waiting_input"}:
                self.sessions.append(self.state.session_id, self.user.id, "assistant",
                                     presentation.display_text)
                if status == "waiting_input":
                    self._pending_clarification = prior or transcript
            self.store.finish_agent_run(run_id, status,
                                        final_text=presentation.display_text,
                                        error=result.error, usage=usage)
            run_finished = True
            self.state.status = "speaking"
            self._changed()
            spoken_audio = await self.tts.synthesize(presentation.spoken_text)
            await self.audio_output.play(spoken_audio)
            self.state.status = status
            self._changed()
            return VoiceTurn(run_id, self.state.session_id, status, result,
                             presentation.spoken_text)
        except asyncio.CancelledError:
            self.state.status = "cancelled"
            if run_id is not None and not run_finished:
                self.store.finish_agent_run(run_id, "cancelled", final_text="Request cancelled.",
                                            error="cancelled")
            await self.audio_output.stop()
            self._changed()
            raise
        except SessionBusyError:
            self.state.status = "blocked"
            self.state.display_text = "Session already has an active run."
            self._changed()
            return VoiceTurn(None, self.state.session_id, "blocked")
        except Exception:
            if run_id is not None and not run_finished:
                self.store.finish_agent_run(run_id, "failed", final_text="Request failed.", error="failed")
            self.state.status = "failed"
            self.state.display_text = "Voice interaction failed. Check audio and speech provider availability."
            await self.audio_output.stop()
            self._changed()
            return VoiceTurn(run_id, self.state.session_id, "failed")
        finally:
            self.state.pending_approval_id = None
            self._task = None

    async def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            if self.state.run_id is not None:
                self.approvals.cancel_run(self.state.run_id)
            task = self._task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.audio_output.stop()

    async def close(self) -> None:
        await self.cancel()
        seen = set()
        for provider in (self.audio_input, self.stt, self.tts, self.audio_output):
            if id(provider) not in seen:
                seen.add(id(provider))
                await provider.close()
