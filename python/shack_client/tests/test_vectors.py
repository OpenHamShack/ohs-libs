"""Runs spec/vectors/sync/T1-T21 (SYNC.md §8) against shack_client's
Station and the reference controller. The vector files are the contract
every implementation runs (Kotlin, Swift and the Dock360 edition later);
this file is the Python runner of their step language, described in
spec/schemas/sync-vector.schema.json.

Time is simulated: one true time advanced by `tick`, and each store or
controller sees true time plus its own skew, so clock faults (a Pi in
1970, a laptop two days fast) are exact and the suite is fast.
"""
import json
import multiprocessing
import socket
import time
from pathlib import Path

import pytest

from shack_client import NeedsDecision, Station
from shack_client import hlc as H
from shack_client.merge import is_live, join_profile
from shack_client.testing import Network, ReferenceController

ROOT = Path(__file__).resolve().parents[3]
VECTORS = sorted((ROOT / "spec" / "vectors" / "sync").glob("T*.json"))


def load(p):
    return json.loads(p.read_text())


class World:
    def __init__(self, vec: dict, tmp: Path):
        self.vec = vec
        self.tmp = tmp
        self.t = H.parse_iso_ms(vec["start"])
        self.skew: dict[str, int] = {}
        self.net = Network()
        self.ctls: dict[str, ReferenceController] = {}
        self.stores: dict[str, Station] = {}
        self.calls: dict[str, int] = {}
        self.marks: dict[str, int] = {}
        self.backups: dict[str, bytes] = {}
        for name, c in vec.get("controllers", {}).items():
            self.skew[name] = c.get("skewMs", 0)
            ctl = ReferenceController(tmp / f"{name}.db", name=c.get("name", name), now_ms=self.clock(name),
                                      users=json.loads(json.dumps(c.get("users")))
                                      if c.get("users") else None)
            self.ctls[name] = ctl
            self.net.add(self.url(name), ctl)
        for name, s in vec.get("stores", {}).items():
            self.skew[name] = s.get("skewMs", 0)
            if "clock" in s:
                self.skew[name] = H.parse_iso_ms(s["clock"]) - self.t
            self.calls[name] = 0
            self.stores[name] = Station.open("shack-vector", "0.0", tmp / name / "station.db", clock=self.clock(name),
                                             transport_factory=self.factory(name), background=False)

    @staticmethod
    def url(name):
        return f"http://{name.lower()}.test/"

    def clock(self, name):
        return lambda: self.t + self.skew[name]

    def factory(self, store):
        def make(base, token):
            t = self.net.factory(base, token)
            inner = t.request

            def counted(*a, **kw):
                self.calls[store] += 1
                return inner(*a, **kw)
            t.request = counted
            return t
        return make

    # ---------------------------------------------------------- steps
    def run(self, steps):
        for s in steps:
            getattr(self, "do_" + s["do"])(s)

    def do_tick(self, s):
        self.t += s["ms"]

    def do_skew(self, s):
        self.skew[s["who"]] = s["ms"]
        if s.get("resetOffset"):
            st = self.stores[s["who"]].store
            with st.tx():
                st.set_meta("clock_offset_ms", 0)

    def do_add(self, s):
        st = self.stores[s["store"]]
        rid = s.get("id")
        if "idFromPage" in s:
            self.page = getattr(self, "page", 0) + 1
            rid = f"{s['idFromPage']}{self.page:012d}"
        st.logbook.add(s["fields"], rid)

    def do_addMany(self, s):
        self.stores[s["store"]].logbook.add_many(
            [{"uuid": f"{s['idPrefix']}{i:012d}", "call": f"T{i}", "start": "2026-10-09T09:00:00Z"}
             for i in range(s["count"])])

    def do_update(self, s):
        fields = s.get("fields") or {s["fieldsFromPage"]: f"page {getattr(self, 'page', 0)}"}
        self.stores[s["store"]].logbook.update(s["id"], fields)

    def do_delete(self, s):
        self.stores[s["store"]].logbook.delete(s["id"])

    def do_profile(self, s):
        self.stores[s["store"]].profile.update(s["set"])

    def do_pair(self, s):
        st = self.stores[s["store"]]
        if "pageLimit" in s:
            st.sync.page_limit = s["pageLimit"]
        if "duringEachPage" in s:
            st.sync.hooks["after_pull_page"] = lambda ans, steps=s["duringEachPage"]: self.run(steps)
        res = st.pair(self.url(s["controller"]), s.get("user", "brian"), s.get("password", "pw"), device_name=s["store"])
        st.sync.hooks.pop("after_pull_page", None)
        got = "needsDecision" if isinstance(res, NeedsDecision) else "paired"
        assert got == s.get("expect", "paired"), f"pair {s['store']} -> {s['controller']}: {got}"
        self.last_pair = res

    def do_resolve(self, s):
        self.stores[s["store"]].resolve(self.last_pair, s["choice"])

    def do_unpair(self, s):
        self.stores[s["store"]].unpair()

    def do_sync(self, s):
        st = self.stores[s["store"]]
        if s.get("fromSeq") == 0:
            p = st.pairing_info()
            st.store.drop_cursor(p["controllerId"])
        status = st.sync.run_once()
        assert status["state"] == s.get("expectState", "idle"), status

    def do_dropNextPushAnswer(self, s):
        self.ctls[s["controller"]].drop_next_push_answer = True

    def do_offline(self, s):
        self.ctls[s["controller"]].offline = s["value"]

    def do_backup(self, s):
        self.backups[s["name"]] = self.ctls[s["controller"]].snapshot()

    def do_restore(self, s):
        self.ctls[s["controller"]].restore(self.backups[s["name"]])

    def do_inject(self, s):
        status, ans = self.ctls[s["controller"]].sync.push({"records": [s["record"]]})
        assert status == 200 and ans["records"][0]["status"] == "merged", ans

    def do_markRequests(self, s):
        self.marks[s["store"]] = self.calls[s["store"]]

    # ---------------------------------------------------------- views
    def records(self, who, collection):
        if who in self.ctls:
            return {r["id"]: {k: v for k, v in r.items() if k != "seq"} for r in self.ctls[who].sync.all(collection)}
        return {r["id"]: r for r in self.stores[who].store.all(collection)}

    def stamps(self, who):
        out = []
        for coll in ("logbook", "profile"):
            for r in self.records(who, coll).values():
                out += list(r["fieldHlc"].values()) + ([r["deletedHlc"]] if r.get("deletedHlc") else [])
        return out

    # ---------------------------------------------------------- expectations
    def check(self, e):
        (kind, a), = e.items()
        getattr(self, "expect_" + kind)(a)

    def expect_record(self, a):
        for who in a["where"]:
            r = self.records(who, a["collection"]).get(a["id"])
            assert r is not None, f"{who} lacks {a['id']}"
            if "live" in a:
                assert is_live(r) == a["live"], f"{who}: live is {is_live(r)}"
            for k, v in a.get("fields", {}).items():
                assert r["fields"].get(k) == v, f"{who}: {k} = {r['fields'].get(k)!r}, expected {v!r}"
            for k, v in a.get("extra", {}).items():
                assert r.get(k) == v, f"{who}: envelope {k} = {r.get(k)!r}"

    def expect_count(self, a):
        for who in a["where"]:
            n = sum(1 for r in self.records(who, a["collection"]).values() if is_live(r))
            assert n == a["live"], f"{who} has {n} live records, expected {a['live']}"

    def expect_countAtLeast(self, a):
        for who in a["where"]:
            n = sum(1 for r in self.records(who, a["collection"]).values() if is_live(r))
            assert n >= a["live"], f"{who} has {n} live records, expected at least {a['live']}"

    def expect_converged(self, a):
        def view(who):
            return {i: (is_live(r), r["fields"], r["fieldHlc"], r.get("deletedHlc"))
                    for i, r in self.records(who, a["collection"]).items()}
        first = a["where"][0]
        for who in a["where"][1:]:
            x, y = view(first), view(who)
            assert set(x) == set(y), f"{first} and {who} hold different records: {set(x) ^ set(y)}"
            diff = [i for i in x if x[i] != y[i]]
            assert not diff, f"{first} and {who} differ on {diff[:3]}: {x[diff[0]]} / {y[diff[0]]}"

    def expect_history(self, a):
        h = self.ctls[a["controller"]].sync.history("logbook", a["id"])["edits"]
        if "edits" in a:
            assert len(h) == a["edits"], h
        if "lostFields" in a:
            assert sorted({f for e in h for f in e["lost"]}) == sorted(a["lostFields"]), h
        if "kinds" in a:
            assert sorted(e["kind"] for e in h) == sorted(a["kinds"]), h

    def expect_status(self, a):
        st = self.stores[a["store"]].sync.status()
        for k, v in a.items():
            if k != "store":
                assert st.get(k) == v, st

    def expect_noFutureHlc(self, a):
        limit = self.ctls[a["controller"]].now_ms() + H.MAX_AHEAD_MS
        bad = [s for s in self.stamps(a["controller"]) if H.wall_of(s) > limit]
        assert not bad, bad

    def expect_hlcAtLeastFloor(self, a):
        stamps = self.stamps(a["store"])
        assert stamps and all(H.wall_of(s) >= H.CLOCK_FLOOR_MS for s in stamps), stamps

    def expect_archive(self, a):
        d = self.stores[a["store"]].store.path.parent
        assert list(d.glob("station-*.db")) and list(d.glob("station-*.adi"))

    def expect_requests(self, a):
        assert self.calls[a["store"]] - self.marks[a["store"]] == a["sinceMark"]

    def expect_profile(self, a):
        def contains(big, small):
            return all(k in big and (contains(big[k], v) if isinstance(v, dict) else big[k] == v)
                       for k, v in small.items())
        for who in a["where"]:
            got = self.stores[who].profile.get()
            assert contains(got, a["has"]), f"{who}: {got}"

    def close(self):
        for s in self.stores.values():
            s.close()
        for c in self.ctls.values():
            c.close()


