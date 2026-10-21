"""Set a radio up for data, and put it back: read, save, set, restore.

Brian's rule for the packet app (18/10/2026): "build in to the app setting
whatever controls are needed for optimum performance". A radio left with
its noise reduction on decodes no AFSK (the IC-9700 on 2 m that morning);
one left on DATA MOD = USB after a USB session keys from the wrong input
the next time somebody uses it. So a data station sets what it needs and
leaves the radio as it found it - NB's setup assistant's rule
(`rigSetupSaved`, GUIDE §5), made general here so every app that drives a
radio for data uses the same one.

This module is maker-neutral and knows no radio. A maker's module
(icom/datasetup.py, yaesu/datasetup.py) hands it a list of `Setting`s,
each one narrow: one thing on the radio, a reader and a writer for it,
from that maker's sourced table. Here is only the order of things, which
is where the safety is:

1. **Never write what was not read.** A setting whose read is not answered
   is reported and left alone: there would be nothing to put back. One the
   radio refuses (an Icom's NG) is reported as not on this radio.
2. **Saved before it is set, on disk.** Each value is written to the
   journal (a small JSON file per radio) before the setting is changed, so
   a process that dies - a crash, a pulled plug, a kill - leaves behind
   exactly what is needed to undo it. The next `recover()` for that radio
   puts it back before anything else happens.
3. **Every write is read back.** A write is believed only when the radio
   says the new value; over USB this also proves the frame left the queue
   (shack_rig's USB writer, 18/10/2026: a queued restore was lost at close).
4. **Restored in reverse.** The frequency and mode are set first and put
   back last, so a setting the radio keeps per band (the 9700's squelch and
   RF power) is read, set and restored on the same band - unless the
   operator has since moved the radio to another band (rule 6 keeps their
   frequency), when such a setting is judged and put back on the band the
   radio is now on.
5. **A report of every line**, before and after, for the person and the
   log: "What the app changed on your radio".
6. **What the operator changed is the operator's.** A setting is put back
   only while the radio still holds what this set-up wrote to it. One the
   operator changed on the radio during the session - the VFO turned to
   another channel, the squelch knob - is left as they set it and reported
   "kept" (Packet Radio 0.3.2: Brian turned his IC-9700's VFO and the app
   would have put the old channel back at close, undoing what he chose).
   The rule is per setting, so a frequency the operator chose stays while
   the mode the app set is still put back. What the app wrote is journalled
   beside the value found, so `recover()` after a crash follows the same
   rule; a journal from before 0.3.2 has no record of it and is put back
   whole, as then.

Settings are read, set and restored one at a time, in order, from the
caller's thread; a setting's reader and writer block until the radio has
answered (an Icom's run on its rig loop through the caller's `call`).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("shack_rig.datasetup")

#: What a reader returns for a setting the radio says it does not have.
UNAVAILABLE = "unavailable"


@dataclass
class Setting:
    """One thing on the radio a data station sets.

    - `key`: stable, short ("nr", "data_mod"); the app's text for it is
      looked up by this.
    - `want`: the raw value to set, a callable (value read) -> raw value, or
      None to read and report only.
    - `read()`: the raw value now, None if the radio did not answer, or
      UNAVAILABLE. None for a setting the radio cannot be asked about:
      then it is set without a read and cannot be put back (`restorable`).
    - `write(raw)`: True if the command went; None for a setting that is
      only read and reported.
    - `show(raw)`: the value in a few words for a report ("on", "LAN").
    - `note`: for a setting this library cannot touch on this radio, what
      the operator should set by hand. Such a setting has neither a reader
      nor a writer.
    - `warn(raw)`: a warning about the value as found, or None.
    - `app_moves`: see the field.

    Raw values must survive JSON (ints, bools, strings, lists of those):
    they are kept in the journal.
    """

    key: str
    want: Any = None
    read: Callable[[], Any] | None = None
    write: Callable[[Any], bool] | None = None
    show: Callable[[Any], str] = str
    note: str = ""
    warn: Callable[[Any], str | None] | None = None
    #: The app itself goes on moving this setting after apply() (a
    #: satellite pass's VFOs, stepped for Doppler): a changed value is the
    #: app's, not the operator's, so rule 6 never keeps it.
    app_moves: bool = False

    def wanted(self, before: Any) -> Any:
        return self.want(before) if callable(self.want) else self.want


#: A report line's status, with how the log says it.
STATUSES = {
    "set": "changed",
    "already": "already as needed",
    "report": "left as it is",
    "unavailable": "not on this radio",
    "unread": "not answered, so left alone",
    "failed": "could not be changed",
    "manual": "set it on the radio",
    "set-unrestorable": "changed; the radio cannot report it, so it is not put back",
    "restored": "put back",
    "restore-failed": "could not be put back",
    "recovered": "put back after the last session ended without doing so",
    "kept": "changed on the radio during the session, so left as the operator set it",
}


@dataclass
class Line:
    """One line of the report."""

    key: str
    status: str
    before: str | None = None
    after: str | None = None
    note: str = ""

    def text(self) -> str:
        """For the log, in English (the app words its own from the fields)."""
        what = STATUSES.get(self.status, self.status)
        if self.status in ("set", "restored", "recovered", "set-unrestorable") and self.before is not None:
            got = f"{self.key}: {self.before} -> {self.after} ({what})"
        elif self.before is not None:
            got = f"{self.key}: {self.before} ({what})"
        else:
            got = f"{self.key}: {what}"
        return f"{got}; {self.note}" if self.note else got

    def public(self) -> dict:
        return {"key": self.key, "status": self.status, "before": self.before, "after": self.after,
                "note": self.note}


def _norm(v: Any) -> Any:
    """A value as the journal will give it back (tuples become lists)."""
    return json.loads(json.dumps(v))


class Journal:
    """The saved values of one radio, on disk until they are put back.

    One small JSON file. Written whole each time (to a temporary file,
    flushed, then renamed over the old one), so a crash mid-write leaves the
    old file or the new one and never half of either.
    """

    def __init__(self, path: Path | str, radio: str = ""):
        self.path = Path(path)
        self.radio = radio

    def load(self) -> list[tuple[str, Any]]:
        """[(key, saved raw value)] in the order they were saved; empty if
        there is no file or it cannot be read (logged)."""
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("the saved radio settings in %s could not be read", self.path, exc_info=True)
            return []
        return [(str(k), v) for k, v in got.get("saved", []) if isinstance(k, str)]

    def load_wrote(self) -> dict[str, Any]:
        """{key: raw value the set-up wrote} (rule 6); {} for a journal
        written before it was kept, or none."""
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        wrote = got.get("wrote") if isinstance(got, dict) else None
        return {str(k): v for k, v in wrote.items()} if isinstance(wrote, dict) else {}

    def save(self, saved: list[tuple[str, Any]], wrote: dict[str, Any] | None = None) -> None:
        if not saved:
            self.clear()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        body = {"radio": self.radio, "pid": os.getpid(), "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "saved": [[k, v] for k, v in saved],
                "wrote": {k: v for k, v in (wrote or {}).items() if any(k == key for key, _ in saved)}}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


@dataclass
class DataSetup:
    """The set-up for one radio: `recover()`, `apply()`, later `restore()`.

    `journal` None keeps the saved values in memory only, which is crash-safe
    for nothing; a caller that has somewhere to write should give it one.
    """

    settings: list[Setting]
    journal: Journal | None = None
    radio: str = ""
    #: (key, raw value as found) for every setting this set-up changed and
    #: can put back, in the order they were changed.
    saved: list[tuple[str, Any]] = field(default_factory=list)
    report: list[Line] = field(default_factory=list)
    restored: list[Line] = field(default_factory=list)
    #: {key: raw value} this set-up last wrote and the radio confirmed, for
    #: every saved setting it wrote (rule 6). A write the radio did not
    #: confirm is not in it: that setting is put back regardless.
    wrote: dict[str, Any] = field(default_factory=dict)
    def _by_key(self) -> dict[str, Setting]:
        return {s.key: s for s in self.settings}

    def _save(self) -> None:
        if self.journal is not None:
            self.journal.save(self.saved, self.wrote)

    def _operator_changed(self, s: Setting | None, wrote: dict[str, Any]) -> Any:
        """Rule 6: the value now on the radio when the operator has changed
        a setting this set-up wrote, else None (put it back as usual). A
        setting that cannot be read, or was never confirmed written, is
        never judged changed."""
        if s is None or s.app_moves or s.read is None or s.key not in wrote:
            return None
        now = self._read(s)
        if now is None or now == UNAVAILABLE or _norm(now) == _norm(wrote[s.key]):
            return None
        return now

    def _read(self, s: Setting) -> Any:
        try:
            return s.read()
        except Exception:                           # noqa: BLE001 - a setting that fails is reported, not fatal
            log.exception("reading %s", s.key)
            return None

    def _write_confirmed(self, s: Setting, raw: Any) -> bool:
        """Write and read back; True only if the radio now says `raw`."""
        try:
            if not s.write(raw):
                return False
        except Exception:                           # noqa: BLE001
            log.exception("writing %s", s.key)
            return False
        if s.read is None:
            return True
        return _norm(self._read(s)) == _norm(raw)

    def _show(self, s: Setting, raw: Any) -> str | None:
        if raw is None:
            return None
        try:
            return s.show(raw)
        except Exception:                           # noqa: BLE001
            return str(raw)

    # -- the three steps ------------------------------------------------------

    def recover(self) -> list[Line]:
        """Put back what a session that died left changed, from the journal.
        Returns a line per setting it tried; the journal keeps the ones that
        could not be put back, for next time."""
        if self.journal is None:
            return []
        left = self.journal.load()
        if not left:
            return []
        by_key = self._by_key()
        wrote = self.journal.load_wrote()
        lines, still = [], []
        for key, raw in reversed(left):
            s = by_key.get(key)
            if s is None or s.write is None:
                # A setting this radio's list no longer has (another
                # transport's MOD level, say): kept, never guessed at.
                still.insert(0, (key, raw))
                continue
            now = self._operator_changed(s, wrote)
            if now is not None:
                lines.append(Line(key, "kept", before=self._show(s, now)))
                continue
            ok = self._write_confirmed(s, raw)
            lines.append(Line(key, "recovered" if ok else "restore-failed", after=self._show(s, raw)))
            if not ok:
                still.insert(0, (key, raw))
        if still:
            self.journal.save(still, wrote)
        else:
            self.journal.clear()
        for line in lines:
            log.warning("%s: %s", self.radio or "the radio", line.text())
        return lines

    def apply(self) -> list[Line]:
        """Read, save and set every setting in order. Returns the report."""
        self.report = []
        for s in self.settings:
            self.report.append(self._apply_one(s))
        for line in self.report:
            log.info("%s set-up: %s", self.radio or "radio", line.text())
        return self.report

    def _apply_one(self, s: Setting) -> Line:
        if s.read is None and s.write is None:
            return Line(s.key, "manual", note=s.note)
        if s.read is None:
            want = s.wanted(None)
            try:
                ok = bool(s.write(want))
            except Exception:                       # noqa: BLE001
                log.exception("writing %s", s.key)
                ok = False
            return Line(s.key, "set-unrestorable" if ok else "failed", after=self._show(s, want))
        before = self._read(s)
        if before is None:
            return Line(s.key, "unread")
        if before == UNAVAILABLE:
            return Line(s.key, "unavailable")
        note = (s.warn(before) if s.warn is not None else None) or ""
        shown = self._show(s, before)
        want = s.wanted(before)
        if want is None or s.write is None:
            return Line(s.key, "report", before=shown, note=note)
        if _norm(before) == _norm(want):
            return Line(s.key, "already", before=shown, note=note)
        # On disk before the radio is touched: rule 2 in the docstring.
        self.saved.append((s.key, _norm(before)))
        self._save()
        if self._write_confirmed(s, want):
            self.wrote[s.key] = _norm(want)
            self._save()
            return Line(s.key, "set", before=shown, after=self._show(s, want), note=note)
        # Not confirmed. It may have half-happened, so the saved value stays
        # and restore() puts it back regardless.
        return Line(s.key, "failed", before=shown, after=self._show(s, want), note=note)

    def restore(self) -> list[Line]:
        """Put back everything apply() changed, last first. Settings that
        could not be put back stay in the journal for `recover()`."""
        by_key = self._by_key()
        lines, still = [], []
        for key, raw in reversed(self.saved):
            s = by_key.get(key)
            now = self._operator_changed(s, self.wrote)
            if now is not None:
                lines.append(Line(key, "kept", before=self._show(s, now)))
                continue
            ok = s is not None and self._write_confirmed(s, raw)
            lines.append(Line(key, "restored" if ok else "restore-failed",
                              after=self._show(s, raw) if s is not None else str(raw)))
            if not ok:
                still.insert(0, (key, raw))
        self.saved = still
        self.wrote = {k: v for k, v in self.wrote.items() if any(k == key for key, _ in still)}
        self._save()
        self.restored = lines
        for line in lines:
            (log.error if line.status == "restore-failed" else log.info)("%s put back: %s",
                                                                          self.radio or "radio", line.text())
        return lines

    def adjust(self, s: Setting, raw: Any) -> Line:
        """A setting the operator changes while the link runs (Packet
        Radio's control strip, 0.3.0): the same rules as apply(). The first
        change saves the value as found - to the journal, before writing -
        so restore() and recover() put it back; every write is read back.
        `s` replaces any read-only setting of the same key (the squelch is
        reported by the set-up and turned by the strip)."""
        self.settings = [x for x in self.settings if x.key != s.key] + [s]
        if s.key not in {k for k, _ in self.saved}:
            before = self._read(s)
            if before is None or before == UNAVAILABLE:
                return Line(s.key, "unread" if before is None else "unavailable", after=self._show(s, raw))
            self.saved.append((s.key, before))
            self._save()
        before = next(v for k, v in self.saved if k == s.key)
        ok = self._write_confirmed(s, raw)
        if ok:
            self.wrote[s.key] = _norm(raw)
        else:
            self.wrote.pop(s.key, None)             # not known to be ours: put back regardless
        self._save()
        line = Line(s.key, "set" if ok else "failed", before=self._show(s, before), after=self._show(s, raw))
        (log.info if ok else log.warning)("%s: %s", self.radio or "radio", line.text())
        return line

    @property
    def changed(self) -> list[Line]:
        """The report's lines that changed something."""
        return [line for line in self.report if line.status in ("set", "set-unrestorable", "failed")]


def journal_name(maker: str, model: str, where: str) -> str:
    """A file name for one radio's journal: its model and where it is (an
    address or a port), as letters, digits and dashes."""
    raw = f"{maker}-{model}-{where}".lower()
    safe = "".join(c if c.isalnum() else "-" for c in raw).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return f"{safe or 'radio'}.json"
