import asyncio
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import ai
import pytest
from ai.execution.request import prepare_request
from assistant.context import AssistantContext
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from interfaces.cli import backend
from interfaces.cli import app as cli_app
from interfaces.cli import commands as cli_commands
from interfaces.cli.commands import CommandContext, handle_command
from interfaces.cli.skill_catalog import load_cli_skills
from interfaces.cli.app import (
    ACTIVITY_ROW_HEIGHT,
    DOT_FRAMES,
    PROMPT_BOX_HEIGHT,
    PromptReader,
    _build_prompt_application,
)
from interfaces.cli.operations import print_due_reminders, print_pending_reminders
from workflows.storage.store import JobStore
from tests.support.ai_helpers import FakeClientSession, patch_model_chat


def test_plain_cli_starts_session_and_uses_a_simple_prompt(monkeypatch, capsys):
    calls = []

    class FakeReader:
        async def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

        async def __call__(self):
            calls.append("> ")
            return "hello"

        def write(self, value):
            calls.append(("write", value))

        def set_activity(self, value):
            calls.append(("activity", value))

    async def fake_session(mode, read_prompt):
        calls.append((mode, await read_prompt()))

    monkeypatch.setattr(cli_app, "run_session", fake_session)
    monkeypatch.setattr(cli_app, "PromptReader", FakeReader)

    cli_app.run("agent")

    assert capsys.readouterr().out == ""
    assert calls[0] == "start"
    assert any(
        call[0] == "write" and "Suto" in call[1] and "agent" in call[1]
        for call in calls
        if isinstance(call, tuple) and call[0] == "write"
    )
    assert ("write", "Type /help for commands. PageUp: history · End: latest\n\n") in calls
    assert ("agent", "hello") in calls
    assert calls[-1] == "stop"


def test_cli_prompt_keeps_history_above_a_nonwrapping_bottom_status_row():
    application = _build_prompt_application("> ")

    assert application.full_screen is True
    assert application.erase_when_done is True
    assert isinstance(application.layout.container, HSplit)
    history = application.layout.container.children[0]
    assert application.suto_history_field.window is history
    activity_row = application.layout.container.children[1]
    assert activity_row.height.preferred == ACTIVITY_ROW_HEIGHT == 1
    assert isinstance(activity_row.content, FormattedTextControl)
    assert activity_row.wrap_lines() is False
    gray_box = application.layout.container.children[2]
    assert isinstance(gray_box, HSplit)
    assert gray_box.height.preferred == PROMPT_BOX_HEIGHT == 3


def test_cli_activity_text_is_rendered_from_layout_state():
    reader = PromptReader()
    reader._activity = "Suto is thinking"
    reader._activity_suffix = DOT_FRAMES[2]

    assert reader._activity_text() == "Suto is thinking..."
    reader._activity = None
    assert reader._activity_text() == ""


async def test_cli_prompt_reader_queues_submitted_input_without_exiting_application(
    monkeypatch,
):
    reader = PromptReader()

    async def fake_start():
        return None

    monkeypatch.setattr(reader, "start", fake_start)
    pending = asyncio.create_task(reader())
    await asyncio.sleep(0)
    reader._accept("hello")

    assert await pending == "hello"


def test_cli_activity_disables_input_and_ctrl_c_requests_cancellation():
    class FakeField:
        read_only = False

    class FakeApplication:
        suto_input_field = FakeField()

        def invalidate(self):
            pass

    reader = PromptReader()
    reader._application = FakeApplication()

    reader.set_activity("Suto is thinking")

    assert reader._application.suto_input_field.read_only is True
    reader._interrupt()
    assert reader.cancellation_event.is_set()

    reader.set_activity(None)
    assert reader._application.suto_input_field.read_only is False


def test_cli_history_follows_new_output_until_user_scrolls_away():
    class FakeApplication:
        suto_history_field = TextArea(read_only=True)

        def invalidate(self):
            pass

    reader = PromptReader()
    reader._application = FakeApplication()

    reader.write("first\n")
    history = reader._application.suto_history_field.buffer
    assert history.text == "first\n"
    assert history.cursor_position == len(history.text)

    reader._scroll_history()
    history.cursor_position = 1
    reader.write("second\n")

    assert history.text == "first\nsecond\n"
    assert history.cursor_position == 1

    reader._follow_history()
    reader.write("third\n")
    assert history.cursor_position == len(history.text)


