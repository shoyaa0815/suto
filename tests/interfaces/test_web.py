import json
import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from application.configuration import load_settings
from interfaces.web import server
from interfaces.web.data import DashboardData, DataUnavailable
from interfaces.web.settings import SettingsEditor, SettingsConflict
from workflows.storage.store import JobStore


@pytest.fixture
def app(tmp_path):
    return server.create_app(database_path=tmp_path / "suto.db", config_path=tmp_path / "config.yaml")


async def request(app, path, *, method="GET", body=None, authenticated=True, origin="http://127.0.0.1:8765", host="127.0.0.1:8765"):
    headers = {"Host": host, "Origin": origin, "Content-Type": "application/json", "X-Suto-Request": "1"}
    if authenticated:
        headers["Cookie"] = "suto_session=" + app[server.STATE]["session"]
    transport = Mock()
    transport.get_extra_info.return_value = ("127.0.0.1", 8765)
    req = make_mocked_request(method, path, headers=headers, app=app, transport=transport)
    req.json = AsyncMock(return_value=body)
    match = await app.router.resolve(req)
    match.add_app(app)
    req._match_info = match
    return await server.guard(req, match.handler)


async def test_authentication_origin_and_host_boundaries(app):
    with pytest.raises(web.HTTPUnauthorized):
        await request(app, "/api/overview", authenticated=False)
    for origin in ("http://evil.test", "null", "http://localhost:8765"):
        with pytest.raises(web.HTTPForbidden):
            await request(app, "/api/settings/save", method="POST", body={}, origin=origin)
    with pytest.raises(web.HTTPForbidden):
        await request(app, "/api/overview", host="evil.test:8765")
    with pytest.raises(web.HTTPUnauthorized):
        await request(app, "/api/auth", method="POST", body={"token":"wrong"}, authenticated=False)
    response = await request(app, "/api/auth", method="POST", body={"token":app[server.STATE]["bootstrap"]}, authenticated=False)
    cookie = response.cookies["suto_session"]
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
    assert app[server.STATE]["bootstrap"] not in response.text


async def test_empty_dashboard_does_not_create_database_or_config(app, tmp_path):
    response = await request(app, "/api/overview")
    assert json.loads(response.text)["database"] == "not_created"
    assert not list(tmp_path.iterdir())
    page = await request(app, "/", authenticated=False)
    assert "Suto Control" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["Cache-Control"] == "no-store"


def test_local_session_scope_messages_pagination_and_read_only(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    local = store.resolve_channel_identity("tui", "local")
    other = store.resolve_channel_identity("discord", "other")
    visible = store.get_or_create_conversation(local.id, "tui", "local")
    private = store.get_or_create_conversation(other.id, "discord", "private")
    linked = store.get_or_create_conversation(local.id, "discord", "linked")
    for conv in (private, linked):
        store.add_message(conv.id, "user", "private content")
    for i in range(35):
        store.add_message(visible.id, "user", f"hello {i}")
    view = DashboardData(path)
    assert [row["id"] for row in view.sessions("hello")] == [visible.id]
    assert view.messages(private.id) is None
    assert view.messages(linked.id) is None
    assert len(view.messages(visible.id)) == 31
    assert len(view.messages(visible.id, 30)) == 5
    assert view.overview()["sessions"] == 1
    with view.connect() as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM messages")
    assert len(store.list_messages(visible.id, 100)) == 35


def test_incompatible_database_is_not_migrated(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    before = path.read_bytes()
    with pytest.raises(DataUnavailable):
        DashboardData(path).overview()
    assert path.read_bytes() == before


def test_log_redaction_filter_and_pagination(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    with store._connect() as db:
        for i in range(55):
            db.execute("INSERT INTO structured_logs(event_type,detail,created_at) VALUES(?,?,?)",
                       ("failed", f"attempt {i} api_key=sk-private123456", "2026-09-15T00:00:00+00:00"))
    view = DashboardData(path)
    assert len(view.logs(event="failed")) == 51
    assert len(view.logs(offset=50)) == 5
    assert view.logs(event="completed") == []
    assert "sk-private" not in json.dumps(view.logs())


async def test_yaml_validate_save_conflict_and_invalid_input(app):
    first = json.loads((await request(app, "/api/settings")).text)
    source = "version: 1\nprofile:\n  timezone: Asia/Bangkok\n  locale: th\n  display_name: Test Owner\n"
    body = {"yaml":source, "revision":first["revision"]}
    validated = await request(app, "/api/settings/validate", method="POST", body=body)
    assert validated.status == 200
    assert "Test Owner" in json.loads(validated.text)["diff"]
    path = app[server.STATE]["settings"].path
    assert not path.exists()
    saved = await request(app, "/api/settings/save", method="POST", body=body)
    assert saved.status == 200
    assert load_settings(path).profile.display_name == "Test Owner"
    assert path.stat().st_mode & 0o777 == 0o600
    conflict = await request(app, "/api/settings/save", method="POST", body=body)
    assert conflict.status == 409
    raw = path.read_bytes()
    for source in ("profile: [oops]", "profile:\n  timezone: invalid", "api_key: sk-private123456", "x" * 32769, "{", None):
        result = await request(app, "/api/settings/save", method="POST", body={"yaml":source,"revision":json.loads(saved.text)["revision"]})
        assert result.status == 400
        assert "sk-private" not in result.text
        assert path.read_bytes() == raw


def test_external_edit_and_failed_replace_preserve_file(tmp_path, monkeypatch):
    editor = SettingsEditor(tmp_path / "config.yaml")
    first = editor.snapshot()
    editor.update(first["yaml"], first["revision"], save=True)
    opened = editor.snapshot()
    editor.path.write_text(opened["yaml"] + "# edited externally\n")
    with pytest.raises(SettingsConflict):
        editor.update(opened["yaml"], opened["revision"], save=True)
    current = editor.snapshot()
    before = editor.path.read_bytes()
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr("application.configuration.os.replace", fail)
    with pytest.raises(OSError):
        editor.update(current["yaml"], current["revision"], save=True)
    assert editor.path.read_bytes() == before


async def test_models_do_not_expose_credentials_or_connect(app, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "sensitive-key")
    monkeypatch.setenv("AI_BASE_URL", "http://user:secret@example.invalid")
    response = await request(app, "/api/models")
    assert json.loads(response.text)["credential_configured"] is True
    assert "sensitive-key" not in response.text and "secret@" not in response.text


def test_browser_failure_does_not_raise_or_expose_error_details(app, capsys):
    def fail(url):
        raise RuntimeError("private-launcher-details")
    app[server.STATE]["browser_opener"] = fail
    server._open_browser(app, server.dashboard_url())
    assert "private-launcher-details" not in capsys.readouterr().out
