"""Phase 3H retention preserves delivery, audit, and deduplication state."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from application.skill_proposals import SkillProposalService
from workflows.storage import operations
from workflows.storage import migrations
from workflows.storage import skill_proposals
from workflows.storage.store import JobStore


class _FrozenDateTime(datetime):
    now_value = datetime(2026, 10, 1, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        value = cls.now_value
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _reset_clock():
    _FrozenDateTime.now_value = datetime(2026, 10, 1, tzinfo=UTC)


def _completed(store, prompt):
    job = store.create_job(prompt)
    assert store.claim_next_job().id == job.id
    assert store.complete_job(job.id, "done", 0, 0)
    return job


def test_cleanup_expiry_boundary_dry_run_and_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "datetime", _FrozenDateTime)
    store = JobStore(tmp_path / "retention.db")
    old = _completed(store, "old")
    boundary = _completed(store, "boundary")
    unread_old = _completed(store, "unread old")
    notices = {row["job_id"]: row for row in store.notifications(unread_only=False)}
    old_notice, boundary_notice = notices[old.id], notices[boundary.id]
    assert store.acknowledge_notification(old_notice["id"])
    assert store.acknowledge_notification(boundary_notice["id"])
    cutoff = (_FrozenDateTime.now_value - timedelta(days=30)).isoformat()
    with store._connect() as db:
        db.execute("UPDATE jobs SET finished_at=? WHERE id=?", (cutoff, boundary.id))
        db.execute("UPDATE notifications SET created_at=? WHERE id=?", (cutoff, boundary_notice["id"]))
        old_stamp = ( _FrozenDateTime.now_value - timedelta(days=31)).isoformat()
        db.execute("UPDATE jobs SET finished_at=? WHERE id=?", (old_stamp, old.id))
        db.execute("UPDATE notifications SET created_at=? WHERE id=?", (old_stamp, old_notice["id"]))
        db.execute("UPDATE jobs SET finished_at=? WHERE id=?", (old_stamp, unread_old.id))
        db.execute("UPDATE notifications SET created_at=? WHERE id=?", (old_stamp, notices[unread_old.id]["id"]))

    preview = store.cleanup(30, dry_run=True)
    assert preview["rows"]["notifications"] == 1
    assert len(store.notifications(unread_only=False)) == 3
    applied = JobStore(store.path).cleanup(30, dry_run=False)
    assert applied["rows"]["notifications"] == 1
    assert {item["id"] for item in store.notifications(unread_only=False)} == {
        boundary_notice["id"], notices[unread_old.id]["id"],
    }
    assert len(store.notifications()) == 1
    assert JobStore(store.path).cleanup(30, dry_run=False)["rows"]["notifications"] == 0


def test_cleanup_redacts_only_expired_rejected_and_keeps_dedup_tombstone(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "datetime", _FrozenDateTime)
    monkeypatch.setattr(skill_proposals, "datetime", _FrozenDateTime)
    store = JobStore(tmp_path / "proposals.db")
    sources = (_completed(store, "one").id, _completed(store, "two").id)
    service = SkillProposalService(store)
    pending = service.create("pending", "Pending private text", sources)
    approved = service.create("approved", "Approved private text", sources)
    approved = service.approve(approved.id)
    approved_skill = store.get_skill("approved")
    approved_version = store.get_current_skill_version(approved_skill.id)
    rejected = service.create("rejected", "Rejected private text", sources)
    rejected = service.reject(rejected.id)
    deleted = service.create("deleted", "Already cleared", sources)
    deleted = service.delete(deleted.id)
    _FrozenDateTime.now_value += timedelta(days=1)
    boundary_rejected = service.reject(service.create("boundary", "Boundary text", sources).id)
    _FrozenDateTime.now_value += timedelta(days=30)

    # A detected proposal exercises the Phase 3G uniqueness hash through cleanup.
    store.create_automation("repeat", "Do {{item}}", tmp_path,
                            {"item": {"type": "string", "required": True}})
    for day in range(2):
        for n in range(2 if day == 0 else 1):
            job, _ = store.create_automation_job("repeat", {"item": str(day * 2 + n)})
            assert store.claim_next_job().id == job.id
            assert store.complete_job(job.id, "done", 0, 0)
            with store._connect() as db:
                db.execute("UPDATE jobs SET finished_at=? WHERE id=?",
                           (f"2026-08-{1 + day:02d}T12:00:00+00:00", job.id))
    _FrozenDateTime.now_value -= timedelta(days=31)
    detected, created, _ = service.detect_repeated()[0]
    assert created
    assert service.reject(detected.id).status.value == "rejected"
    _FrozenDateTime.now_value += timedelta(days=31)

    result = store.cleanup(30, dry_run=True)
    assert result["rows"]["skill_proposals_rejected"] == 2
    assert store.get_skill_proposal(rejected.id).instructions == "Rejected private text"
    applied = JobStore(store.path).cleanup(30, dry_run=False)
    assert applied["rows"]["skill_proposals_rejected"] == 2
    reopened = JobStore(store.path)
    assert reopened.get_skill_proposal(pending.id).instructions == "Pending private text"
    assert reopened.get_skill_proposal(approved.id) == approved
    assert reopened.get_skill("approved") == approved_skill
    assert reopened.get_current_skill_version(approved_skill.id) == approved_version
    redacted = reopened.get_skill_proposal(rejected.id)
    assert (redacted.status.value, redacted.name, redacted.instructions, redacted.source_job_ids) == (
        "rejected", "", "", (),
    )
    assert reopened.get_skill_proposal(deleted.id) == deleted
    assert reopened.get_skill_proposal(boundary_rejected.id).instructions == "Boundary text"
    assert [event.status.value for event in reopened.list_skill_proposal_events(rejected.id)] == [
        "pending", "rejected",
    ]
    duplicate, was_created, _ = SkillProposalService(reopened).detect_repeated()[0]
    assert not was_created and duplicate.id == detected.id
    assert reopened.cleanup(30, dry_run=False)["rows"]["skill_proposals_rejected"] == 0
    with sqlite3.connect(store.path) as db:
        name, instructions, source_ids, detection_key = db.execute(
            "SELECT name,instructions,source_job_ids,detection_key "
            "FROM skill_draft_proposals WHERE id=?", (detected.id,),
        ).fetchone()
    assert (name, instructions, source_ids) == ("", "", "[]")
    assert len(detection_key) == 64


def test_cleanup_concurrent_is_serialized_idempotent_and_restart_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "datetime", _FrozenDateTime)
    monkeypatch.setattr(skill_proposals, "datetime", _FrozenDateTime)
    store = JobStore(tmp_path / "concurrent.db")
    sources = (_completed(store, "one").id, _completed(store, "two").id)
    draft = SkillProposalService(store).reject(
        SkillProposalService(store).create("old", "Private", sources).id
    )
    _FrozenDateTime.now_value += timedelta(days=60)
    other = JobStore(store.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda candidate: candidate.cleanup(30, dry_run=False), (store, other)))
    assert sum(result["rows"]["skill_proposals_rejected"] for result in results) == 1
    assert JobStore(store.path).get_skill_proposal(draft.id).instructions == ""


def test_cleanup_rolls_back_notifications_and_proposal_redaction_together(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "datetime", _FrozenDateTime)
    monkeypatch.setattr(skill_proposals, "datetime", _FrozenDateTime)
    store = JobStore(tmp_path / "rollback.db")
    job = _completed(store, "notice")
    notice = store.notifications()[0]
    store.acknowledge_notification(notice["id"])
    sources = (_completed(store, "one").id, _completed(store, "two").id)
    rejected = SkillProposalService(store).reject(
        SkillProposalService(store).create("rejected", "Sensitive", sources).id
    )
    _FrozenDateTime.now_value += timedelta(days=60)
    old = (_FrozenDateTime.now_value - timedelta(days=60)).isoformat()
    with store._connect() as db:
        db.execute("UPDATE jobs SET finished_at=? WHERE id=?", (old, job.id))
        db.execute("UPDATE notifications SET created_at=? WHERE id=?", (old, notice["id"]))
        db.execute("CREATE TRIGGER reject_redaction BEFORE UPDATE OF redacted ON skill_draft_proposals "
                   "BEGIN SELECT RAISE(ABORT, 'retention rejected'); END")
    notifications_before = store.notifications(unread_only=False)
    with pytest.raises(sqlite3.IntegrityError, match="retention rejected"):
        store.cleanup(30, dry_run=False)
    assert store.notifications(unread_only=False) == notifications_before
    assert store.get_skill_proposal(rejected.id).instructions == "Sensitive"


def test_v21_database_migrates_with_unredacted_existing_proposals(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", 21)
        old = JobStore(path)
        source_ids = (_completed(old, "one").id, _completed(old, "two").id)
        proposal = SkillProposalService(old).create("legacy", "Keep this", source_ids)
    upgraded = JobStore(path)
    assert upgraded.get_skill_proposal(proposal.id).instructions == "Keep this"
    with upgraded._connect() as db:
        assert db.execute("SELECT redacted FROM skill_draft_proposals WHERE id=?",
                          (proposal.id,)).fetchone()[0] == 0
        assert db.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