async def test_cli_cancels_an_active_request_from_the_persistent_reader(
    tmp_path, monkeypatch, capsys
):
    class Reader:
        def __init__(self):
            self.cancellation_event = asyncio.Event()
            self.prompts = iter(["hello", "/exit"])

        async def __call__(self):
            return next(self.prompts)

        def set_activity(self, value):
            if value is not None:
                self.cancellation_event.set()

    async def slow_ask(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", slow_ask)

    await backend.run_session("agent", Reader())

    assert "request cancelled" in capsys.readouterr().out


async def test_cli_skill_activation_persists_between_requests_in_one_run(
    tmp_path, monkeypatch, capsys
):
    seen = []

    async def fake_ask(prompt, **options):
        seen.append((prompt, options["active_skills"]))
        return "done"

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", fake_ask)
    prompts = iter([
        "/skills", "/skill activate coding", "first", "second",
        "/skill deactivate coding", "third", "/skill activate missing", "/exit",
    ])

    async def read_prompt():
        return next(prompts)

    await backend.run_session("agent", read_prompt)

    assert seen == [("first", ("coding",)), ("second", ("coding",)), ("third", ())]
    output = capsys.readouterr().out
    assert "coding (available)" in output
    assert "Skill coding activated." in output
    assert "Skill coding deactivated." in output
    assert "unknown skill: missing" in output


async def test_cli_user_skill_slash_command_applies_to_one_request(
    tmp_path, monkeypatch, capsys
):
    skill_dir = tmp_path / ".suto" / "skills" / "outline"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: outline\ndescription: Outline a topic.\n"
        "allowed_tools: [search_web]\n---\nUse short headings.\n",
        encoding="utf-8",
    )
    seen = []

    async def fake_ask(prompt, **options):
        prepared = prepare_request(
            prompt, "agent", None, None, "", [], None, None,
            active_skills=options["active_skills"],
            skill_registry=options["skill_registry"],
        )
        seen.append((prompt, options["active_skills"], prepared))
        return "done"

    database_path = tmp_path / "suto.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))
    monkeypatch.setattr(backend, "ask_local_ai", fake_ask)
    prompts = iter([
        "/skills", "/outline explain trees", "ordinary", "/outline", "/exit",
    ])
    await backend.run_session("agent", lambda: _next_prompt(prompts))

    assert [(prompt, names) for prompt, names, _ in seen] == [
        ("explain trees", ("outline",)), ("ordinary", ()),
    ]
    assert "Use short headings." in seen[0][2].messages[0]["content"]
    assert "fetch_url" not in seen[0][2].allowed_tools
    assert "Use short headings." not in seen[1][2].messages[0]["content"]
    output = capsys.readouterr().out
    assert "outline (available)" in output
    assert "usage: /outline <message>" in output
    with sqlite3.connect(database_path) as db:
        assert db.execute("SELECT count(*) FROM session_skills").fetchone()[0] == 0


def test_cli_user_skill_catalog_skips_invalid_conflicting_and_symlinked_files(
    tmp_path
):
    root = tmp_path / ".suto" / "skills"
    for name, body in (
        ("valid", "name: valid\ndescription: Valid."),
        ("invalid", "name: invalid"),
        ("help", "name: help\ndescription: Reserved."),
        ("coding", "name: coding\ndescription: Duplicate."),
        ("mismatch", "name: elsewhere\ndescription: Mismatched."),
    ):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\n{body}\n---\nInstructions.\n", encoding="utf-8"
        )
    linked = root / "linked"
    linked.symlink_to(root / "valid", target_is_directory=True)

    registry, warnings = load_cli_skills(tmp_path)

    assert {skill.name for skill in registry.list_skills()} == {
        "coding", "research", "valid"
    }
    assert len(warnings) == 5
    assert any("invalid SKILL.md" in warning for warning in warnings)
    assert any("/help is a CLI command" in warning for warning in warnings)
    assert any("name coding already exists" in warning for warning in warnings)
    assert any("symlinks are not supported" in warning for warning in warnings)
    assert any("directory and skill name differ" in warning for warning in warnings)


