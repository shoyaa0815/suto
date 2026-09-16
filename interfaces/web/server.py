"""Authenticated localhost control panel. No AI or worker starts here."""

import asyncio
import hmac
import os
import secrets
import signal
import threading
import tomllib
import webbrowser
from pathlib import Path

from aiohttp import web

from application.settings import env_text

from .data import DashboardData, DataUnavailable
from .settings import SettingsConflict, SettingsEditor


HOST = "127.0.0.1"
PORT = 8765
ASSETS = Path(__file__).parent / "static"
STATE = web.AppKey("state", dict)


def dashboard_url():
    return f"http://{HOST}:{PORT}"


@web.middleware
async def guard(request, handler):
    address = request.transport.get_extra_info("sockname") if request.transport else None
    host = f"{HOST}:{address[1] if address else PORT}"
    if request.host != host:
        raise web.HTTPForbidden(text="Local dashboard only.")
    if request.method not in {"GET", "HEAD"}:
        if (request.headers.get("Origin") != f"http://{host}"
                or request.headers.get("X-Suto-Request") != "1"
                or request.content_type != "application/json"):
            raise web.HTTPForbidden(text="Same-origin JSON requests required.")
    state = request.app[STATE]
    if request.path.startswith("/api/") and request.path != "/api/auth":
        if not hmac.compare_digest(request.cookies.get("suto_session", "").encode(), state["session"].encode()):
            raise web.HTTPUnauthorized(text="Open the dashboard link printed in your terminal.")
    try:
        response = await handler(request)
    except SettingsConflict as error:
        response = web.json_response({"error": str(error)}, status=409)
    except DataUnavailable as error:
        response = web.json_response({"error": str(error)}, status=503)
    except (ValueError, UnicodeError, RecursionError):
        response = web.json_response({"error": "Invalid input. Check YAML syntax, version: 1, profile fields and timezone."}, status=400)
    except OSError:
        response = web.json_response({"error": "Cannot access the local file. Check permissions and available disk space."}, status=503)
    response.headers.update({
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })
    return response


def page_args(request):
    offset = int(request.query.get("offset", "0"))
    query = request.query.get("q", "")
    if not 0 <= offset <= 100000 or len(query) > 200:
        raise ValueError("Invalid pagination")
    return query, offset


def paginated(rows, size):
    return {"items": rows[:size], "more": len(rows) > size}


def create_app(*, database_path=None, config_path=None, browser_opener=None):
    app = web.Application(middlewares=[guard], client_max_size=65536)
    state = {
        "bootstrap": secrets.token_urlsafe(32), "session": secrets.token_urlsafe(32),
        "data": DashboardData(Path(database_path or os.environ.get("SUTO_DB_PATH", "data/suto.db"))),
        "settings": SettingsEditor(Path(config_path or "config.yaml")),
        "browser_opener": browser_opener or webbrowser.open,
    }
    app[STATE] = state

    async def authenticate(request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("token"), str):
            raise web.HTTPUnauthorized()
        if not hmac.compare_digest(body["token"].encode(), state["bootstrap"].encode()):
            raise web.HTTPUnauthorized()
        response = web.json_response({"ok": True})
        response.set_cookie("suto_session", state["session"], httponly=True, samesite="Strict", path="/")
        return response

    async def overview(request):
        return web.json_response(state["data"].overview())

    async def sessions(request):
        query, offset = page_args(request)
        return web.json_response(paginated(state["data"].sessions(query, offset), 30))

    async def messages(request):
        _, offset = page_args(request)
        rows = state["data"].messages(request.match_info["id"], offset)
        if rows is None:
            raise web.HTTPNotFound()
        return web.json_response(paginated(rows, 30))

    async def logs(request):
        query, offset = page_args(request)
        event = request.query.get("event", "")
        if len(event) > 100:
            raise ValueError("Invalid event")
        return web.json_response(paginated(state["data"].logs(query, event, offset), 50))

    async def models(request):
        # Read current environment defaults without contacting a provider or
        # importing the AI runtime. Never include base URLs or credentials.
        return web.json_response({
            "provider": env_text("AI_PROVIDER", "ollama"),
            "model": env_text("AI_MODEL", env_text("OLLAMA_MODEL", "qwen3.5:9b")),
            "credential_configured": bool(env_text("AI_API_KEY", "")),
        })

    async def system(request):
        project = Path(__file__).resolve().parents[2] / "pyproject.toml"
        version = tomllib.loads(project.read_text())["project"]["version"]
        return web.json_response({"version": version, "transport": "Localhost", "update_available": None})

    async def settings(request):
        if request.method == "GET":
            return web.json_response(state["settings"].snapshot())
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected an object")
        # No await between revision check and atomic save: concurrent dashboard
        # writes are serialized on this event loop. External editors are checked
        # against the exact bytes last loaded, including comments and formatting.
        return web.json_response(state["settings"].update(
            body.get("yaml"), body.get("revision"), save=request.path.endswith("/save"),
        ))

    async def index(request):
        return web.Response(text=(ASSETS / "index.html").read_text(), content_type="text/html")

    async def asset(request):
        name = request.match_info["name"]
        if name not in {"app.css", "app.js"}:
            raise web.HTTPNotFound()
        return web.Response(text=(ASSETS / name).read_text(),
                            content_type="text/css" if name.endswith("css") else "text/javascript")

    app.router.add_get("/", index)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_post("/api/auth", authenticate)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/api/sessions/{id}/messages", messages)
    app.router.add_get("/api/logs", logs)
    app.router.add_get("/api/models", models)
    app.router.add_get("/api/system", system)
    app.router.add_get("/api/settings", settings)
    app.router.add_post("/api/settings/validate", settings)
    app.router.add_post("/api/settings/save", settings)
    return app


def _open_browser(app, url):
    try:
        if not app[STATE]["browser_opener"](url):
            print("Open the dashboard link above in your browser.")
    except Exception:
        print("Browser could not open. Use the dashboard link above.")


async def _serve():
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    try:
        await web.TCPSite(runner, HOST, PORT).start()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop.set)
                installed.append(signum)
            except NotImplementedError:
                pass
        url = dashboard_url() + "/#token=" + app[STATE]["bootstrap"]
        print(f"Suto Control: {url}\nKeep this local sign-in link private. Ctrl+C to stop.", flush=True)
        # A desktop browser launcher may block. It must not block HTTP serving
        # or prevent shutdown on hosts without a working graphical browser.
        threading.Thread(target=_open_browser, args=(app, url), daemon=True).start()
        await stop.wait()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
        await runner.cleanup()


def run(mode="web"):
    if mode != "web":
        raise ValueError("Use python main.py web")
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    except OSError:
        raise SystemExit(f"Cannot start Suto Control on {dashboard_url()}. Check the port and permissions.") from None
