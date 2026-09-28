import pytest

from workflows.storage.store import JobStore


def test_existing_workspace_job_moves_to_agent_without_losing_grants(tmp_path):
    database = tmp_path / "suto.db"
    store = JobStore(database)
    job = store.create_job(
        "inspect workspace",
        workspace=str(tmp_path),
        allow_write=True,
        allow_command=True,
    )
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET mode='developer' WHERE id=?", (job.id,))
        connection.execute("PRAGMA user_version=10")

    upgraded = JobStore(database).get_job(job.id)

    assert upgraded.mode == "agent"
    assert upgraded.allow_write is True
    assert upgraded.allow_command is True
    assert upgraded.status == job.status


def test_new_jobs_accept_only_agent_mode(tmp_path):
    store = JobStore(tmp_path / "suto.db")

    job = store.create_job("inspect")

    assert job.mode == "agent"
    with pytest.raises(ValueError, match="only agent mode"):
        store.create_job("inspect", mode="developer")