def test_cli_user_skill_catalog_ignores_missing_and_symlinked_roots(tmp_path):
    registry, warnings = load_cli_skills(tmp_path)
    assert {skill.name for skill in registry.list_skills()} == {"coding", "research"}
    assert warnings == ()

    home_skills = tmp_path / ".suto" / "skills"
    home_skills.parent.mkdir()
    home_skills.symlink_to(tmp_path, target_is_directory=True)
    registry, warnings = load_cli_skills(tmp_path)
    assert {skill.name for skill in registry.list_skills()} == {"coding", "research"}
    assert "symlink" in warnings[0]


async def test_session_manages_reminders_separately_from_automation_jobs(
    tmp_path, monkeypatch, capsys
):
    database_path = tmp_path / "suto.db"
    store = JobStore(database_path)
    user = store.resolve_channel_identity("tui", "local")
    other = store.resolve_channel_identity("api", "other")
    personal_task = store.create_task(user.id, "ส่งงาน")
    reminder = store.create_reminder(
        user.id,
        "นัดหมอ",
        "2099-01-02T09:30:00+07:00",
        timezone="Asia/Bangkok",
    )
    duplicate_a = store.create_reminder(user.id, "งานซ้ำ", "2099-01-03T09:30:00+07:00")
    duplicate_b = store.create_reminder(user.id, "งานซ้ำ", "2099-01-04T09:30:00+07:00")
    other_reminder = store.create_reminder(other.id, "ของคนอื่น", "2099-01-05T09:30:00+07:00")
    decoy = store.create_reminder(
        user.id, other_reminder.id, "2099-01-06T09:30:00+07:00"
    )
    prefixed_title = store.create_reminder(
        user.id, "rem_sleep", "2099-01-07T09:30:00+07:00"
    )
    prompts = iter(
        [
            "/jobs",
            "/quit",
            "/brief",
            "/setting",
            "/daily",
            "/noti",
            "/notification",
            "/task",
            "/reminder",
            "/reminder remove",
            "/reminder remove ไม่มีชื่อนี้",
            f"/reminder remove {other_reminder.id}",
            "/reminder remove งานซ้ำ",
            f"/reminder remove {duplicate_a.id}",
            "/reminder remove งานซ้ำ",
            "/reminder remove นัดหมอ",
            "/reminder remove rem_sleep",
            f"/reminder remove {decoy.id}",
            "/reminder",
            "/suto",
            "/suto help",
            "/help extra",
            "/version extra",
            "/help",
            "/version",
            "/exit",
        ]
    )

    async def read_prompt():
        return next(prompts)

    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))
    await backend.run_session("agent", read_prompt)

    output = capsys.readouterr().out
    assert "No automation jobs." in output
    assert "Unknown command: /quit" in output
    assert "Unknown command: /brief" in output
    assert "Unknown command: /setting" in output
    assert "Unknown command: /daily" in output
    assert "Unknown command: /noti" in output
    assert "Unknown command: /notification" in output
    assert "Unknown command: /suto. Type /help for commands." in output
    assert "usage: /help" in output
    assert "usage: /version" in output
    assert "Suto 0.1.0" in output
    assert "Open tasks:" in output
    assert personal_task.id in output
    assert "Pending reminders:" in output
    assert reminder.id in output
    assert "2099-01-02 09:30" in output
    assert "usage: /reminder remove <name-or-id>" in output
    assert "Pending reminder not found: ไม่มีชื่อนี้" in output
    assert f"Pending reminder not found: {other_reminder.id}" in output
    assert "Multiple pending reminders are named \"งานซ้ำ\":" in output
    assert duplicate_a.id in output and duplicate_b.id in output
    assert "Reminder deleted: งานซ้ำ" in output
    assert "Reminder deleted: นัดหมอ" in output
    assert "No pending reminders." in output
    assert store.get_task(user.id, personal_task.id).status == "open"
    assert store.get_reminder(user.id, duplicate_a.id) is None
    assert store.get_reminder(user.id, duplicate_b.id) is None
    assert store.get_reminder(user.id, reminder.id) is None
    assert store.get_reminder(user.id, prefixed_title.id) is None
    assert store.get_reminder(user.id, decoy.id) is None
    assert store.get_reminder(other.id, other_reminder.id).status == "scheduled"
    assert "  /help  " in output
    assert "  /version  " in output
    assert "  /suto help" not in output
    assert "  /reminder" in output
    assert "  /reminder remove <name-or-id>" in output
    assert "  /task" in output
    assert "  /jobs" in output
    assert "  /task complete" not in output
    assert "  /daily" not in output
    assert "  /exit" in output
    assert "bye" in output


