"""The API apps use: `Station` (SYNC.md §9.1).

    station = Station.open(app="shack-qo100nb", app_version="1.0.0")
    qso = station.logbook.add({"call": "DL1ABC", "start": "2026-10-09T10:12:30Z"})
    station.profile.update({"locator": "IO75RL"})
    result = station.pair("http://198.51.100.208/", "brian", "...", device_name="Shack laptop")

Every call here reads or writes the local store and returns: no call a UI
makes does network I/O on the caller's thread, except `pair()`, which is
the one operation that exists to talk to a controller and which an app
runs off its UI thread like any other request it makes.
"""
from __future__ import annotations

from ._i18n import _

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import adif as A
from . import hlc as H
from .merge import is_live, join_profile, split_profile
from .pairing import Token, exchange_password
from .store import Store, default_path, uuid7
from .sync import SyncClient, _check
from .transport import HttpTransport

DUPLICATE_WINDOW_S = 300        # SERVICES.md §3.1 (icom/app/logbook.py DUPLICATE_WINDOW)
_LOCATOR = re.compile(r"^[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2}([0-9]{2})?)?$")


@dataclass
class Qso:
    uuid: str
    fields: dict
    created_at: str
    origin: dict
    deleted: bool
    hlc: str
    duplicate_of: list = field(default_factory=list)

    @classmethod
    def of(cls, rec: dict) -> "Qso":
        stamps = list(rec["fieldHlc"].values()) + ([rec["deletedHlc"]] if rec.get("deletedHlc") else [])
        return cls(rec["id"], dict(rec["fields"]), rec.get("createdAt", ""), dict(rec.get("origin") or {}),
                   not is_live(rec), max(stamps) if stamps else "")


@dataclass
class Pairing:
    controller_id: str
    name: str
    base_url: str
    device_id: str
    user: str
    max_level: str


@dataclass
class NeedsDecision:
    """§7: this computer and the controller both have a log and different
    callsigns. The app asks: merge, or use the controller's log."""
    pairing: Pairing
    local_callsign: str
    controller_callsign: str
    local_count: int
    controller_has_log: bool