SCRIPTS = [p for p in VECTORS if load(p)["runner"] == "script"]


def test_every_case_of_the_spec_has_a_vector():
    assert [load(p)["id"] for p in VECTORS] == [f"T{i}" for i in range(1, 22)]


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_sync_vector(path, tmp_path):
    vec = load(path)
    w = World(vec, tmp_path)
    try:
        w.run(vec["steps"])
        for e in vec["expect"]:
            w.check(e)
    finally:
        w.close()


def test_T14_unreachable_controller_never_slows_a_write(tmp_path):
    """A controller that accepts nothing and answers nothing: a socket
    listening with no one accepting, so the connect hangs as dropped
    packets do. Adding a QSO while a sync round is stuck on it returns
    within the vector's budget."""
    vec = load(ROOT / "spec" / "vectors" / "sync" / "T14.json")
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(0)
    # Fill the backlog so further connects stall rather than complete.
    fillers = []
    for _ in range(8):
        f = socket.socket()
        f.setblocking(False)
        try:
            f.connect(hole.getsockname())
        except BlockingIOError:
            pass
        fillers.append(f)
    url = f"http://127.0.0.1:{hole.getsockname()[1]}/"
    from shack_client.transport import HttpTransport
    st = Station.open("shack-vector", "0.0", tmp_path / "a" / "station.db", background=False,
                      transport_factory=lambda base, tok: HttpTransport(base, tok, timeout=1.0))
    try:
        st._write_pairing({"controllerId": "c", "baseUrl": url, "token": "shackdev_x", "deviceId": "", "user": "",
                           "maxLevel": "transmit", "name": "hole"})
        st.sync.now()
        time.sleep(0.05)
        worst = 0.0
        for i in range(20):
            t0 = time.perf_counter()
            st.logbook.add({"call": f"DL{i}ABC", "start": "2026-10-09T10:12:30Z"})
            worst = max(worst, time.perf_counter() - t0)
        assert worst * 1000 < vec["budgetMs"], f"slowest add took {worst * 1000:.1f} ms"
        deadline = time.time() + 5
        while st.sync.status()["state"] != "offline" and time.time() < deadline:
            time.sleep(0.05)
        assert st.sync.status()["state"] == "offline"
    finally:
        st.close()
        for f in fillers:
            f.close()
        hole.close()


