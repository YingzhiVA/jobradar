"""Is each job board still working, and still posting in the market?

    python -m jobradar.board_health                  # the boards you selected
    python -m jobradar.board_health --catalog ch     # also every board in a market's
                                                     # shared list (repeatable)
    python -m jobradar.board_health --only Squirro   # re-check one board, print only

Each board's listing is fetched once, rows only: no detail request, no profile,
no constraints, no model call. Its postings are counted, and those in the
market's country among them, and the board gets one of four labels:

- broken: the board is gone or misconfigured (a 404 or other client error, a
  malformed slug, an ATS with no connector). Act now: it has usually moved to
  another ATS, so the other ATSs are probed for a live board under the
  company's name.
- unreachable: a timeout, a network error or a server error (5xx, 429). Usually
  passing: EY's career site answers right at the timeout on a slow day.
- off-market: postings, but none in the market's country. The company may have
  left the country, or moved its local roles to another board.
- empty: no postings at all. Often just a company not hiring this month.
- healthy: postings in the market.

One check is a snapshot, and a small company can be quiet on the day, so
unreachable, off-market and empty only call for action when the previous check
of the same board said the same; a first sighting is listed as "watching".
Broken needs no second look. The history is data/board_health.jsonl, one line per board per
check; the report is reports/board_health.md, which the monthly discovery email
includes when it has something to act on.

This replaced a check that read the daily runs' per-board counts out of
reports/runs.jsonl. That one saw only the boards you selected, recorded a fetch
error as zero postings (broken looked like quiet for weeks), and counted before
the location filter (a board that stopped posting in the country looked alive).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import httpx

from .boards import BoardsError, catalog_problems, index_catalog, load_companies, read_catalog
from .config import ConfigError, load_search_settings
from .markets import MARKETS, Market
from .search.sources.base import RawPosting
from .search.sources.company_pages import (
    _FETCHERS,
    _TIMEOUT,
    LISTING_ROWS_ONLY,
    fetch_board,
    is_scoped_to_market,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = ROOT / "data" / "board_health.jsonl"
REPORT_PATH = ROOT / "reports" / "board_health.md"

BROKEN = "broken"
UNREACHABLE = "unreachable"
OFF_MARKET = "off-market"
EMPTY = "empty"
HEALTHY = "healthy"

# The statuses that only call for action when the previous check agrees.
_SECOND_LOOK = (UNREACHABLE, OFF_MARKET, EMPTY)

# Locations quoted for an off-market board, so the report shows where it does
# post without opening it.
_SAMPLE_LOCATIONS = 3


@dataclass
class BoardResult:
    market: str
    name: str
    ats: str
    slug: str
    # Which file to fix: a config/boards/ file, or "your companies.yaml".
    where: str
    status: str
    total: int = 0
    in_market: int = 0
    note: str = ""
    sample_locations: list[str] = field(default_factory=list)
    # A live board found on another ATS, "ats/slug", for a broken board.
    moved_to: str = ""
    # Whether the status calls for action now (see the module docstring).
    actionable: bool = False


def _in_market(posting: RawPosting, ats: str, market: Market) -> bool:
    """Whether a posting is in the market's country, as far as its listing row
    can tell. A connector that filters to the country server-side or in code
    (Workday, SuccessFactors, ...) returns nothing else, and its rows often say
    only "2 Locations". Elsewhere the location has to name the country or a town
    the market knows; a row with no location at all (Avature, BrassRing) gets
    the benefit of the doubt."""
    if is_scoped_to_market(ats):
        return True
    location = (posting.location or "").strip()
    if not location:
        return True
    return market.mentions_country(location) or market.region_in(location) is not None


def classify(postings: list[RawPosting], ats: str, market: Market) -> tuple[str, int, int, list[str]]:
    """(status, total, in market, sample locations of an off-market board)."""
    total = len(postings)
    in_market = sum(1 for p in postings if _in_market(p, ats, market))
    if total == 0:
        return EMPTY, 0, 0, []
    if in_market == 0:
        samples = list(dict.fromkeys(p.location for p in postings if p.location))
        return OFF_MARKET, total, 0, samples[:_SAMPLE_LOCATIONS]
    return HEALTHY, total, in_market, []


def check_board(board: dict, client: httpx.Client, market: Market) -> BoardResult:
    result = BoardResult(
        market=market.code,
        name=board["name"],
        ats=board["ats"],
        slug=board["slug"],
        where=board.get("file") or "your companies.yaml",
        status=BROKEN,
    )
    if board["ats"].lower() not in _FETCHERS:
        result.note = f"no connector for ATS {board['ats']!r}"
        return result
    try:
        postings = fetch_board(
            board["name"], board["ats"], board["slug"], client,
            known=LISTING_ROWS_ONLY, market=market,
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code >= 500 or code == 429:
            result.status = UNREACHABLE
        result.note = f"HTTP {code} at {exc.request.url}"
        return result
    except httpx.TransportError as exc:  # timeouts, refused or dropped connections
        result.status = UNREACHABLE
        result.note = " ".join(str(exc).split())[:200] or type(exc).__name__
        return result
    except (httpx.HTTPError, ValueError) as exc:
        result.note = " ".join(str(exc).split())[:200]
        return result
    except Exception as exc:  # noqa: BLE001 - one bad board must not end the check
        result.note = f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"
        return result
    result.status, result.total, result.in_market, result.sample_locations = classify(
        postings, board["ats"], market
    )
    return result


def _identity(record: dict) -> tuple[str, str, str]:
    return record["market"], record["ats"], record["slug"]


def load_history(path: Path) -> list[dict]:
    """Past results, oldest first. Unreadable lines are skipped: a damaged
    history only costs the "two checks in a row" memory, never the check."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and all(k in record for k in ("market", "ats", "slug", "status")):
            records.append(record)
    return records


