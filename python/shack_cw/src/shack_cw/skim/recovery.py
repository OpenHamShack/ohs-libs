"""Per-call recovery: for every call the RBN spotted in a recording, where
the skimmer lost it.

    python3 -m shack_cw.skim.recovery MANIFEST.json [--dir ~/build/cw-corpus]
            [--only ID] [--seconds S] [--json OUT] [--show]

bench.py's corpus score says how many calls were spotted. That was 3 of
18 on the Kiwi corpus and 3 of 39 on ohs-box's 20 m (13/10/2026), and the
number alone did not say which stage to work on. This follows each call
through the stages and names the first one that lost it:

* `spotted`: a valid (or qsy) spot of the call - the goal.
* `unconfirmed` / `busted`: the matcher found it, the hit rules held it.
* `matcher`: the call stands as a word of its own in a channel's text, and
  no spot of any status came of it.
* `glued`: the call is in the text with other letters run onto it
  ("TIQCQDESM5D") - a word space read as a letter gap, or none sent.
* `split`: the call is in the text only once the spaces are taken out
  ("I Z 5FDE") - a letter gap read as a word space.
* `near`: the closest word is one Morse-similar letter from the call
  (calls.similar) - an element or a letter misread.
* `letters`: the channel printed text near it, but not the call.
* `squelch`: a track there, decoded, printing (almost) nothing.
* `channel`: a track there that never had a decoder.
* `absent`: no track within the window: under the front end's birth
  threshold - for the box, mostly calls the RBN's better antennas hear and
  an 80 m dipole on 20 m does not.

The text is everything each track's decoders printed, not the last 200
characters a snapshot keeps, so a call early in a long recording counts.

Kiwi recordings are scored per manifest target (a call within TARGET_HZ
of its offset); a wideband recording against the RBN spots captured with
it (RBN frequencies are to 100 Hz and several skimmers' average, so a
track within RBN_TRACK_HZ). A developer's tool: English output.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import bench
from .calls import similar

TARGET_HZ = bench.TARGET_HZ
RBN_TRACK_HZ = 150.0
FEW_CHARS = 6          # a track printing fewer characters than this has printed nothing
STAGES = ("spotted", "unconfirmed", "busted", "matcher", "glued", "split", "near", "letters", "squelch",
          "channel", "absent")


@dataclass
class TrackLog:
    """What one track did over the whole recording."""
    id: int
    f: list = field(default_factory=list)      # Hz from centre, once a pass
    snr: float = -99.0                          # the best key-on SNR it reached
    text: list = field(default_factory=list)   # every character printed, in order
    decoded: bool = False                       # it had a decoder at some time


class Recorder:
    """Runs a Skimmer and keeps every track's frequencies, SNR and text."""

    def __init__(self, sk):
        self.sk = sk
        self.logs: dict[int, TrackLog] = {}
        run = sk.pool.run

        def logged(X, tracks, t_end):
            out = run(X, tracks, t_end)
            for tid, e in out:
                self._log(tid).text.append(e["text"])
            return out
        sk.pool.run = logged

    def _log(self, tid: int) -> TrackLog:
        lg = self.logs.get(tid)
        if lg is None:
            lg = self.logs[tid] = TrackLog(tid)
        return lg

    def after_block(self) -> None:
        for tr in self.sk.tracker.tracks.values():
            if tr.carrier:
                continue
            lg = self._log(tr.id)
            if not lg.f or abs(lg.f[-1] - tr.f) > 2.0:
                lg.f.append(tr.f)
            lg.snr = max(lg.snr, tr.snr)
            if tr.id in self.sk.pool.channels:
                lg.decoded = True


def run(z, rate, *, lo, hi, centre=0.0, t0=0.0, max_channels=64, country=None, band=None):
    from . import QUIET_FLUSH_S, Skimmer
    import time
    sk = Skimmer(rate, lo, hi, centre, max_channels=max_channels, country=country, band=band)
    rec = Recorder(sk)
    blk = int(round(bench.BLOCK_S * rate))
    c0 = time.process_time()
    for i in range(0, len(z), blk):
        sk.feed(z[i:i + blk], t=t0 + i / rate)
        rec.after_block()
    # The recording stops mid-over; on air the skimmer would judge the last
    # words QUIET_FLUSH_S later. Without this a call in a recording's last
    # seconds counted as a matcher loss.
    t_end = sk._t if sk._t is not None else 0.0
    for tid in list(sk._last_char):
        sk._end_over(tid, t_end + QUIET_FLUSH_S)
    return sk, rec, time.process_time() - c0


