"""`python -m shack_bandplan.validate [path]`: every problem with the dataset,
one sentence per line; exit 1 if there is any (BANDPLAN-DATA.md §9).

A port of ohscore/licences validate() and ohsradio/plans/base.py validate() to
the JSON, plus the dataset-wide rules the JSON adds: every permit sits on a
refused row that names it, every variant's condition exists, a file that is
not partial has no gaps, and every file matches manifest.json. What it
checks is what the guard relies on - one answer per frequency, a source on
every rule, nothing refused and allowed at once - so CI fails before an app
could ever give a wrong answer from a bad edit.

JSON Schema (spec/schemas/bandplan.schema.json) covers the shapes; this
covers what needs more than one row at a time, and runs with the stdlib
alone so an app can call it at start-up too.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from .data import DataError, Dataset, load

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNITS = ("PEP", "mean", "carrier", "ERP", "EIRP")
SAT_DIRECTIONS = ("both", "up", "down")
CEPT_RECS = ("T/R 61-01", "T/R 61-02", "ECC/REC (05)06")
KINDS = ("", "beacon", "satellite", "protected", "output")
RESERVED = ("beacon", "satellite", "protected")
STATUSES = ("checked", "researched", "draft")


def _status_block(where: str, f: dict, p: list) -> None:
    if f.get("status") not in STATUSES:
        p.append(f"{where}: status must be one of {', '.join(STATUSES)}")
    if f.get("partial") is False and f.get("gaps"):
        p.append(f"{where}: partial is false but gaps are listed")
    cov = f.get("coverage") or {}
    if not cov or not cov.get("loHz", 0) < cov.get("hiHz", 0):
        p.append(f"{where}: coverage must be a span with loHz below hiHz")
    for g in f.get("gaps", []):
        if not g.get("why"):
            p.append(f"{where}: gap {g.get('loHz')}-{g.get('hiHz')} Hz has no reason")
        if cov and not (cov["loHz"] <= g["loHz"] < g["hiHz"] <= cov["hiHz"]):
            p.append(f"{where}: gap {g['loHz']}-{g['hiHz']} Hz is outside the coverage")


def _source(where: str, src: dict, p: list) -> None:
    for k in ("title", "url", "version", "date", "checked"):
        if not (src or {}).get(k):
            p.append(f"{where}: source has no {k}")
    for k in ("date", "checked"):
        if (src or {}).get(k) and not _DATE.match(src[k]):
            p.append(f"{where}: source {k} must be an ISO date (YYYY-MM-DD), not {src[k]!r}")


def regime(reg: dict, mode_classes: list) -> list[str]:
    p: list[str] = []
    rid = reg.get("id", "?")
    for k in ("id", "title", "short", "authority", "countries", "classes", "allocations", "source"):
        if not reg.get(k):
            p.append(f"{rid}: {k} is missing")
    _source(rid, reg.get("source"), p)
    _status_block(rid, reg, p)
    for c in reg.get("countries", []):
        if not re.match(r"^[A-Z]{2}$", c):
            p.append(f"{rid}: country {c!r} is not an ISO 3166-1 alpha-2 code")
    classes = [c["id"] for c in reg.get("classes", [])]

    def limits_ok(where, cls, lims):
        if cls not in classes:
            p.append(f"{where}: class {cls!r} is not one of the regime's classes")
        if not lims:
            p.append(f"{where}: class {cls!r} has an empty limit list")
            return
        for lim in lims:
            if not (isinstance(lim.get("watts"), (int, float)) and lim["watts"] > 0):
                p.append(f"{where}: power {lim.get('watts')!r} must be above 0 W")
            if lim.get("unit") not in UNITS:
                p.append(f"{where}: unit {lim.get('unit')!r} is not one of {', '.join(UNITS)}")
            if "modes" in lim and not set(lim["modes"]) <= set(mode_classes):
                p.append(f"{where}: limit modes {sorted(set(lim['modes']) - set(mode_classes))} are not mode classes")
        if any("modes" in lim for lim in lims):
            covered = set().union(*(set(lim.get("modes", mode_classes)) for lim in lims))
            # datv/atv are the dataset's additions: ohscore's rule covers its
            # seven classes, and a regime written before datv/atv existed
            # is not wrong for leaving them to "every limit applies".
            missing = set(mode_classes) - covered - {"datv", "atv"}
            if missing:
                p.append(f"{where}: class {cls!r}'s limits leave {sorted(missing)} with no limit: give every "
                         f"mode class one")

    when = []
    spans = []
    for r in reg.get("allocations", []):
        where = f"{rid} {r['loHz']}-{r['hiHz']} Hz"
        if not r["loHz"] < r["hiHz"]:
            p.append(f"{where}: lower edge not below upper")
        if not r.get("src"):
            p.append(f"{where}: no source")
        if not r.get("limits"):
            p.append(f"{where}: no class may use it (leave it out, or put it in refused)")
        for cls, lims in r.get("limits", {}).items():
            limits_ok(where, cls, lims)
        for k in ("validFrom", "validTo"):
            if k in r and not _DATE.match(str(r[k])):
                p.append(f"{where}: {k} must be an ISO date, not {r[k]!r}")
        if "regions" in r and not set(r["regions"]) <= {1, 2, 3}:
            p.append(f"{where}: regions must be among 1, 2, 3")
        spans.append((r["loHz"], r["hiHz"]))
        when.append((r["loHz"], r["hiHz"], set(r.get("regions") or (1, 2, 3)), r.get("validFrom", ""),
                     r.get("validTo", "9999-12-31")))
    for ch in reg.get("channelled", []):
        where = f"{rid} {ch.get('label', '?')}"
        if not ch.get("src"):
            p.append(f"{where}: no source")
        segs = sorted(tuple(s) for s in ch.get("segments", []))
        if not segs:
            p.append(f"{where}: no segments")
        for a, b in segs:
            if not (ch["loHz"] <= a < b <= ch["hiHz"]):
                p.append(f"{where}: segment {a}-{b} Hz is not inside {ch['loHz']}-{ch['hiHz']} Hz")
        for x, y in zip(segs, segs[1:]):
            if y[0] < x[1]:
                p.append(f"{where}: segments overlap")
        for cls, lims in ch.get("limits", {}).items():
            limits_ok(where, cls, lims)
        spans.append((ch["loHz"], ch["hiHz"]))
        when.append((ch["loHz"], ch["hiHz"], {1, 2, 3}, "", "9999-12-31"))
    when.sort()
    for i, a in enumerate(when):
        for b in when[i + 1:]:
            if b[0] >= a[1]:
                break
            if a[2] & b[2] and a[3] <= b[4] and b[3] <= a[4]:
                p.append(f"{rid}: {a[0]}-{a[1]} Hz and {b[0]}-{b[1]} Hz overlap: one frequency, two answers")

    def inside(lo, hi):
        covered = lo
        for a, b in sorted(spans):
            if a <= covered and b > covered:
                covered = b
        return covered >= hi

    permits = {x["id"]: x for x in reg.get("permits", [])}
    named = set()
    for r in reg.get("refused", []):
        if any(r["loHz"] < b and r["hiHz"] > a for a, b in spans):
            p.append(f"{rid}: refused {r['loHz']}-{r['hiHz']} Hz overlaps an allocation: refused and allowed at once")
        if not r.get("text") or not r.get("src"):
            p.append(f"{rid}: refused {r['loHz']}-{r['hiHz']} Hz needs its sentence and a source")
        if "permit" in r:
            named.add(r["permit"])
            pm = permits.get(r["permit"])
            if pm is None:
                p.append(f"{rid}: refused {r['loHz']}-{r['hiHz']} Hz names permit {r['permit']!r}, which is not "
                         f"in permits")
            elif not (r["loHz"] <= pm["loHz"] < pm["hiHz"] <= r["hiHz"]):
                p.append(f"{rid}: permit {pm['id']} is not inside the refused row that names it")
    cond_keys = {c["key"] for c in reg.get("conditions", [])}
    for pid, pm in permits.items():
        if pid not in named:
            p.append(f"{rid}: permit {pid} sits on no refused row that names it")
        for cls in pm.get("classes", []):
            if cls not in classes:
                p.append(f"{rid}: permit {pid} names class {cls!r}, which is not one of the regime's classes")
        if pm.get("limits") is not None:
            for cls in pm["classes"]:
                limits_ok(f"{rid} permit {pid}", cls, pm["limits"])
        for var in pm.get("variants", []):
            if var.get("condition") not in cond_keys:
                p.append(f"{rid}: permit {pid} has a variant on condition {var.get('condition')!r}, which the "
                         f"regime does not define")
        if not pm.get("src"):
            p.append(f"{rid}: permit {pid} has no source")
    for c in reg.get("conditions", []):
        for k in ("key", "label", "default", "src"):
            if k not in c:
                p.append(f"{rid}: a condition has no {k}")
        banded = "loHz" in c or "hiHz" in c
        if banded and (("refuse" in c) == ("limit" in c)):
            p.append(f"{rid}: condition {c.get('key')!r} needs exactly one of refuse and limit")
        if not banded and ("refuse" in c or "limit" in c):
            p.append(f"{rid}: condition {c.get('key')!r} refuses or limits but names no band")
        if not banded and not any(var.get("condition") == c.get("key") for pm in permits.values()
                                  for var in pm.get("variants", [])):
            p.append(f"{rid}: condition {c.get('key')!r} names no band and no permit variant uses it")
        if not re.match(r"^[a-z][A-Za-z0-9]*$", str(c.get("key", ""))):
            p.append(f"{rid}: condition key {c.get('key')!r} must be a camelCase word")
    sats = reg.get("satellite")
    sat_spans = [(s["loHz"], s["hiHz"]) for s in sats or [] if s["direction"] in ("both", "up")]
    for r in reg.get("allocations", []):
        if r.get("satelliteOnly"):
            covered = r["loHz"]
            for a, b in sorted(sat_spans):
                if a <= covered and b > covered:
                    covered = b
            if covered < r["hiHz"]:
                p.append(f"{rid}: {r['loHz']}-{r['hiHz']} Hz is satelliteOnly but no satellite entry covers it")
    for s in sats or []:
        if s["direction"] not in SAT_DIRECTIONS:
            p.append(f"{rid}: satellite direction {s['direction']!r} is not one of {', '.join(SAT_DIRECTIONS)}")
        if not inside(s["loHz"], s["hiHz"]):
            p.append(f"{rid}: satellite {s['loHz']}-{s['hiHz']} Hz is outside the allocations")
        if not s.get("src"):
            p.append(f"{rid}: satellite {s['loHz']}-{s['hiHz']} Hz has no source")
    for e in reg.get("emissions", []):
        if not inside(e["loHz"], e["hiHz"]):
            p.append(f"{rid}: emissions {e['loHz']}-{e['hiHz']} Hz is outside the allocations")
        for cls, modes in e["modes"].items():
            if cls not in classes:
                p.append(f"{rid}: emissions class {cls!r} is not one of the regime's classes")
            bad = set(modes) - set(mode_classes)
            if bad:
                p.append(f"{rid}: emissions modes {sorted(bad)} are not mode classes")
        if e.get("maxBwHz") is not None and not e["maxBwHz"] > 0:
            p.append(f"{rid}: emissions maxBwHz must be above 0 Hz")
        if not e.get("src"):
            p.append(f"{rid}: emissions {e['loHz']}-{e['hiHz']} Hz has no source")
    for rec, home in reg.get("cept", {}).items():
        if rec not in CEPT_RECS:
            p.append(f"{rid}: CEPT recommendation {rec!r} is not one of {', '.join(CEPT_RECS)}")
        for cls in home:
            if cls not in classes:
                p.append(f"{rid}: CEPT class {cls!r} is not one of the regime's classes")
    if reg.get("cept") and not reg.get("ceptSrc"):
        p.append(f"{rid}: cept has no ceptSrc")
    for rec, cls in reg.get("ceptVisitor", {}).items():
        if rec not in CEPT_RECS or cls not in classes:
            p.append(f"{rid}: ceptVisitor {rec!r} -> {cls!r} names an unknown recommendation or class")
    if reg.get("coverage") and spans:
        lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
        cov = reg["coverage"]
        if lo < cov["loHz"] or hi > cov["hiHz"]:
            p.append(f"{rid}: allocations run {lo}-{hi} Hz, outside the coverage {cov['loHz']}-{cov['hiHz']} Hz")
    return p


def segments(where: str, segs: list, mode_classes: list) -> list[str]:
    p: list[str] = []
    if segs != sorted(segs, key=lambda s: s["loHz"]):
        p.append(f"{where}: segments are not sorted")
    for s in segs:
        w = f"{where}: {s.get('label')} {s['loHz']}-{s['hiHz']} Hz"
        if not s["loHz"] < s["hiHz"]:
            p.append(f"{w}: lower edge not below upper")
        if s["kind"] not in KINDS:
            p.append(f"{w}: kind {s['kind']!r} is not one of {KINDS}")
        if s["kind"] in RESERVED and s["modes"] is not None:
            p.append(f"{w}: a {s['kind']} segment is reserved (modes null)")
        if s["modes"] is None and s["kind"] not in RESERVED:
            p.append(f"{w}: modes null is only for {', '.join(RESERVED)} segments")
        if s["modes"] and not set(s["modes"]) <= set(mode_classes):
            p.append(f"{w}: unknown mode classes {sorted(set(s['modes']) - set(mode_classes))}")
        if s["maxBwHz"] is not None and not s["maxBwHz"] > 0:
            p.append(f"{w}: maxBwHz must be above 0 Hz")
        if not s.get("src"):
            p.append(f"{w}: no source")
    for a, b in zip(segs, segs[1:]):
        if b["loHz"] < a["hiHz"]:
            p.append(f"{where}: {a['label']} ({a['loHz']}-{a['hiHz']}) and {b['label']} ({b['loHz']}-{b['hiHz']}) "
                     f"overlap")
    return p


def plan(pl: dict, vocab: dict) -> list[str]:
    p: list[str] = []
    pid = pl.get("id", "?")
    for k in ("id", "title", "short", "source", "segments", "region"):
        if not pl.get(k):
            p.append(f"{pid}: {k} is missing")
    _source(pid, pl.get("source"), p)
    _status_block(pid, pl, p)
    if pl.get("region") not in (1, 2, 3):
        p.append(f"{pid}: region must be 1, 2 or 3")
    p += segments(pid, pl.get("segments", []), vocab["modeClasses"])
    for c in pl.get("centres", []):
        if vocab["modeClass"].get(c["mode"]) is None:
            p.append(f"{pid}: centre {c['label']} at {c['hz']} Hz: mode {c['mode']!r} is not in the vocabulary")
    cov = pl.get("coverage") or {}
    segs = pl.get("segments", [])
    for u in pl.get("unplanned", []):
        w = f"{pid}: unplanned {u.get('loHz')}-{u.get('hiHz')} Hz"
        if not u.get("why"):
            p.append(f"{w} has no reason")
        if not (cov.get("loHz", 0) <= u["loHz"] < u["hiHz"] <= cov.get("hiHz", 0)):
            p.append(f"{w} is outside the coverage")
        # "The source prints nothing here" and a row from that source here
        # cannot both be true.
        if any(s["loHz"] < u["hiHz"] and s["hiHz"] > u["loHz"] for s in segs):
            p.append(f"{w} overlaps a segment")
    for a in pl.get("advisories", []):
        w = f"{pid}: advisory {a.get('loHz')}-{a.get('hiHz')} Hz"
        if not a["loHz"] < a["hiHz"]:
            p.append(f"{w}: lower edge not below upper")
        if not a.get("text") or not a.get("src"):
            p.append(f"{w} needs its text and a source")
        if a.get("modes") and not set(a["modes"]) <= set(vocab["modeClasses"]):
            p.append(f"{w}: unknown mode classes {sorted(set(a['modes']) - set(vocab['modeClasses']))}")
        if a.get("modes") is None and a.get("maxBwHz") is None:
            p.append(f"{w} restricts neither modes nor width, so it could never warn")
    return p


def satellite(s: dict, vocab: dict) -> list[str]:
    p: list[str] = []
    sid = s.get("id", "?")
    _source(sid, s.get("source"), p)
    _status_block(sid, s, p)
    p += segments(sid, s.get("segments", []), vocab["modeClasses"])
    edge = s["uplink"] if s["segmentsIn"] == "uplink" else s.get("downlink") or {}
    for g in s.get("segments", []):
        if not (edge.get("loHz", 0) <= g["loHz"] < g["hiHz"] <= edge.get("hiHz", 0)):
            p.append(f"{sid}: segment {g['label']} is outside the transponder's {s['segmentsIn']}")
    if s.get("downlink") and s["downlink"]["loHz"] - s["uplink"]["loHz"] != s["uplinkToDownlinkHz"]:
        p.append(f"{sid}: uplink, downlink and uplinkToDownlinkHz disagree")
    return p


def dataset(ds: Dataset) -> list[str]:
    p: list[str] = []
    for rel, digest in ds.manifest.get("files", {}).items():
        f = ds.path / rel
        if not f.is_file():
            p.append(f"manifest: {rel} is missing")
        elif hashlib.sha256(f.read_bytes()).hexdigest() != digest:
            p.append(f"manifest: {rel} does not match its sha256")
    listed = set(ds.manifest.get("files", {}))
    for f in ds.path.rglob("*"):
        if f.is_file() and f.name != "manifest.json" and f.relative_to(ds.path).as_posix() not in listed:
            p.append(f"manifest: {f.relative_to(ds.path).as_posix()} is not listed")
    if not re.match(r"^\d{4}\.\d{2}\.\d+$", ds.manifest.get("dataVersion", "")):
        p.append("manifest: dataVersion must be CalVer YYYY.MM.N")
    mc = ds.vocab["modeClasses"]
    for m, c in ds.vocab["modeClass"].items():
        if c not in mc:
            p.append(f"vocab: mode {m!r} maps to {c!r}, which is not a mode class")
    for reg in ds.regimes.values():
        p += regime(reg, mc)
    for pl in ds.plans.values():
        p += plan(pl, ds.vocab)
    for s in ds.satellites.values():
        p += satellite(s, ds.vocab)
    for c in ds.countries:
        for rid in c["regimes"]:
            if rid not in ds.regimes:
                p.append(f"countries: {c['iso']} names regime {rid!r}, which is not in the dataset")
        if c["plan"] and c["plan"] not in ds.plans:
            p.append(f"countries: {c['iso']} names plan {c['plan']!r}, which is not in the dataset")
        if not c.get("src"):
            p.append(f"countries: {c['iso']} has no source")
    for reg in ds.regimes.values():
        if reg.get("plan") and reg["plan"] not in ds.plans:
            p.append(f"{reg['id']}: plan {reg['plan']!r} is not in the dataset")
    for e in ds.presets:
        for pr in e["presets"]:
            if pr.get("satellite") and pr["satellite"] not in ds.satellites:
                p.append(f"presets: {pr['label']} names satellite {pr['satellite']!r}, which is not in the dataset")
            if pr.get("mode") and ds.mode_class(pr["mode"]) is None:
                p.append(f"presets: {pr['label']}: mode {pr['mode']!r} is not in the vocabulary")
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        ds = load(Path(argv[0]) if argv else None, verify=False)
    except DataError as e:
        print(e)
        return 1
    problems = dataset(ds)
    for line in problems:
        print(line)
    if not problems:
        print(f"band-plan dataset {ds.data_version}: no problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
