"""The HTTP layer: one page, a JSON API under /api/, application files.

Bound to 127.0.0.1 only. That alone doesn't keep other web pages out: any
site open in the same browser can send requests to a local port. Three checks
do, and every /api/ and /files/ request must pass them:

- the ``Host`` header names this server (a page reaching it through a domain
  that resolves to 127.0.0.1 carries its own name there);
- a request that changes something comes from this page (``Origin``);
- it carries the token made at start-up, which only the served page holds.

The page's content-security policy allows scripts from this server only, so
text shown from a job posting can't run as script even if escaping missed it.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import mimetypes
import secrets
import threading
import time
import tomllib
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .. import boards, doctor
from . import jobs, setup, views
from .setup import SetupError

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 12 * 1024 * 1024  # an 8 MB CV, base64-encoded, with room to spare
DEFAULT_PORT = 8765
DOCTOR_TTL = 600  # seconds a setup-check result is reused while its inputs are unchanged

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'"
)


def _version(root: Path) -> str:
    """The version of the code in this folder. Read from pyproject.toml first:
    people run from a clone and update by pulling, which leaves the installed
    package's own record at whatever it was on the day of `pip install`."""
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("jobradar")
    except metadata.PackageNotFoundError:
        return ""


class App:
    """What the handler needs: where the instance lives, the token, the runner."""

    def __init__(self, root: Path, connect: Callable[[], None] | None = None):
        self.root = root
        self.token = secrets.token_urlsafe(24)
        self.runner = jobs.Runner(root)
        self.connect = connect or doctor._default_connect
        self.hosts: set[str] = set()
        self._doctor: tuple | None = None  # (inputs, when, result) of the last setup check

    # ── GET ────────────────────────────────────────────────────────────────

    def state(self, _q: dict) -> dict:
        job = self.runner.job
        return {
            "version": _version(self.root),
            "setup": setup.status(self.root),
            "key": setup.key_status(self.root),
            "job": job.snapshot(len(job.lines)) if job else None,
        }

    def overview(self, _q: dict) -> dict:
        try:
            selected = len(boards.load_companies(self.root, doctor._market(self.root)).boards)
        except boards.BoardsError:
            selected = 0
        try:
            rav = setup.read_search(self.root)["rav"]
        except SetupError:
            rav = False
        return {
            **views.overview(self.root),
            "setup": setup.status(self.root),
            "boards": selected,
            "rav": rav,
            "rav_unfiled": views.rav(self.root)["unfiled"] if rav else [],
        }

    def get_cvs(self, q: dict) -> dict:
        if q.get("name"):
            return {"name": q["name"], "text": setup.read_cv(self.root, q["name"])}
        return {"cvs": setup.list_cvs(self.root)}

    def _check_inputs(self) -> tuple:
        """What the setup check reads, as (path, time changed) pairs: an edit
        to any of it, here or in an editor, makes an earlier result stale."""
        paths = [self.root / ".env", self.root / "profile" / "identity.md"]
        for pattern in ("profile/cvs/*", "config/*.yaml", "config/boards/*.yaml"):
            paths += sorted(self.root.glob(pattern))
        return tuple((str(p), p.stat().st_mtime_ns) for p in paths if p.exists())

    def get_doctor(self, q: dict) -> dict:
        """The doctor's checks. The workflow page asks on every visit, to show
        whether the setup holds *now* however it was last checked, so a result
        is reused for a few minutes while none of its inputs changed. `fresh`
        (the Check again button) always runs it."""
        inputs = self._check_inputs()
        cached = self._doctor
        if (
            not q.get("fresh") and cached is not None and cached[0] == inputs
            and time.time() - cached[1] < DOCTOR_TTL
        ):
            return cached[2]
        checks = doctor.run_checks(self.root, self.connect)
        result = {
            "checks": [self._plain(asdict(c)) for c in checks],
            "failed": sum(c.level == doctor.FAIL for c in checks),
            "warned": sum(c.level == doctor.WARN for c in checks),
        }
        # Keyed on the inputs as they are after the run: the check itself
        # writes the Markdown copy of a Word CV.
        self._doctor = (self._check_inputs(), time.time(), result)
        return result

    @staticmethod
    def _plain(check: dict) -> dict:
        """The doctor writes for someone at a terminal who may be setting up a
        cloud run. The one message every newcomer can meet is put in the
        page's terms; the rest read well enough as they are."""
        if check["name"] == "Anthropic credentials" and check["level"] == doctor.FAIL:
            check["message"] = (
                "Claude did not accept the stored key, or none is stored. Open "
                "“Connect Claude” and paste your key again."
            )
        return check

    def get_rav(self, q: dict) -> dict:
        if not setup.read_search(self.root)["rav"]:
            return {"enabled": False}
        return {"enabled": True, **views.rav(self.root, q.get("month"))}

    def get_job(self, q: dict) -> dict:
        job = self.runner.job
        since = int(q.get("since") or 0)
        return {"job": job.snapshot(since) if job else None}

    # ── POST ───────────────────────────────────────────────────────────────

    def post_key(self, body: dict) -> dict:
        return setup.save_key(self.root, str(body.get("key", "")), self.connect)

    def post_cv(self, body: dict) -> dict:
        if body.get("delete"):
            setup.delete_cv(self.root, str(body["delete"]))
            return {}
        if "data" in body:
            try:
                data = base64.b64decode(body["data"], validate=True)
            except ValueError as exc:
                raise SetupError("The upload arrived damaged; try again.") from exc
            return setup.save_cv_file(self.root, str(body.get("filename", "")), data)
        return setup.save_cv_text(self.root, str(body.get("name", "")), str(body.get("text", "")))

    def post_run(self, body: dict) -> dict:
        name = str(body.get("action", ""))
        action = jobs.ACTIONS.get(name)
        if action is None:
            raise SetupError("Unknown action.")
        if action.background:
            return {"job": self.runner.start(name, body).snapshot()}
        return jobs.run_quick(self.root, name, body)

    def post_file(self, body: dict) -> dict:
        views.write_file(self.root, str(body.get("uid", "")), str(body.get("name", "")), str(body.get("text", "")))
        return {}

    def routes(self) -> tuple[dict, dict]:
        r = self.root
        get = {
            "/api/state": self.state,
            "/api/overview": self.overview,
            "/api/cvs": self.get_cvs,
            "/api/identity": lambda q: setup.read_identity(r),
            "/api/constraints": lambda q: setup.read_constraints(r),
            "/api/boards": lambda q: setup.read_boards(r),
            "/api/search": lambda q: setup.read_search(r),
            "/api/search_warnings": lambda q: {"warnings": setup.search_warnings(q)},
            "/api/doctor": self.get_doctor,
            "/api/report": lambda q: views.report(r),
            "/api/applications": lambda q: {"applications": views.applications(r)},
            "/api/file": lambda q: views.read_file(r, q.get("uid", ""), q.get("name", "")),
            "/api/job": self.get_job,
            "/api/rav": self.get_rav,
        }
        post = {
            "/api/key": self.post_key,
            "/api/cvs": self.post_cv,
            "/api/identity": lambda b: setup.save_identity(r, b.get("sections") or []) or {},
            "/api/constraints": lambda b: setup.save_constraints(r, b) or {},
            "/api/boards": lambda b: {"selected": setup.save_boards(r, b.get("names") or [])},
            "/api/search": lambda b: setup.save_search(r, b) or {},
            "/api/run": self.post_run,
            "/api/file": self.post_file,
            "/api/jd": lambda b: views.paste_jd(r, str(b.get("uid", "")), str(b.get("text", ""))) or {},
            "/api/cancel": lambda b: self.runner.cancel() or {},
        }
        return get, post


