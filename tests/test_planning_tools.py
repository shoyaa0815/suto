import pytest

from automation.runtime.context import PLANNING_TOOLS, ExecutionContext
from automation.models import StepStatus
from automation.storage.store import JobStore
from capabilities.developer.planning import build_planning_tools


def _planning_context(tmp_path, store, job_id):
    return ExecutionContext(
        job_id,
        tmp_path,
        allowed_tools=PLANNING_TOOLS,
        plan_store=store,
    )


def test_planning_tools_create_update_and_revise_plan(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("inspect and report")
    store.claim_next_job()
    tools = build_planning_tools(_planning_context(tmp_path, store, job.id), store)

    created = tools["create_plan"](["Inspect files", "Write report"])
    assert "1. [pending] Inspect files" in created

    updated = tools["update_step"](1, "completed", "inspection complete")
    assert "[completed]" in updated
    assert store.list_steps(job.id)[0].status == StepStatus.COMPLETED

    revised = tools["revise_plan"](
        "The report needs verification",
        ["Write report", "Verify report"],
    )
    assert "revised job plan" in revised
    assert [step.description for step in store.list_steps(job.id)] == [
        "Write report",
        "Verify report",
    ]


def test_planning_tools_require_job_permission(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("inspect")
    store.claim_next_job()
    context = ExecutionContext(job.id, tmp_path, plan_store=store)
    tools = build_planning_tools(context, store)

    with pytest.raises(PermissionError, match="not allowed"):
        tools["create_plan"](["Inspect files"])
