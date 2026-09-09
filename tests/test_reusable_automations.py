import pytest

from ai import AIExecutionResult
from automation.bundles import export_bundle, import_bundle, write_bundle
from automation.definitions import render_automation_prompt
from automation.models import JobStatus
from automation.runner import JobRunner
from automation.store import JobStore
from automation.worker import AutomationWorker
from clients.cli.bot import _parse_automation_run


def test_automation_versions_pin_skill_versions(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_skill("python-testing", "Run targeted tests first.")
    automation = store.create_automation(
        "fix-bug",
        "Fix issue {{issue_id}}.",
        tmp_path,
        {"issue_id": {"type": "integer", "required": True}},
        allow_write=True,
        allow_command=True,
        skill_names=["python-testing"],
    )
    first = store.get_current_automation_version(automation.id)
    first_skill = store.list_automation_skill_versions(first.id)[0][1]

    store.revise_skill("python-testing", "Run all tests.")
    second = store.revise_automation(
        "fix-bug",
        "Resolve issue {{issue_id}}.",
        tmp_path,
        {"issue_id": {"type": "integer", "required": True}},
        skill_names=["python-testing"],
    )

    assert first.version == 1
    assert second.version == 2
    assert store.list_automation_skill_versions(first.id)[0][1] == first_skill
    assert store.list_automation_skill_versions(second.id)[0][1].version == 2


def test_worker_renders_and_submits_automation_with_saved_policy(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_automation(
        "report",
        "Prepare {{kind}} report for {{day}}.",
        tmp_path,
        {
            "kind": {"type": "string", "default": "daily"},
            "day": {"type": "integer", "required": True},
        },
        allow_write=True,
    )
    worker = AutomationWorker(store, JobRunner(store))

    job = worker.submit_automation("report", {"day": 9})

    assert job.prompt == "Prepare daily report for 9."
    assert job.source == "automation"
    assert job.source_ref.startswith("av_")
    assert job.workspace == str(tmp_path.resolve())
    assert job.allow_write is True
    assert job.allow_command is False


def test_render_rejects_missing_unknown_and_wrong_type_parameters(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_automation(
        "typed",
        "Issue {{issue}}",
        tmp_path,
        {"issue": {"type": "integer", "required": True}},
    )
    version = store.get_current_automation_version("typed")

    with pytest.raises(ValueError, match="missing required"):
        render_automation_prompt(version, {})
    with pytest.raises(ValueError, match="unknown parameters"):
        render_automation_prompt(version, {"issue": 1, "extra": True})
    with pytest.raises(ValueError, match="must be integer"):
        render_automation_prompt(version, {"issue": "1"})


async def test_runner_loads_only_pinned_automation_skills(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_skill("review", "Inspect the diff before reporting success.")
    store.create_skill("unrelated", "This must not be loaded.")
    store.create_automation(
        "review-change",
        "Review the project.",
        tmp_path,
        skill_names=["review"],
    )
    worker = AutomationWorker(store, JobRunner(store))
    submitted = worker.submit_automation("review-change")
    job = store.claim_next_job()
    captured = {}

    async def execute(prompt, skill_instructions, **kwargs):
        captured["instructions"] = skill_instructions
        return AIExecutionResult("done", "completed", None, 1, 1, 0.01)

    await JobRunner(store, execute=execute).run(job)

    assert "Inspect the diff" in captured["instructions"]
    assert "This must not be loaded" not in captured["instructions"]
    assert store.get_job(submitted.id).status == JobStatus.COMPLETED


def test_export_import_round_trip_and_history(tmp_path):
    source = JobStore(tmp_path / "source.db")
    source.create_skill("docs", "Keep documentation concise.")
    automation = source.create_automation(
        "write-docs",
        "Document {{topic}}.",
        tmp_path,
        {"topic": {"type": "string", "required": True}},
        description="Reusable documentation workflow",
        skill_names=["docs"],
    )
    worker = AutomationWorker(source, JobRunner(source))
    job = worker.submit_automation("write-docs", {"topic": "jobs"})
    assert source.list_automation_jobs(automation.id) == [job]

    output = tmp_path / "bundle.json"
    write_bundle(source, "write-docs", output)
    target = JobStore(tmp_path / "target.db")
    name, version = import_bundle(target, output)

    assert (name, version) == ("write-docs", 1)
    imported = target.get_current_automation_version(name)
    assert imported.prompt_template == "Document {{topic}}."
    assert target.list_automation_skill_versions(imported.id)[0][0] == "docs"
    assert export_bundle(target, name)["format_version"] == 1


def test_sensitive_parameter_defaults_are_rejected(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    with pytest.raises(ValueError, match="sensitive parameter defaults"):
        store.create_automation(
            "unsafe",
            "Use {{api_key}}",
            tmp_path,
            {"api_key": {"type": "string", "default": "secret-value"}},
        )


def test_export_preserves_sensitive_parameter_schema_names(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_automation(
        "external-check",
        "Check with {{api_key}}.",
        tmp_path,
        {"api_key": {"type": "string", "required": True}},
    )

    bundle = export_bundle(store, "external-check")

    assert bundle["automation"]["parameter_schema"]["api_key"] == {
        "required": True,
        "type": "string",
    }


def test_parse_automation_run_converts_json_scalars():
    name, parameters = _parse_automation_run(
        'fix-bug issue=123 strict=true label="backend"'
    )

    assert name == "fix-bug"
    assert parameters == {"issue": 123, "strict": True, "label": "backend"}
