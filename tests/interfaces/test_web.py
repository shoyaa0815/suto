import json
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from application.configuration import load_settings
from interfaces.web import server
from interfaces.web.settings import SettingsEditor, SettingsConflict
from workflows.storage.store import JobStore


@pytest.fixture
def app(tmp_path):
    return server.create_app(config_path=tmp_path / "config.yaml")


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


async def test_web_page_is_local_and_unsupported_apis_are_not_exposed(app):
    page = await request(app, "/", authenticated=False)
    assert "Suto Settings" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    for path in ("/api/overview", "/api/sessions", "/api/logs", "/api/models", "/api/system",
                 "/api/chat", "/api/chat/runs/123"):
        with pytest.raises(web.HTTPNotFound):
            await request(app, path)
    for path in ("/api/chat", "/api/chat/runs/123/cancel", "/api/chat/runs/123/approval"):
        with pytest.raises(web.HTTPNotFound):
            await request(app, path, method="POST", body={})
    script = (await request(app, "/assets/app.js", authenticated=False)).text
    assert '<aside class="sidebar">' in script
    assert '<nav aria-label="Main navigation">' in script
    assert 'Review changes' not in script
    assert '/api/chat' not in script
    assert 'chatView' not in script
    assert 'Dashboard' not in script
    assert 'conversations' not in script
    assert 'textarea name="yaml"' in script
    assert 'api("settings/save", {yaml:draft,revision})' in script


async def test_authentication_origin_and_host_boundaries(app):
    with pytest.raises(web.HTTPUnauthorized):
        await request(app, "/api/settings", authenticated=False)
    with pytest.raises(web.HTTPUnauthorized):
        await request(app, "/api/profile", authenticated=False)
    for origin in ("http://evil.test", "null", "http://localhost:8765"):
        with pytest.raises(web.HTTPForbidden):
            await request(app, "/api/settings/save", method="POST", body={}, origin=origin)
    with pytest.raises(web.HTTPForbidden):
        await request(app, "/api/settings", host="evil.test:8765")
    with pytest.raises(web.HTTPUnauthorized):
        await request(app, "/api/auth", method="POST", body={"token":"wrong"}, authenticated=False)
    response = await request(app, "/api/auth", method="POST", body={"token":app[server.STATE]["bootstrap"]}, authenticated=False)
    assert response.cookies["suto_session"]["httponly"]
    assert response.cookies["suto_session"]["samesite"] == "Strict"


async def test_settings_page_does_not_create_a_database(tmp_path, monkeypatch):
    database = tmp_path / "suto.db"
    monkeypatch.setenv("SUTO_DB_PATH", str(database))
    app = server.create_app(config_path=tmp_path / "config.yaml")
    assert (await request(app, "/api/profile")).status == 200
    assert not database.exists()


async def test_chat_history_routes_are_removed_and_saved_data_is_preserved(tmp_path):
    database = tmp_path / "suto.db"
    store = JobStore(database)
    user = store.resolve_channel_identity("tui", "local")
    conversation = store.get_or_create_conversation(user.id, "tui", "local")
    store.add_message(conversation.id, "user", "keep private chat")
    app = server.create_app(config_path=tmp_path / "config.yaml", database_path=database)
    for path in ("/api/conversations", f"/api/conversations/{conversation.id}/messages"):
        with pytest.raises(web.HTTPNotFound):
            await request(app, path)
    assert JobStore(database).list_messages(conversation.id)[0].content == "keep private chat"


async def test_profile_saves_without_a_separate_review_request(app):
    original = json.loads((await request(app, "/api/profile")).text)
    profile = {**original["profile"], "display_name": "One click"}
    saved = await request(app, "/api/profile/save", method="POST",
                          body={"profile": profile, "revision": original["revision"]})
    assert json.loads(saved.text)["saved"] is True
    assert load_settings(app[server.STATE]["settings"].path).profile.display_name == "One click"


