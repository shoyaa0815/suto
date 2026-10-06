"""Application boundaries for public automation jobs and schedules."""

from pathlib import Path
from typing import TYPE_CHECKING

from application.runtime_configuration import RuntimeSettings, load_runtime_settings

from workflows.library.definitions import automation_options, load_definition_file, reject_detectable_secrets, validate_name, validate_workspace
from workflows.models import ApprovalRequest, Automation, AutomationVersion, Job, JobResult, JobStatus, MissedRunPolicy, Schedule, ScheduleKind, TriggerEvent
from workflows.errors import ErrorCode, WorkflowError, tag_error
from workflows.runtime.checkpoints import checkpoint_failure
from workflows.runtime.scheduler import Scheduler
from workflows.storage.store import JobStore

if TYPE_CHECKING:
    from workflows.runtime.worker import AutomationWorker


class JobService:
    def __init__(self, store: JobStore, worker: "AutomationWorker | None" = None,
                 *, runtime: RuntimeSettings | None = None) -> None:
        self.store = store
        self.worker = worker
        self.runtime = runtime or load_runtime_settings()

    @property
    def worker_ready(self) -> bool:
        if self.worker is not None:
            return self.worker.ready
        return self.store.diagnostics()["ready"]

    def list_recent(self) -> list[Job]:
        return self.store.list_jobs()

    def get(self, job_id: str) -> Job | None:
        return self.store.get_job(job_id)

    def result(self, job_id: str) -> JobResult | None:
        return self.store.get_job_result(job_id)

    def schedule_trigger_id(self, job: Job) -> int | None:
        return self.store.get_job_trigger_id(job.id) if job.source == "schedule" else None

    def automation_version(self, job: Job) -> int | None:
        version_id = self.store.get_job_automation_version_id(job.id)
        if version_id is None:
            return None
        version = self.store.get_automation_version(version_id)
        return version.version if version is not None else None

    def submit(
        self, prompt: str, *, workspace: str | Path | None = None,
        allow_write: bool = False, allow_command: bool = False,
        source: str = "cli",
    ) -> Job:
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError(ErrorCode.INVALID_INPUT, "/run requires a task")
        try:
            reject_detectable_secrets(prompt, field="job prompt")
        except ValueError as error:
            raise tag_error(error, ErrorCode.INVALID_INPUT)
        path = validate_workspace(self.runtime.workspace if workspace is None else workspace)
        job = self.store.create_job(
            prompt.strip(), source=source, workspace=str(path),
            allow_write=allow_write, allow_command=allow_command,
        )
        if self.worker is not None:
            self.worker.wake()
        return job

    def cancel(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status == JobStatus.CANCELLED:
            return job
        if self.worker is not None:
            self.worker.request_cancel(job_id)
        else:
            self.store.cancel_job(job_id)
        current = self._require_job(job_id)
        if current.status != JobStatus.CANCELLED:
            raise WorkflowError(ErrorCode.JOB_STATE_CONFLICT, f"job cannot be cancelled from {current.status.value}")
        return current

    def resume(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status not in {JobStatus.INTERRUPTED, JobStatus.BLOCKED}:
            raise ValueError(f"job cannot be resumed from {job.status.value}")
        if error := checkpoint_failure(self.store, job, resuming=True):
            raise error
        if self.worker is not None:
            resumed = self.worker.resume(job_id)
        else:
            resumed = self.store.resume_job(job_id)
        if not resumed:
            current = self._require_job(job_id)
            raise ValueError(f"job cannot be resumed from {current.status.value}")
        return self._require_job(job_id)

    def _require_job(self, job_id: str) -> Job:
        job = self.store.get_job(job_id)
        if job is None:
            raise WorkflowError(ErrorCode.JOB_NOT_FOUND, f"Job not found: {job_id}")
        return job


class ApprovalService:
    def __init__(self, store: JobStore, worker: "AutomationWorker | None" = None) -> None:
        self.store = store
        self.worker = worker

    def list_pending(self) -> list[ApprovalRequest]:
        return self.store.list_pending_approvals()

    def get(self, approval_id: str) -> tuple[ApprovalRequest, Job]:
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise ValueError("Approval not found.")
        job = self.store.get_job(approval.job_id)
        if job is None:
            raise ValueError(f"Job not found: {approval.job_id}")
        return approval, job

    def decide(self, approval_id: str, approve: bool) -> tuple[ApprovalRequest, Job, bool, str]:
        approval, _ = self.get(approval_id)
        if self.worker is not None:
            method = self.worker.approve if approve else self.worker.reject
            decided, message = method(approval.job_id, actor="cli", approval_id=approval_id)
        else:
            decided, message = self.store.decide_approval(
                approval.job_id, approve, actor="cli", approval_id=approval_id,
            )
        current, job = self.get(approval_id)
        return current, job, decided, message


class ScheduleService:
    def __init__(self, store: JobStore, worker: "AutomationWorker | None" = None,
                 *, runtime: RuntimeSettings | None = None) -> None:
        self.store = store
        self.worker = worker
        self.runtime = runtime or load_runtime_settings()

    def create(
        self, *, kind: ScheduleKind, expression: str, prompt: str,
        timezone: str | None = None, workspace: str | Path | None = None,
        allow_write: bool = False, allow_command: bool = False,
        missed_run_policy: MissedRunPolicy = MissedRunPolicy.RUN_ONCE,
        retry_limit: int = 0, retry_delay_seconds: int = 60,
    ) -> Schedule:
        schedule = Scheduler(self.store).create(
            kind=kind, expression=expression, prompt=prompt,
            timezone=self.runtime.timezone if timezone is None else timezone,
            workspace=Path(self.runtime.workspace if workspace is None else workspace).expanduser(),
            allow_write=allow_write, allow_command=allow_command,
            missed_run_policy=missed_run_policy, retry_limit=retry_limit,
            retry_delay_seconds=retry_delay_seconds,
        )
        if self.worker is not None:
            self.worker.wake()
        return schedule

    def create_automation(
        self, *, automation_name: str, parameters: dict | None,
        kind: ScheduleKind, expression: str, timezone: str | None = None,
        missed_run_policy: MissedRunPolicy = MissedRunPolicy.RUN_ONCE,
        retry_limit: int = 0, retry_delay_seconds: int = 60,
    ) -> Schedule:
        schedule = Scheduler(self.store).create_automation(
            automation_name=automation_name, parameters=parameters,
            kind=kind, expression=expression,
            timezone=self.runtime.timezone if timezone is None else timezone,
            missed_run_policy=missed_run_policy, retry_limit=retry_limit,
            retry_delay_seconds=retry_delay_seconds,
        )
        if self.worker is not None:
            self.worker.wake()
        return schedule

    def upgrade_automation(
        self, schedule_id: str, target_version: str | int,
        parameters: dict | None = None,
    ) -> Schedule:
        schedule = self.store.upgrade_automation_schedule(
            schedule_id, target_version, parameters,
        )
        if self.worker is not None:
            self.worker.wake()
        return schedule

    def list_recent(self) -> list[Schedule]:
        return self.store.list_schedules()

    def get(self, schedule_id: str) -> Schedule:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            raise WorkflowError(ErrorCode.SCHEDULE_NOT_FOUND, f"Schedule not found: {schedule_id}")
        return schedule

    def history(self, schedule_id: str) -> list[TriggerEvent]:
        self.get(schedule_id)
        return self.store.list_trigger_history(schedule_id)

    def set_paused(self, schedule_id: str, paused: bool) -> Schedule:
        schedule = self.get(schedule_id)
        if schedule.enabled != paused:
            return schedule
        if not self.store.set_schedule_enabled(schedule_id, not paused):
            raise ValueError(f"Schedule not found: {schedule_id}")
        if not paused and self.worker is not None:
            self.worker.wake()
        return self.get(schedule_id)

    @staticmethod
    def state(schedule: Schedule) -> str:
        if not schedule.enabled:
            return "paused"
        return "exhausted" if schedule.next_run_at is None else "active"


class AutomationService:
    def __init__(self, store: JobStore, worker: "AutomationWorker | None" = None) -> None:
        self.store = store
        self.worker = worker

    def create(self, definition_file: str | Path) -> Automation:
        try:
            return self.store.create_automation(**automation_options(load_definition_file(definition_file)))
        except ValueError as error:
            if not hasattr(error, "error_code"):
                tag_error(error, ErrorCode.INVALID_INPUT)
            raise

    def update(self, name: str, definition_file: str | Path) -> AutomationVersion:
        try:
            name = validate_name(name, "automation name")
            options = automation_options(load_definition_file(definition_file), default_name=name)
            if validate_name(options.pop("name"), "automation name") != name:
                raise WorkflowError(ErrorCode.INVALID_INPUT, "definition name does not match automation name")
            return self.store.revise_automation(name, **options)
        except ValueError as error:
            if not hasattr(error, "error_code"):
                tag_error(error, ErrorCode.INVALID_INPUT)
            raise

    def list_recent(self) -> list[Automation]:
        return self.store.list_automations()

    def show(self, name: str) -> tuple[Automation, AutomationVersion, list[str]]:
        automation = self.store.get_automation(name)
        if automation is None:
            raise WorkflowError(ErrorCode.AUTOMATION_NOT_FOUND, f"automation not found: {name}")
        version = self.store.get_current_automation_version(automation.id)
        if version is None:
            raise WorkflowError(ErrorCode.AUTOMATION_VERSION_NOT_FOUND, f"automation version not found: {name}")
        skills = [skill for skill, _ in self.store.list_automation_skill_versions(version.id)]
        return automation, version, skills

    def run(self, name: str, parameters: dict | None = None) -> tuple[Job, AutomationVersion]:
        automation = self.store.get_automation(name)
        if automation is not None:
            name = automation.name
        job, version = self.store.create_automation_job(name, parameters)
        if self.worker is not None:
            self.worker.wake()
        return job, version

    def history(self, name: str) -> list[tuple[Job, int | None]]:
        automation, _, _ = self.show(name)
        history = []
        for job in self.store.list_automation_jobs(automation.id):
            version = self.store.get_automation_version(job.source_ref)
            history.append((job, version.version if version is not None else None))
        return history
