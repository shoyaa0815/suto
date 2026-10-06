"""Durable Runtime API: HTTP validation and presentation over application services."""

import json
from dataclasses import asdict

from aiohttp import web

from application.automation import AutomationService, JobService, ScheduleService
from workflows.errors import ErrorCode, SAFE_MESSAGES, WorkflowError, error_code_for_exception, normalize_error_code
from workflows.storage.redaction import redact_value


def _error(code: ErrorCode, status: int) -> web.Response:
    # Keep the existing API's string error field and add a stable workflow code.
    return web.json_response({"error": SAFE_MESSAGES[code], "error_code": code.value}, status=status)


@web.middleware
async def runtime_errors(request: web.Request, handler):
    if request.path.split("/", 2)[1] not in {"jobs", "automations", "schedules"}:
        return await handler(request)
    try:
        response = await handler(request)
    except web.HTTPException as error:
        code = ErrorCode.PERMISSION_DENIED if error.status == 403 else ErrorCode.INVALID_INPUT
        response = _error(code, error.status)
    except Exception as error:
        code = error_code_for_exception(error)
        status = {
            ErrorCode.JOB_NOT_FOUND: 404,
            ErrorCode.AUTOMATION_NOT_FOUND: 404,
            ErrorCode.AUTOMATION_VERSION_NOT_FOUND: 404,
            ErrorCode.SCHEDULE_NOT_FOUND: 404,
            ErrorCode.JOB_STATE_CONFLICT: 409,
            ErrorCode.WORKSPACE_PERMISSION_DENIED: 403,
            ErrorCode.QUOTA_EXCEEDED: 429,
            ErrorCode.WORKER_UNAVAILABLE: 503,
            ErrorCode.INTERNAL_ERROR: 500,
        }.get(code, 400)
        response = _error(code, status)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })
    return response


def _invalid() -> WorkflowError:
    return WorkflowError(ErrorCode.INVALID_INPUT, "Invalid request")


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


async def _body(request: web.Request, allowed: set[str]) -> dict:
    try:
        body = await request.json(loads=lambda value: json.loads(value, parse_constant=_reject_constant))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(body, dict) or body.keys() - allowed:
        raise _invalid()
    return body


def _job_json(job) -> dict:
    data = asdict(job)
    # Legacy jobs can contain diagnostic error text; only safe messages are public.
    data["error"] = (
        SAFE_MESSAGES[normalize_error_code(job.error_code)]
        if job.error or job.error_code else None
    )
    return redact_value(data)


def add_runtime_routes(
    app: web.Application, *, jobs: JobService,
    automations: AutomationService, schedules: ScheduleService,
) -> None:
    """Receive services only; this adapter never owns a worker or queries storage."""
    def require_job(request):
        job = jobs.get(request.match_info["job_id"])
        if job is None:
            raise WorkflowError(ErrorCode.JOB_NOT_FOUND, "Job not found")
        return job

    async def submit_job(request):
        body = await _body(request, {"prompt", "workspace", "allow_write", "allow_command"})
        prompt = body.get("prompt")
        workspace = body.get("workspace")
        if (
            not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000
            or ("workspace" in body and (not isinstance(workspace, str) or not workspace.strip()
                or len(workspace) > 4096 or "\x00" in workspace))
            or any(type(body.get(key, False)) is not bool for key in ("allow_write", "allow_command"))
        ):
            raise _invalid()
        job = jobs.submit(
            prompt, workspace=workspace,
            allow_write=body.get("allow_write", False),
            allow_command=body.get("allow_command", False), source="api",
        )
        return web.json_response(_job_json(job), status=202)

    async def list_jobs(request):
        return web.json_response({"jobs": [_job_json(job) for job in jobs.list_recent()]})

    async def get_job(request):
        return web.json_response(_job_json(require_job(request)))

    async def get_result(request):
        result = jobs.result(request.match_info["job_id"])
        if result is None:
            raise WorkflowError(ErrorCode.JOB_NOT_FOUND, "Job not found")
        return web.json_response(asdict(result))

    async def cancel_job(request):
        await _body(request, set())
        return web.json_response(_job_json(jobs.cancel(request.match_info["job_id"])), status=202)

    async def list_automations(request):
        return web.json_response({"automations": [asdict(item) for item in automations.list_recent()]})

    async def get_automation(request):
        automation, version, skills = automations.show(request.match_info["automation_id"])
        return web.json_response(redact_value({
            **asdict(automation), "version": asdict(version), "skills": skills,
        }))

    async def run_automation(request):
        body = await _body(request, {"parameters"})
        if "parameters" in body and not isinstance(body["parameters"], dict):
            raise WorkflowError(ErrorCode.INVALID_PARAMETER, "parameters must be an object")
        job, _ = automations.run(request.match_info["automation_id"], body.get("parameters"))
        return web.json_response(_job_json(job), status=202)

    async def list_schedules(request):
        return web.json_response({"schedules": [redact_value(asdict(item)) for item in schedules.list_recent()]})

    async def get_schedule(request):
        return web.json_response(redact_value(asdict(schedules.get(request.match_info["schedule_id"]))))

    app.router.add_post("/jobs", submit_job)
    app.router.add_get("/jobs", list_jobs)
    app.router.add_get("/jobs/{job_id}", get_job)
    app.router.add_get("/jobs/{job_id}/result", get_result)
    app.router.add_post("/jobs/{job_id}/cancel", cancel_job)
    app.router.add_get("/automations", list_automations)
    app.router.add_get("/automations/{automation_id}", get_automation)
    app.router.add_post("/automations/{automation_id}/run", run_automation)
    app.router.add_get("/schedules", list_schedules)
    app.router.add_get("/schedules/{schedule_id}", get_schedule)
