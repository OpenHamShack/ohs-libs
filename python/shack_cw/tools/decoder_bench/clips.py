#!/usr/bin/env python3
"""Cut the decoder bench's clips: one 8 kHz audio channel per known signal.

    python3 tools/decoder_bench/clips.py OUT_DIR [--corpus ~/build/cw-corpus]
            [--flex ID ...] [--rbn-csv 20261012.csv]

The skimmer's per-call recovery (shack_cw.skim.recovery) says most lost
calls are lost to wrong letters. Whether that is our decoder or simply what
any decoder makes of those signals can only be told by giving every
decoder the same audio. So each signal with known content becomes one clip,
the way a skimmer channel or Radio's CW receiver would hear it:

* shifted so the signal sits on a 700 Hz pitch, cut to 8 kHz complex,
  then through the bench's receiver (shack_cw.benchrx.CwReceiver: NB's and
  Radio's 500 Hz CW filter and medium AGC). Every decoder gets this audio.
* the frequency is the strongest keyed line within SEARCH_HZ of what is
  known (a Kiwi target's measured offset; an RBN spot's frequency, which is
  to 100 Hz and averaged over skimmers).

Kinds, with what is known (clips.json beside the audio):

* `call`: a station the RBN spotted while the recording ran (the Kiwi
  corpus targets, every RBN call in ohs-box's 20 m recording - the same 39
  the recovery tool scores - and the RBN calls inside the FlexRadio loop
  surveys of 12/10/2026, against the RBN's archive for that day). Scored:
  is the call in the decoded text.
* `beacon`: an NCDXF/IARU beacon recording; its slots with the call heard
  in each (shack_cw.corpus's ground truth). Scored: character error rate
  per slot, and characters printed in the slots where nothing is heard.
* `noise`: the corpus's quiet targets. Scored: characters a minute.

SNR is measured the same way for every clip (snr_2500): the keying's
peaks (98th percentile of a 20 ms envelope) over the noise beside it, in
2.5 kHz - the corpus's and WSJT-X's convention. RBN reports SNR in 500 Hz,
7 dB higher for the same signal.

Audio is written as float32 .npy (8 kHz): about 19 MB per 10 minutes, so
the clips live outside git (ohs-dev ~/build/cw-decoder-bench/clips, and
office:/srv/iq/derived/cw-decoder-bench).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from shack_cw.benchrx import CwReceiver
from shack_cw.corpus import find_carrier, load_iq
from shack_cw.skim import bench as sbench

FS = 8000
PITCH = 700.0
WIDTH = 500.0
SEARCH_HZ = 120.0       # how far from an RBN spot the keyed line may be
MANIFEST = Path(__file__).resolve().parents[2] / "tests" / "corpus" / "manifest.json"


_SPEC: dict = {}


def to_channel(z: np.ndarray, rate: float, offset: float) -> np.ndarray:
    """IQ at `rate` with the signal `offset` Hz from its centre -> 8 kHz
    complex with the signal at 0 Hz.

    For a wideband recording (ohs-box's 10 minutes at 200 kS/s) the whole
    recording is transformed once and each channel is its +-4 kHz of bins
    transformed back: a brick wall, as the recording itself was cut.
    Mixing and resampling 120 M samples per call took three minutes a
    call; this takes seconds."""
    if rate <= 16000:
        n = np.arange(len(z))
        zz = z * np.exp(-2j * np.pi * offset * n / rate)
        from math import gcd
        r = int(round(rate))
        g = gcd(FS, r)
        return resample_poly(zz.astype(np.complex128), FS // g, r // g).astype(np.complex64)
    key = (id(z), len(z))
    if key not in _SPEC:
        _SPEC.clear()
        _SPEC[key] = np.fft.fft(z)
    Z = _SPEC[key]
    n = len(z)
    m = int(round(n * FS / rate))
    k0 = int(round(offset * n / rate))
    idx = (k0 + np.fft.fftfreq(m, 1.0 / m).astype(np.int64)) % n
    return (np.fft.ifft(Z[idx]) * (m / n)).astype(np.complex64)


def keyed_line(c: np.ndarray, span: float = SEARCH_HZ) -> float:
    """The strongest keyed line within `span` Hz of 0 in 8 kHz complex
    `c`: per 3.9 Hz bin, the 95th percentile of power over 32 ms frames;
    the peak, refined by a parabola."""
    nfft, hop = 2048, 256
    if len(c) < nfft * 4:
        return 0.0
    frames = np.lib.stride_tricks.sliding_window_view(c, nfft)[::hop]
    if len(frames) > 4000:
        frames = frames[np.linspace(0, len(frames) - 1, 4000).astype(int)]
    P = np.abs(np.fft.fft(frames * np.hanning(nfft), axis=1)) ** 2
    f = np.fft.fftfreq(nfft, 1 / FS)
    p = np.percentile(P, 95, axis=0)
    idx = np.where(np.abs(f) <= span)[0]
    i = idx[np.argmax(p[idx])]
    a, b, d = p[i - 1], p[i], p[(i + 1) % nfft]
    den = a - 2 * b + d
    return float(f[i] + (0.5 * (a - d) / den if den else 0.0) * FS / nfft)


def snr_2500(c: np.ndarray) -> float:
    """Key-down SNR in 2.5 kHz of the signal at 0 Hz in 8 kHz complex."""
    hop, w = 40, 160                       # 5 ms hop, 20 ms boxcar (50 Hz)

    def env(hz):
        x = c * np.exp(-2j * np.pi * hz * np.arange(len(c)) / FS)
        cs = np.concatenate([[0], np.cumsum(x)])
        i = np.arange(w, len(x), hop)
        return np.abs((cs[i] - cs[i - w]) / w) ** 2
    s = env(0.0)
    nz = [env(h) for h in (-400.0, -250.0, 250.0, 400.0)]
    floor = min(float(np.median(e)) for e in nz)
    npk = min(float(np.percentile(e, 98)) for e in nz)
    pk = float(np.percentile(s, 98))
    return round(10 * np.log10(max(pk - npk, 1e-12) / floor * (50.0 / 2500.0)), 1)


def receive(c: np.ndarray) -> np.ndarray:
    """The receiver every decoder hears: 75 ms blocks through CwReceiver
    with the signal on the pitch."""
    x = c * np.exp(2j * np.pi * PITCH * np.arange(len(c)) / FS)
    rx = CwReceiver(FS, PITCH, WIDTH)
    blk = int(round(0.075 * FS))
    return np.concatenate([rx(x[i:i + blk]) for i in range(0, len(x), blk)]).astype(np.float32)


class Writer:
    def __init__(self, out: Path):
        self.out = out
        (out / "audio").mkdir(parents=True, exist_ok=True)
        self.clips: list[dict] = []

    def add(self, clip: dict, c: np.ndarray) -> None:
        clip["snr_2500"] = snr_2500(c)
        a = receive(c)
        np.save(self.out / "audio" / f"{clip['id']}.npy", a)
        clip["seconds"] = round(len(a) / FS, 2)
        self.clips.append(clip)
        print(f"{clip['id']:50s} {clip['kind']:6s} {clip.get('call') or ''!s:8s} snr {clip['snr_2500']:6.1f}"
              f" df {clip.get('df', 0):+6.1f}", flush=True)

    def save(self) -> None:
        (self.out / "clips.json").write_text(json.dumps(self.clips, indent=1), encoding="utf-8")


def kiwi(w: Writer, manifest: list[dict], base: Path) -> None:
    for rec in manifest:
        if rec.get("kind") == "wideband":
            continue
        path = base / rec["file"]
        if not path.is_file():
            print("missing", path)
            continue
        z, rate = load_iq(path)
        for k, tg in enumerate(rec.get("targets", [])):
            off = float(tg.get("offset", 0.0))
            cid = f"kiwi-{rec['id']}-{k}"
            if "slots" in tg:
                f0 = find_carrier(z, rate, near=off, span=100.0)
                c = to_channel(z, rate, f0)
                slots = [{"t0": float(tg["t0"]) + 10.0 * i, "t1": float(tg["t0"]) + 10.0 * (i + 1),
                          "expect": call, "snr": (tg.get("snr") or {}).get(str(i))}
                         for i, call in enumerate(tg["slots"]) if call != "-"]
                w.add({"id": cid, "kind": "beacon", "band": rec["band"], "source": "kiwi", "rec": rec["id"],
                       "slots": slots, "df": f0 - off}, c)
            elif tg.get("noise"):
                w.add({"id": cid, "kind": "noise", "band": rec["band"], "source": "kiwi", "rec": rec["id"]},
                      to_channel(z, rate, off))
            elif "call" in tg:
                c = to_channel(z, rate, off)
                df = keyed_line(c, 40.0)
                c = to_channel(z, rate, off + df)
                w.add({"id": cid, "kind": "call", "band": rec["band"], "source": "kiwi", "rec": rec["id"],
                       "call": tg["call"], "wpm": tg.get("wpm"), "df": df}, c)


def rbn_calls(z, rate, centre, t0, rbn: list[dict], lo, hi, w: Writer, band: str, source: str, rec_id: str) -> None:
    """One clip per call the RBN spotted inside [lo, hi] while the
    recording ran (+-30 s, as shack_cw.skim.recovery chooses them)."""
    t1 = t0 + len(z) / rate
    by: dict[str, list] = {}
    for r in rbn:
        if t0 - 30 <= r["t"] <= t1 + 30 and lo <= r["hz"] <= hi:
            by.setdefault(r["call"], []).append(r)
    for call, rs in sorted(by.items()):
        hz = float(np.median([r["hz"] for r in rs])) - centre
        c = to_channel(z, rate, hz)
        df = keyed_line(c)
        c = to_channel(z, rate, hz + df)
        w.add({"id": f"{source}-{rec_id}-{call.replace('/', '_')}", "kind": "call", "band": band, "source": source,
               "rec": rec_id, "call": call, "wpm": int(np.median([r["wpm"] for r in rs])),
               "rbn_snr": max(r["snr"] for r in rs), "rbn_spots": len(rs), "df": df}, c)


def box(w: Writer, manifest: list[dict], base: Path) -> None:
    for rec in manifest:
        if rec.get("kind") != "wideband" or not rec.get("rbn"):
            continue
        z, rate, centre, t0 = sbench.load_sigmf(base / rec["file"])
        rbn = sbench.read_rbn(base / rec["rbn"])
        rbn_calls(z, rate, centre, t0, rbn, rec["lo"], rec["hi"], w, rec["band"], "box", rec["id"])


def read_rbn_csv(path: Path) -> list[dict]:
    """The RBN archive's day file (data.reversebeacon.net/rbn_history)."""
    out = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if r.get("tx_mode") != "CW":
                continue
            t = datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            out.append({"t": t, "hz": float(r["freq"]) * 1000.0, "call": r["dx"], "snr": int(r["db"] or 0),
                        "wpm": int(r["speed"] or 0)})
    return out


def flex(w: Writer, ids: list[str], rbn: list[dict]) -> None:
    for rid in ids:
        d = Path.home() / ".cache" / "iqlib" / rid
        data = next(d.glob("*.sigmf-data"))
        z, rate, centre, t0 = sbench.load_sigmf(data)
        band = rid.split("-")[2]
        rbn_calls(z, rate, centre, t0, rbn, centre - 0.42 * rate, centre + 0.42 * rate, w, band, "flex", rid)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--corpus", default=str(Path.home() / "build" / "cw-corpus"))
    ap.add_argument("--flex", nargs="*", default=[])
    ap.add_argument("--rbn-csv")
    ap.add_argument("--only", choices=["kiwi", "box", "flex"])
    a = ap.parse_args(argv)
    out = Path(os.path.expanduser(a.out))
    w = Writer(out)
    if (out / "clips.json").is_file():
        w.clips = [c for c in json.loads((out / "clips.json").read_text()) if c["source"] != a.only] if a.only else []
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    base = Path(os.path.expanduser(a.corpus))
    if a.only in (None, "kiwi"):
        kiwi(w, manifest, base)
    if a.only in (None, "box"):
        box(w, manifest, base)
    if a.flex and a.only in (None, "flex"):
        flex(w, a.flex, read_rbn_csv(Path(a.rbn_csv)))
    w.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