def mark_actionable(results: list[BoardResult], history: list[dict]) -> None:
    """Broken always; unreachable, off-market and empty when the board's
    previous check had the same status."""
    previous = {_identity(r): r["status"] for r in history}
    for result in results:
        if result.status == BROKEN:
            result.actionable = True
        elif result.status in _SECOND_LOOK:
            result.actionable = previous.get(_identity(asdict(result))) == result.status


def append_history(path: Path, results: list[BoardResult], run_date: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps({
                "date": run_date.isoformat(), "market": r.market, "name": r.name,
                "ats": r.ats, "slug": r.slug, "status": r.status,
                "total": r.total, "in_market": r.in_market,
            }, ensure_ascii=False) + "\n")


def find_moved_boards(results: list[BoardResult], client: httpx.Client) -> None:
    """For each broken board, a live board under the company's name on another
    ATS, skipping the broken one and any board a human has flagged as a
    different company with the same name (the discovery ledger's rejections)."""
    broken = [r for r in results if r.status == BROKEN]
    if not broken:
        return
    from .discovery import discovery_ledger
    from .discovery.discover import _LEDGER_PATH, probe_company

    ledger = discovery_ledger.load_ledger(_LEDGER_PATH)
    for result in broken:
        skip = {(result.ats, result.slug)} | discovery_ledger.rejected_boards(ledger, result.name)
        try:
            match = probe_company(result.name, client, skip=skip)
        except Exception as exc:  # noqa: BLE001 - a failed probe only loses the hint
            logger.warning("Probing other ATSs for %s failed: %s", result.name, exc)
            continue
        if match is not None:
            result.moved_to = f"{match.ats}/{match.slug}"


def _line(r: BoardResult, multi_market: bool) -> str:
    tag = f" [{r.market}]" if multi_market else ""
    head = f"- **{r.name}**{tag} ({r.ats}/{r.slug}, in {r.where})"
    if r.status == BROKEN:
        moved = f" **Possibly moved to {r.moved_to}.**" if r.moved_to else ""
        return f"{head} — {r.note or 'unreadable'}.{moved}"
    if r.status == UNREACHABLE:
        return f"{head} — {r.note or 'no answer'}."
    if r.status == OFF_MARKET:
        where = f" (e.g. {'; '.join(r.sample_locations)})" if r.sample_locations else ""
        country = MARKETS[r.market].country if r.market in MARKETS else r.market
        return f"{head} — {r.total} posting(s), none in {country}{where}."
    return f"{head} — no postings."


_SECTIONS = [
    (BROKEN, "Broken — fix now (the board is gone or misconfigured; it has usually moved)"),
    (UNREACHABLE, "Unreachable — timed out or failed, two checks in a row"),
    (OFF_MARKET, "Off-market — no postings in the country, two checks in a row"),
    (EMPTY, "Empty — no postings, two checks in a row"),
]


