from jobradar.apply.queue import parse_queue, prune_queue
from jobradar.apply.util import normalize_url, slugify


def test_parse_queue_basics():
    text = """
# comment
https://jobs.example.com/pm-123

https://careers.example.org/ai-pm/ full
not-a-url
https://jobs.example.com/pm-123
"""
    entries = parse_queue(text)
    assert [e.url for e in entries] == [
        "https://jobs.example.com/pm-123",
        "https://careers.example.org/ai-pm",
    ]
    assert entries[0].full is False
    assert entries[1].full is True


def test_parse_queue_dedups_normalized_urls():
    text = "https://x.example/a/\nhttps://x.example/a#section\n"
    assert len(parse_queue(text)) == 1


def test_prune_queue_removes_only_matching_urls(tmp_path):
    path = tmp_path / "queue.txt"
    text = (
        "# keep this comment\n"
        "https://jobs.example.com/pm-123\n"
        "\n"
        "https://careers.example.org/ai-pm/ full\n"
        "not-a-url\n"
    )
    path.write_text(text, encoding="utf-8")

    # Normalized matching: the queue line has a trailing slash + flag, the
    # tracker key does not.
    removed = prune_queue(path, {"https://careers.example.org/ai-pm"})

    assert removed == 1
    assert path.read_text(encoding="utf-8") == (
        "# keep this comment\n"
        "https://jobs.example.com/pm-123\n"
        "\n"
        "not-a-url\n"
    )


def test_prune_queue_without_match_leaves_file_untouched(tmp_path):
    path = tmp_path / "queue.txt"
    path.write_text("https://jobs.example.com/pm-123\n", encoding="utf-8")
    before = path.stat().st_mtime_ns
    assert prune_queue(path, {"https://x.example/other"}) == 0
    assert path.stat().st_mtime_ns == before


def test_prune_queue_missing_file_is_noop(tmp_path):
    assert prune_queue(tmp_path / "queue.txt", {"https://x.example/a"}) == 0


def test_normalize_url():
    assert normalize_url("  https://x.example/a/#frag ") == "https://x.example/a"
    # Query strings carry ATS job ids and must survive.
    assert normalize_url("https://x.example/jobs?id=42") == "https://x.example/jobs?id=42"


def test_slugify():
    assert slugify("AI Product Manager – Wealth Management") == "ai-product-manager-wealth-management"
    assert slugify("Zürich AG") == "zurich-ag"
    assert slugify("!!!") == "unknown"
    assert len(slugify("x" * 100)) <= 40
