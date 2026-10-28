"""Latest wins (CLIENT-UI-PLAN.md §2.4), with no socket in it.

The server keeps at most one pending row per client per stream. A row
offered while the client's socket has not drained replaces the pending
one instead of queueing behind it, and the next row that goes out carries
gap. Queueing is what Icom's ack window and NB's 8-deep queue did: a slow
client (a Fire TV on Wi-Fi, a tunnel) then watches the past, ever further
behind. Dropping keeps it current, and the gap flag lets it draw the
break honestly.

The transport is injected: send(client, payload) puts bytes on the
client's socket, and the transport calls drained(client) once that write
has gone. So this is the same logic under aiohttp, websockets or a test.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import replace

from .row import Row, encode


class LatestWins:
    def __init__(self, send: Callable[[Hashable, bytes], None]):
        self._send = send
        self._busy: set = set()
        self._pending: dict[Hashable, dict[int, Row]] = {}
        self._gap: set = set()
        self._seq: dict[tuple, int] = {}
        self.dropped = 0

    def offer(self, client: Hashable, row: Row) -> None:
        """A new row for this client. The sequence is stamped here, per
        client and stream, so a dropped row also shows as a jump in seq."""
        key = (client, row.stream)
        seq = self._seq.get(key, 0)
        self._seq[key] = (seq + 1) & 0xFFFFFFFF
        row = replace(row, seq=seq)
        if client not in self._busy:
            self._write(client, row)
            return
        pending = self._pending.setdefault(client, {})
        if row.stream in pending:
            self.dropped += 1
            self._gap.add(key)
        pending[row.stream] = row

    def drained(self, client: Hashable) -> None:
        """The client's last write has gone: send what is pending, one row
        per stream, in stream order."""
        self._busy.discard(client)
        pending = self._pending.pop(client, {})
        for stream in sorted(pending):
            self._write(client, pending[stream])

    def forget(self, client: Hashable) -> None:
        """The client went: nothing of it is kept."""
        self._busy.discard(client)
        self._pending.pop(client, None)
        self._gap = {k for k in self._gap if k[0] != client}
        self._seq = {k: v for k, v in self._seq.items() if k[0] != client}

    def _write(self, client, row: Row) -> None:
        key = (client, row.stream)
        if key in self._gap:
            self._gap.discard(key)
            row = replace(row, gap=True)
        self._busy.add(client)
        self._send(client, encode(row))
