"""Conservative, metadata-only repeated workflow proposal detection (Phase 3G)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import threading

import pytest

from application.skill_proposals import SkillProposalService
from workflows.models import SkillProposalStatus
from workflows.storage.store import JobStore


def _automation_runs(tmp_path, *, count=3, one_day=False):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    store.create_automation(
        "daily-review", "Review CONFIDENTIAL customer note {{note}}",
        tmp_path, {"note": {"type": "string", "required": True}},
        description="Review private customer information.",
    )
    jobs = []
    for index in range(count):
        job, _ = store.create_automation_job(
            "daily-review", {"note": f"CONFIDENTIAL customer account {index}"},
        )
        assert store.claim_next_job().id == job.id
        assert store.complete_job(job.id, f"PRIVATE RESULT {index}", 1, 1)
        jobs.append(job)
    base = datetime(2026, 8, 1, 12, tzinfo=UTC)
    with store._connect() as connection:
        for index, job in enumerate(jobs):
            day = base if one_day else base + timedelta(days=index % 2)
            completed = day + timedelta(minutes=index)
            connection.execute(
                "UPDATE jobs SET finished_at=? WHERE id=?",
                (completed.isoformat(), job.id),
            )
    return store, path, jobs


def test_detection_requires_three_completed_runs_across_two_utc_dates(tmp_path):
    store, _, jobs = _automation_runs(tmp_path, count=2, one_day=True)
    assert SkillProposalService(store).detect_repeated() == []

    third, _ = store.create_automation_job(
        "daily-review", {"note": "CONFIDENTIAL customer account 3"},
    )
    assert store.claim_next_job().id == third.id
    store.complete_job(third.id, "private result", 1, 1)
    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET finished_at=? WHERE id=?",
            ("2026-08-01T13:00:00+00:00", third.id),
        )
    assert SkillProposalService(store).detect_repeated() == []
    assert store.list_skill_proposals() == []
    assert len(jobs) == 2


def test_detection_does_not_combine_runs_from_different_automation_versions(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_automation("daily-review", "Review {{item}}", tmp_path,
                            {"item": {"type": "string", "required": True}})
    jobs = []
    for index in range(2):
        job, _ = store.create_automation_job("daily-review", {"item": str(index)})
        assert store.claim_next_job().id == job.id
        store.complete_job(job.id, "done", 0, 0)
        jobs.append(job)
    store.revise_automation("daily-review", "Review changed {{item}}", tmp_path,
                            {"item": {"type": "string", "required": True}})
    job, _ = store.create_automation_job("daily-review", {"item": "third"})
    assert store.claim_next_job().id == job.id
    store.complete_job(job.id, "done", 0, 0)
    jobs.append(job)
    with store._connect() as connection:
        for index, completed_job in enumerate(jobs):
            connection.execute(
                "UPDATE jobs SET finished_at=? WHERE id=?",
                (f"2026-08-0{index + 1}T12:00:00+00:00", completed_job.id),
            )
    assert SkillProposalService(store).detect_repeated() == []
    assert store.list_skill_proposals() == []


def test_detection_creates_only_pending_review_proposal_and_minimal_provenance(tmp_path):
    store, path, jobs = _automation_runs(tmp_path)
    detected = SkillProposalService(store).detect_repeated()
    assert len(detected) == 1
    proposal, created, observed = detected[0]
    assert created is True
    assert observed == 3
    assert proposal.status == SkillProposalStatus.PENDING
    assert len(proposal.source_job_ids) == 3
    assert set(proposal.source_job_ids) == {job.id for job in jobs}
    assert "at least three" in proposal.instructions and "at least two UTC dates" in proposal.instructions
    assert "Keep work within the access provided to the current job" in proposal.instructions
    assert store.list_skills() == []
    assert all(not job.allow_write and not job.allow_command for job in jobs)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT name,instructions,source_job_ids,detection_key "
            "FROM skill_draft_proposals WHERE id=?", (proposal.id,),
        ).fetchone()
    assert row[3] and len(row[3]) == 64
    serialized = json.dumps(row)
    for forbidden in (
        "CONFIDENTIAL customer", "PRIVATE RESULT", "private customer information",
        "Review CONFIDENTIAL customer note", "account 0",
    ):
        assert forbidden not in serialized
    assert len(json.loads(row[2])) == 3


def test_detection_is_deduplicated_across_restart_and_review_states(tmp_path):
    store, path, _ = _automation_runs(tmp_path)
    service = SkillProposalService(store)
    first, created, _ = service.detect_repeated()[0]
    assert created
    reopened = SkillProposalService(JobStore(path))
    duplicate, created_again, _ = reopened.detect_repeated()[0]
    assert not created_again
    assert duplicate.id == first.id
    assert len(reopened.list()) == 1

    reviewed = reopened.reject(first.id)
    again, created_after_rejection, _ = reopened.detect_repeated()[0]
    assert reviewed.status == SkillProposalStatus.REJECTED
    assert not created_after_rejection
    assert again.id == first.id
    assert len(reopened.list()) == 1
    tombstone = reopened.delete(first.id)
    after_delete, created_after_delete, _ = reopened.detect_repeated()[0]
    assert tombstone.status == SkillProposalStatus.DELETED
    assert after_delete.id == first.id
    assert not created_after_delete
    with sqlite3.connect(path) as connection:
        retained_key, source_ids = connection.execute(
            "SELECT detection_key,source_job_ids FROM skill_draft_proposals WHERE id=?",
            (first.id,),
        ).fetchone()
    assert retained_key and json.loads(source_ids) == []


def test_concurrent_detection_creates_one_proposal(tmp_path):
    store, path, _ = _automation_runs(tmp_path)
    other_store = JobStore(path)
    barrier = threading.Barrier(2)
    stores = iter((store, other_store))
    stores_lock = threading.Lock()

    def detect():
        with stores_lock:
            current_store = next(stores)
        service = SkillProposalService(current_store)
        barrier.wait(timeout=5)
        return service.detect_repeated()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: detect(), range(2)))
    assert all(len(result) == 1 for result in results)
    assert sum(result[0][1] for result in results) == 1
    assert results[0][0][0].id == results[1][0][0].id
    assert len(JobStore(path).list_skill_proposals()) == 1


def test_proposal_key_cannot_be_changed_and_detection_never_publishes_skill(tmp_path):
    store, path, _ = _automation_runs(tmp_path)
    proposal, _, _ = SkillProposalService(store).detect_repeated()[0]
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="invalid skill proposal transition"):
            connection.execute(
                "UPDATE skill_draft_proposals SET detection_key=? WHERE id=?",
                ("f" * 64, proposal.id),
            )
    reopened = JobStore(path)
    assert reopened.get_skill(proposal.name) is None
    assert reopened.get_skill_proposal(proposal.id).status == SkillProposalStatus.PENDING
    assert reopened.get_job(proposal.source_job_ids[0]).allow_write is False
    assert reopened.get_job(proposal.source_job_ids[0]).allow_command is False