def test_jobs_command_shows_jobs_and_task_command_uses_personal_tasks(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    personal_task = store.create_task(user.id, "ซื้อยา")
    job = store.create_job("Inspect project")

    jobs = handle_command(CommandContext(store, user, "conversation", "agent"), "/jobs")
    tasks = handle_command(CommandContext(store, user, "conversation", "agent"), "/task")
    created = handle_command(
        CommandContext(store, user, "conversation", "agent"), "/task run inspect"
    )

    assert jobs.handled and tasks.handled and created.handled
    output = capsys.readouterr().out
    assert "Recent automation jobs (newest first):" in output
    assert f"{job.id}  queued  Inspect project" in output
    assert f"{personal_task.id}  ซื้อยา" in output
    assert "Task created:" in output
    assert [item.title for item in store.list_tasks(user.id)] == ["ซื้อยา", "run inspect"]
    assert store.get_job(job.id).status.value == "queued"


async def test_slash_commands_create_tasks_and_reminders_without_ai(
    tmp_path, monkeypatch, capsys
):
    database_path = tmp_path / "suto.db"
    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))

    async def unexpected_ai(*args, **kwargs):
        raise AssertionError("slash commands must not call AI")

    monkeypatch.setattr(backend, "ask_local_ai", unexpected_ai)
    prompts = iter([
        "/reminder อีกห้านาทีเตือนกินข้าว",
        "/reminder 00.05 เตือนให้เข้านอนหน่อย",
        "/task ทำอะไรต่างๆบลาๆ",
        "/reminder อีกศูนย์นาทีเตือนกินข้าว",
        "/reminder",
        "/task",
        "/exit",
    ])

    async def read_prompt():
        return next(prompts)

    before = datetime.now(UTC)
    await backend.run_session("agent", read_prompt)
    store = JobStore(database_path)
    user = store.resolve_channel_identity("tui", "local")
    reminders = store.list_reminders(user.id)
    assert len(reminders) == 2
    assert {item.title for item in reminders} == {"กินข้าว", "เข้านอนหน่อย"}
    relative = next(item for item in reminders if item.title == "กินข้าว")
    scheduled = datetime.fromisoformat(relative.remind_at)
    assert before + timedelta(minutes=5) <= scheduled <= datetime.now(UTC) + timedelta(minutes=5)
    clock = next(item for item in reminders if item.title == "เข้านอนหน่อย")
    assert datetime.fromisoformat(clock.remind_at).strftime("%H:%M") == "00:05"
    assert [item.title for item in store.list_tasks(user.id)] == ["ทำอะไรต่างๆบลาๆ"]
    output = capsys.readouterr().out
    assert output.count("Reminder created:") == 2
    assert "Task created:" in output
    assert "ใช้ /reminder" in output
    assert "Pending reminders:" in output and "Open tasks:" in output


