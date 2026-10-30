"""LiveHub: what a client holds after the frames a session sends, checked
by applying them with the reference applier, as a renderer would."""
import asyncio
import json
import queue
import random
import threading
import time

from shack_ui import A, LiveHub, Screen, apply_frame, drawn_keys, item, p, public_state, state_from_document

DOC = None


def doc():
    s = Screen("t/live", level="control")
    s.collection("c", {"base": {"label": "From the document"}})
    s.selection("one", from_="c")
    s.root = p.column(p.text("Starting", id="status"),
                      p.list("rows", from_="c", selection="one", template=p.text(item("label"))),
                      p.chart("snr", [{"mark": "line", "points": []}]),
                      p.panel("side", p.text("x", id="inner")))
    return s.json()


class Client:
    """A renderer's state, fed by one session's frames."""

    def __init__(self, ch, name="tv", max_hz=None):
        self.session = ch.connect(name, max_hz=max_hz)
        self.start = state_from_document(DOC)
        self.state = self.start
        self.frames = []

    def pump(self, now):
        f = self.session.frame(now)
        if f is not None:
            self.frames.append(f)
            self.state = apply_frame(self.state, f, self.start)
        return f


def setup_function():
    global DOC
    DOC = doc()


def channel(**kw):
    return LiveHub(clock=lambda: 0.0, **kw).channel("t/live", document=DOC)


def same_as_view(client, ch):
    """The client holds exactly the channel's live state (with its extras)."""
    view = ch.view(client.session)
    view.update(toasts=[], reload=False)
    mine = public_state(client.state)
    theirs = public_state(view)
    mine.update(toasts=[], reload=False)
    return mine == theirs


def test_the_first_frame_is_a_snap_of_the_document_plus_changes():
    ch = channel()
    ch.up("c", "a", label="A")
    c = Client(ch)
    assert c.session.hello() == {"t": "hello", "rev": DOC["rev"], "maxHz": 2.0}
    f = c.pump(0.0)
    assert f["t"] == "snap" and f["seq"] == 1
    assert f["ops"] == [{"op": "up", "c": "c", "items": {"a": {"label": "A"}}}]
    assert drawn_keys(c.state, "c") == ["base", "a"]


def test_many_moves_between_frames_are_one_up_with_the_latest():
    ch = channel()
    c = Client(ch)
    c.pump(0.0)
    for i in range(100):
        ch.up("c", "a", lat=i, lon=-i)
    assert c.pump(0.1) is None                      # 2 Hz: not yet
    f = c.pump(0.5)
    assert f == {"t": "p", "seq": 2, "ops": [{"op": "up", "c": "c", "items": {"a": {"lat": 99, "lon": -99}}}]}
    assert c.pump(1.0) is None                      # nothing new: nothing sent


def test_a_frame_carries_only_changed_fields_and_nulls_for_deleted_ones():
    ch = channel()
    ch.up("c", "a", lat=1, lon=2, label="A")
    c = Client(ch)
    c.pump(0.0)
    ch.up("c", "a", lat=1, lon=3, label=None)
    assert c.pump(1.0)["ops"] == [{"op": "up", "c": "c", "items": {"a": {"lon": 3, "label": None}}}]


def test_trails_and_charts_go_as_the_new_points_only():
    ch = channel()
    c = Client(ch)
    ch.up("c", "a", label="A")
    for i in range(5):
        ch.push("c", "a", "trail", [[i, i]], max=3)
    c.pump(0.0)
    for i in range(5, 7):
        ch.push("c", "a", "trail", [[i, i]], max=3)
        ch.append("snr", [[i, -i]], max=4)
    f = c.pump(1.0)
    assert {"op": "push", "c": "c", "key": "a", "f": "trail", "v": [[5, 5], [6, 6]], "max": 3} in f["ops"]
    assert {"op": "append", "id": "snr", "mark": 0, "v": [[5, -5], [6, -6]], "max": 2} in f["ops"]
    assert c.state["collections"]["c"]["items"]["a"]["trail"] == [[4, 4], [5, 5], [6, 6]]
    assert same_as_view(c, ch)


def test_removal_and_order():
    ch = channel()
    for k in "abc":
        ch.up("c", k, label=k)
    ch.order("c", ["c", "b", "a", "base"])
    c = Client(ch)
    c.pump(0.0)
    assert drawn_keys(c.state, "c") == ["c", "b", "a", "base"]
    ch.rm("c", "b")
    f = c.pump(1.0)
    assert f["ops"] == [{"op": "rm", "c": "c", "keys": ["b"]}]          # rm already fixes the order
    ch.order("c", ["c", "a", "base"])                                    # the same: no frame
    assert c.pump(2.0) is None
    ch.order("c", ["a", "c", "base"])
    assert c.pump(3.0)["ops"] == [{"op": "order", "c": "c", "keys": ["a", "c", "base"]}]


