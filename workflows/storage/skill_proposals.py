"""Durable draft Skill proposals and atomic, explicit Skill publication."""

import json
import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from ..library.definitions import (
    reject_detectable_secrets,
    validate_name,
    validate_skill_instructions,
)
from ..models import SkillDraftProposal, SkillProposalEvent, SkillProposalStatus


_CAPABILITY_DIRECTIVE = re.compile(
    r"(?im)^\s*(?:allowed_tools|recommended_tools|tool_access|permissions|"
    r"allow_write|allow_command)\s*:|\b(?:bypass|disable|skip|ignore|grant|"
    r"elevate|escalate)\b[^\n]{0,80}\b(?:permissions?|approvals?|sandbox|"
    r"tool access)\b"
)


def _validate_publishable(name: str, instructions: str) -> tuple[str, str]:
    name = validate_name(name, "skill name")
    if not isinstance(instructions, str):
        raise ValueError("skill instructions must be text")
    instructions = validate_skill_instructions(instructions)
    reject_detectable_secrets(instructions, field="skill proposal instructions")
    if _CAPABILITY_DIRECTIVE.search(instructions):
        raise ValueError("skill proposal cannot request tool access or permissions")
    return name, instructions


def _proposal(row: sqlite3.Row | None) -> SkillDraftProposal | None:
    if row is None:
        return None
    try:
        status = SkillProposalStatus(row["status"])
        source_job_ids = json.loads(row["source_job_ids"])
    except (ValueError, TypeError):
        raise ValueError("invalid skill proposal data") from None
    if status == SkillProposalStatus.DELETED:
        if source_job_ids != [] or row["name"] != "" or row["instructions"] != "":
            raise ValueError("invalid skill proposal data")
    elif (not isinstance(source_job_ids, list) or
          not 2 <= len(source_job_ids) <= 50 or
          any(not isinstance(job_id, str) or not job_id for job_id in source_job_ids) or
          len(set(source_job_ids)) != len(source_job_ids)):
        raise ValueError("invalid skill proposal provenance")
    return SkillDraftProposal(
        id=row["id"], name=row["name"], instructions=row["instructions"],
        status=status, source_job_ids=tuple(source_job_ids),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class SkillProposalStore:
    def approve_skill_proposal(self, proposal_id: str) -> SkillDraftProposal:
        """Publish a new versioned Skill and record approval in one transaction."""
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM skill_draft_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ValueError("skill proposal not found")
            proposal = _proposal(row)
            if proposal.status != SkillProposalStatus.PENDING:
                raise ValueError(f"skill proposal cannot transition from {proposal.status} to approved")
            source_job_ids = proposal.source_job_ids
            rows = connection.execute(
                f"SELECT id, status, workspace FROM jobs WHERE id IN ({','.join('?' for _ in source_job_ids)})",
                source_job_ids,
            ).fetchall()
            if (len(rows) != len(source_job_ids) or
                    any(source["status"] != "completed" for source in rows) or
                    len({source["workspace"] for source in rows}) != 1):
                raise ValueError("invalid skill proposal provenance")
            name, instructions = _validate_publishable(row["name"], row["instructions"])
            if connection.execute(
                "SELECT 1 FROM skills WHERE name=? COLLATE NOCASE", (name,)
            ).fetchone():
                raise ValueError(f"skill already exists: {name}")
            self._insert_skill(connection, name, instructions, timestamp)
            connection.execute(
                "UPDATE skill_draft_proposals SET status='approved',updated_at=? WHERE id=?",
                (timestamp, proposal_id),
            )
            result = _proposal(connection.execute(
                "SELECT * FROM skill_draft_proposals WHERE id=?", (proposal_id,)
            ).fetchone())
        return result

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
            result = _proposal(connection.execute(
                "SELECT * FROM skill_draft_proposals WHERE id=?", (proposal_id,),
            ).fetchone())
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
        if status == SkillProposalStatus.APPROVED:
            raise ValueError("skill approval requires explicit publication")
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM skill_draft_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ValueError("skill proposal not found")
            try:
                current = SkillProposalStatus(row["status"])
            except ValueError:
                raise ValueError("invalid skill proposal data") from None
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
            result = _proposal(connection.execute(
                "SELECT * FROM skill_draft_proposals WHERE id=?", (proposal_id,)
            ).fetchone())
        return result
