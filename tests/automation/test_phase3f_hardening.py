"""Phase 3F privacy, retention, and lifecycle regression boundaries."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from application.skill_proposals import SkillProposalService
from workflows.models import SkillProposalStatus
from workflows.storage import migrations
from workflows.storage.store import JobStore


def _completed(store, prompt):
    job = store.create_job(prompt)
    assert store.claim_next_job().id == job.id
    assert store.complete_job(job.id, "private raw result", 0, 0)
    return job.id


def _draft(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    sources = (_completed(store, "first private prompt"),
               _completed(store, "second private prompt"))
    return SkillProposalService(store), sources, path


def test_notifications_store_metadata_and_expire_at_retention_boundary(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    first = _completed(store, "prompt secret=private-one")
    second = _completed(store, "prompt secret=private-two")
    events = list(reversed(store.notifications()))
    assert [event["job_id"] for event in events] == [first, second]
    assert set(events[0]) == {"id", "job_id", "status", "created_at", "read_at", "kind"}
    assert store.acknowledge_notification(events[0]["id"])
    assert store.cleanup(days=1, dry_run=False)["rows"]["notifications"] == 0
    assert [event["id"] for event in store.notifications()] == [events[1]["id"]]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET finished_at='2000-01-01T00:00:00+00:00'")
        db.execute("UPDATE notifications SET created_at='2000-01-01T00:00:00+00:00'")
        stored = json.dumps(db.execute("SELECT * FROM notifications").fetchall())
        assert "private" not in stored
    reopened = JobStore(path)
    assert [event["id"] for event in reopened.notifications()] == [events[1]["id"]]
    assert store.cleanup(days=1)["rows"]["notifications"] == 2
    assert store.cleanup(days=1, dry_run=False)["rows"]["notifications"] == 2
    assert reopened.notifications(unread_only=False) == []
    assert not reopened.acknowledge_notification(events[1]["id"])


def test_proposal_keeps_only_review_content_and_delete_scrubs_it(tmp_path):
    service, sources, path = _draft(tmp_path)
    with pytest.raises(ValueError, match="detectable secret"):
        service.create("unsafe", "Use api_key=sk-secretvalue123", sources)
    draft = service.create("review", "Summarize the completed work.", sources)
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT * FROM skill_draft_proposals WHERE id=?", (draft.id,)).fetchone()
        assert len(row) == 7
        stored = json.dumps(row)
        assert "private raw result" not in stored
        assert "private prompt" not in stored
        assert "sk-secretvalue123" not in stored
    assert service.reject(draft.id).status == SkillProposalStatus.REJECTED
    assert service.delete(draft.id).status == SkillProposalStatus.DELETED
    reopened = JobStore(path)
    assert reopened.get_skill_proposal(draft.id).source_job_ids == ()
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name,instructions,source_job_ids FROM skill_draft_proposals WHERE id=?",
            (draft.id,),
        ).fetchone() == ("", "", "[]")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("corruption", ["duplicate", "missing", "invalid_json"])
def test_corrupt_provenance_cannot_publish_or_leak_raw_data(tmp_path, corruption):
    service, sources, path = _draft(tmp_path)
    draft = service.create("review", "Check the result.", sources)
    raw = {
        "duplicate": json.dumps([sources[0], sources[0]]),
        "missing": json.dumps([sources[0], "missing"]),
        "invalid_json": "private raw data",
    }[corruption]
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("DROP TRIGGER skill_draft_proposals_transition")
        db.execute("UPDATE skill_draft_proposals SET source_job_ids=? WHERE id=?", (raw, draft.id))
    history_before = service.history(draft.id)
    with pytest.raises(ValueError, match="invalid skill proposal") as error:
        service.approve(draft.id)
    assert raw not in str(error.value)
    reopened = JobStore(path)
    assert reopened.list_skills() == []
    assert reopened.list_skill_proposal_events(draft.id) == history_before
    if corruption != "invalid_json":
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_returns_its_committed_state_if_delete_follows_immediately(
    tmp_path, monkeypatch, decision
):
    service, sources, path = _draft(tmp_path)
    draft = service.create("review", "Check the result.", sources)
    # The first service commits, then another connection deletes before it returns.
    committed = threading.Event()
    resume = threading.Event()
    original_connect = service.store._connect

    @contextmanager
    def pause_after_commit():
        with original_connect() as connection:
            yield connection
        if not committed.is_set():
            committed.set()
            assert resume.wait(timeout=5)

    monkeypatch.setattr(service.store, "_connect", pause_after_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(getattr(service, decision), draft.id)
        try:
            assert committed.wait(timeout=5)
            SkillProposalService(JobStore(path)).delete(draft.id)
        finally:
            resume.set()
        decided = future.result(timeout=5)
    assert decided.status == {
        "approve": SkillProposalStatus.APPROVED,
        "reject": SkillProposalStatus.REJECTED,
    }[decision]
    assert JobStore(path).get_skill_proposal(draft.id).status == SkillProposalStatus.DELETED


def test_concurrent_approve_and_reject_commit_only_one_decision(tmp_path):
    service, sources, path = _draft(tmp_path)
    draft = service.create("review", "Check the result.", sources)
    gate = threading.Barrier(3)
    actors = {
        action: SkillProposalService(JobStore(path)) for action in ("approve", "reject")
    }

    def decide(action):
        gate.wait(timeout=10)
        try:
            return getattr(actors[action], action)(draft.id).status
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        approved = pool.submit(decide, "approve")
        rejected = pool.submit(decide, "reject")
        gate.wait(timeout=10)
        outcomes = [approved.result(timeout=10), rejected.result(timeout=10)]
    assert outcomes.count(None) == 1
    winner = next(status for status in outcomes if status is not None)
    reopened = JobStore(path)
    assert reopened.get_skill_proposal(draft.id).status == winner
    assert [event.status for event in reopened.list_skill_proposal_events(draft.id)] == [
        SkillProposalStatus.PENDING, winner,
    ]
    assert len(reopened.list_skills()) == (winner == SkillProposalStatus.APPROVED)


@pytest.mark.parametrize("decision", ["reject", "delete"])
def test_reject_or_delete_audit_failure_rolls_back_content_and_status(tmp_path, decision):
    service, sources, path = _draft(tmp_path)
    draft = service.create("review", "Private review notes.", sources)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER fail_review BEFORE INSERT ON skill_proposal_events
            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        getattr(service, decision)(draft.id)
    reopened = JobStore(path)
    assert reopened.get_skill_proposal(draft.id) == draft
    assert [event.status for event in reopened.list_skill_proposal_events(draft.id)] == [
        SkillProposalStatus.PENDING,
    ]


def test_concurrent_duplicate_notification_acknowledgement_commits_once(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    _completed(store, "result")
    event_id = store.notifications()[0]["id"]
    actors = [JobStore(path), JobStore(path)]
    gate = threading.Barrier(3)

    def acknowledge(actor):
        gate.wait(timeout=10)
        return actor.acknowledge_notification(event_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(acknowledge, actor) for actor in actors]
        gate.wait(timeout=10)
        assert sorted(future.result(timeout=10) for future in results) == [False, True]
    event = JobStore(path).notifications(unread_only=False)[0]
    assert event["id"] == event_id and event["read_at"] is not None
    assert JobStore(path).notifications() == []


def test_v19_notification_database_upgrades_and_supports_proposal_review(
    tmp_path, monkeypatch
):
    path = tmp_path / "suto.db"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", 19)
        legacy = JobStore(path)
        sources = (_completed(legacy, "legacy first"),
                   _completed(legacy, "legacy second"))
        notification_ids = [row["id"] for row in legacy.notifications()]
    upgraded = JobStore(path)
    assert [row["id"] for row in upgraded.notifications()] == notification_ids
    proposal = SkillProposalService(upgraded).create(
        "legacy-review", "Check both results.", sources
    )
    assert SkillProposalService(JobStore(path)).approve(proposal.id).status == SkillProposalStatus.APPROVED
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
