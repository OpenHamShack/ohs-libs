"""The store's per-thread connections end with their threads.

A server built on ThreadingHTTPServer touches the store from a fresh
thread per request; every one of those used to leave its connection (the
database and its WAL, two fds) open until the store closed."""
import os
import sys
import threading

import pytest

from shack_client import Station

T = "2026-10-09T10:12:30Z"
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="counts /proc/self/fd")


@pytest.fixture
def st(tmp_path):
    s = Station.open("shack-qo100nb", "1.0.0", tmp_path / "station.db", background=False)
    yield s
    s.close()


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join()


@linux_only
def test_short_lived_threads_do_not_pile_up_connections(st):
    st.logbook.add({"call": "DL1ABC", "start": T})
    # Warm up: the sweep only runs when the next connection opens, so the
    # last thread's connection is always still waiting for it.
    for _ in range(5):
        _in_thread(st.logbook.count)
    before = _fds()
    for _ in range(200):
        _in_thread(st.logbook.count)
    # Unbounded, this would be about 400 more.
    assert _fds() - before <= 4


@linux_only
def test_release_thread_closes_the_callers_connection(st):
    seen = {}

    def work():
        st.logbook.count()
        seen["open"] = _fds()
        st.store.release_thread()
        seen["released"] = _fds()
        st.store.release_thread()           # a second call is harmless
        seen["count"] = st.logbook.count()  # and the store still works after

    _in_thread(work)
    assert seen["open"] - seen["released"] >= 1
    assert seen["count"] == 0


def test_release_thread_leaves_other_threads_connections_alone(st):
    st.logbook.add({"call": "DL1ABC", "start": T})
    _in_thread(st.store.release_thread)     # a thread with no connection
    _in_thread(lambda: (st.logbook.count(), st.store.release_thread()))
    assert st.logbook.count() == 1          # the main thread's still works


def test_close_still_closes_live_threads_connections(st):
    ready, done = threading.Event(), threading.Event()
    conns = []

    def worker():
        conns.append(st.store.db)
        ready.set()
        done.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    ready.wait(5)
    st.store.close()
    done.set()
    t.join()
    with pytest.raises(Exception, match="closed"):
        conns[0].execute("SELECT 1")


def test_close_waits_for_the_change_watcher(st):
    st.on_change(lambda *a: None)
    w = st._watch
    st.close()
    assert not w.is_alive()
