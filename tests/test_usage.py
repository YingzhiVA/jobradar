import logging
import threading
import time

from jobradar import usage


class _Usage:
    def __init__(self, creation, read, inp):
        self.cache_creation_input_tokens = creation
        self.cache_read_input_tokens = read
        self.input_tokens = inp


class _Response:
    def __init__(self, usage=None):
        self.usage = usage


def test_accumulate_sums_across_calls():
    acc = {}
    usage.accumulate(acc, _Response(_Usage(100, 0, 20)))    # first call: cache write
    usage.accumulate(acc, _Response(_Usage(0, 100, 20)))    # second: cache read
    assert acc["calls"] == 2
    assert acc["cache_creation"] == 100
    assert acc["cache_read"] == 100
    assert acc["input"] == 40


def test_accumulate_handles_missing_usage():
    acc = {}
    usage.accumulate(acc, _Response(usage=None))
    assert acc == {}


def test_accumulate_handles_missing_fields():
    # A usage object without cache fields (e.g. older shape) must not crash.
    class _Bare:
        input_tokens = 10

    usage.accumulate({}, _Response(_Bare()))  # should not raise


def test_log_summary_noop_when_no_calls(caplog):
    with caplog.at_level(logging.INFO):
        usage.log_summary(logging.getLogger("t"), "scoring", {})
    assert caplog.records == []


def test_log_summary_emits_when_calls(caplog):
    acc = {"calls": 3, "cache_creation": 100, "cache_read": 200, "input": 30}
    with caplog.at_level(logging.INFO):
        usage.log_summary(logging.getLogger("t"), "scoring", acc)
    msg = caplog.text
    assert "scoring cache" in msg
    assert "cache-read" in msg


# --- thread safety --------------------------------------------------------
#
# matching.score_postings shares one accumulator across a thread pool. Each
# line in accumulate() is a read-modify-write (`acc.get(...) + ...` then
# store), so without the lock two threads can both read the old value and one
# update is lost. The counts only feed the cache log line, so a lost update is
# silent — hence a test that forces the interleaving rather than hoping for it.


class _SlowUsage:
    """Sleeps between the accumulator's read and its write.

    accumulate() evaluates `acc.get(key, 0)` before `getattr(usage, key)`, so
    delaying the attribute read parks every thread in exactly the window where
    an unsynchronised update would be lost.
    """

    def __init__(self, delay):
        self._delay = delay

    def _slow(self, value):
        time.sleep(self._delay)
        return value

    @property
    def cache_creation_input_tokens(self):
        return self._slow(1)

    @property
    def cache_read_input_tokens(self):
        return self._slow(10)

    @property
    def input_tokens(self):
        return self._slow(5)


def _accumulate_from_threads(lock, threads=8, delay=0.02):
    acc = {}
    barrier = threading.Barrier(threads)

    def worker():
        barrier.wait()  # all threads enter accumulate() together
        usage.accumulate(acc, _Response(_SlowUsage(delay)), lock)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return acc


def test_accumulate_with_a_lock_loses_nothing_under_contention():
    acc = _accumulate_from_threads(threading.Lock())
    assert acc == {"calls": 8, "cache_creation": 8, "cache_read": 80, "input": 40}


def test_accumulate_without_a_lock_does_lose_updates():
    """Pins why the lock parameter exists: the same contention without it
    demonstrably drops updates. If this ever stops losing counts the guard
    above has become untestable and should be revisited, not deleted.
    """
    acc = _accumulate_from_threads(None)
    assert acc["cache_read"] < 80
