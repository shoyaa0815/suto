import json
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from application.configuration import load_settings
from interfaces.web import server
from interfaces.web.settings import SettingsEditor, SettingsConflict


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


async def test_settings_page_is_local_and_dashboard_apis_are_not_exposed(app):
    page = await request(app, "/", authenticated=False)
    assert "Suto Settings" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    for path in ("/api/overview", "/api/sessions", "/api/logs", "/api/models", "/api/system"):
        with pytest.raises(web.HTTPNotFound):
            await request(app, path)


async def test_authentication_origin_and_host_boundaries(app):
    with pytest.raises(web.HTTPUnauthorized):
        await request(app, "/api/settings", authenticated=False)
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


def test_browser_failure_does_not_expose_error_details(app, capsys):
    def fail(url):
        raise RuntimeError("private-launcher-details")
    app[server.STATE]["browser_opener"] = fail
    server._open_browser(app, server.settings_url())
    assert "private-launcher-details" not in capsys.readouterr().out
