"""The CW decoder against real HF: score it on recorded IQ with known text.

    python3 -m shack_cw.corpus MANIFEST.json [--dir ~/build/cw-corpus] [--show] [--width 500]

The synthetic bench (shack_cw.bench) measures what it renders: white noise,
one signal, keying it chose. Brian's 20 m report of 13/10/2026 - a weak
signal in the passband, and the decoder printing "27.4 WPM .2 NE TU A H OW
TEE I* NE H E M EE 7 UN PAU" - was none of those things, so this scores
the decoder on recordings of the bands themselves (tools/cw_corpus.py
records them from public KiwiSDRs; the manifest says what is in each).

Each recording is IQ with a list of targets: an audio offset from the IQ's
centre, and what is known there. A target is shifted onto the pitch, put
through a receiver (the bench's: NB's CW filter and medium AGC, `--width`
wide) in 75 ms blocks, and decoded. What is known decides the score:

* `text`: character error rate against it (bench.cer).
* `slots`: an NCDXF/IARU beacon frequency. The 18 beacons take 10 s turns
  in a 3-minute cycle (ncdxf.org/beacon); the manifest lists, slot by slot,
  the call heard (scored as text, the events of that slot only) or `null`
  for a slot with no beacon audible (scored as noise). A slot left out of
  the list (a beacon at the edge of hearing) is not scored either way.
* `noise: true`: nothing is there; the score is characters printed a
  minute, which should be 0.
* `call`, `wpm`: a station the Reverse Beacon Network spotted there at the
  time: whether the call appears in the decode, and the speed error.

Also, for every target with a signal: the speed the decoder settled on
(the median of its events' wpm) and the lock time - from the signal's
first keying (`onset`, s into the recording, or the slot's start) to the
first character printed.

A developer's measuring tool, like the bench: its output is English for
whoever runs it, and is not translated.
"""
from __future__ import annotations

import argparse
import json
import os
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from . import bench
from .decoder import Decoder

PITCH = bench.PITCH
SLOT_S = 10.0
CYCLE_S = 180.0
# The NCDXF/IARU beacons in transmission order: each sends its call at
# 22 WPM and four 1 s dashes (100 W, 10 W, 1 W, 100 mW) in its 10 s slot,
# on 14100 kHz at the slot that starts each 3-minute cycle and one slot
# later on each band up (18110, 21150, 24930, 28200).
NCDXF = ["4U1UN", "VE8AT", "W6WX", "KH6RS", "ZL6B", "VK6RBP", "JA2IGY", "RR9O", "VR2B",
         "4S7B", "ZS6DN", "5Z4B", "4X6TU", "OH2B", "CS3B", "LU4AA", "OA4B", "YV5B"]
NCDXF_KHZ = [14100.0, 18110.0, 21150.0, 24930.0, 28200.0]
NCDXF_WPM = 22.0