def stage(call: str, near_logs: list[TrackLog], spots: list[dict]) -> tuple[str, str]:
    """(the stage that lost `call`, the evidence) - see the module."""
    mine = [s for s in spots if s["call"] == call]
    st = {s["status"] for s in mine}
    if st & {"valid", "qsy"}:
        return "spotted", ""
    if "unconfirmed" in st:
        return "unconfirmed", ""
    if "busted" in st:
        return "busted", next(s.get("bustedOf", "") for s in mine if s["status"] == "busted")
    texts = ["".join(lg.text) for lg in near_logs]
    words = [w for t in texts for w in t.split()]

    def context(w: str) -> str:
        for t in texts:
            i = (" " + t + " ").find(" " + w + " ")
            if i >= 0:
                return t[max(0, i - 30):i + len(w) + 30]
        return w
    if call in words:
        return "matcher", context(call)
    glued = [w for w in words if call in w]
    if glued:
        return "glued", context(glued[0])
    for t in texts:
        w = t.split()
        flat = t.replace(" ", "")
        if call in flat:
            # The words that hold it, as printed.
            i = flat.index(call)
            pos, out = 0, []
            for x in w:
                if pos + len(x) > i and pos < i + len(call):
                    out.append(x)
                pos += len(x)
            return "split", " ".join(out)
    close = [w for w in words if similar(call, w)]
    if close:
        return "near", close[0]
    if any(len(t.replace(" ", "")) >= FEW_CHARS for t in texts):
        return "letters", max(texts, key=len)[-60:]
    if any(lg.decoded for lg in near_logs):
        return "squelch", f"{sum(len(t) for t in texts)} chars"
    if near_logs:
        return "channel", f"snr {max(lg.snr for lg in near_logs):.0f}"
    return "absent", ""


def _near(logs: dict[int, TrackLog], hz: float, within: float) -> list[TrackLog]:
    return [lg for lg in logs.values() if lg.f and min(abs(f - hz) for f in lg.f) <= within]


def score_kiwi(manifest, base: Path, country) -> tuple[list[dict], dict]:
    """(a row per RBN call, the quiet targets' score). A quiet target
    should show no character at all from any track within TARGET_HZ, and
    no spot: the noise-only result of the squelch work (bench.py counts
    only spots there)."""
    from ..corpus import load_iq
    rows = []
    quiet = {"targets": 0, "chars": 0, "with_chars": [], "spots": 0}
    for rec in manifest:
        if rec.get("kind") == "wideband" or not any("call" in t or t.get("noise") for t in rec.get("targets", [])):
            continue
        path = base / rec["file"]
        if not path.is_file():
            continue
        z, rate = load_iq(path)
        rate = round(rate / 200.0) * 200.0
        lim = 0.42 * rate
        sk, lg, _ = run(z, rate, lo=-lim, hi=lim, country=country, band=rec.get("band"))
        spots = sk.calls.list(len(z) / rate)
        for tg in rec["targets"]:
            if tg.get("noise"):
                off = float(tg.get("offset", 0.0))
                n = sum(len("".join(x.text).replace(" ", "")) for x in _near(lg.logs, off, TARGET_HZ))
                quiet["targets"] += 1
                quiet["chars"] += n
                quiet["spots"] += any(abs(s["f"] - off) <= TARGET_HZ for s in spots
                                      if s["status"] in ("valid", "qsy"))
                if n:
                    quiet["with_chars"].append((rec["id"], tg.get("name"), n))
            if "call" not in tg:
                continue
            off = float(tg["offset"])
            near = _near(lg.logs, off, TARGET_HZ)
            sp = [s for s in spots if abs(s["f"] - off) <= TARGET_HZ]
            s, why = stage(tg["call"], near, sp)
            rows.append({"rec": rec["id"], "band": rec.get("band"), "call": tg["call"], "stage": s, "why": why,
                         "snr": max((x.snr for x in near), default=None),
                         "text": " | ".join("".join(x.text)[-80:] for x in near if x.text)})
    return rows, quiet