def test_remove_commands_delete_only_one_owned_row_and_delivery_state(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    other = store.resolve_channel_identity("api", "other")
    context = CommandContext(store, user, "conversation", "agent")
    first = store.create_task(user.id, "ซ้ำ")
    second = store.create_task(user.id, "ซ้ำ")
    foreign = store.create_task(other.id, "ของคนอื่น")
    decoy = store.create_task(user.id, foreign.id)
    target = store.get_or_create_delivery_target(
        user.id, "test", "local", "dm", "local"
    )
    reminder = store.create_reminder(
        user.id, "กินยา", "2099-01-02T09:30:00+07:00",
        delivery_target_id=target.id,
    )
    foreign_reminder = store.create_reminder(
        other.id, "ของคนอื่น", "2099-01-02T09:30:00+07:00"
    )

    handle_command(context, "/task remove ซ้ำ")
    assert store.get_task(user.id, first.id) is not None
    assert store.get_task(user.id, second.id) is not None
    handle_command(context, f"/task remove {foreign.id}")
    assert store.get_task(user.id, decoy.id) is not None
    assert store.get_task(other.id, foreign.id) is not None
    handle_command(context, f"/task remove {first.id}")
    assert store.get_task(user.id, first.id) is None
    handle_command(context, "/task remove ซ้ำ")
    assert store.get_task(user.id, second.id) is None
    handle_command(context, "/reminder remove กินยา")
    assert store.get_reminder(user.id, reminder.id) is None
    assert store.get_reminder(other.id, foreign_reminder.id) is not None
    with store._connect() as db:
        assert db.execute(
            "SELECT 1 FROM reminder_deliveries WHERE reminder_id=?", (reminder.id,)
        ).fetchone() is None
    output = capsys.readouterr().out
    assert "Multiple open tasks" in output
    assert "Task deleted: ซ้ำ" in output
    assert "Reminder deleted: กินยา" in output


def test_cli_ai_request_cannot_use_personal_task_or_reminder_tools(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    conversation = store.get_or_create_conversation(user.id, "tui", "local")
    context = AssistantContext(
        store, user.id, conversation.id, allow_personal_tools=False
    )

    prepared = prepare_request(
        "เตือนให้กินข้าวอีกห้านาที", "agent", None,
        ai.ReplyLanguage("th", "Thai", "test"), "", None, context, None,
    )

    assert "create_reminder_in" not in prepared.allowed_tools
    assert "create_task" not in prepared.allowed_tools
    assert "list_reminders" not in prepared.allowed_tools
    assert "save_memory" in prepared.allowed_tools
    assert "/task and /reminder commands" in prepared.messages[0]["content"]


def test_version_reads_project_metadata_and_handles_missing_file(
    tmp_path, monkeypatch, capsys
):
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text('[project]\nversion = "9.8.7"\n')
    monkeypatch.setattr(cli_commands, "PROJECT_FILE", project_file)
    context = CommandContext(None, None, "conversation", "agent")

    handle_command(context, "/version")
    assert capsys.readouterr().out == "Suto 9.8.7\n"

    monkeypatch.setattr(cli_commands, "PROJECT_FILE", tmp_path / "missing.toml")
    handle_command(context, "/version")
    assert capsys.readouterr().out == "Suto version unavailable.\n"


def test_remove_command_reports_database_failure_without_claiming_deletion(
    tmp_path, monkeypatch, capsys
):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    task = store.create_task(user.id, "ซื้อยา")
    context = CommandContext(store, user, "conversation", "agent")

    def fail_remove(*args):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(store, "remove_task", fail_remove)
    handle_command(context, f"/task remove {task.id}")

    assert store.get_task(user.id, task.id) is not None
    output = capsys.readouterr().out
    assert "ลบงานไม่สำเร็จ" in output
    assert "Task deleted" not in output


async def test_cli_identity_uses_shared_yaml_profile(tmp_path, monkeypatch):
    database_path = tmp_path / "suto.db"
    store = JobStore(database_path)
    existing = store.resolve_channel_identity(
        "tui",
        "local",
        display_name="Old",
        timezone="UTC",
        locale="en",
    )
    (tmp_path / "config.yaml").write_text(
        "version: 1\nprofile:\n  timezone: Asia/Bangkok\n"
        "  locale: th\n  display_name: Suto Owner\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))

    async def read_prompt():
        return "/exit"

    await backend.run_session("agent", read_prompt)

    user = JobStore(database_path).get_user(existing.id)
    assert user.display_name == "Suto Owner"
    assert user.timezone == "Asia/Bangkok"
    assert user.locale == "th"


def test_overdue_reminder_prints_original_time_and_is_not_repeated(
    tmp_path,
    capsys,
):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    store.create_reminder(
        user.id,
        "ประชุมทีม",
        "2020-01-02T09:30:00+07:00",
        timezone="Asia/Bangkok",
        now="2019-01-01T00:00:00+07:00",
    )

    assert print_due_reminders(store, user.id, overdue=True) == 1
    output = capsys.readouterr().out
    assert "เลยเวลาแล้ว" in output
    assert "ประชุมทีม" in output
    assert "2020-01-02 09:30 +07" in output
    assert print_due_reminders(store, user.id, overdue=True) == 0
    assert print_pending_reminders(store, user.id) == 0
    assert "No pending reminders." in capsys.readouterr().out


async def test_cli_restores_skill_and_uses_generic_approval_broker(tmp_path, monkeypatch, capsys):
    observed = []

    async def fake_ask(prompt, **options):
        observed.append(options["active_skills"])
        broker = options["approval_broker"]
        request, decision = await broker.request(options["run_id"],
                                                  f"{options['run_id']}:1:1", "test.action")
        assert request.run_id == options["run_id"]
        assert decision.choice == "allow_once"
        return "done"

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", fake_ask)
    first = iter(["/skill activate research", "/exit"])
    await backend.run_session("agent", lambda: _next_prompt(first))
    second = iter(["hello", "allow", "/exit"])
    await backend.run_session("agent", lambda: _next_prompt(second))
    assert observed == [("research",)]
    assert "Approve test.action" in capsys.readouterr().out


async def _next_prompt(prompts):
    return next(prompts)


async def test_cli_runs_the_real_assistant_session_backend(
    tmp_path,
    monkeypatch,
    capsys,
):
    calls = []

    async def fake_ask(prompt, **options):
        assert options["mode"] == "agent"
        assert options["assistant_context"] is not None
        calls.append((prompt, options["conversation_history"]))
        return f"answer: {prompt}"

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", fake_ask)
    prompts = iter(["hello", "/clear", "again", "/reset all", "third", "/exit"])

    async def read_prompt():
        return next(prompts)

    await backend.run_session("agent", read_prompt)

    output = capsys.readouterr().out
    assert "answer: hello" in output
    assert "suto>" not in output
    assert "Chat context cleared." in output
    assert "answer: again" in output
    assert "All saved conversations deleted" in output
    assert "answer: third" in output
    assert calls == [
        ("hello", []),
        ("again", []),
        ("third", []),
    ]


async def test_cli_continues_after_memory_index_failure(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "suto.db"
    original_search = JobStore.search_memories
    attempts = 0
    model_calls = []

    def flaky_search(self, user_id, query, limit=5):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("private database detail")
        return original_search(self, user_id, query, limit)

    async def fake_chat(_session, messages, _schemas, think=False):
        model_calls.append(messages[-1]["content"])
        return {"message": {"content": "second answer"}}

    async def english_language(_prompt, _previous):
        return ai.ReplyLanguage("en", "English", "test")

    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))
    monkeypatch.setattr(JobStore, "search_memories", flaky_search)
    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda _text: "en")
    monkeypatch.setattr(backend, "_choose_reply_language_async", english_language)
    patch_model_chat(monkeypatch)
    prompts = iter(["first question", "second question", "/exit"])

    async def read_prompt():
        return next(prompts)

    await backend.run_session("agent", read_prompt)

    output = capsys.readouterr().out
    assert "Memory search is unavailable. Please try again." in output
    assert "second answer" in output
    assert "private database detail" not in output
    assert model_calls == ["second question"]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("2", "09:00"), ("07:30", "07:30"), ("", None)],
)
async def test_cli_clarification_accepts_number_custom_text_or_cancel(
    typed, expected, monkeypatch, capsys
):
    class FakeApplication:
        async def run_async(self):
            return typed

    def fake_application(prompt):
        assert prompt == "answer> "
        return FakeApplication()

    monkeypatch.setattr(cli_app, "patch_stdout", nullcontext)

    answer = await PromptReader(fake_application).request_clarification(
        {
            "question": "พรุ่งนี้เช้าคือกี่โมง?",
            "options": ["08:00", "09:00"],
        }
    )

    output = capsys.readouterr().out
    assert "Suto needs one detail" in output
    assert "1. 08:00" in output
    assert "2. 09:00" in output
    assert answer == expected
