"""The recordings folder on a module's data volume: what is in it, how much
room it takes, and keeping it inside its limits.

Brian's rules (12/10/2026):

- Recordings live on the module's data volume
  (`/var/lib/dock360/modules/<id>/data/recordings/`, `/data/recordings` in
  the container), which survives updates.
- Past a disk-use limit (a module setting) the oldest are deleted.
- Never fill the disk: below a free-space floor nothing starts, and a
  recording that is running stops there.

**One recording is files sharing a base name**, which is its id:

| kind | data | metadata |
|---|---|---|
| audio | `<id>.flac` | `<id>.json` (written by shack_rec.audio) |
| iq | `<id>.sigmf-data` | `<id>.sigmf-meta` (SigMF; QO-100 NB and Radio write it) |

The folder is the database. There is no index to fall out of step with
the files: a recording copied in by hand appears, and one deleted by hand
goes. Listing reads every metadata file each time, which is nothing at the
hundreds of recordings a limit of a few GB allows.

**Ids are checked twice** before a path is built from one: against a strict
pattern, then against the names actually in the folder, so no request can
reach outside it.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from .i18n import _

log = logging.getLogger("shack_rec")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
# suffix -> (kind, part)
LAYOUT = {".flac": ("audio", "data"), ".json": ("audio", "meta"),
          ".sigmf-data": ("iq", "data"), ".sigmf-meta": ("iq", "meta")}
CONTENT_TYPES = {".flac": "audio/flac", ".json": "application/json",
                 ".sigmf-data": "application/octet-stream", ".sigmf-meta": "application/json"}
GB = 1_000_000_000


class Refused(Exception):
    """Why a recording request was not done: an HTTP status, a snake_case
    code for clients, and a sentence for the person."""

    def __init__(self, status: int, code: str, text: str):
        super().__init__(text)
        self.status, self.code, self.text = status, code, text

    def body(self) -> dict:
        return {"error": self.text, "code": self.code}


def iso_utc(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text) -> float | None:
    try:
        return _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _split(name: str) -> tuple[str, str] | None:
    for suffix in LAYOUT:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)], suffix
    return None


def _read_json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, obj: dict) -> None:
    """Whole and renamed into place, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, path)


def _sigmf_entry(meta: dict) -> dict:
    """The fields a list shows, from a SigMF recording's metadata (NB and
    Radio's `openhamshack:` namespace where core has no field)."""
    g = meta.get("global") if isinstance(meta.get("global"), dict) else {}
    caps = meta.get("captures") if isinstance(meta.get("captures"), list) else []
    first = caps[0] if caps and isinstance(caps[0], dict) else {}
    receive = g.get("openhamshack:receive") if isinstance(g.get("openhamshack:receive"), dict) else {}
    start = g.get("openhamshack:start_utc") or first.get("core:datetime")
    t = parse_utc(start) if start else None
    out = {"format": "sigmf-" + str(g.get("core:datatype", "")),
           "startUtc": iso_utc(t) if t else None,
           "sampleRate": g.get("core:sample_rate"),
           "freqHz": receive.get("rxDial") or first.get("core:frequency"),
           "mode": str(receive.get("mode") or "").upper() or None,
           "station": g.get("core:author") or g.get("openhamshack:callsign"),
           "seconds": g.get("openhamshack:seconds"),
           "complete": bool(g.get("openhamshack:complete")),
           "stopped": g.get("openhamshack:stopped") or ""}
    if t is not None:
        out["_t"] = t
    return out