def score_wideband(rec, base: Path, country, seconds=None, max_channels=64) -> tuple[list[dict], float]:
    z, rate, centre, t0 = bench.load_sigmf(base / rec["file"])
    if seconds:
        z = z[:int(seconds * rate)]
    lo, hi = rec.get("lo", centre - 0.42 * rate), rec.get("hi", centre + 0.42 * rate)
    sk, lg, c = run(z, rate, lo=lo, hi=hi, centre=centre, t0=t0, max_channels=max_channels, country=country,
                    band=rec.get("band"))
    secs = len(z) / rate
    t1 = t0 + secs
    spots = sk.calls.list(t1)
    rbn = [r for r in bench.read_rbn(base / rec["rbn"]) if t0 - 30 <= r["t"] <= t1 + 30 and lo <= r["hz"] <= hi]
    by: dict[str, list] = {}
    for r in rbn:
        by.setdefault(r["call"], []).append(r)
    rows = []
    for call, rs in sorted(by.items()):
        hz = float(np.median([r["hz"] for r in rs])) - centre
        near = _near(lg.logs, hz, RBN_TRACK_HZ)
        sp = [s for s in spots if abs(s["f"] - centre - hz) <= bench.RBN_HZ]
        s, why = stage(call, near, sp)
        rows.append({"rec": rec["id"], "band": rec.get("band"), "call": call, "stage": s, "why": why,
                     "rbn_snr": max(r["snr"] for r in rs), "snr": max((x.snr for x in near), default=None),
                     "text": " | ".join("".join(x.text)[-80:] for x in near if x.text)})
    return rows, 100 * c / secs


def summary(rows: list[dict]) -> dict:
    """Per band: calls, spotted, in the text (as a word, unconfirmed or
    spotted: the decoders delivered it), and the count at each stage."""
    out = {}
    for band in sorted({r["band"] for r in rows}) + ["all"]:
        rr = [r for r in rows if band == "all" or r["band"] == band]
        c = Counter(r["stage"] for r in rr)
        out[band] = {"calls": len(rr), "spotted": c["spotted"],
                     "in_text": sum(c[k] for k in ("spotted", "unconfirmed", "busted", "matcher")),
                     "heard": sum(1 for r in rr if r["stage"] != "absent"),
                     "stages": {k: c[k] for k in STAGES if c[k]}}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("manifest")
    ap.add_argument("--dir", default=str(Path.home() / "build" / "cw-corpus"))
    ap.add_argument("--only", help="a recording id, or 'kiwi' / 'wide'")
    ap.add_argument("--seconds", type=float, help="score only this much of a wideband recording")
    ap.add_argument("--json")
    ap.add_argument("--show", action="store_true", help="every call's row, with its text")
    a = ap.parse_args(argv)
    base = Path(os.path.expanduser(a.dir))
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    if a.only == "kiwi":
        manifest = [r for r in manifest if r.get("kind") != "wideband"]
    elif a.only == "wide":
        manifest = [r for r in manifest if r.get("kind") == "wideband"]
    elif a.only:
        manifest = [r for r in manifest if r["id"] == a.only]
    country = bench.bandplan_country()
    rows, quiet = score_kiwi(manifest, base, country)
    cpu = {}
    for rec in manifest:
        if rec.get("kind") == "wideband" and rec.get("rbn"):
            r, cpu[rec["id"]] = score_wideband(rec, base, country, a.seconds)
            rows += r
    res = {"summary": summary(rows), "quiet": quiet, "rows": rows, "cpu_pct": cpu}
    for band, s in res["summary"].items():
        print(f"{band:>4}: {s['spotted']}/{s['calls']} spotted, {s['in_text']} in the text, "
              f"{s['heard']} with a track; {s['stages']}")
    if quiet["targets"]:
        print(f"quiet targets: {quiet['targets']}, {quiet['chars']} characters printed near them"
              f" {quiet['with_chars']}, {quiet['spots']} with a spot")
    if a.show:
        for r in rows:
            snr = "-" if r["snr"] is None else round(r["snr"])
            print(f"  {r['rec'][:22]:22} {r['call']:10} {r['stage']:11} snr {snr}/{r.get('rbn_snr', '')}"
                  f"  {r['why'][-70:]!r}\n{'':40} {r['text'][-70:]}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