async def test_profile_form_validates_saves_and_preserves_conflicting_draft(app):
    original = json.loads((await request(app, "/api/profile")).text)
    profile = {"display_name": "Test Owner", "locale": "th", "timezone": "Asia/Bangkok"}
    payload = {"profile": profile, "revision": original["revision"]}
    validated = await request(app, "/api/profile/validate", method="POST", body=payload)
    assert json.loads(validated.text)["profile"] == profile
    path = app[server.STATE]["settings"].path
    assert not path.exists()
    saved = await request(app, "/api/profile/save", method="POST", body=payload)
    assert json.loads(saved.text)["saved"] is True
    assert load_settings(path).profile.display_name == "Test Owner"
    assert (await request(app, "/api/profile/save", method="POST", body=payload)).status == 409
    for invalid in ({"display_name":"x", "locale":"th"},
                    {**profile, "timezone":"Unknown/Place"},
                    {**profile, "api_key":"secret"}):
        response = await request(app, "/api/profile/save", method="POST",
                                 body={"profile":invalid, "revision":json.loads(saved.text)["revision"]})
        assert response.status == 400
        assert load_settings(path).profile.display_name == "Test Owner"


async def test_yaml_validate_save_conflict_and_invalid_input(app):
    first = json.loads((await request(app, "/api/settings")).text)
    source = "version: 1\nprofile:\n  timezone: Asia/Bangkok\n  locale: th\n  display_name: Test Owner\n"
    body = {"yaml":source, "revision":first["revision"]}
    validated = await request(app, "/api/settings/validate", method="POST", body=body)
    assert "Test Owner" in json.loads(validated.text)["diff"]
    path = app[server.STATE]["settings"].path
    assert not path.exists()
    saved = await request(app, "/api/settings/save", method="POST", body=body)
    assert load_settings(path).profile.display_name == "Test Owner"
    assert path.stat().st_mode & 0o777 == 0o600
    conflict = await request(app, "/api/settings/save", method="POST", body=body)
    assert conflict.status == 409
    raw = path.read_bytes()
    for invalid in ("profile: [oops]", "profile:\n  timezone: invalid", "api_key: sk-private123456", "x" * 32769, "{", None):
        result = await request(app, "/api/settings/save", method="POST", body={"yaml":invalid,"revision":json.loads(saved.text)["revision"]})
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
    monkeypatch.setattr("application.configuration.os.replace", lambda *args: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError):
        editor.update(current["yaml"], current["revision"], save=True)
    assert editor.path.read_bytes() == before


def test_profile_editor_preserves_voice_configuration(tmp_path):
    editor = SettingsEditor(tmp_path / "config.yaml")
    initial = editor.snapshot()
    configured = initial["yaml"].replace(
        "  stt_provider: none", "  stt_provider: openai",
    )
    editor.update(configured, initial["revision"], save=True)
    snapshot = editor.profile_snapshot()
    profile = {**snapshot["profile"], "display_name": "Voice User"}
    editor.update_profile(profile, snapshot["revision"], save=True)
    saved = load_settings(editor.path)
    assert saved.profile.display_name == "Voice User"
    assert saved.voice.stt_provider == "openai"


def test_browser_failure_does_not_expose_error_details(app, capsys):
    def fail(url):
        raise RuntimeError("private-launcher-details")
    app[server.STATE]["browser_opener"] = fail
    server._open_browser(app, server.settings_url())
    assert "private-launcher-details" not in capsys.readouterr().out


async def test_runtime_settings_save_validates_and_preserves_conflicting_file(app, tmp_path):
    import yaml
    original = json.loads((await request(app, "/api/settings")).text)
    values = yaml.safe_load(original["yaml"])
    values["runtime"] = {
        "provider": "openai-compatible", "model": "test-model",
        "base_url": "http://localhost:1234/v1", "timezone": "Asia/Bangkok",
        "workspace": str(tmp_path), "options": {"timeout_seconds": 45},
        "limits": {"max_tokens": 1000},
    }
    payload = {"yaml": yaml.safe_dump(values), "revision": original["revision"]}
    response = await request(app, "/api/settings/save", method="POST", body=payload)
    assert response.status == 200
    path = app[server.STATE]["settings"].path
    runtime = load_settings(path).runtime
    assert runtime.model == "test-model" and runtime.provider == "openai-compatible"
    assert runtime.timezone == "Asia/Bangkok" and runtime.workspace == str(tmp_path)
    assert runtime.options.timeout_seconds == 45 and runtime.limits.max_tokens == 1000
    before = path.read_bytes()
    assert (await request(app, "/api/settings/save", method="POST", body=payload)).status == 409
    values["runtime"]["limits"]["max_tokens"] = 0
    invalid = {"yaml": yaml.safe_dump(values), "revision": json.loads(response.text)["revision"]}
    assert (await request(app, "/api/settings/save", method="POST", body=invalid)).status == 400
    assert path.read_bytes() == before
