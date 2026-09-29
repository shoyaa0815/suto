"""Voice adapter behavior with deterministic audio and executor ports."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import AgentRequest, AgentRuntime
from agent.events import AgentEvent
from ai.execution import loop
from ai.executor import execute_local_ai
from application.configuration import parse_settings
from interfaces.voice.audio import AudioClip, FileAudioInput, FileAudioOutput
from interfaces.voice.controller import VoiceController
from interfaces.voice.factory import VoiceSetupError, build_controller, diagnostics
from interfaces.voice.providers import OpenAISpeech, SpeechUnavailable
from llm.types import ModelResponse, ToolCall
from mcp_integration.config import MCPConfig
from permissions import ApprovalBroker
from workflows.storage.store import JobStore


WAV = b"RIFF\x04\x00\x00\x00WAVE"


class Input:
    def __init__(self, *, error=None):
        self.error = error
        self.captures = 0
        self.closed = 0

    async def capture(self):
        self.captures += 1
        if self.error:
            raise self.error
        return AudioClip(WAV)

    async def close(self):
        self.closed += 1


class STT:
    def __init__(self, *texts, error=None):
        self.texts = iter(texts or ("hello",))
        self.error = error
        self.received = []
        self.closed = 0

    async def transcribe(self, audio):
        self.received.append(audio)
        if self.error:
            raise self.error
        return next(self.texts)

    async def close(self):
        self.closed += 1


class TTS:
    def __init__(self, *, error=None):
        self.error = error
        self.texts = []
        self.closed = 0

    async def synthesize(self, text):
        self.texts.append(text)
        if self.error:
            raise self.error
        return AudioClip(WAV)

    async def close(self):
        self.closed += 1


class Output:
    def __init__(self, *, error=None):
        self.error = error
        self.played = []
        self.stops = 0
        self.closed = 0

    async def play(self, audio):
        if self.error:
            raise self.error
        self.played.append(audio)

    async def stop(self):
        self.stops += 1

    async def close(self):
        self.closed += 1


def outcome(text="Hello there.", status="completed"):
    return SimpleNamespace(text=text, status=status, prompt_tokens=3, output_tokens=2)


def make_controller(tmp_path, monkeypatch, executor, *, source=None, stt=None, tts=None,
                    sink=None, store=None, broker=None):
    monkeypatch.setattr("interfaces.voice.controller.load_settings", lambda: SimpleNamespace(
        profile=SimpleNamespace(display_name="Local", timezone="UTC", locale="en")
    ))
    store = store or JobStore(tmp_path / "suto.db")
    source, stt, tts, sink = source or Input(), stt or STT(), tts or TTS(), sink or Output()
    controller = VoiceController(source, stt, tts, sink, store=store,
                                 executor=executor, approval_broker=broker)
    return controller, store, source, stt, tts, sink


async def test_audio_transcription_becomes_normal_agent_request_and_tts(tmp_path, monkeypatch):
    seen = []

    async def execute(request, **options):
        seen.append((request, options))
        return outcome()

    controller, store, source, stt, tts, sink = make_controller(
        tmp_path, monkeypatch, execute, stt=STT("Please answer in English"))
    turn = await controller.run_once()
    request, options = seen[0]
    assert isinstance(request, AgentRequest)
    assert request.user_message == "Please answer in English"
    assert request.metadata == {}
    assert request.run_id == turn.run_id
    assert options["assistant_context"].conversation_id == turn.session_id
    assert options["skill_registry"] is controller.skills.registry
    assert source.captures == 1 and stt.received == [AudioClip(WAV)]
    assert tts.texts == ["Hello there."] and sink.played == [AudioClip(WAV)]
    assert store.get_agent_run(turn.run_id)["status"] == "completed"
    assert [m.role for m in store.list_messages(turn.session_id)] == ["user", "assistant"]
    await controller.close()
    assert (source.closed, stt.closed, tts.closed, sink.closed) == (1, 1, 1, 1)


async def test_session_history_and_skills_restore_across_controllers(tmp_path, monkeypatch):
    seen = []

    async def execute(request, **options):
        seen.append((request, options["conversation_history"]))
        return outcome(f"reply {len(seen)}")

    first, store, *_ = make_controller(tmp_path, monkeypatch, execute, stt=STT("first"))
    first.activate_skill("research")
    await first.run_once()
    second, _, *_ = make_controller(tmp_path, monkeypatch, execute,
                                    stt=STT("second"), store=JobStore(store.path))
    assert second.state.session_id == first.state.session_id
    assert second.state.active_skills == ("research",)
    await second.run_once()
    assert seen[1][0].active_skills == ("research",)
    assert any(item.get("content") == "reply 1" for item in seen[1][1])
    previous = second.state.session_id
    assert second.new_session() != previous
    second.resume_session(previous)
    assert second.state.active_skills == ("research",)
    foreign = store.resolve_channel_identity("api", "local")
    foreign_session = store.get_or_create_conversation(foreign.id, "api", "local")
    with pytest.raises(ValueError, match="unavailable"):
        second.resume_session(foreign_session.id)


async def test_missing_skill_blocks_before_audio_capture(tmp_path, monkeypatch):
    called = []

    async def execute(request, **options):
        called.append(request)
        return outcome()

    first, store, *_ = make_controller(tmp_path, monkeypatch, execute)
    store.set_session_skills(first.state.session_id, ("missing_skill",))
    second, _, source, *_ = make_controller(tmp_path, monkeypatch, execute,
                                            store=JobStore(store.path))
    turn = await second.run_once()
    assert turn.status == "blocked"
    assert "missing_skill" in second.state.display_text
    assert source.captures == 0 and not called


async def test_tool_events_are_consumed_without_new_trace_schema(tmp_path, monkeypatch):
    async def execute(request, **options):
        notify = options["agent_event_callback"]
        notify(AgentEvent(request.run_id, request.session_id, "model.requested"))
        notify(AgentEvent(request.run_id, request.session_id, "tool.started",
                          {"tool_name": "get_current_datetime"}))
        notify(AgentEvent(request.run_id, request.session_id, "tool.completed",
                          {"tool_name": "get_current_datetime"}))
        return outcome("The time is available.")

    controller, _, _, _, tts, _ = make_controller(tmp_path, monkeypatch, execute)
    turn = await controller.run_once()
    assert turn.status == "completed"
    assert controller.state.tool_name == "get_current_datetime"
    assert [event.type for event in controller.state.events] == [
        "model.requested", "tool.started", "tool.completed",
    ]
    assert tts.texts == ["The time is available."]


@pytest.mark.parametrize("text,status,spoken", [
    ("API_KEY=private-token", "completed", "I have a detailed response. It is available as text."),
    ("```python\nprint(1)\n```", "completed", "I have a detailed response. It is available as text."),
    ("A long explanation. " * 30, "completed", "I have a detailed response. It is available as text."),
    ("private-provider-exception", "failed", "The request could not be completed."),
])
async def test_presentation_never_speaks_sensitive_or_code_output(tmp_path, monkeypatch,
                                                                   text, status, spoken):
    async def execute(request, **options):
        return outcome(text, status)

    controller, store, _, _, tts, _ = make_controller(tmp_path, monkeypatch, execute)
    turn = await controller.run_once()
    assert turn.spoken_text == spoken
    assert tts.texts == [spoken]
    assert "private-token" not in turn.spoken_text
    assert "private-provider-exception" not in controller.state.display_text
    assert store.get_agent_run(turn.run_id)["status"] == status


async def test_cancel_during_runtime_cancels_child_and_persists_state(tmp_path, monkeypatch):
    entered = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def execute(request, **options):
        async def child():
            try:
                entered.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                raise

        return await child()

    controller, store, _, _, _, sink = make_controller(tmp_path, monkeypatch, execute)
    task = asyncio.create_task(controller.run_once())
    await entered.wait()
    run_id = controller.state.run_id
    await controller.cancel()
    assert task.cancelled() and child_cancelled.is_set()
    assert controller.state.status == "cancelled"
    assert store.get_agent_run(run_id)["status"] == "cancelled"
    assert sink.stops >= 1


async def test_cancel_during_playback_stops_audio_without_rewriting_run(tmp_path, monkeypatch):
    entered = asyncio.Event()

    class WaitingOutput(Output):
        async def play(self, audio):
            entered.set()
            await asyncio.Event().wait()

    async def execute(request, **options):
        return outcome()

    sink = WaitingOutput()
    controller, store, *_ = make_controller(tmp_path, monkeypatch, execute, sink=sink)
    task = asyncio.create_task(controller.run_once())
    await entered.wait()
    run_id = controller.state.run_id
    await controller.cancel()
    assert task.cancelled()
    assert controller.state.status == "cancelled" and sink.stops >= 1
    assert store.get_agent_run(run_id)["status"] == "completed"


async def test_speech_cannot_authorize_pending_approval(tmp_path, monkeypatch):
    broker = ApprovalBroker(timeout_seconds=0.01)

    async def execute(request, **options):
        pending, decision = await options["approval_broker"].request(
            request.run_id, f"{request.run_id}:1:1", "test.action"
        )
        return outcome("denied", "blocked" if decision.choice == "deny" else "completed")

    controller, _, _, _, tts, _ = make_controller(
        tmp_path, monkeypatch, execute, stt=STT("yes"), broker=broker)
    turn = await controller.run_once()
    assert controller.state.transcript == "yes"
    assert turn.status == "blocked"
    assert tts.texts == ["The request could not be completed."]


async def test_independent_controllers_share_session_reservation(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def waiting(request, **options):
        entered.set()
        await asyncio.Event().wait()

    first, store, *_ = make_controller(tmp_path, monkeypatch, waiting)
    second, _, _, _, _, _ = make_controller(tmp_path, monkeypatch,
                                           lambda request, **options: outcome(),
                                           store=JobStore(store.path))
    task = asyncio.create_task(first.run_once())
    await entered.wait()
    assert (await second.run_once()).status == "blocked"
    await first.cancel()
    assert store.get_agent_run(first.state.run_id)["status"] == "cancelled"


@pytest.mark.parametrize("which", ["input", "stt", "tts", "output"])
async def test_provider_failures_are_safe_and_release_run(tmp_path, monkeypatch, which):
    async def execute(request, **options):
        return outcome()

    controller, store, _, _, tts, _ = make_controller(
        tmp_path, monkeypatch, execute,
        source=Input(error=RuntimeError("private audio secret")) if which == "input" else None,
        stt=STT(error=RuntimeError("private speech secret")) if which == "stt" else None,
        tts=TTS(error=RuntimeError("private tts secret")) if which == "tts" else None,
        sink=Output(error=RuntimeError("private output secret")) if which == "output" else None,
    )
    turn = await controller.run_once()
    assert turn.status == "failed"
    assert "private" not in controller.state.display_text
    if turn.run_id:
        assert store.get_agent_run(turn.run_id)["status"] == "completed"
    else:
        assert not store.list_messages(controller.state.session_id)


async def test_file_backed_voice_smoke(tmp_path, monkeypatch):
    source_file = tmp_path / "sample.wav"
    sink_file = tmp_path / "response.wav"
    source_file.write_bytes(WAV)

    async def execute(request, **options):
        return outcome("Voice smoke passed.")

    controller, store, *_ = make_controller(
        tmp_path, monkeypatch, execute,
        source=FileAudioInput(source_file), stt=STT("smoke"),
        tts=TTS(), sink=FileAudioOutput(sink_file),
    )
    turn = await controller.run_once()
    assert turn.status == "completed"
    assert sink_file.read_bytes() == WAV
    assert store.get_agent_run(turn.run_id)["status"] == "completed"
    await controller.close()


def test_runtime_imports_no_voice_types():
    import inspect

    source = inspect.getsource(AgentRuntime)
    assert "voice" not in source.casefold()
    assert "microphone" not in source.casefold()


@pytest.mark.parametrize("choice,expected", [("allow_once", "completed"), ("deny", "blocked")])
async def test_explicit_local_approval_uses_broker(tmp_path, monkeypatch, choice, expected):
    pending = asyncio.Event()

    async def execute(request, **options):
        _, decision = await options["approval_broker"].request(
            request.run_id, f"{request.run_id}:1:1", "test.action",
        )
        return outcome("approved" if decision.choice == "allow_once" else "denied",
                       "completed" if decision.choice == "allow_once" else "blocked")

    controller, _, _, _, _, _ = make_controller(tmp_path, monkeypatch, execute)
    controller.on_change = lambda state: pending.set() if state.pending_approval_id else None
    task = asyncio.create_task(controller.run_once())
    await pending.wait()
    assert controller.state.status == "waiting_approval"
    assert controller.submit_approval(controller.state.pending_approval_id, choice)
    assert (await task).status == expected


async def test_openai_speech_protocol_and_error_redaction(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status, data):
            self.status = status
            self.content = self
            self.data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def read(self, limit):
            return self.data[:limit]

    class Session:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/transcriptions"):
                return Response(200, b'{"text":"recognized"}')
            return Response(200, WAV)

    monkeypatch.setattr("interfaces.voice.providers.aiohttp.ClientSession", Session)
    speech = OpenAISpeech("private-key")
    assert await speech.transcribe(AudioClip(WAV)) == "recognized"
    assert await speech.synthesize("Speak this") == AudioClip(WAV)
    assert calls[0][0].endswith("/audio/transcriptions")
    assert calls[1][1]["json"]["input"] == "Speak this"
    assert all(call[1]["allow_redirects"] is False for call in calls)

    def rejected(self, url, **kwargs):
        return Response(401, b"private-key error")

    monkeypatch.setattr(Session, "post", rejected)
    with pytest.raises(SpeechUnavailable) as error:
        await speech.transcribe(AudioClip(WAV))
    assert "private-key" not in str(error.value)


def test_voice_startup_diagnostics_are_safe_and_fail_closed(tmp_path, monkeypatch):
    settings = parse_settings({"voice": {
        "stt_provider": "openai", "tts_provider": "openai",
        "input_provider": "file", "input_path": str(tmp_path / "missing.wav"),
        "output_provider": "file", "output_path": str(tmp_path / "out.wav"),
    }})
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    lines = "\n".join(diagnostics(settings))
    assert "STT provider: openai" in lines and "WAV input file unavailable" in lines
    assert "private-key" not in lines
    with pytest.raises(VoiceSetupError, match="unavailable"):
        build_controller(settings)


@pytest.mark.parametrize("responses,expected_status,expected_event", [
    ((ModelResponse("", [ToolCall("get_current_datetime", {})]),
      ModelResponse("Time checked")), "completed", "tool.completed"),
    ((ModelResponse("", [ToolCall("mcp.fake.exec", {})]),), "blocked", "tool.failed"),
    ((ModelResponse("", [ToolCall("agent.delegate", {
        "role": "research", "task": "summarize",
    })]), ModelResponse("Child summary"), ModelResponse("Parent answer")),
     "completed", "delegation.completed"),
])
async def test_voice_uses_existing_executor_for_tools_mcp_and_delegation(
    tmp_path, monkeypatch, responses, expected_status, expected_event,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ai.executor.load_mcp_config", lambda: MCPConfig(()))
    monkeypatch.setattr("ai.response.detect_language_code", lambda text: "en")

    class Model:
        def __init__(self):
            self.responses = iter(responses)

        async def generate(self, request):
            return next(self.responses)

    monkeypatch.setattr(loop, "build_model_router", lambda session: Model())
    controller, store, _, _, tts, _ = make_controller(
        tmp_path, monkeypatch, execute_local_ai, stt=STT("Please answer in English"),
    )
    turn = await controller.run_once()
    assert turn.status == expected_status
    events = store.list_run_tree_events(turn.run_id)
    assert expected_event in [event["event_type"] for event in events]
    assert tts.texts
    if expected_event == "delegation.completed":
        children = [event for event in events if event["parent_run_id"] == turn.run_id]
        assert children and all(json.loads(event["data"])["child_role"] == "research"
                                for event in children)
        assert store.get_agent_run(children[0]["run_id"])["parent_run_id"] == turn.run_id
    if expected_status == "blocked":
        assert "mcp.fake.exec" not in tts.texts[0]
