import json
import threading
import urllib.error
import urllib.request

import pytest

from jobradar.ui import server


@pytest.fixture
def running(tmp_path):
    """The real server on a free port, over an empty instance folder."""
    boards = tmp_path / "config" / "boards"
    boards.mkdir(parents=True)
    for name in ("global", "ch"):
        (boards / f"{name}.yaml").write_text("boards: []\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n', encoding="utf-8")
    app = server.App(tmp_path, connect=lambda: None)
    httpd = server.make_server(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _call(url: str, *, body: dict | None = None, **headers) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, raw, kind = response.status, response.read().decode(), response.headers["Content-Type"]
    except urllib.error.HTTPError as error:
        status, raw, kind = error.code, error.read().decode(), error.headers["Content-Type"]
    return status, json.loads(raw) if kind == "application/json" else raw


def test_the_page_carries_the_token_and_a_policy_that_forbids_foreign_scripts(running):
    app, base = running
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        html, policy = response.read().decode(), response.headers["Content-Security-Policy"]
    assert f'content="{app.token}"' in html and "{{token}}" not in html
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    status, script = _call(base + "/static/app.js")
    assert status == 200 and "use strict" in script


def test_the_page_names_its_own_icon_so_the_browser_asks_for_no_favicon(running):
    # Without the link a browser asks for /favicon.ico, which carries no token
    # and is refused: an error in the console on every load.
    _, base = running
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        html = response.read().decode()
    assert 'rel="icon"' in html and "/static/favicon.svg" in html
    with urllib.request.urlopen(base + "/static/favicon.svg", timeout=10) as response:
        assert response.status == 200 and response.headers["Content-Type"] == "image/svg+xml"
        assert b"<svg" in response.read()


def test_state_needs_the_token(running):
    app, base = running
    assert _call(base + "/api/state")[0] == 403
    assert _call(base + "/api/state", **{"X-Jobradar-Token": "wrong"})[0] == 403
    status, state = _call(base + "/api/state", **{"X-Jobradar-Token": app.token})
    assert status == 200 and state["version"] == "9.9.9" and state["setup"]["ready"] is False


def test_a_page_on_another_site_cannot_use_the_api_even_with_the_token(running):
    app, base = running
    token = {"X-Jobradar-Token": app.token}
    # Reached through a name that resolves here (DNS rebinding):
    assert _call(base + "/api/state", Host="evil.example", **token)[0] == 403
    assert _call(base + "/", Host="evil.example")[0] == 403
    # A form or script on another origin posting to the local port:
    assert _call(base + "/api/boards", body={}, **token)[0] == 403
    assert _call(base + "/api/boards", body={}, Origin="https://evil.example", **token)[0] == 403
    assert not (app.root / "data").exists()
    assert _call(base + "/api/boards", body={"names": []}, Origin=base, **token)[0] == 200
    assert (app.root / "data" / "ui_state.json").exists()


def test_a_mistake_in_a_form_comes_back_as_a_message(running):
    app, base = running
    status, reply = _call(
        base + "/api/key", body={"key": "nope"}, Origin=base, **{"X-Jobradar-Token": app.token}
    )
    assert status == 400 and "sk-ant-" in reply["error"]
    status, reply = _call(
        base + "/api/run", body={"action": "format-disk"}, Origin=base, **{"X-Jobradar-Token": app.token}
    )
    assert status == 400


@pytest.mark.parametrize("path", [
    "/static/../server.py", "/static/..%2Fserver.py", "/static/", "/files/1/notes.md", "/api/nothing",
])
def test_nothing_outside_the_page_and_the_api_is_served(running, path):
    app, base = running
    status, _ = _call(base + path, **{"X-Jobradar-Token": app.token})
    assert status == 404


def test_overview_of_an_empty_instance(running):
    app, base = running
    status, data = _call(base + "/api/overview", **{"X-Jobradar-Token": app.token})
    assert status == 200
    assert data["setup"]["ready"] is False and data["last_run"] is None
    assert (data["matches"], data["drafted"], data["boards"], data["rav"]) == (0, 0, 0, False)


def test_the_setup_check_is_reused_until_something_it_reads_changes(running, monkeypatch):
    app, base = running
    # The check loads .env into the environment; this puts it back afterwards.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = []
    app.connect = lambda: calls.append(1)
    token = {"X-Jobradar-Token": app.token}
    first = _call(base + "/api/doctor", **token)[1]
    assert "failed" in first and "warned" in first
    _call(base + "/api/doctor", **token)
    assert len(calls) == 1  # the workflow page asking again costs nothing
    _call(base + "/api/doctor?fresh=1", **token)
    assert len(calls) == 2  # "Check again" always checks
    (app.root / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-changed\n", encoding="utf-8")
    _call(base + "/api/doctor", **token)
    assert len(calls) == 3  # an edit in an editor makes the old result stale


@pytest.mark.parametrize("path", ["/api/report/archive?offset=x", "/api/applications/archive?offset=-"])
def test_a_page_that_is_not_a_number_is_refused(running, path):
    app, base = running
    assert _call(base + path, **{"X-Jobradar-Token": app.token})[0] == 400