class Logbook:
    def __init__(self, station: "Station"):
        self.st = station

    def _origin(self) -> dict:
        return self.st.origin()

    @staticmethod
    def _clean(fields: dict, require: bool) -> dict:
        out = {}
        for k, v in fields.items():
            if not isinstance(k, str) or not re.match(r"^[a-z][a-z0-9_]*$", k):
                raise ValueError(_("{name} is not a logbook field name: fields are lower-case ADIF names").format(name=repr(k)))
            out[k] = v
        if "call" in out and out["call"] is not None:
            # Locale-independent upper case: str.upper is, unlike some
            # platforms' (the Turkish dotted I).
            out["call"] = str(out["call"]).strip().upper()
        if require:
            if not out.get("call"):
                raise ValueError(_("A contact needs a callsign"))
            if not out.get("start"):
                raise ValueError(_("A contact needs a start time"))
        if out.get("start"):
            try:
                H.parse_iso_ms(out["start"])
            except ValueError:
                raise ValueError(_("The start time {start} is not an ISO date and time").format(start=repr(out["start"]))) from None
        return out

    def add(self, fields: dict, uuid_: str | None = None) -> Qso:
        f = self._clean(fields, require=True)
        rid = (uuid_ or uuid7(self.st.store.now())).lower()
        if self.st.store.get("logbook", rid) is not None:
            # Idempotent by UUID (SERVICES.md §3.2 POST): the same contact
            # again is the stored contact, not a second one.
            return self.get(rid)
        q = Qso.of(self.st.store.edit("logbook", rid, "create", f, self._origin()))
        q.duplicate_of = self._duplicates(q)
        self.st._local_change("logbook", [rid])
        return q

    def add_many(self, items: list[dict]) -> list[Qso]:
        edits = []
        for fields in items:
            f = self._clean(fields, require=True)
            rid = str(fields.get("uuid") or "").lower() or uuid7(self.st.store.now())
            f.pop("uuid", None)
            edits.append(("logbook", rid, "create", f, self._origin()))
        recs = self.st.store.edit_many(edits)
        self.st._local_change("logbook", [r["id"] for r in recs])
        return [Qso.of(r) for r in recs]

    def update(self, uuid_: str, fields: dict) -> Qso:
        if self.st.store.get("logbook", uuid_) is None:
            raise LookupError(_("There is no contact with that id in this logbook"))
        f = self._clean(fields, require=False)
        q = Qso.of(self.st.store.edit("logbook", uuid_, "update", f, self._origin()))
        self.st._local_change("logbook", [uuid_])
        return q

    def delete(self, uuid_: str) -> None:
        if self.st.store.get("logbook", uuid_) is None:
            raise LookupError(_("There is no contact with that id in this logbook"))
        self.st.store.edit("logbook", uuid_, "delete", {}, self._origin())
        self.st._local_change("logbook", [uuid_])

    def get(self, uuid_: str) -> Qso | None:
        rec = self.st.store.get("logbook", uuid_)
        return Qso.of(rec) if rec else None

    def query(self, call: str | None = None, band: str | None = None, mode: str | None = None,
              since: str | None = None, until: str | None = None, limit: int = 100,
              include_deleted: bool = False) -> list[Qso]:
        """Newest first. Deleted contacts are skipped unless asked for
        (lists and lookups skip them; history and sync do not: §5)."""
        q, args = "SELECT id FROM qso_index WHERE 1=1", []
        for col, val in (("call", call and call.upper()), ("band", band), ("mode", mode and mode.upper())):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        if since:
            q += " AND start>=?"
            args.append(since)
        if until:
            q += " AND start<=?"
            args.append(until)
        if not include_deleted:
            q += " AND deleted=0"
        q += " ORDER BY start DESC, id LIMIT ?"
        args.append(int(limit))
        return [self.get(r[0]) for r in self.st.store.db.execute(q, args).fetchall()]

    def count(self) -> int:
        return self.st.store.db.execute("SELECT COUNT(*) FROM qso_index WHERE deleted=0").fetchone()[0]

    def worked_before(self, call: str, band: str | None = None, mode: str | None = None) -> list[Qso]:
        return self.query(call=call, band=band, mode=mode, limit=1000)

    def _duplicates(self, q: Qso) -> list:
        """Advice, not a rule (SERVICES.md §3.1): same call, band and mode
        within 300 s is probably the same contact."""
        try:
            t = H.parse_iso_ms(q.fields["start"]) // 1000
        except (KeyError, ValueError):
            return []
        band = q.fields.get("band") or A.band_for(q.fields.get("freq_hz"))
        out = []
        for other in self.query(call=q.fields.get("call"), band=band or None, mode=q.fields.get("mode"), limit=50):
            if other.uuid == q.uuid:
                continue
            try:
                if abs(H.parse_iso_ms(other.fields["start"]) // 1000 - t) <= DUPLICATE_WINDOW_S:
                    out.append(other.uuid)
            except (KeyError, ValueError):
                continue
        return out

    def history(self, uuid_: str) -> list[dict]:
        """Every edit made to this contact on this machine, oldest first.
        The controller's history (SERVICES.md §3.2) adds every other
        device's, with `lost` marks."""
        rows = self.st.store.db.execute("SELECT op_id, hlc, kind, fields, origin, created_at FROM ops "
                                        "WHERE collection='logbook' AND id=? ORDER BY hlc", (uuid_,)).fetchall()
        return [{"opId": o, "hlc": h, "kind": k, "fields": json.loads(f), "origin": json.loads(org), "createdAt": c}
                for o, h, k, f, org, c in rows]

    def export_adif(self, program_version: str = "") -> str:
        recs = [r for r in self.st.store.all("logbook") if is_live(r)]
        recs.sort(key=lambda r: (r["fields"].get("start", ""), r["id"]))
        return A.write(recs, program_version or self.st.app_version)

    def import_adif(self, text: str) -> dict:
        """SERVICES.md §3.2 import: every record gets its APP_SHACK_UUID or a
        fresh v7 UUID; duplicates are reported, never skipped silently."""
        added, dups, errors, edits = 0, [], [], []
        pending: list[Qso] = []
        for line, rec in A.parse(text):
            try:
                fields, uid, _origin = A.to_fields(rec)
                fields = self._clean(fields, require=True)
            except ValueError as e:
                errors.append({"line": line, "error": str(e)})
                continue
            if uid and self.st.store.get("logbook", uid) is not None:
                dups.append({"line": line, "of": uid})
                continue
            rid = uid or uuid7(self.st.store.now())
            probe = Qso(rid, fields, "", {}, False, "")
            same = self._duplicates(probe) or [p.uuid for p in pending if self._near(p, probe)]
            if same:
                dups.append({"line": line, "of": same[0]})
                continue
            pending.append(probe)
            edits.append(("logbook", rid, "create", fields, self._origin()))
        if edits:
            self.st.store.edit_many(edits)
            added = len(edits)
            self.st._local_change("logbook", [e[1] for e in edits])
        return {"added": added, "duplicates": dups, "errors": errors}

    @staticmethod
    def _near(a: Qso, b: Qso) -> bool:
        fa, fb = a.fields, b.fields
        if fa.get("call") != fb.get("call") or str(fa.get("mode", "")).upper() != str(fb.get("mode", "")).upper():
            return False
        if (fa.get("band") or A.band_for(fa.get("freq_hz"))) != (fb.get("band") or A.band_for(fb.get("freq_hz"))):
            return False
        try:
            return abs(H.parse_iso_ms(fa["start"]) - H.parse_iso_ms(fb["start"])) <= DUPLICATE_WINDOW_S * 1000
        except (KeyError, ValueError):
            return False


class Profile:
    """The station profile, one record (collection "profile", id
    "station"): SERVICES.md §2.1, merged per field with the two maps split
    into dotted paths (SYNC.md §4.2)."""
    ID = "station"

    def __init__(self, station: "Station"):
        self.st = station

    def get(self) -> dict:
        rec = self.st.store.get("profile", self.ID)
        out = {"schema": 1}
        if rec:
            out.update(join_profile(rec["fields"]))
        return out

    def update(self, partial: dict) -> dict:
        p = dict(partial)
        if "callsign" in p and p["callsign"] is not None:
            p["callsign"] = str(p["callsign"]).strip().upper()
        if p.get("locator"):
            loc = str(p["locator"]).strip()
            if not _LOCATOR.match(loc):
                raise ValueError(_("{locator} is not a Maidenhead locator (4, 6 or 8 characters)").format(locator=loc))
            p["locator"] = loc[:4].upper() + loc[4:6].upper() + loc[6:]
        p.pop("schema", None)
        p.pop("updatedAt", None)
        self.st.store.edit("profile", self.ID, "update", split_profile(p), self.st.origin())
        self.st._local_change("profile", [self.ID])
        return self.get()


class Station:
    def __init__(self, store: Store, app: str, app_version: str, transport_factory=None, background: bool = True,
                 device: str = ""):
        self.store = store
        self.app = app
        self.app_version = app_version
        self.device = device
        self.transport_factory = transport_factory or (lambda base, token: HttpTransport(base, token))
        self.logbook = Logbook(self)
        self.profile = Profile(self)
        self.sync = SyncClient(self)
        self._listeners: list = []
        self._watch: threading.Thread | None = None
        self._closed = threading.Event()
        self.background = background
        if background and self.pairing_info() is not None:
            self.sync.start()

    @classmethod
    def open(cls, app: str, app_version: str = "", path: str | Path | None = None, *, clock=H.system_ms,
             transport_factory=None, background: bool = True) -> "Station":
        return cls(Store(path or default_path(), clock=clock), app, app_version, transport_factory, background)

    # ---------------------------------------------------------- identity
    @property
    def pairing_path(self) -> Path:
        return self.store.path.with_name("pairing.json")

    def pairing_info(self) -> dict | None:
        try:
            return json.loads(self.pairing_path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def origin(self) -> dict:
        p = self.pairing_info() or {}
        return {"app": self.app, "device": p.get("deviceId") or self.device or self.store.store_id,
                "user": p.get("user", "")}

    def client_id(self) -> str:
        cid = self.store.meta("client_id")
        if not cid:
            with self.store.tx():
                cid = self.store.meta("client_id") or str(uuid.uuid4())
                self.store.set_meta("client_id", cid)
        return cid

    def transport(self):
        p = self.pairing_info()
        return self.transport_factory(p["baseUrl"], p["token"])

    # ---------------------------------------------------------- change notification
    def on_change(self, callback) -> None:
        """callback(collection, ids) after local or synced changes in this
        process; callback(None, []) when another process changed the store
        (noticed by PRAGMA data_version, once a second)."""
        self._listeners.append(callback)
        if self._watch is None:
            self._watch = threading.Thread(target=self._watch_loop, name="shack-store-watch", daemon=True)
            self._watch.start()

    def _watch_loop(self) -> None:
        import sqlite3
        db = sqlite3.connect(str(self.store.path), check_same_thread=False)
        seen = db.execute("PRAGMA data_version").fetchone()[0]
        while not self._closed.wait(1.0):
            now = db.execute("PRAGMA data_version").fetchone()[0]
            if now != seen:
                seen = now
                for cb in list(self._listeners):
                    cb(None, [])
        db.close()

    def _notify(self, keys: list) -> None:
        by: dict = {}
        for coll, rid in keys:
            by.setdefault(coll, []).append(rid)
        for coll, ids in by.items():
            for cb in list(self._listeners):
                cb(coll, ids)

    def _local_change(self, collection: str, ids: list) -> None:
        for cb in list(self._listeners):
            cb(collection, ids)
        self.sync.local_write()

    # ---------------------------------------------------------- pairing
    def pair(self, base_url: str, username: str, password: str, totp: str | None = None,
             device_name: str = "", device_kind: str = "desktop"):
        """Pair with a controller (CONTROLLER-CONTRACT.md §4.2) and, when
        nothing needs deciding, do the first full exchange (§7). Returns
        Pairing or NeedsDecision; raises PairingError or Offline."""
        t = self.transport_factory(base_url, None)
        tok: Token = exchange_password(t, username, password, totp=totp, device_name=device_name or self.app,
                                       device_kind=device_kind, client_id=self.client_id(), app=self.app)
        name, station_call = "", ""
        try:
            status, wk = t.request("GET", ".well-known/openhamshack")
            if status == 200:
                name = wk.get("name", "")
                station_call = (wk.get("station") or {}).get("callsign", "")
        except Exception:
            pass
        info = {"controllerId": tok.controller_id, "baseUrl": base_url, "token": tok.token, "deviceId": tok.device_id,
                "user": tok.user, "maxLevel": tok.max_level, "name": name, "pairedAt": self.store.now_iso()}
        self._write_pairing(info)
        pairing = Pairing(tok.controller_id, name, base_url, tok.device_id, tok.user, tok.max_level)
        if self.store.cursor(tok.controller_id) is None:
            # A first exchange pushes everything (§7): records confirmed by
            # another controller are not dirty, but this one lacks them.
            self.store.mark_all_dirty()
        decision = self._needs_decision(pairing, station_call)
        if decision is not None:
            with self.store.tx():
                self.store.set_meta("needs_decision", tok.controller_id)
            return decision
        self.sync.run_once()
        if self.background:
            self.sync.start()
        return pairing

    def _write_pairing(self, info: dict) -> None:
        tmp = self.pairing_path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(info, f)
        os.replace(tmp, self.pairing_path)

    def _needs_decision(self, pairing: Pairing, station_call: str = "") -> NeedsDecision | None:
        """§7: a full exchange when the controller's log is empty or its
        callsign matches; also when this computer has no log at all
        (nothing to decide). Otherwise the app asks."""
        # A controller this store has synced with before already holds the
        # union: re-pairing with it is as a first pairing, minus the
        # question (§7 "Re-pair with the same controller").
        if self.store.cursor(pairing.controller_id) is not None or \
                pairing.controller_id in json.loads(self.store.meta("synced_with") or "[]"):
            return None
        local_n = self.logbook.count()
        if local_n == 0:
            return None
        t = self.transport()
        status, logs = t.request("GET", "api/shack/v1/sync/pull", query={"since": 0, "limit": 1,
                                                                        "collections": "logbook"})
        _check(status, logs)
        if not logs.get("records"):
            return None
        # The controller's station callsign: its public advert
        # (/.well-known station.callsign), else its synced profile.
        theirs = station_call
        if not theirs:
            status, prof = t.request("GET", "api/shack/v1/sync/pull", query={"since": 0, "limit": 5,
                                                                            "collections": "profile"})
            _check(status, prof)
            theirs = next((r["fields"].get("callsign", "") for r in prof.get("records", [])
                           if r["id"] == Profile.ID), "")
        mine = self.profile.get().get("callsign", "")
        if theirs and mine and theirs.upper() == mine.upper():
            return None
        return NeedsDecision(pairing, mine, theirs, local_n, True)

    def resolve(self, result, choice: str = "merge"):
        """Answer a NeedsDecision: "merge" this computer's log with the
        controller's, or "use-controller": archive this one
        (station-<date>.db and an .adi beside it) and take the
        controller's. There is no "keep two logs": one store is one
        station."""
        if choice not in ("merge", "use-controller"):
            raise ValueError(_("The choice is merge or use-controller"))
        if choice == "use-controller":
            stamp = self.store.now_iso()[:10]
            base = self.store.path.with_name(f"station-{stamp}")
            n = 1
            while base.with_suffix(".db").exists():
                n += 1
                base = self.store.path.with_name(f"station-{stamp}-{n}")
            base.with_suffix(".adi").write_text(self.logbook.export_adif(), encoding="utf-8")
            self.store.archive_to(base.with_suffix(".db"))
            self.store.wipe_replica()
        with self.store.tx():
            self.store.db.execute("DELETE FROM meta WHERE key='needs_decision'")
        self.sync.run_once()
        if self.background:
            self.sync.start()
        return result.pairing if isinstance(result, NeedsDecision) else result

    def unpair(self) -> None:
        """§7: delete pairing.json and that controller's cursor. Records,
        ops and history stay exactly as they are; no network request."""
        p = self.pairing_info()
        self.sync.stop()
        if p is not None:
            self.store.drop_cursor(p["controllerId"])
        try:
            self.pairing_path.unlink()
        except FileNotFoundError:
            pass
        with self.store.tx():
            self.store.db.execute("DELETE FROM meta WHERE key='needs_decision'")
        self.sync.state = "unpaired"
        self.sync.profile_rejected = False

    def close(self) -> None:
        self._closed.set()
        # Wait for the watcher to close its own connection before the store
        # goes: left running, it could poll a database a test has already
        # deleted and log sqlite "disk I/O error" from a daemon thread. It
        # wakes from _closed at once, so the join is short; a listener that
        # calls close() runs on the watcher itself and must not join it.
        w = self._watch
        if w is not None and w is not threading.current_thread():
            w.join(timeout=5.0)
        self.sync.stop()
        self.store.close()