def ncdxf_call(khz: float, utc_s: float) -> str:
    """The beacon whose turn it is on `khz` at `utc_s` (seconds of UTC)."""
    band = NCDXF_KHZ.index(float(khz))
    slot = int((utc_s % CYCLE_S) // SLOT_S)
    return NCDXF[(slot - band) % len(NCDXF)]


def load_iq(path: Path) -> tuple[np.ndarray, float]:
    """A recording's IQ as complex64, and its rate. KiwiSDR writes 16-bit
    stereo WAV, I left; its header's rate is the nominal one (11999 or
    12000 Hz), close enough that the keying's timing is unaffected."""
    if path.suffix == ".npz":
        # A test fixture (tools/cw_corpus.py fixture): int16 I/Q pairs cut
        # to +-750 Hz round the target, brought back to 12 kHz here by
        # zero-padding its spectrum, so the receiver sees its usual rate.
        with np.load(path) as f:
            q, rate = f["iq"].astype(np.float32) / 32768.0, float(f["rate"])
        y = q[:, 0] + 1j * q[:, 1]
        n, m = len(y), int(round(len(y) * bench.FS / rate))
        Y = np.fft.fft(y)
        Z = np.zeros(m, np.complex128)
        Z[:n // 2], Z[-(n - n // 2):] = Y[:n // 2], Y[n // 2:]
        return (np.fft.ifft(Z) * (m / n)).astype(np.complex64), float(bench.FS)
    with wave.open(str(path)) as w:
        rate = float(w.getframerate())
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    x = x.reshape(-1, 2) / 32768.0
    return (x[:, 0] + 1j * x[:, 1]).astype(np.complex64), rate


def _envelope(z: np.ndarray, rate: float, hz: float, win_s: float = 0.02) -> np.ndarray:
    """Power at `hz` from the IQ's centre through a `win_s` boxcar (ENBW
    1/win_s), one value every 5 ms."""
    n = np.arange(len(z))
    x = z * np.exp(-2j * np.pi * hz * n / rate)
    hop = int(round(0.005 * rate))
    w = int(round(win_s * rate))
    c = np.concatenate([[0], np.cumsum(x)])
    idx = np.arange(w, len(x), hop)
    return np.abs((c[idx] - c[idx - w]) / w) ** 2


def find_carrier(z: np.ndarray, rate: float, near: float = 0.0, span: float = 100.0) -> float:
    """The strongest line within `span` Hz of `near`, from the whole
    recording's averaged spectrum (0.7 Hz bins): a Kiwi's own frequency
    error, or a station a little off its spot."""
    nfft = 1 << 14
    segs = len(z) // nfft
    P = np.zeros(nfft)
    win = np.hanning(nfft)
    for k in range(segs):
        P += np.abs(np.fft.fft(z[k * nfft:(k + 1) * nfft] * win)) ** 2
    f = np.fft.fftfreq(nfft, 1 / rate)
    m = np.abs(f - near) <= span
    return float(f[m][np.argmax(P[m])])


def find_signals(z: np.ndarray, rate: float, span: float = 4500.0, min_db: float = 3.0) -> list[dict]:
    """The keyed lines in a recording, for annotating it by hand against
    the Reverse Beacon Network's spots: [{offset, snr (dB in 2.5 kHz, at
    the keying's peaks), keyed (fraction of the time it is up), onset (s,
    its first keying)}], strongest first, at least 60 Hz apart."""
    nfft = 1 << 13
    segs = len(z) // nfft
    win = np.hanning(nfft)
    P = np.array([np.abs(np.fft.fft(z[k * nfft:(k + 1) * nfft] * win)) ** 2 for k in range(segs)])
    f = np.fft.fftfreq(nfft, 1 / rate)
    hi = np.percentile(P, 90, axis=0)
    floor = np.median(hi[np.abs(f) <= span])
    order = np.argsort(hi)[::-1]
    out: list[dict] = []
    for i in order:
        if abs(f[i]) > span or hi[i] < floor * 10 ** (min_db / 10) * 4:
            continue
        # A strong station's keying sidebands (clicks) are lines too: 10 dB
        # down and within 300 Hz of it, a line is taken for its sideband.
        if any(abs(f[i] - o["offset"]) < 60 or (abs(f[i] - o["offset"]) < 300 and hi[i] * 10 < o["_p"])
               for o in out):
            continue
        e = _envelope(z, rate, float(f[i]))
        # The noise is the line's own key-up: Morse is up about half the
        # time, so its quietest fifth is the gaps.
        pk, nz = float(np.percentile(e, 95)), float(np.percentile(e, 20))
        up = float(np.mean(e > 0.25 * pk))
        # Its first keying: the first 15 ms over a quarter of its peaks
        # (where lock time is measured from - a station that starts a
        # minute in is not "locked" by noise printed before it).
        hot = np.convolve((e > 0.25 * pk).astype(float), np.ones(3), "valid") >= 3
        onset = float(np.argmax(hot)) * 0.005 if hot.any() else 0.0
        out.append({"offset": round(float(f[i]), 1),
                    "snr": round(float(10 * np.log10(max(pk - nz, 1e-12) / nz * (50 / 2500))), 1),
                    "keyed": round(up, 2), "onset": round(onset, 2), "_p": float(hi[i])})
        if len(out) >= 25:
            break
    return [{k: v for k, v in o.items() if k != "_p"} for o in out]


def beacon_slots(z: np.ndarray, rate: float, khz: float, utc_s: float, *,
                 present_db: float = -3.0, absent_db: float = -15.0) -> dict:
    """An NCDXF target for a recording that starts at `utc_s`: the slot
    grid (t0: where slot 0 starts, see below), each slot's call if it is
    heard (SNR in 2.5 kHz at least `present_db`), None if nothing is (at
    most `absent_db`), "-" between, and each slot's SNR."""
    f0 = find_carrier(z, rate)
    p = _envelope(z, rate, f0)
    pn = _envelope(z, rate, f0 + 300.0)     # the same filter's noise beside it
    hop = 0.005
    nominal = (-utc_s) % SLOT_S             # where a slot starts by the clock
    pc = np.concatenate([[0.0], np.cumsum(p)])

    def mean(a: float, b: float) -> float:
        i, j = int(max(0.0, a) / hop), min(int(max(0.0, b) / hop), len(p))
        return (pc[j] - pc[i]) / (j - i) if j > i else 0.0

    # Where the slots really start: the recording's first sample comes a
    # second or three after it was asked for, so the clock's grid is late
    # by that much. Each beacon keys for about 7 s from the start of its
    # slot and is silent for the last 2, so the grid is slid (-4 to +2 s)
    # to where the keying most stands over the second before it, summed
    # over every slot - the strong ones decide it - with the first second
    # counted twice, so the grid starts on the first letter, not anywhere
    # before it that still holds the whole call. (The first try, the
    # strong slots' first crossing of half their peak, put the grid a
    # second or two out, and letters were scored in the next beacon's
    # slot: "ETU OH" for 4X6TU.)
    nslots = int((len(z) / rate - nominal) // SLOT_S) + 1
    best, t0 = -np.inf, nominal
    for shift in np.arange(-4.0, 2.0001, 0.05):
        t = nominal + shift
        sc = sum(mean(t + k * SLOT_S, t + k * SLOT_S + 6.5) + mean(t + k * SLOT_S, t + k * SLOT_S + 1.0)
                 - 2 * mean(t + k * SLOT_S - 1.5, t + k * SLOT_S)
                 for k in range(nslots) if t + k * SLOT_S - 1.5 >= 0)
        if sc > best:
            best, t0 = sc, float(t)
    # Slot k starts at t0 + 10k in the file and at the clock's nominal +
    # 10k: the grid moved, not which beacon's turn it was. Slots not wholly
    # in the recording are left out.
    snr: dict[int, float] = {}
    slots: list = []
    for k in range(nslots):
        a, b = t0 + k * SLOT_S, t0 + k * SLOT_S + 8.0
        if a < 0 or b * rate > len(z):
            slots.append("-")
            continue
        seg, nseg = p[int(a / hop):int(b / hop)], pn[int(a / hop):int(b / hop)]
        # The keying's peaks over the noise's own peaks in the same filter
        # (the 95th percentile of noise alone is 3x its median, so peaks
        # over the median read -12 dB with nothing there).
        noise = float(np.median(nseg))
        s = max(float(np.percentile(seg, 95)) - float(np.percentile(nseg, 95)), 1e-12)
        db = round(float(10 * np.log10(s / noise * (50.0 / 2500.0))), 1)   # 20 ms boxcar: 50 Hz
        snr[k] = db
        call = ncdxf_call(khz, utc_s + nominal + k * SLOT_S + 1.0)
        slots.append(call if db >= present_db else None if db <= absent_db else "-")
    return {"name": "ncdxf", "offset": f0, "t0": round(t0, 2), "slots": slots,
            "snr": {str(k): v for k, v in snr.items()}}


def decode_timed(z: np.ndarray, rate: float, offset_hz: float, *, pitch: float = PITCH,
                 width: float = 500.0, decoder: Callable | None = None,
                 receiver: Callable | None = None, opts: dict | None = None) -> tuple[list[tuple[float, dict]], float]:
    """[(seconds, event)] for the signal `offset_hz` from the IQ's centre,
    heard on the pitch; and the decoder's CPU seconds. The time is the end
    of the block the event came out of.

    `receiver(rate, pitch, width)` is an app's own CW receiver (as
    openhamshack-nb's tools/cw_bench.py --corpus passes NB's Demod), made with
    the filter width this target wants; by default the bench's."""
    import time as _t
    n = np.arange(len(z))
    zz = (z * np.exp(2j * np.pi * (pitch - offset_hz) * n / rate)).astype(np.complex64)
    rx = (receiver or bench.CwReceiver)(rate, pitch, width)
    dec = (decoder or Decoder)(None, freq=pitch, **(opts or {}))
    block = int(round(bench.BLOCK_S * rate))
    out: list[tuple[float, dict]] = []
    cpu = 0.0
    for i in range(0, len(zz), block):
        a = rx(zz[i:i + block])
        c0 = _t.process_time()
        ev = dec.feed(a, rate)
        cpu += _t.process_time() - c0
        t = min(i + block, len(zz)) / rate
        out.extend((t, e) for e in ev)
    return out, cpu


def _text(events) -> str:
    return "".join(e["text"] for _, e in events)


def _wpm(events) -> float | None:
    w = [e["wpm"] for _, e in events if e.get("wpm")]
    return float(np.median(w)) if w else None


def _first(events, after: float) -> float | None:
    for t, e in events:
        if t >= after and e["text"].strip():
            return t
    return None


def score_target(z, rate, rec: dict, tgt: dict, **kw) -> list[dict]:
    """One row per thing scored: {kind, band, id, cer|cpm|hit, wpm, lock, ...}."""
    if "width" in tgt:
        kw = dict(kw, width=float(tgt["width"]))   # this target's own filter (Brian's wide one)
    events, cpu = decode_timed(z, rate, float(tgt.get("offset", 0.0)), **kw)
    secs = len(z) / rate
    base = {"id": rec["id"], "band": rec["band"], "target": tgt.get("name", tgt.get("offset", 0.0)),
            "category": tgt.get("category", rec.get("kind")), "secs": secs, "cpu": cpu,
            "decoded": _text(events)}
    rows = []
    if tgt.get("noise"):
        n = len(base["decoded"].replace(" ", ""))
        rows.append(dict(base, kind="noise", cpm=n * 60.0 / secs, wpm_shown=_wpm(events)))
        return rows
    if "slots" in tgt:
        # Events belong to the slot their letter was keyed in: a letter
        # comes out when the gap after it is long enough, up to ~1.5 s
        # after the slot's last keying, which is still inside the slot.
        t0 = float(tgt.get("t0", 0.0))    # seconds into the recording of slot 0's start
        for k, call in enumerate(tgt["slots"]):
            if call == "-":
                continue   # not scored
            a, b = t0 + k * SLOT_S, t0 + (k + 1) * SLOT_S
            ev = [(t, e) for t, e in events if a + 0.5 <= t < b + 0.5]
            txt = _text(ev)
            if call is None:
                rows.append(dict(base, kind="noise", category="beacon-gap", slot=k, decoded=txt,
                                 cpm=len(txt.replace(" ", "")) * 60.0 / SLOT_S, wpm_shown=_wpm(ev)))
            else:
                f = _first(ev, a)
                rows.append(dict(base, kind="text", slot=k, expect=call, decoded=txt,
                                 cer=bench.cer(txt, call), wpm=_wpm(ev), wpm_true=NCDXF_WPM,
                                 lock=None if f is None else f - a,
                                 category=tgt.get("category", "beacon"), snr=(tgt.get("snr") or {}).get(str(k))))
        return rows
    onset = float(tgt.get("onset", 0.0))
    f = _first(events, onset)
    row = dict(base, wpm=_wpm(events), wpm_true=tgt.get("wpm"), lock=None if f is None else f - onset)
    if "text" in tgt:
        row.update(kind="text", expect=tgt["text"], cer=bench.cer(row["decoded"], tgt["text"]))
    elif "call" in tgt:
        row.update(kind="call", expect=tgt["call"], hit=tgt["call"] in row["decoded"].replace(" ", ""))
    rows.append(row)
    return rows


def score(manifest: list[dict], base: Path, **kw) -> list[dict]:
    rows = []
    for rec in manifest:
        path = base / rec["file"]
        # A wideband recording (the skimmer's, shack_cw.skim.bench) has no
        # single-signal targets: nothing to load here.
        if not rec.get("targets") or not path.is_file():
            continue
        z, rate = load_iq(path)
        if "start" in rec or "seconds_used" in rec:
            a = int(float(rec.get("start", 0)) * rate)
            z = z[a:a + int(float(rec.get("seconds_used", len(z) / rate)) * rate)]
        for tgt in rec.get("targets", []):
            rows.extend(score_target(z, rate, rec, tgt, **kw))
    return rows


def summary(rows: list[dict]) -> dict:
    """Totals by band and by category: CER (characters wrong over sent,
    pooled), characters a minute on noise, calls found, median |WPM
    error| and median lock time."""
    out: dict = {}
    for key in ("band", "category"):
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r[key], []).append(r)
        table = {}
        for g, rs in sorted(groups.items()):
            text = [r for r in rs if r["kind"] == "text"]
            noise = [r for r in rs if r["kind"] == "noise"]
            calls = [r for r in rs if r["kind"] == "call"]
            werr = [abs(r["wpm"] - r["wpm_true"]) for r in rs if r.get("wpm") and r.get("wpm_true")]
            locks = [r["lock"] for r in rs if r.get("lock") is not None and r["kind"] != "noise"]
            sent = sum(len(bench._norm(r["expect"])) for r in text)
            table[g] = {
                "cer": (sum(r["cer"] * len(bench._norm(r["expect"])) for r in text) / sent) if sent else None,
                "n_text": len(text),
                "cpm": (sum(r["cpm"] * (SLOT_S if r.get("slot") is not None else r["secs"]) for r in noise)
                        / sum(SLOT_S if r.get("slot") is not None else r["secs"] for r in noise)) if noise else None,
                "noise_min": sum(SLOT_S if r.get("slot") is not None else r["secs"] for r in noise) / 60.0,
                "wpm_shown_on_noise": sum(1 for r in noise if r.get("wpm_shown")),
                "calls": f"{sum(r['hit'] for r in calls)}/{len(calls)}" if calls else None,
                "wpm_err": float(np.median(werr)) if werr else None,
                "lock": float(np.median(locks)) if locks else None,
                "lock_found": f"{len(locks)}/{len([r for r in rs if r['kind'] != 'noise'])}",
            }
        out[key] = table
    return out


def print_summary(s: dict) -> None:
    for key, table in s.items():
        print(f"\nby {key}:")
        print(f"  {'':18s} {'CER %':>7s} {'n':>4s} {'chr/min':>8s} {'noise min':>9s} {'WPM shown':>9s}"
              f" {'calls':>7s} {'|dWPM|':>7s} {'lock s':>7s} {'locked':>7s}")
        for g, v in table.items():
            f = lambda x, fmt: (fmt % x) if x is not None else "-"
            print(f"  {g:18s} {f(None if v['cer'] is None else 100 * v['cer'], '%7.1f')} {v['n_text']:4d}"
                  f" {f(v['cpm'], '%8.1f')} {v['noise_min']:9.1f} {v['wpm_shown_on_noise']:9d}"
                  f" {f(v['calls'], '%7s')} {f(v['wpm_err'], '%7.1f')} {f(v['lock'], '%7.1f')} {v['lock_found']:>7s}")


def main(argv=None, receiver: Callable | None = None) -> int:
    """The command line. `receiver(rate, pitch, width)` replaces the
    bench's receiver (see decode_timed)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("manifest")
    ap.add_argument("--dir", default=str(Path.home() / "build" / "cw-corpus"),
                    help="where the manifest's files are (default ~/build/cw-corpus)")
    ap.add_argument("--width", type=float, default=500.0, help="the receiver's CW filter (Hz)")
    ap.add_argument("--show", action="store_true", help="print every target's decode")
    ap.add_argument("--json", help="write the rows and the summary here")
    ap.add_argument("--decoder", help="a .py file whose Decoder class to measure instead")
    ap.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE",
                    help="a Decoder argument, e.g. polarity=0 (as Radio makes it) or snr_db=8")
    a = ap.parse_args(argv)
    decoder = None
    if a.decoder:
        import importlib.util
        spec = importlib.util.spec_from_file_location("cw_under_test", a.decoder)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        decoder = mod.Decoder
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    opts = {k: float(v) for k, v in (o.split("=", 1) for o in a.opt)}
    rows = score(manifest, Path(os.path.expanduser(a.dir)), width=a.width, decoder=decoder, receiver=receiver,
                 opts=opts)
    if a.show:
        for r in rows:
            what = (f"CER {100 * r['cer']:.0f}% want {r['expect']!r}" if r["kind"] == "text" else
                    f"{r['cpm']:.1f}/min" if r["kind"] == "noise" else f"call {'yes' if r['hit'] else 'no'} {r['expect']}")
            print(f"{r['id']:24s} {str(r['target']):>8s} {str(r.get('slot', '')):>3s} {what:28s}"
                  f" wpm {r.get('wpm') or r.get('wpm_shown') or '-'} {r['decoded'][:70]!r}")
    s = summary(rows)
    print_summary(s)
    # A beacon target's slots are rows of one decode: count each decode once.
    runs = {(r["id"], str(r["target"])): (r["cpu"], r["secs"]) for r in rows}
    cpu, secs = sum(c for c, _ in runs.values()), sum(s for _, s in runs.values())
    print(f"\ndecoder CPU: {1000 * cpu / max(1e-9, secs):.2f} ms per second of audio")
    if a.json:
        Path(a.json).write_text(json.dumps({"rows": rows, "summary": s}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
