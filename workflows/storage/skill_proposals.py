"""Durable draft Skill proposals. No method here creates or enables a Skill."""

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from ..library.definitions import (
    reject_detectable_secrets,
    validate_name,
    validate_skill_instructions,
)
from ..models import SkillDraftProposal, SkillProposalEvent, SkillProposalStatus


def _proposal(row: sqlite3.Row | None) -> SkillDraftProposal | None:
    if row is None:
        return None
    return SkillDraftProposal(
        id=row["id"], name=row["name"], instructions=row["instructions"],
        status=SkillProposalStatus(row["status"]),
        source_job_ids=tuple(json.loads(row["source_job_ids"])),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class SkillProposalStore:
    def create_skill_proposal(
        self, name: str, instructions: str, source_job_ids: tuple[str, ...]
    ) -> SkillDraftProposal:
        name = validate_name(name, "skill proposal name")
        if not isinstance(instructions, str):
            raise ValueError("skill proposal instructions must be text")
        instructions = validate_skill_instructions(instructions)
        reject_detectable_secrets(instructions, field="skill proposal instructions")
        if (not isinstance(source_job_ids, (tuple, list)) or
                len(source_job_ids) < 2 or len(source_job_ids) > 50 or
                any(not isinstance(job_id, str) or not job_id for job_id in source_job_ids) or
                len(set(source_job_ids)) != len(source_job_ids)):
            raise ValueError("skill proposal requires 2 to 50 distinct source job IDs")

        proposal_id = f"skp_{uuid4().hex}"
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT id, status, workspace FROM jobs WHERE id IN ({','.join('?' for _ in source_job_ids)})",
                source_job_ids,
            ).fetchall()
            if (len(rows) != len(source_job_ids) or
                    any(row["status"] != "completed" for row in rows) or
                    len({row["workspace"] for row in rows}) != 1):
                raise ValueError("source jobs must exist, be completed, and share a workspace")
            connection.execute(
                "INSERT INTO skill_draft_proposals "
                "(id,name,instructions,status,source_job_ids,created_at,updated_at) "
                "VALUES (?,?,?,'pending',?,?,?)",
                (proposal_id, name, instructions, json.dumps(source_job_ids), timestamp, timestamp),
            )
        result = self.get_skill_proposal(proposal_id)
        if result is None:
            raise RuntimeError("failed to create skill proposal")
        return result

    def get_skill_proposal(self, proposal_id: str) -> SkillDraftProposal | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM skill_draft_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        return _proposal(row)

    def list_skill_proposals(
        self, status: SkillProposalStatus | None = None
    ) -> list[SkillDraftProposal]:
        if status is not None:
            status = SkillProposalStatus(status)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM skill_draft_proposals WHERE (? IS NULL OR status=?) "
                "ORDER BY created_at, id",
                (status, status),
            ).fetchall()
        return [_proposal(row) for row in rows]

    def list_skill_proposal_events(self, proposal_id: str) -> list[SkillProposalEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM skill_proposal_events WHERE proposal_id=? ORDER BY id",
                (proposal_id,),
            ).fetchall()
        return [SkillProposalEvent(
            id=row["id"], proposal_id=row["proposal_id"],
            status=SkillProposalStatus(row["status"]), created_at=row["created_at"],
        ) for row in rows]

    def transition_skill_proposal(
        self, proposal_id: str, status: SkillProposalStatus
    ) -> SkillDraftProposal:
        status = SkillProposalStatus(status)
        if status == SkillProposalStatus.PENDING:
            raise ValueError("skill proposal cannot return to pending")
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM skill_draft_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"skill proposal not found: {proposal_id}")
            current = SkillProposalStatus(row["status"])
            if current == SkillProposalStatus.DELETED or (
                current != SkillProposalStatus.PENDING and status != SkillProposalStatus.DELETED
            ):
                raise ValueError(f"skill proposal cannot transition from {current} to {status}")
            if status == SkillProposalStatus.DELETED:
                connection.execute(
                    "UPDATE skill_draft_proposals SET status=?,name='',instructions='',"
                    "source_job_ids='[]',updated_at=? WHERE id=?",
                    (status, timestamp, proposal_id),
                )
            else:
                connection.execute(
                    "UPDATE skill_draft_proposals SET status=?,updated_at=? WHERE id=?",
                    (status, timestamp, proposal_id),
                )
        result = self.get_skill_proposal(proposal_id)
        if result is None:
            raise RuntimeError("failed to transition skill proposal")
        return result