class Store:
    """The recordings folder. `limit_bytes` is the most the folder may hold
    before the oldest go; `floor_bytes` the free space below which nothing
    records. `disk_usage` is shutil.disk_usage (tests give their own)."""

    def __init__(self, folder, limit_bytes: int, floor_bytes: int, disk_usage=shutil.disk_usage):
        self.folder = Path(folder)
        self.limit_bytes = int(limit_bytes)
        self.floor_bytes = int(floor_bytes)
        self.disk_usage = disk_usage

    # ---------------------------------------------------------------- reading
    def _groups(self) -> dict[str, dict[str, Path]]:
        """{id: {suffix: path}} for every recording-shaped file."""
        out: dict[str, dict[str, Path]] = {}
        try:
            names = os.listdir(self.folder)
        except FileNotFoundError:
            return out
        for name in names:
            split = _split(name)
            if split is None or not ID_RE.match(split[0]):
                continue
            out.setdefault(split[0], {})[split[1]] = self.folder / name
        return out

    def entry(self, rid: str, files: dict[str, Path] | None = None) -> dict | None:
        """One recording as the list shows it, or None if `rid` is not one."""
        if files is None:
            if not ID_RE.match(rid or ""):
                return None
            files = self._groups().get(rid)
        if not files:
            return None
        if ".sigmf-data" in files:
            kind, data, meta = "iq", files[".sigmf-data"], files.get(".sigmf-meta")
            facts = _sigmf_entry(_read_json(meta)) if meta else {"complete": False}
        elif ".flac" in files:
            kind, data, meta = "audio", files[".flac"], files.get(".json")
            facts = _read_json(meta) if meta else {"complete": False}
        else:
            return None                                # metadata whose data is gone
        parts, total = {}, 0
        for part, path in (("data", data), ("meta", meta)):
            if path is None:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            total += st.st_size
            parts[part] = {"name": path.name, "bytes": st.st_size, "type": CONTENT_TYPES[_split(path.name)[1]]}
            if part == "data":
                mtime = st.st_mtime
        if "data" not in parts:
            return None
        t = facts.pop("_t", None) or parse_utc(facts.get("startUtc")) or mtime
        keep = ("format", "startUtc", "endUtc", "seconds", "sampleRate", "channels", "freqHz", "mode", "station",
                "radio", "app", "complete", "stopped")
        e = {"id": rid, "kind": kind, **{k: facts.get(k) for k in keep if k in facts}, "bytes": total,
             "parts": parts}
        e.setdefault("startUtc", iso_utc(t))
        e["complete"] = bool(e.get("complete"))
        e["_t"] = t
        return e

    def list(self) -> list[dict]:
        """Every recording, newest first."""
        out = [e for rid, files in self._groups().items() if (e := self.entry(rid, files))]
        out.sort(key=lambda e: e["_t"], reverse=True)
        for e in out:
            e.pop("_t", None)
        return out

    def used(self) -> int:
        total = 0
        for files in self._groups().values():
            for p in files.values():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def free(self) -> int:
        try:
            return int(self.disk_usage(self.folder if self.folder.exists() else self.folder.parent).free)
        except OSError:
            return 0

    def usage(self) -> dict:
        return {"bytes": self.used(), "limitBytes": self.limit_bytes, "freeBytes": self.free(),
                "floorBytes": self.floor_bytes}

    def file(self, rid: str, part: str = "data") -> tuple[Path, str]:
        """(path, content type) of one part of a recording; Refused 404."""
        e = self.entry(rid)
        p = (e or {}).get("parts", {}).get(part)
        if p is None:
            raise Refused(404, "no_recording", _("There is no such recording."))
        return self.folder / p["name"], p["type"]

    # ---------------------------------------------------------------- changing
    def new_id(self, app: str, t: float, freq_hz: float | None, mode: str | None) -> str:
        """A fresh id: app, UTC start, dial and mode, so a folder of
        recordings sorts by time and says what each is at a glance. Never
        one in use (two in the same second get -2, -3...)."""
        stamp = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime(t))
        bits = [re.sub(r"[^A-Za-z0-9]+", "", app) or "rec", stamp]
        if freq_hz:
            bits.append(f"{float(freq_hz) / 1e6:.6f}MHz")
        if mode:
            bits.append(re.sub(r"[^A-Za-z0-9]+", "", str(mode)).upper())
        name = "-".join(b for b in bits if b)
        taken = self._groups()
        for i in range(1, 1000):
            rid = name if i == 1 else f"{name}-{i}"
            if rid not in taken:
                return rid
        raise Refused(409, "name_taken", _("Too many recordings started in the same second."))

    def delete(self, rid: str) -> dict:
        e = self.entry(rid)
        if e is None:
            raise Refused(404, "no_recording", _("There is no such recording."))
        for p in self._groups().get(rid, {}).values():
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        e.pop("_t", None)
        return e

    def check_room(self) -> None:
        """Refused 507 when free space is below the floor: the one check
        before anything records."""
        free = self.free()
        if free < self.floor_bytes:
            raise Refused(507, "low_disk", _(
                "Not enough free disk space to record: {free} free, and recordings stop at {floor}.").format(
                free=size_text(free), floor=size_text(self.floor_bytes)))

    def prune(self, keep: set[str] = frozenset()) -> list[str]:
        """Delete the oldest recordings, never those in `keep` (the ones
        being written), until the folder is within its limit. Returns the
        ids deleted."""
        gone = []
        used = self.used()
        if used <= self.limit_bytes:
            return gone
        for e in sorted(self.list(), key=lambda e: parse_utc(e["startUtc"]) or 0):
            if used <= self.limit_bytes:
                break
            if e["id"] in keep:
                continue
            self.delete(e["id"])
            used -= e["bytes"]
            gone.append(e["id"])
            log.info("recordings over %s: deleted %s (%s)", size_text(self.limit_bytes), e["id"],
                     size_text(e["bytes"]))
        return gone


def size_text(n: float) -> str:
    """Bytes as a person reads them: kB, MB, then GB."""
    n = float(n)
    if n >= GB:
        return f"{n / GB:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e3:.0f} kB"