def _writer(path, n, tag):
    from shack_client import Station as S
    s = S.open("shack-vector", "0.0", path, background=False)
    for i in range(n):
        s.logbook.add({"call": f"{tag}{i}X", "start": "2026-10-09T10:12:30Z"})
    s.close()


def test_T18_two_processes_one_store(tmp_path):
    vec = load(ROOT / "spec" / "vectors" / "sync" / "T18.json")
    path = tmp_path / "shared" / "station.db"
    Station.open("shack-vector", "0.0", path, background=False).close()
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(path), vec["perProcess"], f"P{k}A"))
             for k in range(vec["processes"])]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    st = Station.open("shack-vector", "0.0", path, background=False)
    try:
        assert st.logbook.count() == vec["processes"] * vec["perProcess"]
        stamps = [h for (h,) in st.store.db.execute("SELECT hlc FROM ops")]
        assert len(stamps) == len(set(stamps)), "two edits shared a stamp"
        # Only one process syncs at a time: the lease.
        other = Station.open("shack-vector", "0.0", path, background=False)
        try:
            assert st.store.take_lease(st.sync.holder)
            assert not other.store.take_lease(other.sync.holder)
            st.store.release_lease(st.sync.holder)
            assert other.store.take_lease(other.sync.holder)
        finally:
            other.close()
    finally:
        st.close()


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_vector_files_match_their_schema(path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "spec" / "schemas" / "sync-vector.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(load(path))
