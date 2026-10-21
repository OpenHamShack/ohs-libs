"""Recordings: replaying IQ through the monitor, and making the files.

`FileSource` plays a SigMF recording (the .sigmf-meta beside a
.sigmf-data, as QO-100 NB and Radio write them and as GNU Radio, SDR++
and SigDigger can) or a raw file (rtl_sdr's .cu8/.bin, a .cs16, a .cf32)
for which the rate and centre are given. `record()` writes SigMF from any
source, so an evening on a real RTL-SDR becomes a test that runs anywhere.

Reading is our own small reader rather than openhamshack-nb's ohscore.sigmf
(which ohs-libs cannot depend on) or the sigmf package (another
dependency for twenty lines): the datatypes are the ones openhamshack-nb's
DTYPES lists - ci16_le (NB's), cf32_le, ci8, cu8 and the big-endian forms -
one channel only, `core:frequency` of the first capture as the centre.
Later captures that move the centre are not followed: an RTL-SDR left on
2 m does not move, and a recording that does is better split. When
shack_rec grows an IQ reader every app shares, this moves there.

A recording knows its own time (`core:datetime`) and its own losses (a
capture that starts later than the samples before it imply), so frames
replayed are dated as they were heard. `realtime=False` (the default)
plays as fast as the decoder takes it - for tests and for searching an
hour of recording in a minute; `realtime=True` paces to the clock, as the
app shows it.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import numpy as np

from .source import IqSource, SdrError, cu8_to_complex

DATA, META = ".sigmf-data", ".sigmf-meta"

#: SigMF datatype -> (numpy dtype of one component, scale to full scale 1.0, offset)
DTYPES = {
    "ci16_le": ("<i2", 1 / 32768.0, 0.0), "ci16_be": (">i2", 1 / 32768.0, 0.0),
    "cf32_le": ("<f4", 1.0, 0.0), "cf32_be": (">f4", 1.0, 0.0),
    "ci8": ("i1", 1 / 128.0, 0.0), "cu8": ("u1", 1 / 127.5, 127.5),
}
RAW_SUFFIX = {".cu8": "cu8", ".bin": "cu8", ".cs8": "ci8", ".cs16": "ci16_le", ".ci16": "ci16_le",
              ".cf32": "cf32_le", ".cfile": "cf32_le", ".raw": "cu8"}


def _utc(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class FileSource(IqSource):
    kind = "file"
    live = False

    def __init__(self, path, *, rate: float | None = None, centre: float | None = None, datatype: str | None = None,
                 realtime: bool = False, block_s: float = 0.05, loop: bool = False):
        p = Path(path)
        self.meta: dict = {}
        self.captures: list[tuple[int, float | None]] = []      # (sample_start, utc)
        if p.suffix in (DATA, META) or p.with_suffix(META).exists():
            self.data_path, meta_path = p.with_suffix(DATA), p.with_suffix(META)
            try:
                self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
                g = self.meta["global"]
            except (OSError, ValueError, KeyError) as e:
                raise SdrError("bad-file", f"{meta_path}: {e}") from e
            datatype = g.get("core:datatype")
            rate = rate or g.get("core:sample_rate")
            caps = self.meta.get("captures") or [{}]
            centre = centre if centre is not None else caps[0].get("core:frequency")
            self.captures = [(int(c.get("core:sample_start", 0)), _utc(c.get("core:datetime"))) for c in caps]
            if int(g.get("core:num_channels", 1) or 1) != 1:
                raise SdrError("bad-file", f"{meta_path}: more than one channel")
        else:
            self.data_path = p
            datatype = datatype or RAW_SUFFIX.get(p.suffix.lower())
        if datatype not in DTYPES:
            raise SdrError("bad-file", f"{self.data_path.name}: samples of type {datatype} (playable: "
                           f"{', '.join(DTYPES)})")
        if not rate or centre is None:
            raise SdrError("bad-file", f"{self.data_path.name}: the sample rate and centre frequency are needed")
        super().__init__(float(centre), float(rate))
        self.datatype = datatype
        comp, self.scale, self.offset = DTYPES[datatype]
        self.comp = np.dtype(comp)
        self.realtime = realtime
        self.loop = loop
        self.live = False
        self.block = int(round(self.rate * block_s))
        try:
            self.total = self.data_path.stat().st_size // (2 * self.comp.itemsize)
        except OSError as e:
            raise SdrError("bad-file", f"{self.data_path}: {e}") from e

    def _open(self) -> None:
        try:
            self.f = open(self.data_path, "rb")
        except OSError as e:
            raise SdrError("bad-file", f"{self.data_path}: {e}") from e
        self.pos = 0
        self._started = time.monotonic()
        # The recording's own clock: sample 0 at the first capture's time.
        first = self.captures[0][1] if self.captures else None
        if first is not None:
            self._t0 = first

    def _close(self) -> None:
        self.f.close()

    def _gap_before(self, pos: int) -> int:
        """Samples the recording lost before `pos`, if a capture starts
        there later than the samples before it imply."""
        for i, (start, utc) in enumerate(self.captures[1:], 1):
            if start == pos and utc is not None:
                prev_start, prev_utc = self.captures[i - 1]
                if prev_utc is not None:
                    lost = int(round((utc - prev_utc) * self.rate)) - (start - prev_start)
                    return max(0, lost)
        return 0

    def _read(self) -> np.ndarray | None:
        if self.pos >= self.total:
            if not self.loop or not self.total:
                return None
            self.f.seek(0)
            self.pos = 0
        n = min(self.block, self.total - self.pos)
        # Stop at the next capture so its gap is reported at its sample.
        for start, _ in self.captures[1:]:
            if self.pos < start < self.pos + n:
                n = start - self.pos
        self.lose(self._gap_before(self.pos))
        raw = self.f.read(n * 2 * self.comp.itemsize)
        if not raw:
            return None
        n = len(raw) // (2 * self.comp.itemsize)
        self.pos += n
        if self.datatype == "cu8":
            z = cu8_to_complex(raw)
        else:
            a = np.frombuffer(raw, self.comp).astype(np.float32)
            z = (a * np.float32(self.scale)).view(np.complex64)
        if self.realtime:
            ahead = self.pos / self.rate - (time.monotonic() - self._started)
            if ahead > 0:
                time.sleep(ahead)
        return z


def record(source: IqSource, path, seconds: float, *, author: str = "", description: str = "") -> Path:
    """Write `seconds` of `source` (opened here if it is not) as SigMF
    ci16_le: NB's datatype, half the size of cf32, and 16 bits hold an
    8-bit RTL or a 12-bit Pluto exactly. Losses the source reports start a
    new capture with its true time, as the reader expects. Returns the
    .sigmf-data path."""
    base = Path(path)
    if base.suffix in (DATA, META):
        base = base.with_suffix("")
    data = base.with_name(base.name + DATA)
    meta = base.with_name(base.name + META)
    opened_here = not source.opened
    if opened_here:
        source.open()
    captures = []
    written = 0
    want = int(seconds * source.rate)
    try:
        with open(data, "wb") as f:
            while written < want:
                b = source.read()
                if b is None:
                    break
                z = b.samples[: want - written]
                if not captures or b.dropped:
                    stamp = dt.datetime.fromtimestamp(b.time, dt.timezone.utc).isoformat().replace("+00:00", "Z")
                    captures.append({"core:sample_start": written, "core:frequency": source.centre,
                                     "core:datetime": stamp})
                s = np.empty(2 * len(z), "<i2")
                s[0::2] = np.clip(np.rint(z.real * 32767), -32768, 32767)
                s[1::2] = np.clip(np.rint(z.imag * 32767), -32768, 32767)
                f.write(s.tobytes())
                written += len(z)
    finally:
        if opened_here:
            source.close()
    g = {"core:datatype": "ci16_le", "core:sample_rate": source.rate, "core:version": "1.0.0",
         "core:num_channels": 1, "core:recorder": "shack_packet.sdr", "core:hw": source.kind}
    if author:
        g["core:author"] = author
    if description:
        g["core:description"] = description
    meta.write_text(json.dumps({"global": g, "captures": captures, "annotations": []}, indent=2) + "\n",
                    encoding="utf-8")
    return data