def render_report(results: list[BoardResult], scope: str, run_date: date) -> str:
    """The report. Only actionable boards get a "## " section and a "- **"
    line: notify_discovery counts those to decide whether to email."""
    multi_market = len({r.market for r in results}) > 1
    counts = {
        s: sum(1 for r in results if r.status == s)
        for s in (HEALTHY, OFF_MARKET, EMPTY, UNREACHABLE, BROKEN)
    }
    lines = [
        f"# Board health — {run_date.isoformat()}",
        "",
        f"Checked {len(results)} board(s): {scope}. "
        f"{counts[HEALTHY]} healthy, {counts[OFF_MARKET]} off-market, "
        f"{counts[EMPTY]} empty, {counts[UNREACHABLE]} unreachable, {counts[BROKEN]} broken.",
    ]
    acted = False
    for status, heading in _SECTIONS:
        group = [r for r in results if r.status == status and r.actionable]
        if group:
            acted = True
            lines += ["", f"## {heading}", ""]
            lines += [_line(r, multi_market) for r in group]
    watching = [r for r in results if r.status in _SECOND_LOOK and not r.actionable]
    if watching:
        names = ", ".join(
            f"{r.name} ({r.status}{', ' + r.market if multi_market else ''})" for r in watching
        )
        lines += [
            "",
            f"Watching, first time this way (flagged if the next check agrees): {names}.",
        ]
    if not acted:
        lines += ["", "Nothing to act on."]
    return "\n".join(lines) + "\n"


def _boards_to_check(root: Path, catalog_markets: list[Market]) -> tuple[list[tuple[Market, dict]], list[str]]:
    """(market, board) pairs: the selection in the configured market, then each
    requested market's whole catalogue, each board once per market. Also a
    description of that scope for the report."""
    try:
        market = load_search_settings(root).market
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    pairs: list[tuple[Market, dict]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(m: Market, board: dict) -> None:
        key = (m.code, board["ats"].lower(), board["slug"])
        if key not in seen:
            seen.add(key)
            pairs.append((m, board))

    try:
        selection = load_companies(root, market)
    except BoardsError as exc:
        raise SystemExit(str(exc)) from exc
    for board in selection.boards:
        add(market, board)
    scope = [f"your selection ({market.country})"]
    for m in catalog_markets:
        try:
            entries = read_catalog(root / "config", m)
        except BoardsError as exc:
            raise SystemExit(str(exc)) from exc
        for problem in catalog_problems(entries):
            logger.warning("%s", problem)
        for entry in index_catalog(entries).values():
            add(m, {**entry, "ats": str(entry["ats"]).lower(), "slug": str(entry["slug"])})
        scope.append(f"the {m.code} shared list")
    return pairs, scope


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--catalog", action="append", default=[], metavar="MARKET",
        help=f"also check every board in this market's shared list ({', '.join(MARKETS)}); repeatable",
    )
    parser.add_argument(
        "--only", metavar="NAME",
        help="check only the board(s) with this name and print the result; "
        "writes neither the report nor the history",
    )
    args = parser.parse_args(argv)
    unknown = [code for code in args.catalog if code.lower() not in MARKETS]
    if unknown:
        parser.error(f"unknown market(s) {', '.join(unknown)}; known: {', '.join(MARKETS)}")
    catalog_markets = [MARKETS[code.lower()] for code in args.catalog]

    pairs, scope = _boards_to_check(ROOT, catalog_markets)
    if args.only:
        wanted = " ".join(args.only.split()).casefold()
        pairs = [(m, b) for m, b in pairs if " ".join(b["name"].split()).casefold() == wanted]
        if not pairs:
            raise SystemExit(f"No board named {args.only!r} in what this command checks.")

    with httpx.Client(timeout=_TIMEOUT) as client:
        results = []
        for m, board in pairs:
            result = check_board(board, client, m)
            logger.info("%s [%s]: %s (%d postings, %d in market)",
                        result.name, m.code, result.status, result.total, result.in_market)
            results.append(result)
        find_moved_boards(results, client)

    today = date.today()
    if args.only:
        for r in results:
            if r.status == HEALTHY:
                print(f"healthy: **{r.name}** [{r.market}] ({r.ats}/{r.slug}) — "
                      f"{r.total} posting(s), {r.in_market} in the market.")
            else:
                print(f"{r.status}: {_line(r, multi_market=True)[2:]}")
        return
    mark_actionable(results, load_history(HISTORY_PATH))
    append_history(HISTORY_PATH, results, today)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(results, ", ".join(scope), today), encoding="utf-8")
    flagged = sum(r.actionable for r in results)
    logger.info("Board health: %d board(s) to act on — see %s", flagged, REPORT_PATH)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # One line per board is the progress; one per HTTP request buries it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
