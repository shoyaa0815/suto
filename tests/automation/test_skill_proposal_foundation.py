"""Phase 3D proposal persistence and lifecycle boundaries."""

import sqlite3

import pytest

from application.skill_proposals import SkillProposalService
from workflows.models import SkillProposalStatus
from workflows.storage import migrations
from workflows.storage.store import JobStore


def _completed_job(store, label, workspace="."):
    job = store.create_job(label, workspace=workspace)
    assert store.claim_next_job().id == job.id
    assert store.complete_job(job.id, "done", 0, 0)
    return job


def _service(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    jobs = (_completed_job(store, "first"), _completed_job(store, "second"))
    return SkillProposalService(store), tuple(job.id for job in jobs), path


def test_proposal_persists_provenance_and_approval_creates_skill(tmp_path):
    service, source_ids, path = _service(tmp_path)
    draft = service.create("Review-Workflow", "Check the result.", source_ids)
    assert (draft.name, draft.instructions, draft.status, draft.source_job_ids) == (
        "review-workflow", "Check the result.", SkillProposalStatus.PENDING, source_ids,
    )
    assert JobStore(path).get_skill_proposal(draft.id) == draft
    assert service.list(SkillProposalStatus.PENDING) == [draft]
    assert [event.status for event in service.history(draft.id)] == [SkillProposalStatus.PENDING]
    approved = SkillProposalService(JobStore(path)).approve(draft.id)
    assert approved.status == SkillProposalStatus.APPROVED
    assert approved.source_job_ids == source_ids
    assert JobStore(path).get_skill_proposal(draft.id) == approved
    assert [event.status for event in SkillProposalService(JobStore(path)).history(draft.id)] == [
        SkillProposalStatus.PENDING, SkillProposalStatus.APPROVED,
    ]
    skill = JobStore(path).get_skill("review-workflow")
    assert skill is not None
    assert skill.current_version == 1
    assert JobStore(path).get_current_skill_version(skill.id).instructions == "Check the result."


def test_reject_delete_scrubs_draft_and_preserves_tombstone(tmp_path):
    service, source_ids, path = _service(tmp_path)
    draft = service.create("review", "Private draft details.", source_ids)
    rejected = service.reject(draft.id)
    assert rejected.status == SkillProposalStatus.REJECTED
    deleted = service.delete(draft.id)
    assert (deleted.status, deleted.name, deleted.instructions, deleted.source_job_ids) == (
        SkillProposalStatus.DELETED, "", "", (),
    )
    assert JobStore(path).get_skill_proposal(draft.id) == deleted
    assert [event.status for event in service.history(draft.id)] == [
        SkillProposalStatus.PENDING, SkillProposalStatus.REJECTED, SkillProposalStatus.DELETED,
    ]
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT name,instructions,source_job_ids FROM skill_draft_proposals WHERE id=?",
            (draft.id,),
        ).fetchone()
    assert raw == ("", "", "[]")
    assert service.list(SkillProposalStatus.PENDING) == []


@pytest.mark.parametrize("decision", ["approve", "reject", "delete"])
def test_pending_decisions_and_invalid_terminal_transitions(tmp_path, decision):
    service, source_ids, _ = _service(tmp_path)
    draft = service.create("review", "Check the result.", source_ids)
    decided = getattr(service, decision)(draft.id)
    assert decided.status == {
        "approve": SkillProposalStatus.APPROVED,
        "reject": SkillProposalStatus.REJECTED,
        "delete": SkillProposalStatus.DELETED,
    }[decision]
    for operation in (service.approve, service.reject):
        with pytest.raises(ValueError, match="cannot transition"):
            operation(draft.id)
    if decision == "delete":
        with pytest.raises(ValueError, match="cannot transition"):
            service.delete(draft.id)
    else:
        assert service.delete(draft.id).status == SkillProposalStatus.DELETED
    with pytest.raises(ValueError, match="not found"):
        service.approve("missing")


def test_source_and_content_validation_leave_no_proposal(tmp_path):
    service, source_ids, path = _service(tmp_path)
    other = _completed_job(service.store, "other workspace", workspace=str(tmp_path))
    queued = service.store.create_job("not finished")
    cases = [
        ("review", "safe", (source_ids[0], source_ids[0])),
        ("review", "safe", (source_ids[0], "missing")),
        ("review", "safe", (source_ids[0], queued.id)),
        ("review", "safe", (source_ids[0], other.id)),
        ("review", "api_key=sk-abcdefghij", source_ids),
        ("bad name", "safe", source_ids),
    ]
    for name, instructions, ids in cases:
        with pytest.raises(ValueError):
            service.create(name, instructions, ids)
    assert JobStore(path).list_skill_proposals() == []


def test_sql_guards_and_write_failure_roll_back(tmp_path):
    service, source_ids, path = _service(tmp_path)
    draft = service.create("review", "Check the result.", source_ids)
    with service.store._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="must start pending"):
            connection.execute(
                "INSERT INTO skill_draft_proposals "
                "(id,name,instructions,status,source_job_ids,created_at,updated_at) VALUES "
                "('invalid','review','text','approved','[]','now','now')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="invalid skill proposal transition"):
            connection.execute(
                "UPDATE skill_draft_proposals SET instructions='changed' WHERE id=?", (draft.id,)
            )
        connection.execute("""CREATE TRIGGER reject_decision BEFORE INSERT ON skill_proposal_events
            BEGIN SELECT RAISE(ABORT, 'decision failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="decision failed"):
        service.approve(draft.id)
    assert JobStore(path).get_skill_proposal(draft.id) == draft
    assert [event.status for event in service.history(draft.id)] == [SkillProposalStatus.PENDING]
    assert JobStore(path).list_skills() == []
    with service.store._connect() as connection:
        connection.execute("DROP TRIGGER reject_decision")
        connection.execute("""CREATE TRIGGER reject_draft BEFORE INSERT ON skill_draft_proposals
            BEGIN SELECT RAISE(ABORT, 'draft failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="draft failed"):
        service.create("another", "Safe draft.", source_ids)
    assert JobStore(path).list_skill_proposals() == [draft]
    assert [event.status for event in service.history(draft.id)] == [SkillProposalStatus.PENDING]


def test_v19_migration_preserves_jobs_and_failed_migration_retries(tmp_path, monkeypatch):
    path = tmp_path / "suto.db"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", 19)
        old = JobStore(path)
        job = _completed_job(old, "legacy")
    with monkeypatch.context() as patch:
        patch.setattr(
            migrations, "SKILL_DRAFT_PROPOSALS",
            migrations.SKILL_DRAFT_PROPOSALS + "\nINVALID SQL;",
        )
        with pytest.raises(sqlite3.OperationalError):
            JobStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 19
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='skill_draft_proposals'"
        ).fetchone() is None
    reopened = JobStore(path)
    assert reopened.get_job(job.id).status == "completed"
    assert reopened.list_skill_proposals() == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
