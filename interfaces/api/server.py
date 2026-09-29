"""Loopback HTTP/SSE adapter. Execution, permissions, and traces stay in Suto."""

import asyncio
import ipaddress
import json
import os
from dataclasses import dataclass, field
from uuid import uuid4

from aiohttp import web

from agent import AgentRequest, AgentResult
from ai import execute_local_ai
from application.configuration import load_settings
from assistant import AssistantContext
from sessions import SessionService, SessionStore
from skills import builtin_registry
from workflows.storage.redaction import redact_text
from workflows.storage.store import JobStore
from workflows.storage.runs import SessionBusyError, TERMINAL


HOST = "127.0.0.1"
PORT = 8766
INTERFACE = "api"
MAX_ACTIVE_RUNS = 4
STATE = web.AppKey("api_state", object)


@dataclass
class Run:
    id: str
    session_id: str
    status: str = "queued"
    result: AgentResult | None = None
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)


def _notify(run: Run) -> None:
    """Wake every current SSE reader without one reader clearing another's signal."""
    run.changed.set()
    run.changed = asyncio.Event()


def _result_json(result: AgentResult) -> dict:
    return {
        "session_id": result.session_id,
        "final_text": result.final_text,
        "status": result.status,
        "usage": result.usage,
        "error": result.error,
    }


def _run_json(run: Run) -> dict:
    return {
        "run_id": run.id,
        "runtime_run_id": run.id,
        "session_id": run.session_id,
        "status": run.status,
        "result": _result_json(run.result) if run.result else None,
    }


def _bad_request() -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": "Invalid request"}), content_type="application/json")


@web.middleware
async def local_guard(request: web.Request, handler):
    address = request.transport.get_extra_info("sockname") if request.transport else None
    peer = request.transport.get_extra_info("peername") if request.transport else None
    expected = f"{HOST}:{address[1]}" if address else None
    try:
        local_peer = peer is not None and ipaddress.ip_address(peer[0]).is_loopback
    except ValueError:
        local_peer = False
    if not local_peer or request.host != expected:
        raise web.HTTPForbidden(text="Local API only")
    origin = request.headers.get("Origin")
    if origin is not None and origin != f"http://{expected}":
        raise web.HTTPForbidden(text="Cross-origin requests are unavailable")
    if request.method == "POST" and (
        request.headers.get("X-Suto-Request") != "1"
        or request.content_type != "application/json"
    ):
        raise web.HTTPForbidden(text="JSON request header required")
    response = await handler(request)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })
    return response


async def _body(request: web.Request) -> tuple[str, str | None, tuple[str, ...] | None]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise _bad_request() from None
    if not isinstance(body, dict) or set(body) - {"message", "session_id", "skills"}:
        raise _bad_request()
    message = body.get("message")
    session_id = body.get("session_id")
    skills = body.get("skills")
    if (
        not isinstance(message, str) or not message.strip() or len(message) > 8000
        or (session_id is not None and (not isinstance(session_id, str) or len(session_id) > 64))
        or (skills is not None and (not isinstance(skills, list) or len(skills) > 8
        or any(not isinstance(name, str) or len(name) > 64 for name in skills)))
    ):
        raise _bad_request()
    return message.strip(), session_id, tuple(skills) if skills is not None else None


def _trace_rows(state: dict, run: Run) -> list[dict]:
    return state["store"].list_run_tree_events(run.id)


def _stored_run_json(record: dict) -> dict:
    terminal = record["status"] not in {"queued", "running"}
    result = None
    if terminal:
        result = {
            "session_id": record["session_id"], "final_text": record["final_text"] or "",
            "status": record["status"], "usage": record["usage"], "error": record["error"],
        }
    return {
        "run_id": record["id"], "runtime_run_id": record["id"],
        "session_id": record["session_id"], "parent_run_id": record["parent_run_id"],
        "status": record["status"], "created_at": record["created_at"],
        "started_at": record["started_at"], "completed_at": record["completed_at"],
        "result": result,
    }


def _public_event(row: dict) -> dict:
    return {
        "event_id": row["event_id"],
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "parent_run_id": row["parent_run_id"],
        "job_id": row["job_id"],
        "tool_call_id": row["tool_call_id"],
        "type": row["event_type"],
        "timestamp": row["created_at"],
        "data": json.loads(row["data"]),
    }


