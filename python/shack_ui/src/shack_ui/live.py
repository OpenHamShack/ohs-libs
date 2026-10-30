"""The live socket's server side (SCREENS.md §4): one hub per app process,
one channel per screen, one session per connected client.

    hub = LiveHub()
    feed = hub.channel("radio/aircraft", document=aircraft(Req()).json())
    feed.up("aircraft", icao, lat=..., lon=..., rot=...)        # from the app's own callbacks
    feed.push("aircraft", icao, "trail", [[lat, lon]], max=30)
    feed.on_select(lambda client, key: client.up("aircraft", key, photo=...), sel="aircraft")

    hub.serve_blocking(sock, "radio/aircraft", req)    # http.server + a socket with send_text/recv (Radio)
    await hub.serve(ws, "radio/aircraft", req)         # asyncio, aiohttp's WebSocketResponse shape

**How it keeps up (§4.3).** A channel holds the screen's live state, the
same state a client holds (ops.py), seeded from the document. Each
session remembers what its client was last sent and which keys, parts
and charts changed since. A frame is the difference between the two,
taken when the session may send (at most `maxHz` a second), so a hundred
moves of one aircraft between frames are one `up` with the latest
fields: latest wins by construction, nothing queues behind a slow socket,
and a busy TV gets fewer, fuller frames. Trails and charts that only grew
go as `push`/`append` of the new points, not the whole array.

**Reconnects.** The first frame after `hello` is a `snap`: the
difference between the document's own state and the live state, which a
client applies to the document's state (ops.apply_frame), so anything
removed while it was away is gone (SCREENS.md §4.1, as the vectors read
it).

**Per-client extras.** `on_select` callbacks add fields to one client's
view of one item (the photo, the full trail, the cockpit values: only
what the selected item needs). They are cleared, and the client told,
when the selection moves.

The sans-IO core (Channel, Session) does no I/O and keeps no thread; the
two drivers at the bottom are the only code that touches a socket.
Changes may come from any thread: a channel's lock covers its state and
its sessions' bookkeeping.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from typing import Any, Callable

from .ops import apply_ops, state_from_document

PING_S = 30.0          # Cloudflare closes idle sockets near 100 s (GUIDE §5)
HIDDEN_HZ = 0.2        # §4.1: a covered screen may slow to this
DEFAULT_HZ = 2.0       # §4.3: 1 on a TV (the client asks), 2 elsewhere


def _encode(frame: dict) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def _tail(old: list, new: list) -> list | None:
    """The points to push so `old` becomes `new` under a cap of len(new):
    None when `new` is not `old` (or its tail) followed by more points."""
    if new[:len(old)] == old:
        return new[len(old):]
    # The cap dropped points from the front: find where old's tail starts new.
    for cut in range(1, len(old) + 1):
        keep = old[cut:]
        if new[:len(keep)] == keep:
            return new[len(keep):]
    return None


class ClientView:
    """What an `on_select` or `on_event` callback gets: the client's id, its
    request, and `up` for fields only this client sees."""

    def __init__(self, session: Session):
        self._s = session

    @property
    def client(self) -> str | None:
        return self._s.client

    @property
    def req(self):
        return self._s.req

    def up(self, c: str, key: str, fields: dict | None = None, **more) -> None:
        self._s.channel._extra(self._s, c, key, dict(fields or {}, **more))


class Channel:
    """One screen's live state, shared by every client showing it."""

    def __init__(self, name: str, document: dict | None = None, *, max_hz: float = DEFAULT_HZ):
        self.name = name
        self.max_hz = max_hz
        self.lock = threading.RLock()
        self.sessions: list[Session] = []
        self._select_cbs: list[tuple[str | None, Callable]] = []
        self._event_cbs: list[Callable] = []
        self.closed = False
        self.reset(document)

    def reset(self, document: dict | None) -> None:
        """Start again from a document: its rev for `hello`, its state as
        the base a snap is taken from. Every live change made before is
        dropped and every client gets a fresh snap: for a screen whose
        structure changed, not for each request (LiveHub.channel never
        calls it on a channel that exists)."""
        with self.lock:
            doc = document or {"root": None}
            self.rev = doc.get("rev")
            self.base = state_from_document(doc)
            self.state = copy.deepcopy(self.base)
            self.selections = {k: v.get("from") for k, v in (doc.get("selections") or {}).items()
                               if isinstance(v, dict)}
            self.replaced: dict[str, dict] = {}
            for s in self.sessions:
                s._all_dirty()

    # ---- changes, from the app (any thread)
    def _mark(self, mark: tuple, event: dict | None = None) -> None:
        for s in self.sessions:
            s.dirty.add(mark)
            if event is not None:
                s.events.append(event)
            s._wake()

    def up(self, c: str, key: str, fields: dict | None = None, **more) -> None:
        """Merge fields into an item, creating it; a None field deletes."""
        f = dict(fields or {}, **more)
        with self.lock:
            coll = self.state["collections"].setdefault(c, {"items": {}, "order": []})
            item = coll["items"].setdefault(key, {})
            for k, v in f.items():
                if v is None:
                    item.pop(k, None)
                else:
                    item[k] = copy.deepcopy(v)
            self._mark(("c", c, key))

    def put(self, c: str, key: str, fields: dict) -> None:
        """Replace an item outright (fields not given are gone)."""
        with self.lock:
            coll = self.state["collections"].setdefault(c, {"items": {}, "order": []})
            coll["items"][key] = copy.deepcopy(fields)
            self._mark(("c", c, key))

    def rm(self, c: str, *keys: str) -> None:
        with self.lock:
            coll = self.state["collections"].get(c)
            if coll is None:
                return
            for k in keys:
                coll["items"].pop(k, None)
                self._mark(("c", c, k))
            if any(k in coll["order"] for k in keys):
                coll["order"] = [k for k in coll["order"] if k not in keys]
                self._mark(("order", c))

    def order(self, c: str, keys: list[str]) -> None:
        """The display order. Sent only when it differs from what a client
        has, so calling it every second costs nothing when it is steady."""
        with self.lock:
            coll = self.state["collections"].setdefault(c, {"items": {}, "order": []})
            if coll["order"] != list(keys):
                coll["order"] = list(keys)
                self._mark(("order", c))

    def push(self, c: str, key: str, f: str, values: list, max: int | None = None) -> None:
        """Append to an item's array field, keeping the last `max`."""
        with self.lock:
            coll = self.state["collections"].get(c)
            item = coll["items"].get(key) if coll else None
            if item is None:
                return
            arr = list(item.get(f) or []) + copy.deepcopy(list(values))
            if max is not None and len(arr) > max:
                arr = arr[len(arr) - max:]
            item[f] = arr
            self._mark(("c", c, key))

    def set(self, part: str, props: dict | None = None, **more) -> None:
        """Merge properties into a part; a None property deletes."""
        v = dict(props or {}, **more)
        with self.lock:
            target = self.state["parts"].get(part)
            if target is None:
                return
            for k, x in v.items():
                if x is None:
                    target.pop(k, None)
                else:
                    target[k] = copy.deepcopy(x)
            self._mark(("part", part))

    def replace(self, part: str, new: dict) -> None:
        """Replace a part and its children (the structure moved). Every
        client gets the op itself, and a later snap carries it too."""
        op = {"op": "replace", "id": part, "part": copy.deepcopy(new)}
        with self.lock:
            if part not in self.state["parts"]:
                return
            self.state = apply_ops(self.state, [op])
            self.replaced[part] = op
            self._mark(("part", part), op)

    def append(self, chart: str, points: list, mark: int = 0, max: int | None = None) -> None:
        with self.lock:
            marks = self.state["charts"].get(chart)
            if marks is None or mark not in marks:
                return
            pts = marks[mark] + copy.deepcopy(list(points))
            if max is not None and len(pts) > max:
                pts = pts[len(pts) - max:]
            marks[mark] = pts
            self._mark(("chart", chart, mark))

    def toast(self, text: str, tone: str | None = None, s: float | None = None) -> None:
        op = {"op": "toast", "text": text}
        if tone is not None:
            op["tone"] = tone
        if s is not None:
            op["s"] = s
        with self.lock:
            self._mark(("event",), op)

    def reload(self) -> None:
        """Tell every client to refetch the document (the structure changed)."""
        with self.lock:
            self._mark(("event",), {"op": "reload"})

    def apply(self, ops: list[dict]) -> None:
        """Ops an app already has (a poll answer it also streams)."""
        for op in ops:
            kind = op.get("op")
            if kind == "up":
                for k, f in (op.get("items") or {}).items():
                    self.up(op["c"], k, f)
            elif kind == "rm":
                self.rm(op["c"], *op.get("keys", []))
            elif kind == "order":
                self.order(op["c"], op["keys"])
            elif kind == "push":
                self.push(op["c"], op["key"], op["f"], op["v"], op.get("max"))
            elif kind == "set":
                self.set(op["id"], op["v"])
            elif kind == "replace":
                self.replace(op["id"], op["part"])
            elif kind == "append":
                self.append(op["id"], op["v"], op.get("mark", 0), op.get("max"))
            elif kind == "toast":
                self.toast(op.get("text", ""), op.get("tone"), op.get("s"))
            elif kind == "reload":
                self.reload()

    # ---- per-client
    def on_select(self, fn: Callable[[ClientView, Any], None], sel: str | None = None) -> None:
        """fn(client, key) when a client selects an item (key None: cleared),
        for selection `sel` or every selection."""
        self._select_cbs.append((sel, fn))

    def on_event(self, fn: Callable[[ClientView, str, Any], None]) -> None:
        """fn(client, name, value) for a `send` action's event."""
        self._event_cbs.append(fn)

    def _extra(self, s: Session, c: str, key: str, fields: dict) -> None:
        with self.lock:
            s.extras.setdefault(c, {}).setdefault(key, {}).update(copy.deepcopy(fields))
            s.dirty.add(("c", c, key))
            s._wake()

    def close(self) -> None:
        """No more frames: every session ends and its driver closes the
        socket (the client reconnects, refetching the screen)."""
        with self.lock:
            self.closed = True
            for s in list(self.sessions):
                s.closed = True
                s._wake()
            self.sessions.clear()

    def connect(self, client: str | None = None, req=None, *, max_hz: float | None = None) -> Session:
        with self.lock:
            s = Session(self, client, req, max_hz or self.max_hz)
            self.sessions.append(s)
            return s

    def disconnect(self, s: Session) -> None:
        with self.lock:
            if s in self.sessions:
                self.sessions.remove(s)

    def view(self, s: Session | None = None) -> dict:
        """What a client should hold now: the live state, with this
        session's extras over the items they belong to."""
        with self.lock:
            v = copy.deepcopy(self.state)
            if s is not None:
                for c, items in s.extras.items():
                    coll = v["collections"].get(c)
                    for k, f in items.items():
                        if coll is not None and k in coll["items"]:
                            coll["items"][k].update(copy.deepcopy(f))
            return v


