"""Hybrid logical clocks (SYNC.md §3; Kulkarni et al., 2014).

An HLC string orders by plain comparison: wall time, then count, then
node. It is what makes last-edit-wins safe without a central clock: after
a store has seen an edit, everything it does next sorts after it, whatever
its own clock says (the receive rule), and two edits never share a stamp
(the node differs, or the count rises).

    0001928a3c2f1e40-0003-st_K4M2Q7Z9XW3A
    wall ms, 16 hex   count  store id (node)

State is held by the caller (the store keeps it in its meta table, inside
the same transaction as the edit it stamps, so two processes writing one
store never mint the same stamp): these are pure functions of
(state, clock) -> (state, stamp).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

# The clock floor: no stamp is ever earlier than the library's build date,
# so a Raspberry Pi with no RTC that boots believing it is 1970 (§3.3, T9)
# still stamps after every edit made before this library existed. Raise it
# with each release.
CLOCK_FLOOR_MS = int(datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp() * 1000)

# The controller refuses edits more than this far ahead of its own clock
# (§3.3): a stamp days in the future would beat every real edit for days.
MAX_AHEAD_MS = 10 * 60 * 1000

class HlcError(ValueError):
    """A string that is not a hybrid logical clock: a machine fault, never
    shown to a person, so not a translated sentence."""


_RE = re.compile(r"^([0-9a-f]{16})-([0-9a-f]{4})-([A-Za-z0-9_]+)$")


def fmt(wall: int, count: int, node: str) -> str:
    return f"{wall:016x}-{count:04x}-{node}"


def parse(s: str) -> tuple[int, int, str]:
    m = _RE.match(s or "")
    if not m:
        raise HlcError(f"{s!r} is not a hybrid logical clock")
    return int(m.group(1), 16), int(m.group(2), 16), m.group(3)


def wall_of(s: str | None) -> int:
    return parse(s)[0] if s else 0


def system_ms() -> int:
    return int(time.time() * 1000)


def corrected(now_ms: int, offset_ms: int) -> int:
    """`now()` of §3.2: the system clock plus the measured offset, never
    below the clock floor."""
    return max(now_ms + offset_ms, CLOCK_FLOOR_MS)


def local(wall: int, count: int, node: str, now: int) -> tuple[int, int, str]:
    """A local edit: (new wall, new count, stamp)."""
    w = max(wall, now)
    c = count + 1 if w == wall else 0
    if c > 0xFFFF:
        # 65536 edits within one millisecond of one store: move the wall on
        # rather than wrap the count, which would break the ordering.
        w, c = w + 1, 0
    return w, c, fmt(w, c, node)


def receive(wall: int, count: int, remote: str, now: int) -> tuple[int, int]:
    """Seeing a remote stamp: everything this store does next is later."""
    rw, rc, _ = parse(remote)
    w = max(wall, rw, now)
    if w == wall and w == rw:
        c = max(count, rc) + 1
    elif w == wall:
        c = count + 1
    elif w == rw:
        c = rc + 1
    else:
        c = 0
    if c > 0xFFFF:
        w, c = w + 1, 0
    return w, c


def iso_ms(ms: int) -> str:
    """ISO 8601 UTC with milliseconds and Z (SERVICES.md §1)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def parse_iso_ms(s: str) -> int:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))