class Handler(BaseHTTPRequestHandler):
    app: App  # set on the subclass made in `serve`
    server_version = "jobradar"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base class's name
        logger.debug("%s %s", self.address_string(), format % args)

    # ── replies ────────────────────────────────────────────────────────────

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    # ── checks ─────────────────────────────────────────────────────────────

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.app.hosts

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-Jobradar-Token") or query.get("t", "")
        return secrets.compare_digest(given, self.app.token)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is not None and urlsplit(origin).netloc in self.app.hosts

    # ── dispatch ───────────────────────────────────────────────────────────

    def _call(self, handler: Callable[[dict], dict], argument: dict) -> None:
        try:
            self._json(handler(argument) or {})
        except SetupError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 - the page shows it; the server stays up
            logger.exception("request failed: %s", self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def do_GET(self) -> None:  # noqa: N802 - the base class's name
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "Open the address jobradar printed when it started.")
        if url.path == "/":
            return self._page()
        if url.path.startswith("/static/"):
            return self._static(url.path.removeprefix("/static/"))
        if not self._token_ok(query):
            return self._error(HTTPStatus.FORBIDDEN, "This page is out of date; reload it.")
        if url.path.startswith("/files/"):
            return self._file(url.path.removeprefix("/files/"))
        handler = self.app.routes()[0].get(url.path)
        if handler is None:
            return self._error(HTTPStatus.NOT_FOUND, "Not found.")
        self._call(handler, query)

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        if not (self._host_ok() and self._origin_ok() and self._token_ok({})):
            return self._error(HTTPStatus.FORBIDDEN, "This page is out of date; reload it.")
        handler = self.app.routes()[1].get(url.path)
        if handler is None:
            return self._error(HTTPStatus.NOT_FOUND, "Not found.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "That is too large to upload.")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "The request could not be read.")
        if not isinstance(body, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "The request could not be read.")
        self._call(handler, body)

    # ── files ──────────────────────────────────────────────────────────────

    def _page(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace("{{token}}", self.app.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": CSP})

    def _static(self, name: str) -> None:
        path = STATIC / name
        if Path(name).name != name or not path.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "Not found.")
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        self._send(200, path.read_bytes(), kind)

    def _file(self, rest: str) -> None:
        """A document of one application: /files/<id>/<name>. PDFs open in the
        browser; the .html print version is sent with a policy that lets it
        style itself and nothing more."""
        uid, _, name = rest.partition("/")
        try:
            path = views.file_path(self.app.root, uid, name)
        except SetupError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))
        kind = {
            ".pdf": "application/pdf",
            ".html": "text/html; charset=utf-8",
            ".md": "text/plain; charset=utf-8",
        }[path.suffix.lower()]
        self._send(200, path.read_bytes(), kind, {
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            "Content-Disposition": f'inline; filename="{path.name}"',
        })


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    app.hosts = {f"127.0.0.1:{actual}", f"localhost:{actual}"}
    return server


def _bind(app: App, port: int) -> ThreadingHTTPServer:
    """The wanted port, or the next free one after it: a second copy left
    running, or another program, shouldn't stop the tool from starting."""
    for candidate in range(port, port + 20):
        try:
            return make_server(app, candidate)
        except OSError:
            continue
    return make_server(app, 0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Open jobradar in your browser.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to serve on (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="Print the address instead of opening it")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    app = App(ROOT)
    server = _bind(app, args.port)
    address = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"\n  jobradar is running at {address}\n", flush=True)
    print("  Keep this window open while you use it. To stop: close it, or press Ctrl+C.\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(address,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        app.runner.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