def create_app(*, database_path=None, store=None, executor=execute_local_ai):
    """Create one in-process local API; tests may inject a store or fake executor."""
    store = store or JobStore(database_path or os.environ.get("SUTO_DB_PATH", "data/suto.db"))
    store.recover_interrupted_runs()
    profile = load_settings().profile
    user = store.resolve_channel_identity(
        INTERFACE, "local", display_name=profile.display_name,
        timezone=profile.timezone, locale=profile.locale,
    )
    user = store.apply_profile_settings(
        user.id, display_name=profile.display_name,
        timezone=profile.timezone, locale=profile.locale,
    )
    state = {
        "store": store, "user": user, "sessions": SessionService(SessionStore(store)),
        "runs": {}, "active_sessions": {}, "executor": executor,
    }
    app = web.Application(middlewares=[local_guard], client_max_size=16384)
    app[STATE] = state

    async def health(request):
        return web.json_response({"status": "ok"})

    async def create_run(request):
        message, session_id, selected = await _body(request)
        if session_id is None:
            session = state["sessions"].resume(user.id, INTERFACE, uuid4().hex)
        else:
            session = store.get_conversation(session_id)
            if session is None or session.user_id != user.id or session.channel != INTERFACE:
                raise web.HTTPNotFound()
        if selected is None:
            selected = store.get_session_skills(session.id)
        try:
            builtin_registry().active(selected)
        except ValueError:
            return web.json_response({"error": "Selected Skill is unavailable; update the session selection"}, status=400)
        active = state["active_sessions"]
        if session.id in active:
            return web.json_response({"error": "Session already has an active run"}, status=409)
        if len(active) >= MAX_ACTIVE_RUNS:
            return web.json_response({"error": "Too many active runs"}, status=429)
        try:
            run = Run(store.begin_agent_run(session.id), session.id)
        except SessionBusyError:
            return web.json_response({"error": "Session already has an active run"}, status=409)
        try:
            store.set_session_skills(session.id, selected)
        except Exception:
            store.finish_agent_run(run.id, "failed", final_text="Request failed.", error="failed")
            raise
        state["runs"][run.id] = run
        active[session.id] = run.id

        async def execute():
            run.status = "running"
            try:
                store.start_agent_run(run.id)
                history = state["sessions"].before_prompt(session.id, user.id)
                state["sessions"].append(session.id, user.id, "user", message)
                agent_request = AgentRequest(
                    message, session_id=session.id, active_skills=selected, run_id=run.id
                )

                def on_event(event):
                    _notify(run)

                outcome = await state["executor"](
                    agent_request,
                    conversation_history=history,
                    assistant_context=AssistantContext(store, user.id, session.id),
                    skill_registry=builtin_registry(),
                    agent_event_callback=on_event,
                )
                status = outcome.status if outcome.status in TERMINAL else "failed"
                public_text = (
                    redact_text(outcome.text)
                    if status in {"completed", "waiting_input"}
                    else "Request did not complete."
                )
                public_error = None if status == "completed" else status
                run.result = AgentResult(
                    session.id, public_text, status,
                    {"prompt_tokens": outcome.prompt_tokens, "output_tokens": outcome.output_tokens},
                    error=public_error,
                )
                if status == "completed":
                    state["sessions"].append(session.id, user.id, "assistant", outcome.text)
                run.status = status
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.result = AgentResult(session.id, "Request cancelled.", "cancelled", error="cancelled")
                raise
            except Exception:
                run.status = "failed"
                run.result = AgentResult(session.id, "Request failed.", "failed", error="failed")
            finally:
                store.finish_agent_run(
                    run.id, run.status,
                    final_text=run.result.final_text if run.result else "Request failed.",
                    error=run.result.error if run.result else "failed",
                    usage=run.result.usage if run.result else {},
                )
                if active.get(session.id) == run.id:
                    active.pop(session.id)
                _notify(run)

        run.task = asyncio.create_task(execute())

        def task_finished(task):
            # A task cancelled before its first step never enters execute's finally.
            if active.get(session.id) == run.id:
                active.pop(session.id)
            if task.cancelled() and run.result is None:
                run.status = "cancelled"
                run.result = AgentResult(session.id, "Request cancelled.", "cancelled", error="cancelled")
                store.finish_agent_run(run.id, "cancelled", final_text="Request cancelled.", error="cancelled")
            _notify(run)

        run.task.add_done_callback(task_finished)
        return web.json_response(_run_json(run), status=202)

    def find_run(request):
        record = store.get_agent_run(request.match_info["run_id"])
        if record is None:
            raise web.HTTPNotFound()
        session = store.get_conversation(record["session_id"])
        if session is None or session.user_id != user.id or session.channel != INTERFACE:
            raise web.HTTPNotFound()
        return record

    async def get_run(request):
        return web.json_response(_stored_run_json(find_run(request)))

    async def cancel_run(request):
        record = find_run(request)
        run = state["runs"].get(record["id"])
        if run is None:
            return web.json_response(_stored_run_json(record), status=202)
        if run.result is None and run.task is not None and not run.task.done():
            run.task.cancel()
        return web.json_response(_stored_run_json(store.get_agent_run(run.id)), status=202)

    async def events(request):
        record = find_run(request)
        run = state["runs"].get(record["id"]) or Run(record["id"], record["session_id"])
        cursor = request.headers.get("Last-Event-ID")
        rows = _trace_rows(state, run)
        ids = [row["event_id"] for row in rows]
        if cursor is not None and cursor not in ids:
            raise _bad_request()
        index = ids.index(cursor) + 1 if cursor else 0
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })
        await response.prepare(request)
        try:
            while True:
                change = run.changed
                rows = _trace_rows(state, run)
                for row in rows[index:]:
                    payload = json.dumps(_public_event(row), ensure_ascii=False, separators=(",", ":"))
                    await response.write(f"id: {row['event_id']}\nevent: {row['event_type']}\ndata: {payload}\n\n".encode())
                index = len(rows)
                if store.get_agent_run(run.id)["status"] not in {"queued", "running"}:
                    break
                try:
                    await asyncio.wait_for(change.wait(), timeout=15)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except (ConnectionError, asyncio.CancelledError):
            pass
        return response

    async def cleanup(app):
        tasks = [run.task for run in state["runs"].values() if run.task and not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app.router.add_get("/health", health)
    app.router.add_post("/runs", create_run)
    app.router.add_get("/runs/{run_id}", get_run)
    app.router.add_get("/runs/{run_id}/events", events)
    app.router.add_post("/runs/{run_id}/cancel", cancel_run)
    app.on_cleanup.append(cleanup)
    return app


def run(mode="agent"):
    if mode != "agent":
        raise ValueError("API supports agent mode only")
    web.run_app(create_app(), host=HOST, port=PORT, access_log=None)
