"""Email the daily headline from reports/latest.json after a run.

Decoupled from the pipeline: main.py writes latest.json, this reads it and
emails it. SMTP settings come from environment variables (set as CI secrets).

Degrades gracefully — if there's nothing to send or SMTP isn't configured, it
logs and exits 0 rather than failing the scheduled job. By default it only
emails on a signal day (matches found, or a degraded run); quiet "no matches"
days stay silent to avoid notification fatigue. Set JOBRADAR_NOTIFY_ALWAYS=1 to
email on every run.

Required env: SMTP_USER, SMTP_PASS.
Optional env: EMAIL_TO (default SMTP_USER — i.e. email yourself), SMTP_HOST
(default smtp.gmail.com), SMTP_PORT (default 587), EMAIL_FROM (default
SMTP_USER), JOBRADAR_NOTIFY_ALWAYS.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import markdown

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"(?<![<(\[])https?://[^\s<>\"')\]]+")

ROOT = Path(__file__).resolve().parents[3]


def should_send(summary: dict) -> bool:
    if os.environ.get("JOBRADAR_NOTIFY_ALWAYS") == "1":
        return True
    # Signal days only: something matched, or the run was degraded (worth knowing).
    return bool(summary.get("matches")) or bool(summary.get("degraded"))


def build_message(summary: dict) -> tuple[str, str]:
    """Compose (subject, body) from a latest.json summary. Pure — no I/O."""
    headline = summary.get("headline") or "jobradar"
    subject = f"jobradar — {headline}"

    lines = [headline, ""]
    matches = summary.get("matches") or []
    for i, m in enumerate(matches):
        if i > 0:
            lines.append("---")
            lines.append("")
        tier = m.get("tier", "?").upper()
        lines.append(f"[{tier}] {m.get('title', '?')} @ {m.get('company', '?')}")
        if m.get("url"):
            lines.append(f"Link: {m['url']}")
        if m.get("liveness") == "unverified":
            lines.append(
                "⚠️ Link unverified — the posting page did not answer when checked; "
                "it may already be closed. Open it before reading on."
            )
        if m.get("location"):
            lines.append(f"Location: {m['location']}")
        skill = m.get("skill_score")
        interest = m.get("interest_score")
        if skill is not None and interest is not None:
            lines.append(f"Skill: {skill}/100 | Interest: {interest}/100")
        if m.get("writeup"):
            lines.append("")
            lines.append(m["writeup"])
        lines.append("")
    if summary.get("degraded"):
        failed = ", ".join(summary.get("failed_sources") or [])
        lines.append(f"Degraded run - source(s) failed: {failed}. Result may be incomplete.")
        lines.append("")
    if summary.get("date"):
        lines.append(f"Run date: {summary['date']}")
    return subject, "\n".join(lines)


def _smtp_config() -> dict | None:
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not (user and password):
        return None
    # Use `or default` rather than get(key, default): an UNSET CI secret is
    # passed as an empty string (the key exists), which would slip past a
    # get-default — and int("") would crash before main()'s try block.
    # EMAIL_TO/EMAIL_FROM default to SMTP_USER (the common "email myself" case).
    return {
        "host": os.environ.get("SMTP_HOST") or "smtp.gmail.com",
        "port": int(os.environ.get("SMTP_PORT") or "587"),
        "user": user,
        "password": password,
        "to": os.environ.get("EMAIL_TO") or user,
        "from": os.environ.get("EMAIL_FROM") or user,
    }


def build_html(subject: str, body: str) -> str:
    """An HTML rendering of the plain-text body, for mail clients that show
    it: clickable links, a rule between matches, readable line breaks. The
    plain text stays the primary part, so nothing is lost where HTML is off."""
    # Bare URLs become <url> so python-markdown autolinks them; nl2br keeps
    # the one-fact-per-line layout instead of merging lines into paragraphs.
    # Escape first: python-markdown passes raw HTML through, and a posting
    # title is untrusted text. quote=False keeps quotes readable in prose.
    linked = _URL_RE.sub(lambda m: f"<{m.group(0)}>", html.escape(body, quote=False))
    rendered = markdown.markdown(linked, extensions=["nl2br"])
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(subject)}</title></head>"
        "<body style=\"font-family: -apple-system, 'Segoe UI', Helvetica, Arial, "
        "sans-serif; font-size: 15px; line-height: 1.5; max-width: 40em; "
        "margin: 1em auto; padding: 0 1em; color: #222;\">"
        f"{rendered}</body></html>"
    )


def compose_email(subject: str, body: str, config: dict) -> EmailMessage:
    """The message as sent: plain text first, HTML as the alternative."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["from"]
    msg["To"] = config["to"]
    msg.set_content(body)
    msg.add_alternative(build_html(subject, body), subtype="html")
    return msg


def send_email(subject: str, body: str, config: dict) -> None:
    msg = compose_email(subject, body, config)
    context = ssl.create_default_context()
    with smtplib.SMTP(config["host"], config["port"]) as server:
        server.starttls(context=context)
        server.login(config["user"], config["password"])
        server.send_message(msg)


def main() -> None:
    summary_path = ROOT / "reports" / "latest.json"
    if not summary_path.exists():
        logger.warning("No %s found - nothing to notify", summary_path)
        return
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    if not should_send(summary):
        logger.info("Quiet day (no matches, not degraded) - skipping email")
        return

    config = _smtp_config()
    if config is None:
        logger.warning("SMTP not configured (need SMTP_USER and SMTP_PASS) - skipping email")
        return

    subject, body = build_message(summary)
    try:
        send_email(subject, body, config)
        logger.info("Sent notification email to %s", config["to"])
    except Exception as exc:  # noqa: BLE001 - email failure must not fail the job
        logger.error("Failed to send notification email: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