def test_parts_set_replace_toast_and_reload():
    ch = channel()
    c = Client(ch)
    c.pump(0.0)
    ch.set("status", value="On air", tone="onAir")
    ch.replace("side", {"type": "panel", "child": {"type": "text", "id": "fresh", "value": "new"}})
    ch.set("fresh", value="newer")
    ch.toast("Saved", "good", 4)
    f = c.pump(1.0)
    kinds = [op["op"] for op in f["ops"]]
    assert kinds == ["replace", "toast", "set", "set"]
    assert c.state["parts"]["fresh"]["value"] == "newer" and "inner" not in c.state["parts"]
    assert c.state["toasts"] == [{"text": "Saved", "tone": "good", "s": 4}]
    ch.reload()
    assert c.pump(2.0)["ops"] == [{"op": "reload"}]


def test_a_late_joiner_s_snap_equals_what_an_early_client_built_up():
    """Random changes; at every step a client that connects now gets, from
    its snap alone, the state the client connected throughout holds."""
    rnd = random.Random(7)
    ch = channel()
    early = Client(ch, "early")
    early.pump(0.0)
    now = 0.0
    keys = [f"k{i}" for i in range(8)] + ["base"]
    for step in range(200):
        r = rnd.random()
        k = rnd.choice(keys)
        if r < 0.4:
            ch.up("c", k, lat=rnd.randint(0, 9), label=rnd.choice([None, "x", "y"]))
        elif r < 0.55:
            ch.rm("c", k)
        elif r < 0.7:
            ch.push("c", k, "trail", [[step]], max=rnd.choice([None, 3]))
        elif r < 0.8:
            ch.order("c", rnd.sample(keys, rnd.randint(0, len(keys))))
        elif r < 0.9:
            ch.set("status", value=rnd.choice(["a", "b", None]))
        else:
            ch.append("snr", [[step, step]], max=5)
        now += rnd.choice([0.1, 0.6])
        early.pump(now)
        if step % 20 == 19:
            early.pump(now + 10)
            now += 10
            assert same_as_view(early, ch), step
            late = Client(ch, f"late{step}")
            late.pump(now)
            assert public_state(late.state)["collections"] == public_state(early.state)["collections"]
            assert public_state(late.state)["parts"] == public_state(early.state)["parts"]
            assert public_state(late.state)["charts"] == public_state(early.state)["charts"]
            ch.disconnect(late.session)


def test_items_removed_while_away_are_gone_after_the_snap():
    ch = channel()
    ch.up("c", "a", label="A")
    c = Client(ch)
    c.pump(0.0)
    ch.disconnect(c.session)
    ch.rm("c", "a", "base")
    again = ch.connect("tv")
    snap = again.frame(5.0)
    state = apply_frame(c.state, snap, c.start)
    assert drawn_keys(state, "c") == []


def test_select_sends_this_client_s_extras_and_clears_them_on_moving():
    ch = channel()
    ch.up("c", "a", label="A")
    ch.up("c", "b", label="B")
    seen = []
    ch.on_select(lambda client, key: (seen.append((client.client, key)),
                                      key and client.up("c", key, photo=f"{key}.jpg")), sel="one")
    tv, phone = Client(ch, "tv"), Client(ch, "phone")
    tv.pump(0.0), phone.pump(0.0)
    tv.session.receive(json.dumps({"t": "select", "sel": "one", "key": "a"}))
    tv.pump(1.0), phone.pump(1.0)
    assert tv.state["collections"]["c"]["items"]["a"]["photo"] == "a.jpg"
    assert "photo" not in phone.state["collections"]["c"]["items"]["a"]
    ch.up("c", "a", label="A2")                      # the item moves on; the extra stays
    tv.pump(2.0)
    assert tv.state["collections"]["c"]["items"]["a"] == {"label": "A2", "photo": "a.jpg"}
    tv.session.receive(json.dumps({"t": "select", "sel": "one", "key": "b"}))
    tv.pump(3.0)
    assert "photo" not in tv.state["collections"]["c"]["items"]["a"]
    assert tv.state["collections"]["c"]["items"]["b"]["photo"] == "b.jpg"
    assert seen == [("tv", "a"), ("tv", "b")]


