"""Application boundary for reviewing and deciding draft Skill proposals."""

from workflows.models import SkillDraftProposal, SkillProposalEvent, SkillProposalStatus
from workflows.storage.store import JobStore


class SkillProposalService:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def create(
        self, name: str, instructions: str, source_job_ids: tuple[str, ...]
    ) -> SkillDraftProposal:
        return self.store.create_skill_proposal(name, instructions, source_job_ids)

    def get(self, proposal_id: str) -> SkillDraftProposal | None:
        return self.store.get_skill_proposal(proposal_id)

    def list(self, status: SkillProposalStatus | None = None) -> list[SkillDraftProposal]:
        return self.store.list_skill_proposals(status)

    def history(self, proposal_id: str) -> list[SkillProposalEvent]:
        return self.store.list_skill_proposal_events(proposal_id)

    def approve(self, proposal_id: str) -> SkillDraftProposal:
        return self.store.approve_skill_proposal(proposal_id)

    def reject(self, proposal_id: str) -> SkillDraftProposal:
        return self.store.transition_skill_proposal(proposal_id, SkillProposalStatus.REJECTED)

    def delete(self, proposal_id: str) -> SkillDraftProposal:
        return self.store.transition_skill_proposal(proposal_id, SkillProposalStatus.DELETED)
