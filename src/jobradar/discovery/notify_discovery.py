"""Email a summary of the companies a discovery run surfaced.

The monthly discovery workflow runs `jobradar.discover` (which writes
data/discovered_companies.yaml) and then this, so you get a proactive
"here are N new companies to review" email rather than having to watch the repo.
Reuses notify.py's SMTP config/sender. Degrades gracefully (exit 0) when there's
nothing to send or SMTP isn't configured.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ..search import notify

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]


def load_suggestions(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("companies", []) if isinstance(data, dict) else []


def load_health_report(path: Path) -> str:
    """The board-health report body when it flagged something, else "".

    discover.py always writes the report (so the artifact reflects the current
    state in git), rendering "## <group>" sections only when a board is flagged;
    a clean run has none, so their presence is the has-findings signal.
    """
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    return text if "\n## " in text else ""


def build_message(suggestions: list[dict], health_report: str = "") -> tuple[str, str]:
    """Compose (subject, body) for the monthly discovery summary. Pure — no I/O."""
    n = len(suggestions)
    n_flagged = health_report.count("\n- **")  # one list item per flagged board

    subject_parts = []
    if n:
        subject_parts.append(f"{n} new compan{'y' if n == 1 else 'ies'}")
    if n_flagged:
        subject_parts.append(f"{n_flagged} board{'' if n_flagged == 1 else 's'} to check")
    subject = "jobradar discovery — " + (", ".join(subject_parts) or "nothing to review")

    sections: list[str] = []
    if suggestions:
        lines = [
            f"Discovery surfaced {n} ATS-verified compan{'y' if n == 1 else 'ies'}. "
            "Review and copy the good ones into config/companies.yaml:",
            "",
        ]
        for c in suggestions:
            name = c.get("name", "?")
            ats = c.get("ats", "?")
            slug = c.get("slug", "?")
            verified = c.get("company_name")
            suffix = f"  [{verified}]" if verified else ""
            lines.append(f"  - {name} ({ats} / {slug}){suffix}")
        sections.append("\n".join(lines))
    if health_report:
        sections.append(health_report.strip())
    return subject, "\n\n".join(sections)


def main() -> None:
    suggestions = load_suggestions(ROOT / "data" / "discovered_companies.yaml")
    health_report = load_health_report(ROOT / "reports" / "company_health.md")
    if not suggestions and not health_report:
        logger.info("Nothing to email (no new companies, no board-health flags).")
        return
    config = notify._smtp_config()
    if config is None:
        logger.warning("SMTP not configured (need SMTP_USER and SMTP_PASS) - skipping email")
        return
    subject, body = build_message(suggestions, health_report)
    try:
        notify.send_email(subject, body, config)
        logger.info(
            "Sent discovery summary (%d companies, %d boards flagged) to %s",
            len(suggestions), health_report.count("\n- **"), config["to"],
        )
    except Exception as exc:  # noqa: BLE001 - email failure must not fail the job
        logger.error("Failed to send discovery email: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
