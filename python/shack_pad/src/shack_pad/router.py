"""Pad events to the app in front, and the app's faces back to the keys.

One owner at a time, routing to exactly one app (CONTROLLER-PAD.md §6):

- On the controller, the platform's `shack-input` service owns every pad
  and tells InputHost which app is in front. Each app is a PadApp; the
  edition implements PadApp over HTTP to the app (`api/…/pad/action`,
  `…/pad/faces`, the app's `api/ptt` lease) - that bridge is the edition's
  job, not this library's.
- On a standalone desktop, the app runs an InputHost with itself as the
  only PadApp (InProcessApp), and focus.py decides whether it may open the
  pad at all. No HTTP, no socket.

Either way the host does the same things: resolves the operator's map,
turns presses into actions for the app, keeps transmit to the app's lease
(tx.py), redraws keys from the app's faces, and - when a pad goes away -
unkeys before it closes anything.

What the host never does: key a radio, or send a release to an app. Apps
see presses and turns; the momentary release of `ptt` is the library's,
turned into the lease's unkey.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from . import actions as actionsmod
from . import diagnose, mapping
from .actions import Action
from .drivers import DRIVERS, by_model
from .drivers.base import Driver, Event
from .faces import BLANK, Face
from .i18n import _
from .tx import PadTx, TxLease

# Test mode (start_test): how long each key stays lit in the opening sweep -
# no shorter than the host's half-second redraw, or a key would be skipped -
# and how long a test lasts if nobody stops it.
TEST_SWEEP = 0.5
TEST_SECONDS = 60.0


@dataclass(frozen=True)
class PadAction:
    """What an app receives: the body of CONTROLLER-PAD.md §6.1's
    `POST …/pad/action`. A keypad's `tune:up` arrives as `tune`, turn, +1:
    apps only implement the turn."""
    action: str
    kind: str          # press | turn
    step: int
    control: str
    model: str
    pad: str

    def to_json(self) -> dict:
        return asdict(self)


class PadApp:
    """The callback interface an app presents to the host.

    kind: the app kind (`qo100nb`); name: its short name, already
    translated, for "on air" faces; declaration: its pad-actions.json;
    layout: its pad-layout.json; lease: its transmit lease (None for an app
    that never transmits); capabilities: what it can do here, for `needs`
    (None: offer everything declared).

    Override perform() and faces(); on_air() defaults to the faces' onAir.
    The platform bridge overrides on_air() from the broker's state, so the
    pad shows an app on air whichever client keyed it.
    """

    kind = ""
    name = ""
    declaration: dict = {}
    layout: dict = {}
    lease: TxLease | None = None
    capabilities: set[str] | None = None

    def perform(self, action: PadAction) -> None:
        raise NotImplementedError

    def faces(self) -> dict:
        """{"faces": {action: {"l1", "l2", "tone"}}, "onAir": bool}"""
        return {}

    def on_air(self) -> bool:
        try:
            return bool(self.faces().get("onAir"))
        except Exception:
            return False

    def text(self, key_or_text: str) -> str:
        """An action's `short` may be a catalog key; the app translates its
        own catalog. Default: shown as given."""
        return key_or_text


class InProcessApp(PadApp):
    """The reference PadApp: a desktop app's own handlers, called in process.

        app = InProcessApp("qo100nb", "QO-100 NB", decl, layout,
                           perform=window.on_pad_action, faces=window.pad_faces,
                           lease=window.tx_lease)
    """

    def __init__(self, kind: str, name: str, declaration: dict, layout: dict | None = None, *,
                 perform: Callable[[PadAction], None], faces: Callable[[], dict] | None = None,
                 lease: TxLease | None = None, capabilities: set[str] | None = None,
                 text: Callable[[str], str] | None = None):
        self.kind, self.name, self.declaration, self.layout = kind, name, declaration, layout or {}
        self._perform, self._faces, self.lease, self.capabilities = perform, faces, lease, capabilities
        self._text = text

    def perform(self, action: PadAction) -> None:
        self._perform(action)

    def faces(self) -> dict:
        return self._faces() if self._faces else {}

    def text(self, key_or_text: str) -> str:
        return self._text(key_or_text) if self._text else key_or_text


class InputHost:
    """Owns pads and routes them. Thread-safe: serve() runs one thread per
    device, and set_foreground() comes from the kiosk or the app's UI."""

    def __init__(self, apps: list[PadApp], maps: dict | None = None, *, foreground: str | None = None,
                 platform: bool = False, on_maps: Callable[[dict], None] | None = None,
                 on_switch: Callable[[str], None] | None = None, host: str | None = None,
                 brightness: int = 60, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None):
        self.apps: dict[str, PadApp] = {a.kind: a for a in apps}
        self.maps = maps or {}
        self.front = foreground or (apps[0].kind if apps else "")
        self.platform = platform
        self.on_maps = on_maps
        self.on_switch = on_switch
        self.host = host or socket.gethostname().split(".")[0].lower() or "local"
        self.brightness = brightness
        self.clock = clock
        self.log = log or (lambda m: None)
        self.lock = threading.RLock()
        self.txs: dict[str, PadTx] = {}
        self.page_at: dict[tuple[str, str], int] = {}
        self.down: set[tuple[str, str]] = set()
        self.pending_front: str | None = None
        self.handover: str | None = None       # desktop: another app wants the pad (focus.py)
        self.devices: dict[str, Driver] = {}
        self.flash: dict[tuple[str, str], tuple[Face, float]] = {}
        self.generation = 0                   # bumped on anything that should redraw at once
        self.message = ""
        # The last thing that went wrong with a device, as a sentence that
        # says what to do (diagnose.explain); "" while all is well. Kept
        # apart from message, which also carries ordinary news.
        self.last_error = ""
        # Raw events, as they arrive, for a settings page's live view; and
        # the test mode that page starts (start_test).
        self.listeners: list[Callable[[str, Event], None]] = []
        self.test: dict | None = None
        self._acts: dict[str, tuple[dict[str, Action], list[str]]] = {}
        self._drawn: dict[str, tuple[int, float]] = {}   # model -> (generation, when) last drawn
        self._alive_at: dict[str, float] = {}             # model -> when its last keep-alive (or wake) went

    # Apps and their maps --------------------------------------------------------------
    def actions(self, kind: str) -> dict[str, Action]:
        with self.lock:
            if kind not in self._acts:
                app = self.apps[kind]
                kinds = list(self.apps) if self.platform else None
                self._acts[kind] = actionsmod.load(app.declaration, app.capabilities, kinds)
            return self._acts[kind][0]

    def reload(self, kind: str | None = None) -> None:
        """An app's declaration, capabilities or layout changed."""
        with self.lock:
            if kind:
                self._acts.pop(kind, None)
            else:
                self._acts.clear()
            self.generation += 1

    def pages(self, kind: str, model: str) -> tuple[list[dict], list[str]]:
        drv = by_model(model)
        acts = self.actions(kind)
        pages, notes = mapping.resolve(drv, self.apps[kind].layout, mapping.entry_of(self.maps, kind, model), acts)
        return pages, self._acts[kind][1] + notes

    def set_maps(self, maps: dict) -> None:
        """The station profile's controllerMaps changed (a sync, another
        screen): use them from the next event."""
        with self.lock:
            self.maps = maps or {}
            self.generation += 1

    def _store(self, maps: dict) -> None:
        self.maps = maps
        self.generation += 1
        if self.on_maps:
            self.on_maps(maps)

    def set_binding(self, kind: str, model: str, page: int, cid: str, slot: str, aid: str) -> dict:
        """Remap one control; the new controllerMaps go to on_maps (the
        station profile) and are returned. ValueError before any change."""
        with self.lock:
            entry = mapping.set_binding(by_model(model), self.apps[kind].layout, mapping.entry_of(self.maps, kind, model),
                                        self.actions(kind), page, cid, slot, aid, on_air=self._app_on_air(kind))
            maps = mapping.with_entry(self.maps, kind, model, entry)
            self._store(maps)
            return maps

    def set_pages(self, kind: str, model: str, count: int) -> dict:
        with self.lock:
            entry = mapping.set_pages(mapping.entry_of(self.maps, kind, model), count)
            if self.page_at.get((kind, model), 0) >= count:
                self.page_at[(kind, model)] = 0
            maps = mapping.with_entry(self.maps, kind, model, entry)
            self._store(maps)
            return maps

    def reset(self, kind: str, model: str) -> dict:
        with self.lock:
            mapping.reset(by_model(model), self.apps[kind].layout, mapping.entry_of(self.maps, kind, model),
                          self.actions(kind), on_air=self._app_on_air(kind))
            self.page_at[(kind, model)] = 0
            maps = mapping.with_entry(self.maps, kind, model, None)
            self._store(maps)
            return maps

    # Who is in front ------------------------------------------------------------------
    def set_foreground(self, kind: str) -> bool:
        """The kiosk (or a desktop's window) says this app is in front. A
        held momentary PTT makes the switch wait for its release
        (CONTROLLER-PAD.md §7.3): False then, and it happens on release."""
        with self.lock:
            if kind not in self.apps:
                raise KeyError(kind)
            if any(t.holding() for t in self.txs.values()):
                self.pending_front = kind
                return False
            self.pending_front = None
            if kind != self.front:
                self.front = kind
                self.generation += 1
            return True

    def request_switch(self, target: str | None) -> None:
        """`app:next` / `app:<kind>` pressed. On the platform the kiosk
        brings the app forward and then calls set_foreground; in process,
        with no kiosk, the host switches itself."""
        kinds = list(self.apps)
        if target is None:
            target = kinds[(kinds.index(self.front) + 1) % len(kinds)] if self.front in kinds else kinds[0]
        if self.on_switch:
            self.on_switch(target)
        elif target in self.apps:
            self.set_foreground(target)

    # Transmit -------------------------------------------------------------------------
    def tx_for(self, model: str) -> PadTx:
        with self.lock:
            if model not in self.txs:
                self.txs[model] = PadTx(f"pad-{self.host}-{model}", self.clock, self.log)
            return self.txs[model]

    def _app_on_air(self, kind: str) -> bool:
        if any(t.keyed_app() == kind for t in self.txs.values()):
            return True
        app = self.apps.get(kind)
        return bool(app and app.on_air())

    def on_air_apps(self) -> list[str]:
        return [k for k in self.apps if self._app_on_air(k)]

    def binding(self, model: str, cid: str, slot: str, on_air: list[str] | None = None) -> tuple[str, str]:
        """(app kind, action) a control runs now. An app on air that is not
        in front keeps its transmit key on its control, whatever app is in
        front, so the key that stops the carrier never moves (§7.3)."""
        drv = by_model(model)
        if slot == "press":
            for k in self.on_air_apps() if on_air is None else on_air:
                if k != self.front:
                    held = mapping.tx_holders(self.pages(k, model)[0][0], self.actions(k)).get(cid)
                    if held:
                        return k, held
        pages, _notes = self.pages(self.front, model)
        p = self._page(self.front, model, len(pages))
        b = pages[p].get(cid) or ({"press": "none"} if drv.control_kind(cid) else {})
        return self.front, b.get(slot, "none" if slot == "press" else "none-turn")

    def _page(self, kind: str, model: str, count: int) -> int:
        p = self.page_at.get((kind, model), 0)
        if p >= count:                  # pages taken away under it
            p = self.page_at[(kind, model)] = 0
        return p

    # Events ---------------------------------------------------------------------------
    def handle(self, model: str, event: Event) -> None:
        """One event from a pad of this model (or, for model `keyboard`, from
        a desktop window's key handler)."""
        kind, cid, step = event
        for listen in list(self.listeners):
            try:
                listen(model, event)
            except Exception as exc:     # a page's view must never drop the pad
                self.log(f"pad listener: {exc}")
        if self.testing():
            # The test only shows what arrived: no action runs and nothing
            # can key, whatever the map says.
            with self.lock:
                if kind == "press" and self.test is not None:
                    self.test["hit"][cid] = self.clock()
                elif kind == "release":
                    # Pressed before the test began: forget it, or the first
                    # press after the test would read as auto-repeat.
                    self.down.discard((model, cid))
                self.generation += 1
            return
        tx = self.tx_for(model)
        with self.lock:
            if kind == "release":
                self.down.discard((model, cid))
                tx.release(cid)
                if self.pending_front and not tx.holding():
                    self.set_foreground(self.pending_front)
                self.generation += 1
                return
            if kind == "press":
                if (model, cid) in self.down:
                    return              # a second press with no release is auto-repeat, never a press
                self.down.add((model, cid))
            elif kind != "turn":
                return
            app_kind, aid = self.binding(model, cid, kind)
            act = self.actions(app_kind).get(aid)
            if act is None or aid in ("none", "none-turn"):
                return
            if self.handover and not act.tx:
                return                  # handing over: only the stop key does anything
            app = self.apps[app_kind]
            pad = f"pad-{self.host}-{model}"
            self.generation += 1
            if act.tx:
                if app.lease is None:
                    self.flash[(model, cid)] = (Face(_("TX"), _("no lease"), "warn"), self.clock() + 2.5)
                    return
                tx.press(app_kind, aid, cid, app.lease, "hold" if act.hold else "arm", app.on_air())
                return
            if aid == "page":
                pages, _n = self.pages(app_kind, model)
                p = self.page_at[(app_kind, model)] = (self._page(app_kind, model, len(pages)) + 1) % len(pages)
                self.flash[(model, cid)] = (Face(_("PAGE"), f"{p + 1}/{len(pages)}", "info"), self.clock() + 2.5)
                return
            if aid == "app:next" or aid.startswith("app:"):
                self.request_switch(None if aid == "app:next" else aid[4:])
                return
        # The app's own action, outside the lock: an app handler (or the HTTP
        # bridge) may be slow, and the host's other pads must not wait on it.
        if act.turn_of:
            pa = PadAction(act.turn_of, "turn", act.step, cid, model, pad)
        else:
            pa = PadAction(aid, kind, step if kind == "turn" else 0, cid, model, pad)
        try:
            app.perform(pa)
        except Exception as exc:        # an action failing must not drop the pad
            self.message = f"{aid}: {exc}"[:120]
            self.log(self.message)

    # Test mode ------------------------------------------------------------------------
    def start_test(self, seconds: float = TEST_SECONDS) -> None:
        """Light every key in turn, then show each press on its own key, for
        `seconds` or until stop_test(). Events still reach the listeners;
        none reaches an app, so the test can never key anything. Refused
        (RuntimeError, a sentence) while any app is on the air: the key
        that stops the carrier must keep showing ON AIR. Anything armed or
        keyed from the pad is let go first."""
        with self.lock:
            if self.on_air_apps():
                raise RuntimeError(_("Not while on the air: stop transmitting first."))
            for t in self.txs.values():
                t.abort()
            now = self.clock()
            self.test = {"start": now, "until": now + seconds, "hit": {}}
            self.generation += 1

    def stop_test(self) -> None:
        with self.lock:
            if self.test is not None:
                self.test = None
                self.generation += 1

    def testing(self) -> bool:
        """In test mode now. It ends by itself when its time is up, and the
        moment any app goes on the air (keyed from its window, say)."""
        with self.lock:
            if self.test is None:
                return False
            if self.clock() >= self.test["until"] or self.on_air_apps():
                self.test = None
                self.generation += 1
                return False
            return True

    def _test_faces(self, drv: type[Driver]) -> dict[str, Face]:
        now = self.clock()
        keys = drv.screen_keys()
        lit = int((now - self.test["start"]) / TEST_SWEEP)
        out = {}
        for n, cid in enumerate(keys):
            hit = self.test["hit"].get(cid)
            if hit is not None and now - hit < 1.5:
                out[cid] = Face(cid, _("pressed"), "active")
            elif lit < len(keys) and n == lit:
                out[cid] = Face(cid, _("test"), "info")
            else:
                out[cid] = Face(cid, _("press") if hit is None else _("ok"), "normal")
        return out

    # Faces ----------------------------------------------------------------------------
    def faces(self, model: str) -> dict[str, Face]:
        """What each screen key of this model shows now."""
        drv = by_model(model)
        if self.testing():
            with self.lock:
                if self.test is not None:
                    return self._test_faces(drv)
        now = self.clock()
        tx = self.tx_for(model)
        cache: dict[str, dict] = {}

        def app_faces(k: str) -> dict:
            if k not in cache:
                try:
                    cache[k] = self.apps[k].faces() or {}
                except Exception as exc:
                    self.log(f"{k} faces: {exc}")
                    cache[k] = {}
            return cache[k]

        out = {}
        with self.lock:
            # Asked once per drawing, not once per key: on the platform each
            # app's on_air() may be a broker or HTTP lookup.
            on_air = self.on_air_apps()
            for cid in drv.screen_keys():
                flash = self.flash.get((model, cid))
                if flash and now < flash[1]:
                    out[cid] = flash[0]
                    continue
                k, aid = self.binding(model, cid, "press", on_air)
                act = self.actions(k).get(aid)
                if act is None or aid == "none" or (self.handover and not act.tx):
                    out[cid] = BLANK
                elif act.tx:
                    out[cid] = self._tx_face(k, act, app_faces(k), tx)
                else:
                    out[cid] = self._face(k, act, app_faces(k), model)
        return out

    def _tx_face(self, k: str, act: Action, data: dict, tx: PadTx) -> Face:
        app = self.apps[k]
        sent = (data.get("faces") or {}).get(act.id)
        on_air = tx.keyed_app() == k or bool(data.get("onAir")) or app.on_air()
        if app.lease is None:
            return Face(_("TX"), _("no lease"), "disabled")
        if on_air:
            # The app may add a line (the time-out countdown in its last
            # minute); the colour and the words are the library's.
            l2 = (sent or {}).get("l2", "") if (sent or {}).get("tone") == "onair" else ""
            if self.handover:
                l2 = app.name[:8]
            return Face(_("ON AIR"), l2, "onair")
        if tx.is_armed(k):
            return Face(_("ARM"), _("again"), "warn")
        reason = tx.refusal(k)
        if reason is not None:
            return Face(_("TX"), " ".join(reason.split()[:2])[:8] or _("refused"), "warn")
        if sent:
            return Face.from_app(sent)
        return Face(_("PTT") if act.hold else _("TX"), "", "normal", act.icon)

    def _face(self, k: str, act: Action, data: dict, model: str) -> Face:
        if act.id == "page":
            pages, _n = self.pages(k, model)
            p = self._page(k, model, len(pages))
            return Face(_("PAGE"), f"{p + 1}/{len(pages)}", "info" if p else "normal")
        if act.id == "app:next":
            return Face(_("APPS"), "", "normal", "apps")
        if act.id.startswith("app:"):
            target = self.apps.get(act.id[4:])
            return Face(target.name[:8] if target else act.id[4:], "", "active" if act.id[4:] == self.front else "normal",
                        act.icon)
        sent = (data.get("faces") or {}).get(act.id)
        if sent:
            return Face.from_app(sent, act.icon)
        app = self.apps[k]
        l1, l2 = app.text(act.short[0]), app.text(act.short[1])
        if not l1 and not act.icon:
            l1 = act.id.split(":")[0].upper()[:6]
        return Face(l1, l2, "normal", act.icon)

    # Devices --------------------------------------------------------------------------
    def attach(self, device: Driver) -> None:
        """Wake a newly opened device and start serving it (see step())."""
        device.start(self.brightness)
        with self.lock:
            self.devices[type(device).model] = device
            self.message = f"{type(device).name}: {device.path}"
            self.last_error = ""
            self.generation += 1
        self._drawn.pop(type(device).model, None)
        self._alive_at[type(device).model] = self.clock()

    def refresh(self, device: Driver) -> None:
        if type(device).screen:
            for cid, face in self.faces(type(device).model).items():
                device.show(cid, face)

    def step(self, device: Driver, timeout: float = 0.25) -> None:
        """One turn of a device's loop: read, act, heartbeat, redraw what
        changed. OSError when the device has gone - the caller then calls
        detach() at once."""
        model = type(device).model
        events = device.read(timeout)
        for event in events:
            self.handle(model, event)
        self.tx_for(model).tick(lambda k: self.apps[k].on_air() if k in self.apps else None)
        now = self.clock()
        gen = self.generation
        drawn_gen, drawn_at = self._drawn.get(model, (-1, float("-inf")))
        # Redraw at once after anything that changes a face, and every half
        # second anyway for faces that follow the app (a frequency, ON AIR
        # keyed from another client). Only changed keys reach the USB.
        if events or gen != drawn_gen or now - drawn_at >= 0.5:
            try:
                self.refresh(device)
            except OSError:
                raise
            except Exception as exc:
                # A face that will not draw (a font, a Pillow without
                # FreeType) must not end the device's loop: serve() would
                # blank it, the desktop would open and wake it again a
                # quarter second later, and every key would flicker for
                # ever. The keys still work; the reason is kept.
                self.message = f"drawing the keys: {exc}"[:120]
                self.log(self.message)
            self._drawn[model] = (gen, now)
        # A pad that goes quiet on its host drops the connection
        # (drivers/ajazz.py says how this was found). Sent whatever else
        # is written: whether a picture counts for the pad is not known.
        every = type(device).keep_alive_every
        if every and now - self._alive_at.get(model, now) >= every:
            device.keep_alive()
            self._alive_at[model] = now

    def detach(self, device: Driver) -> bool:
        """The device has gone or is being let go. Unkey FIRST - anything this
        pad keyed stops before the device is touched again - then blank and
        close. True if a transmission was stopped."""
        model = type(device).model
        stopped = self.tx_for(model).abort()
        try:
            device.blank()
        except OSError:
            pass                        # gone: nothing to blank
        device.close()
        with self.lock:
            if self.devices.get(model) is device:
                self.devices.pop(model)
            self.down = {d for d in self.down if d[0] != model}
            self.generation += 1
        return stopped

    def serve(self, device: Driver, stop: threading.Event | None = None) -> None:
        """Serve one device until it goes or stop is set."""
        stop = stop or threading.Event()
        try:
            self.attach(device)
            while not stop.is_set():
                self.step(device, 0.25)
        except OSError as exc:
            self.message = _("Controller disconnected ({why})").format(why=exc.strerror or exc)
            self.last_error = diagnose.explain(exc, doing="write" if "write" in str(exc).lower() else "read")
            self.log(self.message)
        finally:
            self.detach(device)

    def run(self, stop: threading.Event, drivers: list[type[Driver]] | None = None, cfg: dict | None = None,
            every: float = 3.0) -> None:
        """Look for pads every few seconds and serve each on its own thread
        until it is unplugged, then look again (the box's Controller.run)."""
        busy: set[str] = set()
        threads: list[threading.Thread] = []
        lock = threading.Lock()

        def one(drv: type[Driver], path: str) -> None:
            try:
                self.serve(drv.open(path), stop)
            except OSError as exc:
                self.log(f"{path}: {exc}")
            finally:
                with lock:
                    busy.discard(path)

        while not stop.is_set():
            for drv in drivers if drivers is not None else DRIVERS:
                try:
                    paths = drv.find(cfg)
                except Exception:
                    paths = []
                for path in paths:
                    with lock:
                        if path in busy:
                            continue
                        busy.add(path)
                    t = threading.Thread(target=one, args=(drv, path), daemon=True, name=f"shack-pad {path}")
                    threads.append(t)
                    t.start()
            threads = [t for t in threads if t.is_alive()]
            stop.wait(every)
        # Stopping is a clean release: every pad unkeyed, blanked and closed
        # before run() returns, not left to daemon threads at exit.
        for t in threads:
            t.join(timeout=2.0)