def test_hello_visible_events_and_junk():
    ch = channel()
    c = Client(ch)
    got = []
    ch.on_event(lambda client, name, value: got.append((client.client, name, value)))
    c.session.receive(json.dumps({"t": "hello", "maxHz": 1, "client": "remote-7f3a"}))
    assert c.session.hz == 1.0 and c.session.client == "remote-7f3a"
    c.session.receive(json.dumps({"t": "visible", "value": False}))
    assert c.session.hz == 0.2
    c.session.receive(json.dumps({"t": "visible", "value": True}))
    c.session.receive(json.dumps({"t": "hello", "maxHz": 50}))
    assert c.session.hz == 2.0                       # never above the hub's
    c.session.receive(json.dumps({"t": "event", "name": "scan", "value": 1}))
    c.session.receive("not json")
    c.session.receive("[1, 2]")
    c.session.receive(json.dumps({"t": "view", "part": "map", "bbox": [1, 2, 3, 4], "zoom": 9}))
    assert got == [("remote-7f3a", "scan", 1)]
    assert c.session.view_area == {"part": "map", "bbox": [1, 2, 3, 4], "zoom": 9}


def test_hidden_clients_get_fewer_frames():
    ch = channel()
    c = Client(ch)
    c.pump(0.0)
    c.session.receive(json.dumps({"t": "visible", "value": False}))
    ch.up("c", "a", label="A")
    assert c.pump(1.0) is None and c.pump(5.0) is not None


def test_apply_takes_ops_an_app_already_has():
    ch = channel()
    c = Client(ch)
    ch.apply([{"op": "up", "c": "c", "items": {"a": {"label": "A"}}}, {"op": "order", "c": "c", "keys": ["a"]},
              {"op": "set", "id": "status", "v": {"value": "On"}}, {"op": "push", "c": "c", "key": "a", "f": "t",
                                                                   "v": [1], "max": 2},
              {"op": "rm", "c": "c", "keys": ["base"]}, {"op": "append", "id": "snr", "v": [[1, 1]]},
              {"op": "toast", "text": "Hi"}, {"op": "unknown"}])
    c.pump(0.0)
    assert drawn_keys(c.state, "c") == ["a"] and c.state["parts"]["status"]["value"] == "On"
    assert same_as_view(c, ch)


# ---------------------------------------------------------------- the drivers

class FakeSock:
    """Radio's ohscore.server.ws.Socket shape: send_text, recv."""

    def __init__(self):
        self.out = queue.Queue()
        self.inbox = queue.Queue()

    def send_text(self, text):
        self.out.put(json.loads(text))

    def close(self):
        self.inbox.put(None)

    def recv(self):
        m = self.inbox.get()
        return None if m is None else (0x1, m.encode())


