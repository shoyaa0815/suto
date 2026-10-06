"""Authenticated localhost editor for host-managed Suto settings."""

import asyncio
import hmac
import ipaddress
import os
import secrets
import signal
import sqlite3
import threading
import webbrowser
from pathlib import Path

from aiohttp import web

from application.configuration import configuration_path

from . import history
from .settings import SettingsConflict, SettingsEditor


HOST = "127.0.0.1"
PORT = 8765
ASSETS = Path(__file__).parent / "static"
STATE = web.AppKey("state", dict)


def settings_url():
    return f"http://{HOST}:{PORT}"


@web.middleware
async def guard(request, handler):
    address = request.transport.get_extra_info("sockname") if request.transport else None
    peer = request.transport.get_extra_info("peername") if request.transport else None
    host = f"{HOST}:{address[1] if address else PORT}"
    try:
        local_peer = peer is not None and ipaddress.ip_address(peer[0]).is_loopback
    except (ValueError, TypeError):
        local_peer = False
    if request.host != host or not local_peer:
        raise web.HTTPForbidden(text="Local settings editor only.")
    if request.method not in {"GET", "HEAD"}:
        if (request.headers.get("Origin") != f"http://{host}"
                or request.headers.get("X-Suto-Request") != "1"
                or request.content_type != "application/json"):
            raise web.HTTPForbidden(text="Same-origin JSON requests required.")
    state = request.app[STATE]
    if request.path.startswith("/api/") and request.path != "/api/auth":
        if not hmac.compare_digest(request.cookies.get("suto_session", "").encode(), state["session"].encode()):
            raise web.HTTPUnauthorized(text="Open the settings link printed in your terminal.")
    try:
        response = await handler(request)
    except SettingsConflict as error:
        response = web.json_response({"error": str(error)}, status=409)
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


def create_app(*, config_path=None, browser_opener=None, database_path=None):
    app = web.Application(middlewares=[guard], client_max_size=65536)
    state = {
        "bootstrap": secrets.token_urlsafe(32), "session": secrets.token_urlsafe(32),
        "settings": SettingsEditor(configuration_path(config_path)),
        "history_db": Path(database_path or os.environ.get("SUTO_DB_PATH", "data/suto.db")).expanduser().resolve(),
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

    async def settings(request):
        if request.method == "GET":
            return web.json_response(state["settings"].snapshot())
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected an object")
        # No await between revision check and atomic save: concurrent editor
        # writes are serialized on this event loop. External editors are checked
        # against the exact bytes last loaded, including comments and formatting.
        return web.json_response(state["settings"].update(
            body.get("yaml"), body.get("revision"), save=request.path.endswith("/save"),
        ))

    async def profile(request):
        if request.method == "GET":
            return web.json_response(state["settings"].profile_snapshot())
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected an object")
        return web.json_response(state["settings"].update_profile(
            body.get("profile"), body.get("revision"),
            save=request.path.endswith("/save"),
        ))

    async def conversations(request):
        raw_offset = request.query.get("offset", "0")
        if not raw_offset.isdecimal() or len(raw_offset) > 9:
            raise web.HTTPBadRequest(text="Invalid conversation page.")
        try:
            result = history.list_conversations(state["history_db"], int(raw_offset))
        except sqlite3.Error:
            return web.json_response({"error": "Could not read chat history."}, status=503)
        return web.json_response(result)

    async def conversation_messages(request):
        conversation_id = request.match_info["conversation_id"]
        raw_after = request.query.get("after", "0")
        if not raw_after.isdecimal() or len(raw_after) > 18 or len(conversation_id) > 64:
            raise web.HTTPBadRequest(text="Invalid conversation page.")
        try:
            result = history.list_messages(state["history_db"], conversation_id, int(raw_after))
        except sqlite3.Error:
            return web.json_response({"error": "Could not read chat history."}, status=503)
        if result is None:
            return web.json_response({"error": "Conversation is no longer available."}, status=404)
        return web.json_response(result)

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
    app.router.add_get("/api/settings", settings)
    app.router.add_post("/api/settings/validate", settings)
    app.router.add_post("/api/settings/save", settings)
    app.router.add_get("/api/profile", profile)
    app.router.add_post("/api/profile/validate", profile)
    app.router.add_post("/api/profile/save", profile)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_get("/api/conversations/{conversation_id}/messages", conversation_messages)
    return app


def _open_browser(app, url):
    try:
        if not app[STATE]["browser_opener"](url):
            print("Open the settings link above in your browser.")
    except Exception:
        print("Browser could not open. Use the settings link above.")


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
        url = settings_url() + "/#token=" + app[STATE]["bootstrap"]
        print(f"Suto Settings: {url}\nKeep this local sign-in link private. Ctrl+C to stop.", flush=True)
        # A desktop browser launcher may block. It must not block HTTP serving
        # or prevent shutdown on hosts without a working graphical browser.
        threading.Thread(target=_open_browser, args=(app, url), daemon=True).start()
        await stop.wait()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
        await runner.cleanup()


def run(mode="agent"):
    if mode != "agent":
        raise ValueError("Use python3 main.py setting")
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    except OSError:
        raise SystemExit(f"Cannot start Suto Settings on {settings_url()}. Check the port and permissions.") from None
