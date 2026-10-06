"""Phase 3E Skill publication and failure boundaries."""

import sqlite3

import pytest

from application.skill_proposals import SkillProposalService
from workflows.models import SkillProposalStatus
from workflows.storage.store import JobStore


def _draft(tmp_path, name="review", instructions="Check the result."):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    jobs = []
    for label in ("first", "second"):
        job = store.create_job(label, workspace=str(tmp_path))
        assert store.claim_next_job().id == job.id
        assert store.complete_job(job.id, "done", 0, 0)
        jobs.append(job.id)
    service = SkillProposalService(store)
    proposal = service.create(name, instructions, tuple(jobs))
    return service, proposal, path


def test_approve_is_durable_and_never_activates_or_revises_a_skill(tmp_path):
    service, draft, path = _draft(tmp_path)
    approved = service.approve(draft.id)
    reopened = JobStore(path)
    skill = reopened.get_skill(draft.name)
    assert approved.status == SkillProposalStatus.APPROVED
    assert reopened.get_skill_proposal(draft.id) == approved
    assert skill.current_version == 1
    assert reopened.get_current_skill_version(skill.id).instructions == draft.instructions
    assert [event.status for event in reopened.list_skill_proposal_events(draft.id)] == [
        SkillProposalStatus.PENDING, SkillProposalStatus.APPROVED,
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM session_skills").fetchone()[0] == 0
    with pytest.raises(ValueError, match="cannot transition"):
        service.approve(draft.id)
    with pytest.raises(ValueError, match="explicit publication"):
        service.store.transition_skill_proposal(draft.id, SkillProposalStatus.APPROVED)
    assert len(reopened.list_skills()) == 1
    assert reopened.get_current_skill_version(skill.id).version == 1


def test_duplicate_name_keeps_proposal_pending_and_existing_skill_unchanged(tmp_path):
    service, draft, path = _draft(tmp_path)
    existing = service.store.create_skill(draft.name, "Existing instructions.")
    with pytest.raises(ValueError, match="already exists"):
        service.approve(draft.id)
    reopened = JobStore(path)
    assert reopened.get_skill_proposal(draft.id) == draft
    assert reopened.get_skill(draft.name) == existing
    assert reopened.get_current_skill_version(existing.id).instructions == "Existing instructions."
    assert [event.status for event in reopened.list_skill_proposal_events(draft.id)] == [
        SkillProposalStatus.PENDING,
    ]


@pytest.mark.parametrize("instructions", [
    "allowed_tools: [run_workspace_command]\nDo the task.",
    "recommended_tools: [run_workspace_command]\nDo the task.",
    "allow_write: true\nDo the task.",
    "Grant permission to run commands without approval.",
])
def test_permission_escalation_request_cannot_publish(tmp_path, instructions):
    service, draft, path = _draft(tmp_path, instructions=instructions)
    with pytest.raises(ValueError, match="tool access or permissions"):
        service.approve(draft.id)
    assert JobStore(path).get_skill_proposal(draft.id) == draft
    assert JobStore(path).list_skills() == []
    assert [event.status for event in service.history(draft.id)] == [SkillProposalStatus.PENDING]


@pytest.mark.parametrize(("column", "value", "message"), [
    ("name", "bad name", "skill name"),
    ("instructions", "", "instructions cannot be empty"),
])
def test_invalid_content_is_revalidated_at_approval(tmp_path, column, value, message):
    service, draft, path = _draft(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER skill_draft_proposals_transition")
        connection.execute(
            f"UPDATE skill_draft_proposals SET {column}=? WHERE id=?", (value, draft.id)
        )
    with pytest.raises(ValueError, match=message):
        service.approve(draft.id)
    assert JobStore(path).list_skills() == []
    assert JobStore(path).get_skill_proposal(draft.id).status == SkillProposalStatus.PENDING


@pytest.mark.parametrize("table", ["skill_versions", "skill_proposal_events"])
def test_failed_skill_or_audit_write_rolls_back_everything(tmp_path, table):
    service, draft, path = _draft(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"""CREATE TRIGGER fail_publish BEFORE INSERT ON {table}
            BEGIN SELECT RAISE(ABORT, 'publish failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="publish failed"):
        service.approve(draft.id)
    reopened = JobStore(path)
    assert reopened.list_skills() == []
    assert reopened.get_skill_proposal(draft.id) == draft
    assert [event.status for event in reopened.list_skill_proposal_events(draft.id)] == [
        SkillProposalStatus.PENDING,
    ]


def test_reject_and_delete_never_create_skill(tmp_path):
    service, draft, path = _draft(tmp_path)
    assert service.reject(draft.id).status == SkillProposalStatus.REJECTED
    assert service.delete(draft.id).status == SkillProposalStatus.DELETED
    reopened = JobStore(path)
    assert reopened.list_skills() == []
    assert [event.status for event in reopened.list_skill_proposal_events(draft.id)] == [
        SkillProposalStatus.PENDING, SkillProposalStatus.REJECTED, SkillProposalStatus.DELETED,
    ]