def next_frame(q, t="p", timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        f = q.get(timeout=end - time.monotonic())
        if f["t"] == t:
            return f
    raise AssertionError(f"no {t} frame")


def test_blocking_driver_serves_hello_snap_frames_and_pings():
    hub = LiveHub(max_hz=50, ping_s=0.2)
    ch = hub.channel("t/live", document=DOC)
    sock = FakeSock()
    t = threading.Thread(target=hub.serve_blocking, args=(sock, "t/live"), daemon=True)
    t.start()
    assert sock.out.get(timeout=3)["t"] == "hello"
    assert next_frame(sock.out, "snap")["seq"] == 1
    ch.up("c", "a", label="A")                         # from another thread
    assert next_frame(sock.out)["ops"] == [{"op": "up", "c": "c", "items": {"a": {"label": "A"}}}]
    assert next_frame(sock.out, "ping")
    sock.inbox.put(json.dumps({"t": "select", "sel": "one", "key": "a"}))
    sock.inbox.put(None)                               # the client closes
    t.join(timeout=3)
    assert not t.is_alive() and ch.sessions == []


def test_async_driver_serves_the_same():
    async def run():
        hub = LiveHub(max_hz=50, ping_s=5)
        ch = hub.channel("t/live", document=DOC)
        sent, inbox = asyncio.Queue(), asyncio.Queue()

        class WS:
            async def send_str(self, text):
                await sent.put(json.loads(text))

            def __aiter__(self):
                return self

            async def __anext__(self):
                m = await inbox.get()
                if m is None:
                    raise StopAsyncIteration
                return m

        task = asyncio.create_task(hub.serve(WS(), "t/live"))
        assert (await asyncio.wait_for(sent.get(), 3))["t"] == "hello"
        assert (await asyncio.wait_for(sent.get(), 3))["t"] == "snap"
        await inbox.put(json.dumps({"t": "hello", "maxHz": 50}))
        # A change from another thread, as an ADS-B callback makes it.
        threading.Thread(target=lambda: ch.up("c", "a", label="A")).start()
        f = await asyncio.wait_for(sent.get(), 3)
        assert f["t"] == "p" and f["ops"][0]["items"] == {"a": {"label": "A"}}
        await inbox.put(None)
        await asyncio.wait_for(task, 3)
        assert ch.sessions == []

    asyncio.run(run())


def test_asking_for_a_channel_again_does_not_start_it_again():
    hub = LiveHub(clock=lambda: 0.0)
    ch = hub.channel("t/live", document=DOC)
    ch.up("c", "a", label="A")
    other = dict(DOC, rev="something-else")
    assert hub.channel("t/live", document=DOC) is ch              # every request may ask
    assert hub.channel("t/live", document=other) is ch
    assert "a" in ch.view()["collections"]["c"]["items"]          # the live data stays
    ch.reset(other)                                               # only when asked to
    assert ch.rev == "something-else" and "a" not in ch.view()["collections"]["c"]["items"]


def test_removing_a_channel_ends_its_clients():
    hub = LiveHub(max_hz=50, ping_s=5)
    hub.channel("t/live", document=DOC)
    sock = FakeSock()
    t = threading.Thread(target=hub.serve_blocking, args=(sock, "t/live"), daemon=True)
    t.start()
    next_frame(sock.out, "snap")
    assert hub.remove("t/live") and not hub.remove("t/live")
    t.join(timeout=3)
    assert not t.is_alive() and hub.channels() == []


def test_each_client_gets_the_rev_of_the_document_it_fetched():
    """A screen built per form has a rev per form; a hello with another rev
    makes the client refetch, so each session must carry its own."""
    tv = DOC
    s = Screen("t/live", level="control", form="phone")
    s.collection("c", {"base": {"label": "From the document"}})
    s.selection("one", from_="c")
    s.root = p.column(p.text("Starting", id="status"), p.text("phone only", id="extra"))
    phone = s.json()
    assert phone["rev"] != tv["rev"]
    hub = LiveHub(clock=lambda: 0.0)
    ch = hub.channel("t/live", document=tv)
    ch.up("c", "a", label="A")
    ch.set("status", value="On")
    ch.add(phone)                                       # registered later: the data and the set carry over
    on_tv = ch.connect("tv", rev=tv["rev"])
    on_phone = ch.connect("phone", rev=phone["rev"])
    stray = ch.connect("old", rev="no-such-rev")
    assert on_tv.hello()["rev"] == tv["rev"] and on_phone.hello()["rev"] == phone["rev"]
    assert stray.hello()["rev"] == phone["rev"]         # the newest: it will refetch
    start = state_from_document(phone)
    st = apply_frame(start, on_phone.frame(0.0), start)
    assert st["parts"]["status"]["value"] == "On" and drawn_keys(st, "c") == ["base", "a"]
    ch.set("extra", value="changed")                    # a part only the phone's document has
    ch.set("rows", hidden=True)                         # and one only the TV's has
    f = on_phone.frame(1.0)
    assert f["ops"] == [{"op": "set", "id": "extra", "v": {"value": "changed"}}]
    tv_start = state_from_document(tv)
    tv_state = apply_frame(tv_start, on_tv.frame(1.0), tv_start)
    assert tv_state["parts"]["rows"]["hidden"] is True and "extra" not in tv_state["parts"]


def test_variants_are_capped_but_never_one_in_use():
    hub = LiveHub(clock=lambda: 0.0)
    ch = hub.channel("t/live", document=DOC)
    keep = ch.connect("tv", rev=DOC["rev"])
    for i in range(40):
        ch.add(dict(DOC, rev=f"r{i}"))
    assert len(ch.variants) <= 17 and DOC["rev"] in ch.variants and keep.hello()["rev"] == DOC["rev"]


def test_the_drivers_bind_the_session_to_the_document_given():
    hub = LiveHub(max_hz=50, ping_s=5)
    hub.channel("t/live", document=DOC)
    other = dict(DOC, rev="phone-rev")
    sock = FakeSock()
    t = threading.Thread(target=hub.serve_blocking, args=(sock, "t/live"), kwargs={"document": other}, daemon=True)
    t.start()
    assert sock.out.get(timeout=3) == {"t": "hello", "rev": "phone-rev", "maxHz": 50}
    sock.inbox.put(None)
    t.join(timeout=3)
