"""CLI review and explicit Skill proposal decisions."""

from application.skill_proposals import SkillProposalService
from interfaces.cli.commands import CommandContext, handle_command
from workflows.models import SkillProposalStatus
from workflows.storage.store import JobStore


def _setup(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    jobs = []
    for label in ("first", "second"):
        job = store.create_job(label, workspace=str(tmp_path))
        assert store.claim_next_job().id == job.id
        assert store.complete_job(job.id, "done", 0, 0)
        jobs.append(job.id)
    service = SkillProposalService(store)
    draft = service.create("review", "Check the result.", tuple(jobs))
    context = CommandContext(store, None, "conversation", "agent")
    return path, context, service, draft


def test_cli_review_requires_explicit_approve_to_publish(tmp_path, capsys):
    path, context, service, draft = _setup(tmp_path)
    for command in ("/skill-proposal list", f"/skill-proposal show {draft.id}",
                    f"/skill-proposal history {draft.id}"):
        assert handle_command(context, command).handled
    output = capsys.readouterr().out
    assert draft.id in output
    assert "Check the result." in output
    assert all(job_id in output for job_id in draft.source_job_ids)
    assert "pending" in output
    assert JobStore(path).list_skills() == []

    handle_command(context, f"/skill-proposal approve {draft.id}")
    assert "not activated" in capsys.readouterr().out
    assert JobStore(path).get_skill("review") is not None
    assert service.get(draft.id).status == SkillProposalStatus.APPROVED
    handle_command(context, f"/skill-proposal approve {draft.id}")
    assert "cannot transition" in capsys.readouterr().out
    assert len(JobStore(path).list_skills()) == 1


def test_cli_reject_delete_and_invalid_commands(tmp_path, capsys):
    path, context, service, draft = _setup(tmp_path)
    handle_command(context, "/skill-proposal approve")
    assert "usage:" in capsys.readouterr().out
    handle_command(context, f"/skill-proposal reject {draft.id}")
    assert "rejected" in capsys.readouterr().out
    handle_command(context, f"/skill-proposal delete {draft.id}")
    assert "deleted" in capsys.readouterr().out
    handle_command(context, f"/skill-proposal history {draft.id}")
    assert "pending" in capsys.readouterr().out
    assert JobStore(path).list_skills() == []
    assert service.get(draft.id).instructions == ""