class Session:
    """One client on one channel, without I/O: the drivers ask it for
    frames and hand it what the client sent."""

    def __init__(self, channel: Channel, client: str | None, req, max_hz: float):
        self.channel = channel
        self.client = client
        self.req = req
        self.hub_hz = max_hz
        self.client_hz: float | None = None
        self.visible = True
        self.seq = 0
        self.sent: dict | None = None
        self.dirty: set[tuple] = set()
        self.events: list[dict] = []
        self.extras: dict[str, dict[str, dict]] = {}
        self.selected: dict[str, Any] = {}
        self.view_area: dict | None = None
        self.last_at: float | None = None
        self.wake: Callable[[], None] | None = None
        self.closed = False

    def _wake(self) -> None:
        if self.wake is not None:
            self.wake()

    def _all_dirty(self) -> None:
        self.sent = None
        self.events.clear()
        self._wake()

    @property
    def hz(self) -> float:
        hz = min(self.hub_hz, self.client_hz or self.hub_hz)
        return min(hz, HIDDEN_HZ) if not self.visible else hz

    # ---- frames
    def hello(self) -> dict:
        return {"t": "hello", "rev": self.channel.rev, "maxHz": self.hz}

    def pending(self) -> bool:
        return self.sent is None or bool(self.dirty) or bool(self.events)

    def wait_s(self, now: float) -> float | None:
        """Seconds until a frame may go: 0 now, None when nothing is pending."""
        if not self.pending():
            return None
        if self.sent is None or self.last_at is None:
            return 0.0
        return max(0.0, self.last_at + 1.0 / self.hz - now)

    def frame(self, now: float) -> dict | None:
        """The next frame if one may go now: the snap first, then a `p`
        of every change since, or None."""
        with self.channel.lock:
            if self.closed:
                return None
            w = self.wait_s(now)
            if w is None or w > 0:
                return None
            view = self.channel.view(self)
            if self.sent is None:
                start = apply_ops(self.channel.base, list(self.channel.replaced.values()))
                ops = list(self.channel.replaced.values()) + _diff(start, view, None)
                kind = "snap"
            else:
                events = self.events
                start = apply_ops(self.sent, [e for e in events if e["op"] == "replace"])
                ops = list(events) + _diff(start, view, self.dirty)
                kind = "p"
            self.events = []
            self.dirty = set()
            self.sent = {"parts": view["parts"], "collections": view["collections"], "charts": view["charts"],
                         "toasts": [], "reload": False, "subtrees": view.get("subtrees", {})}
            if kind == "p" and not ops:
                return None         # changed and changed back: nothing to say
            self.last_at = now
            self.seq += 1
            return {"t": kind, "seq": self.seq, "ops": ops}

    # ---- what the client sent
    def receive(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        t = msg.get("t")
        if t == "hello":
            hz = msg.get("maxHz")
            if isinstance(hz, (int, float)) and not isinstance(hz, bool) and hz > 0:
                self.client_hz = float(hz)
            if isinstance(msg.get("client"), str):
                self.client = msg["client"]
        elif t == "visible":
            self.visible = msg.get("value") is not False
            self._wake()
        elif t == "view":
            self.view_area = {k: msg.get(k) for k in ("part", "bbox", "zoom")}
        elif t == "select":
            self._select(msg.get("sel"), msg.get("key"))
        elif t == "event":
            for fn in list(self.channel._event_cbs):
                fn(ClientView(self), msg.get("name"), msg.get("value"))

    def _select(self, sel, key) -> None:
        if not isinstance(sel, str):
            return
        ch = self.channel
        c = ch.selections.get(sel, sel)
        with ch.lock:
            # The extras belonged to the old selection: drop them, and the
            # next frame tells the client (their fields go as nulls).
            for k in list(self.extras.get(c, {})):
                self.dirty.add(("c", c, k))
            self.extras.pop(c, None)
            self.selected[sel] = key
        for want, fn in list(ch._select_cbs):
            if want is None or want == sel:
                fn(ClientView(self), key)
        self._wake()


def _diff(old: dict, new: dict, dirty: set | None) -> list[dict]:
    """The ops that turn state `old` into `new`, looking only at `dirty`
    (None: everything). Per collection: rm, up, push, order; then parts'
    sets, then chart appends."""
    out: list[dict] = []
    oc, nc = old["collections"], new["collections"]
    if dirty is None:
        keys = {(c, k) for c in nc for k in nc[c]["items"]} | {(c, k) for c in oc for k in oc[c]["items"]}
        orders = set(nc)
        parts = set(new["parts"])
        charts = {(cid, m) for cid, marks in new["charts"].items() for m in marks}
    else:
        keys = {(d[1], d[2]) for d in dirty if d[0] == "c"}
        orders = {d[1] for d in dirty if d[0] == "order"}
        parts = {d[1] for d in dirty if d[0] == "part"}
        charts = {(d[1], d[2]) for d in dirty if d[0] == "chart"}
    for c in sorted({c for c, _ in keys} | orders, key=lambda c: list(nc).index(c) if c in nc else 1 << 30):
        o_items = oc.get(c, {}).get("items", {})
        n_items = nc.get(c, {}).get("items", {})
        n_order = list(n_items)
        ks = sorted((k for cc, k in keys if cc == c),
                    key=lambda k: n_order.index(k) if k in n_items else -1)
        gone = [k for k in ks if k in o_items and k not in n_items]
        if gone:
            out.append({"op": "rm", "c": c, "keys": gone})
        ups, pushes = {}, []
        for k in ks:
            if k not in n_items:
                continue
            o, n = o_items.get(k), n_items[k]
            if o is None:
                ups[k] = copy.deepcopy(n)
                continue
            fields = {}
            for f, v in n.items():
                if f in o and o[f] == v:
                    continue
                if isinstance(v, list) and isinstance(o.get(f), list) and v:
                    more = _tail(o[f], v)
                    if more is not None:
                        if more:
                            pushes.append({"op": "push", "c": c, "key": k, "f": f, "v": copy.deepcopy(more),
                                           "max": len(v)})
                        continue
                fields[f] = copy.deepcopy(v)
            for f in o:
                if f not in n:
                    fields[f] = None
            if fields:
                ups[k] = fields
        if ups:
            out.append({"op": "up", "c": c, "items": ups})
        out.extend(pushes)
        o_order = oc.get(c, {}).get("order", [])
        n_ord = nc.get(c, {}).get("order", [])
        if (c in orders or gone) and n_ord != o_order and c in nc:
            # rm already took gone keys out of the client's order; compare
            # with that, so a removal alone sends no order.
            if n_ord != [k for k in o_order if k not in gone]:
                out.append({"op": "order", "c": c, "keys": list(n_ord)})
    for pid in [p for p in new["parts"] if p in parts]:
        o, n = old["parts"].get(pid), new["parts"][pid]
        if o is None:
            continue
        v = {k: copy.deepcopy(x) for k, x in n.items() if o.get(k, object()) != x}
        v |= {k: None for k in o if k not in n}
        if v:
            out.append({"op": "set", "id": pid, "v": v})
    for cid, m in sorted(charts, key=lambda x: (list(new["charts"]).index(x[0]) if x[0] in new["charts"] else 0, x[1])):
        n = new["charts"].get(cid, {}).get(m)
        o = old["charts"].get(cid, {}).get(m, [])
        if n is None or n == o:
            continue
        more = _tail(o, n)
        if more:
            out.append({"op": "append", "id": cid, "mark": m, "v": copy.deepcopy(more), "max": len(n)})
    return out


class LiveHub:
    """One per app process: the channels by screen, and the two drivers."""

    def __init__(self, *, max_hz: float = DEFAULT_HZ, ping_s: float = PING_S,
                 clock: Callable[[], float] = time.monotonic):
        self.max_hz = max_hz
        self.ping_s = ping_s
        self.clock = clock
        self._channels: dict[str, Channel] = {}
        self._lock = threading.Lock()

    def channel(self, name: str, document: dict | None = None) -> Channel:
        """The channel for a screen, made on first use and seeded from
        `document` (Screen.json()'s) then. Calling it again, on every
        request, is cheap and changes nothing: Channel.reset() is the
        explicit way to start a channel again."""
        with self._lock:
            ch = self._channels.get(name)
            if ch is None:
                ch = self._channels[name] = Channel(name, document, max_hz=self.max_hz)
            return ch

    def remove(self, name: str) -> bool:
        """Drop a screen's channel (the feature went, the app is stopping a
        decoder for good): its clients' sockets close. False if none."""
        with self._lock:
            ch = self._channels.pop(name, None)
        if ch is None:
            return False
        ch.close()
        return True

    def channels(self) -> list[str]:
        return list(self._channels)

    def serve_blocking(self, sock, name: str, req=None) -> None:
        """Serve one client on a blocking socket with `send_text(str)` and
        `recv() -> (opcode, bytes) | None` (Radio's ohscore.server.ws),
        until it closes. Frames go from a thread of their own, so a slow
        client never holds up the reader and the reader never holds up
        the frames."""
        ch = self.channel(name)
        s = ch.connect(getattr(req, "client", None), req)
        cond = threading.Condition()
        closed = threading.Event()

        def wake():
            with cond:
                cond.notify()
        s.wake = wake

        def sender():
            last = self.clock()
            try:
                sock.send_text(_encode(s.hello()))
                while not closed.is_set() and not s.closed:
                    now = self.clock()
                    f = s.frame(now)
                    if f is not None:
                        sock.send_text(_encode(f))
                        last = now
                        continue
                    if now - last >= self.ping_s:
                        sock.send_text(_encode({"t": "ping"}))
                        last = now
                    with cond:
                        # Checked again under the lock wake() takes, so a
                        # change between frame() and here is never missed.
                        if closed.is_set() or s.closed:
                            break
                        w = s.wait_s(self.clock())
                        if w == 0.0:
                            continue
                        timeout = self.ping_s - (now - last)
                        cond.wait(timeout=max(0.001, timeout if w is None else min(timeout, w)))
            except (OSError, ConnectionError):
                pass
            closed.set()
            if s.closed and hasattr(sock, "close"):
                sock.close()            # unblocks the reader's recv()

        t = threading.Thread(target=sender, name=f"shack-ui-live-{name}", daemon=True)
        t.start()
        try:
            while not closed.is_set():
                m = sock.recv()
                if m is None:
                    break
                op, data = m
                if op == 0x1:
                    s.receive(data.decode("utf-8", "replace"))
        except (OSError, ConnectionError):
            pass
        finally:
            closed.set()
            wake()
            ch.disconnect(s)
            t.join(timeout=2)

    async def serve(self, ws, name: str, req=None) -> None:
        """Serve one client on an asyncio WebSocket: `send_str(str)` (or
        `send`) and async iteration over messages (str, or objects with
        `.data`: aiohttp's WSMessage), until it closes."""
        loop = asyncio.get_running_loop()
        ch = self.channel(name)
        s = ch.connect(getattr(req, "client", None), req)
        ev = asyncio.Event()
        s.wake = lambda: loop.call_soon_threadsafe(ev.set)
        send = getattr(ws, "send_str", None) or ws.send

        async def sender():
            last = self.clock()
            await send(_encode(s.hello()))
            while not s.closed:
                now = self.clock()
                f = s.frame(now)
                if f is not None:
                    await send(_encode(f))
                    last = now
                    continue
                if now - last >= self.ping_s:
                    await send(_encode({"t": "ping"}))
                    last = now
                ev.clear()
                w = s.wait_s(self.clock())
                if w == 0.0:
                    continue
                timeout = self.ping_s - (now - last)
                try:
                    await asyncio.wait_for(ev.wait(), timeout=max(0.001, timeout if w is None else min(timeout, w)))
                except asyncio.TimeoutError:
                    pass
            if hasattr(ws, "close"):
                await ws.close()        # the channel went: end the reader too

        task = asyncio.create_task(sender())
        try:
            async for msg in ws:
                data = getattr(msg, "data", msg)
                if isinstance(data, bytes):
                    continue
                if isinstance(data, str):
                    s.receive(data)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, ConnectionError, OSError):
                pass
            ch.disconnect(s)
