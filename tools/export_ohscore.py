#!/usr/bin/env python3
"""Convert the OpenHamShack Python band-plan and licence modules into the
shared JSON dataset, data/bandplan/ (BANDPLAN-DATA.md §1-§5).

Why a script and not a retyping: the UK regime was checked line by line
against Ofcom's PDF (openhamshack-nb docs/ham/licence-check.md). Reading the
values out of the very modules that check covered carries them exactly;
a person copying 300 rows by hand would introduce the errors the check
removed. So this script imports the modules read-only and writes what they
hold, adding only the three things BANDPLAN-DATA.md §1 says they lack
(NoVs as permits, the QO-100 wideband plan as data, presets) and the
status/partial/coverage/gaps fields of §3.

It is a one-time conversion (REPO-LAYOUT.md §2 "export_ohscore.py"): once
the apps read the JSON, the JSON is the source of truth and edits go to it
directly. Re-running it after that would overwrite hand edits, which is
why it refuses to write over a dataset whose manifest says
"generatedBy" is anything but this script, unless --force is given.

Until then, rows ohs-libs transcribed itself (rows the OpenHamShack modules
never carried, or carry wrongly) live in data/bandplan-overrides/, one
file per plan, each row citing its source, and this script applies them
on every run. Hand-editing data/bandplan instead would be lost at the next
export; patching the OpenHamShack modules is not ours to do (openhamshack-nb is the
NB session's repo). The overrides sit beside data/bandplan rather than in
it because the export rebuilds data/bandplan from nothing, the validator
refuses any file there the manifest does not list, and the wheel copies
data/bandplan whole: source material for the export has no business in an
app.

To reproduce a committed dataset exactly, export from `git archive`s of
the commits its manifest records and pass them as --nb-commit and
--box-commit (an archive has no git history to read them from):

    git -C ~/openhamshack-nb archive <sha> | tar -x -C ~/build/bp-src/nb
    git -C ~/openhamshack archive <sha> appliance | tar -x -C ~/build/bp-src/box
    tools/export_ohscore.py --nb ~/build/bp-src/nb --box ~/build/bp-src/box \
        --nb-commit <sha> --box-commit <sha>

Exporting from today's checkouts instead also brings in whatever their
sentences have changed since (dataset 2026.10.2 was cut from the 2026.10.1
commits, so its only changes are the overrides; 2026.10.3 was cut from
openhamshack-nb and openhamshack main on 09/10 and differs from 2026.10.2 only in the
OpenHamShack -> OpenHamShack rewording of notes).

Usage:
    tools/export_ohscore.py --nb ~/openhamshack-nb --box ~/openhamshack [--out data/bandplan]

Sources are never written to: ~/openhamshack-nb is the NB session's repo and
~/openhamshack the Classic box's.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
DATA_VERSION = "2026.10.3"
GENERATED_BY = "tools/export_ohscore.py"
OVERRIDES = Path(__file__).resolve().parent.parent / "data" / "bandplan-overrides"

# 10.5 GHz is the top of what every plan here speaks for: Ofcom's licence
# ends there (3 cm), and nothing in the family transmits above it.
TOP_HZ = 10_500_000_000


# ---------------------------------------------------------------- output
def _num(v):
    """Integral floats as ints, so 1000.0 W is written 1000 and Hz values
    never carry a ".0" that a strict reader would call a float."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _hz(v) -> int:
    # Every source frequency is an exact Hz value written as kHz/MHz
    # arithmetic (5258.5 * 1e3); round() removes float noise such as
    # 430393749.99999994, never a real fraction of a hertz.
    return int(round(float(v)))


def dumps(obj) -> str:
    """JSON with one row per line: top-level keys on their own lines, and
    a list of objects or lists one element per line. Diffs then show the
    row that changed, which is the point of keeping law as text."""
    def compact(v):
        return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))

    def block(v, indent):
        pad = " " * indent
        if isinstance(v, list) and v and all(isinstance(x, (dict, list)) for x in v):
            inner = ",\n".join(pad + "  " + compact(x) for x in v)
            return "[\n" + inner + "\n" + pad + "]"
        return compact(v)

    if isinstance(obj, list):
        return block(obj, 0) + "\n"
    lines = [f"  {json.dumps(k)}: {block(v, 2)}" for k, v in obj.items()]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def write(out: Path, rel: str, obj) -> None:
    p = out / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(obj), encoding="utf-8")


# ---------------------------------------------------------------- limits
def limits_json(value, licences) -> list[dict]:
    out = []
    for lim in licences.limits_of(value):
        d = {"watts": _num(float(lim.watts)), "unit": lim.unit}
        if lim.modes is not None:
            d["modes"] = sorted(lim.modes)
        out.append(d)
    return out


def limit_one(lim) -> dict:
    d = {"watts": _num(float(lim.watts)), "unit": lim.unit}
    if getattr(lim, "modes", None) is not None:
        d["modes"] = sorted(lim.modes)
    return d


EXTRA_NAMES = {"valid_from": "validFrom", "valid_to": "validTo", "regions": "regions",
               "unconfirmed": "unconfirmed", "satellite_only": "satelliteOnly"}


# ---------------------------------------------------------------- regimes
def regime_json(mod, licences, status: str, partial: bool, partial_why: str, extra: dict | None = None) -> dict:
    allocs = []
    for row in mod.ALLOCATIONS:
        lo, hi, lims, note, src = row[:5]
        r = {"loHz": _hz(lo), "hiHz": _hz(hi),
             "limits": {cls: limits_json(v, licences) for cls, v in lims.items()},
             "note": note, "src": src}
        for k, v in licences.row_extras(row).items():
            r[EXTRA_NAMES[k]] = sorted(v) if k == "regions" else v
        allocs.append(r)
    channelled = []
    for ch in getattr(mod, "CHANNELLED", []):
        channelled.append({"loHz": _hz(ch["lo"]), "hiHz": _hz(ch["hi"]), "label": ch["label"], "where": ch["where"],
                           "segments": [[_hz(a), _hz(b)] for a, b in ch["segments"]],
                           "limits": {cls: [limit_one(licences.limits_of(lim)[0]) for lim in lims]
                                      for cls, lims in ch["limits"].items()},
                           "maxBwHz": _num(float(ch["max_bw"])) if ch.get("max_bw") is not None else None,
                           "src": ch["src"]})
    emissions = [{"loHz": _hz(lo), "hiHz": _hz(hi), "modes": {cls: sorted(m) for cls, m in per.items()},
                  "maxBwHz": _num(float(bw)) if bw is not None else None, "note": note, "src": src}
                 for lo, hi, per, bw, note, src in getattr(mod, "EMISSIONS", [])]
    refused = [{"loHz": _hz(lo), "hiHz": _hz(hi), "text": text, "src": src}
               for lo, hi, text, src in getattr(mod, "REFUSED", [])]
    conditions = []
    for c in getattr(mod, "CONDITIONS", []):
        d = {"key": c["key"], "loHz": _hz(c["lo"]), "hiHz": _hz(c["hi"]), "label": c["label"],
             "default": bool(c["default"]), "when": bool(c.get("when", True))}
        if "refuse" in c:
            d["refuse"] = c["refuse"]
        else:
            d["limit"] = limit_one(c["limit"])
        d["src"] = c["src"]
        conditions.append(d)
    sats = getattr(mod, "SATELLITE", None)
    spans = [(r["loHz"], r["hiHz"]) for r in allocs] + [(c["loHz"], c["hiHz"]) for c in channelled]
    src = dict(mod.SOURCE)
    out = {
        "schema": SCHEMA, "id": mod.ID, "title": mod.TITLE, "short": mod.SHORT, "authority": mod.AUTHORITY,
        "countries": list(mod.COUNTRY), "plan": getattr(mod, "PLAN", ""),
        "status": status, "partial": partial,
        "coverage": {"loHz": min(a for a, _ in spans), "hiHz": max(b for _, b in spans)}, "gaps": [],
        "source": src,
        "classes": [{"id": c, "name": mod.CLASS_NAME[c]} for c in mod.CLASSES],
        "edges": getattr(mod, "EDGES", "closed"),
        "allocations": allocs, "channelled": channelled, "emissions": emissions, "refused": refused,
        "permits": [], "conditions": conditions,
        # None (not []) keeps ohscore's distinction: a regime with no
        # SATELLITE table says nothing about the satellite service, which
        # is not the same as saying it is nowhere permitted.
        "satellite": None if sats is None else [{"loHz": _hz(a), "hiHz": _hz(b), "direction": d, "src": s}
                                                for a, b, d, s in sats],
        "cept": {k: list(v) for k, v in getattr(mod, "CEPT", {}).items()},
        "ceptSrc": getattr(mod, "CEPT_SRC", ""),
        "ceptVisitor": dict(getattr(mod, "CEPT_VISITOR", {})),
        "notes": [{"topic": t, "text": x, "src": s} for t, x, s in getattr(mod, "NOTES", [])],
    }
    if partial_why:
        out["partialWhy"] = partial_why
    if extra:
        extra(out)
    return out


def uk_permits(reg: dict) -> None:
    """The 146 MHz Notice of Variation as a permit (BANDPLAN-DATA.md §4.4).

    Values from the box's transcription (~/openhamshack/appliance/openhamshacklib/
    bands.py:13-18: NOV146_TOP, 147.0 in the rest of the UK and 146.9375 in
    Scotland). `researched`, not `checked`: the NoV document itself has not
    been cited for the power figure or the 40 km rule (beta decision 4,
    BETA-PLAN.md). `limits: null` means the dataset has no power figure, so
    power is not judged there - never that it is unlimited.

    70.5-71.5 MHz and 2300-2302 MHz are innovation bands too, but nothing
    in the family holds their NoV terms, so they stay plain refusals: a
    permit with invented terms would be a guess."""
    for row in reg["refused"]:
        if (row["loHz"], row["hiHz"]) == (146_000_000, 147_000_000):
            row["permit"] = "gb-146"
    reg["permits"] = [{
        "id": "gb-146", "label": "146-147 MHz Notice of Variation",
        "loHz": 146_000_000, "hiHz": 147_000_000, "classes": ["full"], "limits": None,
        "variants": [{"condition": "scotland40km", "when": True, "hiHz": 146_937_500,
                      "text": ("In Scotland, and within 40 km of the Scottish border or coast, the 146 MHz NoV "
                               "stops at 146.9375 MHz")}],
        "status": "researched",
        "src": "~/openhamshack/appliance/openhamshacklib/bands.py:13-18 (box transcription); the NoV document itself to be cited",
    }]
    reg["conditions"].append({
        "key": "scotland40km",
        "label": "Station in Scotland or within 40 km of the Scottish border or coast",
        # True, the restrictive side, as the box's licence_region() reads
        # anything it does not recognise as Scotland.
        "default": True, "derive": {"dxccPrefix": ["GM", "GM/s"]},
        "src": "as gb-146",
    })


# ---------------------------------------------------------------- plans
# The plan modules cite pages per band in comments, not per segment; the
# validator requires a `src` on every row, so each segment gets its band's
# citation as the module's comments give it.
PLAN_SRC = {
    "rsgb-2026": [(0, 30e6, "R1 (bandplan-r1-uk.md s.2)"), (30e6, 1e12, "R1 (bandplan-r1-uk.md s.3)")],
    "iaru-r1": [(0, 30e6, "I1 p.1-2"), (50e6, 54e6, "I2 p.35-36"), (144e6, 146e6, "I2 p.38-40"),
                (430e6, 440e6, "I2 p.41-44"), (1240e6, 1300e6, "I2 p.45-47"), (2300e6, 2450e6, "I2 p.48"),
                (5650e6, 5850e6, "I2 p.50")],
    "iaru-r2": [(0, 30e6, "N1 p.5-10"), (30e6, 1e12, "N1 p.11-18")],
    "iaru-r3": [(0, 30e6, "P1 p.5-12"), (30e6, 1e12, "P1 p.13-18")],
}
SOURCE_KEYS = {"rsgb-2026": ["R1"], "iaru-r1": ["I1", "I2"], "iaru-r2": ["N1"], "iaru-r3": ["P1"]}

# What each plan module is known to lack, inside the span it should speak
# for (BANDPLAN-DATA.md §3). Every gap here is a fact about the OpenHamShack
# transcription, not about the plan: the RSGB 2026 PDF does plan LF/MF and
# 3 cm. They stay listed even though data/bandplan-overrides now fills all
# of them ("fills" there removes a gap it covers), so that dropping an
# override file brings its gap back instead of leaving the plan silently
# overstating its coverage.
PLAN_GAPS = {
    "rsgb-2026": [(135_700, 137_800, "2200 m: the RSGB plan's LF section is not transcribed"),
                  (472_000, 479_000, "630 m: the RSGB plan's MF section is not transcribed"),
                  (10_000_000_000, 10_475_000_000,
                   "3 cm below the satellite segment: the RSGB plan's 3 cm rows (narrow band 10368-10370 MHz, "
                   "alternative 10450-10452 MHz) are not transcribed")],
    "iaru-r1": [(135_700, 137_800, "2200 m: the IARU R1 HF plan's LF section is not transcribed"),
                (472_000, 479_000, "630 m: the IARU R1 HF plan's MF section is not transcribed"),
                (10_000_000_000, TOP_HZ, "3 cm: the IARU R1 VHF Handbook's 10 GHz plan is not transcribed")],
}


def load_overrides(pid: str) -> dict | None:
    p = OVERRIDES / "plans" / f"{pid}.json"
    if not p.is_file():
        return None
    ov = json.loads(p.read_text(encoding="utf-8"))
    if ov.get("plan") != pid:
        raise SystemExit(f"{p}: says it is for {ov.get('plan')!r}, not {pid!r}")
    return ov


def apply_overrides(pid: str, ov: dict, segs: list, gaps: list, centres: list, sources: list) -> list:
    """The plan's override file on top of what its module gave; returns the
    segments. Fails loudly rather than guess: a drop span that cuts a
    generated row in two, or an added row overlapping one that stays,
    means the override was written against different module data."""
    for d in ov.get("drop", []):
        for s in segs:
            inside = d["loHz"] <= s["loHz"] and s["hiHz"] <= d["hiHz"]
            if not inside and s["loHz"] < d["hiHz"] and s["hiHz"] > d["loHz"]:
                raise SystemExit(f"{pid}: override drop {d['loHz']}-{d['hiHz']} Hz cuts {s['label']} "
                                 f"({s['loHz']}-{s['hiHz']} Hz) in two")
        segs = [s for s in segs if not (d["loHz"] <= s["loHz"] and s["hiHz"] <= d["hiHz"])]
    for r in ov.get("segments", []):
        if not r.get("src"):
            raise SystemExit(f"{pid}: override row {r['loHz']}-{r['hiHz']} Hz has no source")
    segs = sorted(segs + [dict(r) for r in ov.get("segments", [])], key=lambda s: (s["loHz"], s["hiHz"]))
    for a, b in zip(segs, segs[1:]):
        if b["loHz"] < a["hiHz"]:
            raise SystemExit(f"{pid}: {a['label']} ({a['loHz']}-{a['hiHz']}) and {b['label']} "
                             f"({b['loHz']}-{b['hiHz']}) overlap after the overrides")
    fills = ov.get("fills", [])
    gaps[:] = [g for g in gaps if not any(f["loHz"] <= g["loHz"] and g["hiHz"] <= f["hiHz"] for f in fills)]
    have = {c["hz"] for c in centres}
    centres += [dict(c) for c in ov.get("centres", []) if c["hz"] not in have]
    centres.sort(key=lambda c: c["hz"])
    for s in sources:
        s.update(ov.get("sources", {}).get(s["key"], {}))
    return segs


def plan_json(mod, status: str) -> dict:
    pid = mod.ID

    def src_for(lo):
        for a, b, s in PLAN_SRC[pid]:
            if a <= lo < b:
                return s
        raise SystemExit(f"{pid}: no citation for a segment at {lo:.0f} Hz")

    segs = []
    for s in mod.SEGMENTS:
        modes = None if s.modes is None else sorted(s.modes)
        # BANDPLAN-DATA.md §4.1 decision: the ATV/DATV segments become
        # explicit datv/atv rows. Only IARU R1 prints such segments among
        # these plans ("ATV and DATV", 23 cm); its module filed them under
        # image because OpenHamShack HAM sent no television.
        if modes == ["image"] and "ATV" in s.label:
            modes = ["atv", "datv", "image"]
        segs.append({"loHz": _hz(s.lo), "hiHz": _hz(s.hi), "label": s.label, "modes": modes, "kind": s.kind,
                     "maxBwHz": _num(float(s.max_bw)) if s.max_bw is not None else None, "src": src_for(s.lo)})
    gaps = [{"loHz": a, "hiHz": b, "why": w} for a, b, w in PLAN_GAPS.get(pid, [])]
    centres = [{"hz": _hz(f), "label": label, "mode": mode} for f, label, mode in mod.CENTRES]
    sources = list(getattr(mod, "SOURCES", [mod.SOURCE]))
    keyed = [dict(s, key=k) for s, k in zip(sources, SOURCE_KEYS[pid])]
    ov = load_overrides(pid)
    if ov:
        segs = apply_overrides(pid, ov, segs, gaps, centres, keyed)
    lo = min(x["loHz"] for x in segs)
    hi = max(x["hiHz"] for x in segs)
    if pid in ("iaru-r2", "iaru-r3"):
        # BANDPLAN-DATA.md §3: "IARU R2 and R3 as partial: true above 6 cm".
        gaps.append({"loHz": hi, "hiHz": TOP_HZ, "why": "above 6 cm: not transcribed"})
    if gaps:
        lo = min([lo] + [g["loHz"] for g in gaps])
    out = {
        "schema": SCHEMA, "id": pid, "title": mod.TITLE, "short": mod.SHORT,
        "source": keyed[0], "sources": keyed, "region": mod.REGION, "countries": list(mod.COUNTRY),
        "status": status, "partial": bool(gaps), "coverage": {"loHz": lo, "hiHz": max(hi, TOP_HZ)}, "gaps": gaps,
        "segments": segs,
        "centres": centres,
        "ibp": [_hz(f) for f in mod.IBP],
    }
    # Written only when there are any, so the plans without overrides stay
    # byte for byte what the modules give.
    for k in ("unplanned", "advisories"):
        if ov and ov.get(k):
            out[k] = [dict(x) for x in ov[k]]
    return out


# ---------------------------------------------------------------- satellites
def qo100_nb(nb) -> dict:
    """ohsnb/bandplan.py, in downlink Hz with the transponder offset.

    NB's own vocabulary differs from ohscore's (BANDPLAN-DATA.md §4.1): it
    says "digital" where ohscore says "digi", and files AM under SSB and
    FreeDV and SSTV under digital. The shared vocabulary is ohscore's, so
    each NB class is written as the ohscore classes of the modes NB filed
    under it - "ssb" becomes [am, ssb], "digital" [digi, dv, image] - which
    keeps every NB answer the same through the new vocabulary."""
    cls_map = {"ssb": ["am", "ssb"], "cw": ["cw"], "digital": ["digi", "dv", "image"]}
    segs = []
    for lo, hi, label, classes, maxbw in nb.SEGMENTS:
        if classes is None:
            kind = "protected" if "broadcast" in label else "beacon"
            modes = None
            bw = None
        else:
            kind = ""
            modes = sorted({m for c in classes for m in cls_map[c]})
            bw = _num(float(maxbw))
        segs.append({"loHz": _hz(lo), "hiHz": _hz(hi), "label": label, "modes": modes, "kind": kind,
                     "maxBwHz": bw, "src": "Q1"})
    return {
        "schema": SCHEMA, "id": "qo100-nb", "kind": "satellite", "title": "QO-100 narrowband transponder",
        "status": "researched", "partial": False,
        "coverage": {"loHz": _hz(nb.DL_LO), "hiHz": _hz(nb.DL_HI)}, "gaps": [],
        "source": {"key": "Q1", "title": "AMSAT-DL QO-100 narrowband bandplan, beacons, IARU R1 emergency notice",
                   "url": "https://amsat-dl.org/en/p4-a-nb-transponder-bandplan-and-operating-guidelines/",
                   "version": "as compiled in openhamshack-nb docs/qo100-research.md s.1 (2026-10-04)",
                   "date": "2026-10-04", "checked": "2026-10-04"},
        "uplinkToDownlinkHz": _hz(nb.TRANSPONDER_LO),
        "uplink": {"loHz": _hz(nb.UL_LO), "hiHz": _hz(nb.UL_HI)},
        "downlink": {"loHz": _hz(nb.DL_LO), "hiHz": _hz(nb.DL_HI)},
        "segmentsIn": "downlink",
        "maxBwHz": _num(float(nb.MAX_BW)),
        "edgeRule": "occupied",
        "segments": segs,
        "beacons": [{"id": k, "label": b["name"], "downlinkHz": _hz(b["dl"]), "kind": b["kind"],
                     "refHz": _hz(b["ref"])} for k, b in nb.BEACONS.items()],
        "neighbours": [{"loHz": 2_401_000_000, "hiHz": 2_410_000_000, "label": "QO-100 wideband transponder"}],
    }


def qo100_wb(bands) -> dict:
    """openhamshacklib/bands.py's QO100_* and qo100_channels, as data
    (BANDPLAN-DATA.md §5). The channel lists are regenerated here from the
    function itself and then expressed as first/step/count, and
    tests/test_conformance.py proves the dataset gives the function's
    answer for every symbol rate."""
    off = _hz(bands.QO100_OFFSET_MHZ * 1e6)
    up_lo, up_hi = (_hz(x * 1e6) for x in bands.QO100_WB_UPLINK)
    us_lo, us_hi = (_hz(x * 1e6) for x in bands.QO100_WB_USERS)
    nb_lo, nb_hi = (_hz(x * 1e6) for x in bands.QO100_NB_UPLINK)
    return {
        "schema": SCHEMA, "id": "qo100-wb", "kind": "satellite", "title": "QO-100 wideband transponder",
        "status": "checked", "partial": False,
        "coverage": {"loHz": up_lo, "hiHz": up_hi}, "gaps": [],
        "source": {"key": "BATC", "title": "QO-100 WB Bandplan", "url": "https://wiki.batc.org.uk/QO-100_WB_Bandplan",
                   "version": "V3, Feb 2021", "date": "2021-02-01", "checked": "2026-10-09"},
        "uplinkToDownlinkHz": off,
        "uplink": {"loHz": up_lo, "hiHz": up_hi},
        "downlink": {"loHz": up_lo + off, "hiHz": up_hi + off},
        "segmentsIn": "uplink",
        "segments": [
            {"loHz": up_lo, "hiHz": us_lo, "label": "beacon", "modes": None, "kind": "beacon", "maxBwHz": None,
             "src": "BATC"},
            {"loHz": us_lo, "hiHz": us_hi, "label": "users", "modes": ["datv"], "kind": "", "maxBwHz": None,
             "src": "BATC"},
        ],
        "neighbours": [{"loHz": nb_lo, "hiHz": nb_hi, "label": "QO-100 narrowband transponder"}],
        # QO-100's edges are judged at the symbol rate, as BATC draws the
        # plan (its 1.5 MS channel 1 reaches exactly the beacon edge at its
        # symbol rate), while licence edges use the occupied width
        # (OCCUPIED_FACTOR 1.35): bands.py qo100_blocked docstring.
        "edgeRule": "symbolRate",
        "occupiedFactor": bands.OCCUPIED_FACTOR,
        "channels": [
            {"srKs": [1000, 1500], "downlinkHz": [10_493_250_000, 10_494_750_000, 10_496_250_000]},
            {"srKs": [500], "downlinkFirstHz": 10_492_750_000, "stepHz": 500_000, "count": 9},
            {"srKs": [333, 250], "downlinkFirstHz": 10_492_750_000, "stepHz": 500_000, "count": 14},
            {"srKs": [125, 66, 33], "downlinkFirstHz": 10_492_750_000, "stepHz": 250_000, "count": 27},
        ],
        "preferred": {"belowKs": 333, "rule": "333 kS and below use the segment above 10497.0 MHz first",
                      "aboveDownlinkHz": 10_497_000_000},
    }


# ---------------------------------------------------------------- repeaters, presets, vocab
def scheme_json(s) -> dict:
    d = {"name": s.name, "outLoHz": _hz(s.out_lo), "outHiHz": _hz(s.out_hi), "shiftHz": _hz(s.shift),
         "stepHz": _hz(s.step), "first": s.first}
    if s.note:
        d["note"] = s.note
    return d


def repeaters_json(plans_mod, rep_mod, bands_mod) -> dict:
    by_plan = []
    for pid, mod in plans_mod.PLANS.items():
        if getattr(mod, "REPEATERS", None):
            by_plan.append({"plan": pid, "schemes": [scheme_json(s) for s in mod.REPEATERS],
                            "offsets": {k: _hz(v) for k, v in getattr(mod, "RPT_OFFSET", {}).items()},
                            "simplex": [{"prefix": n, "firstHz": _hz(f), "stepHz": _hz(st), "first": k,
                                         "lastHz": _hz(last)} for n, f, st, k, last in getattr(mod, "SIMPLEX", [])],
                            "src": f"ohsradio/plans/{pid.replace('-', '_')}.py REPEATERS"})
    by_country = [{"country": iso, "schemes": [scheme_json(s) for s in e["schemes"]],
                   "offsets": {k: _hz(v) for k, v in e.get("offsets", {}).items()}, "src": e["src"]}
                  for iso, e in rep_mod.BY_COUNTRY.items()]
    return {"schema": SCHEMA, "byPlan": by_plan, "byCountry": by_country}


def presets_json(nb) -> list:
    # The pad's TX_PRESETS (~/openhamshack/appliance/pad.py:34): 146.5, 437.0,
    # 1255.0 MHz and QO-100's 2405.25. In the dataset 437.0 falls inside
    # rsgb-2026's 435-438 MHz satellite segment, which the plan reserves:
    # the preset is listed (most specific wins, never hidden) and shows
    # disabled with the verdict's sentence until the plan data carries the
    # RSGB's DATV allowance there.
    return [
        {"kind": "datv", "country": "GB",
         "presets": [{"label": "2 m NoV DATV", "txHz": 146_500_000, "mode": "dvbs2", "permit": "gb-146"},
                     {"label": "70 cm DATV", "txHz": 437_000_000, "mode": "dvbs2"},
                     {"label": "23 cm DATV", "txHz": 1_255_000_000, "mode": "dvbs2"}],
         "src": "~/openhamshack/appliance/pad.py:34 TX_PRESETS"},
        {"kind": "qo100wb", "everywhere": True,
         "presets": [{"label": "QO-100 WB 2405.25", "txHz": 2_405_250_000, "mode": "dvbs2",
                      "satellite": "qo100-wb"}],
         "src": "~/openhamshack/appliance/pad.py:34 TX_PRESETS"},
        {"kind": "qo100nb", "everywhere": True,
         "presets": [{"label": p["name"], "rxHz": _hz(p["dl"]), "mode": p["mode"], "satellite": "qo100-nb"}
                     for p in nb.PRESETS],
         "src": "ohsnb/bandplan.py PRESETS"},
    ]


# ADIF 3.1.4's Band enumeration (lower and upper edges, MHz), the names the
# logbook stores and the profile's antennaGainDbi keys: one vocabulary for
# "which band is this", international rather than the UK edges
# ohsradio/bands.py uses for its band bar.
ADIF_BANDS = [
    ("2190m", .1357, .1378), ("630m", .472, .479), ("560m", .501, .504), ("160m", 1.8, 2.0), ("80m", 3.5, 4.0),
    ("60m", 5.06, 5.45), ("40m", 7.0, 7.3), ("30m", 10.1, 10.15), ("20m", 14.0, 14.35), ("17m", 18.068, 18.168),
    ("15m", 21.0, 21.45), ("12m", 24.89, 24.99), ("10m", 28.0, 29.7), ("8m", 40.0, 45.0), ("6m", 50.0, 54.0),
    ("5m", 54.000001, 69.9), ("4m", 70.0, 71.0), ("2m", 144.0, 148.0), ("1.25m", 222.0, 225.0),
    ("70cm", 420.0, 450.0), ("33cm", 902.0, 928.0), ("23cm", 1240.0, 1300.0), ("13cm", 2300.0, 2450.0),
    ("9cm", 3300.0, 3500.0), ("6cm", 5650.0, 5925.0), ("3cm", 10000.0, 10500.0), ("1.25cm", 24000.0, 24250.0),
    ("6mm", 47000.0, 47200.0), ("4mm", 75500.0, 81000.0), ("2.5mm", 119980.0, 123000.0),
    ("2mm", 134000.0, 149000.0), ("1mm", 241000.0, 250000.0),
]


def vocab_json(licences) -> dict:
    mode_class = dict(licences.MODE_CLASS)
    # Two classes ohscore lacks (BANDPLAN-DATA.md §4.1): the DATV and ATV
    # apps transmit wide image modes a plan's "image" (SSTV, fax) does not
    # describe.
    mode_class.update({"dvbs": "datv", "dvbs2": "datv", "pal-am": "atv", "pal-fm": "atv"})
    return {
        "schema": SCHEMA,
        "modeClasses": list(licences.MODE_CLASSES) + ["datv", "atv"],
        "modeClass": mode_class,
        # Plans written before datv/atv existed treat them as image until a
        # row names them (BANDPLAN-DATA.md §4.1).
        "fallbackClass": {"datv": "image", "atv": "image"},
        "classWord": {"ssb": "SSB", "am": "AM", "fm": "FM", "cw": "CW", "digi": "digital modes",
                      "dv": "digital voice", "image": "SSTV", "datv": "DATV", "atv": "ATV"},
        "units": list(licences.UNITS),
        "segmentKinds": ["", "beacon", "satellite", "protected", "output"],
        "reservedKinds": ["beacon", "satellite", "protected"],
        "bands": [{"name": n, "loHz": _hz(a * 1e6), "hiHz": _hz(b * 1e6)} for n, a, b in ADIF_BANDS],
        "bandsSrc": "ADIF 3.1.4 Band enumeration",
    }


# ohscore/countries.py WRITTEN, one for one; each `src` is that row's comment.
COUNTRY_SRC = {
    "GB": "Ofcom Amateur Radio Guidance (14 Oct 2025) s.3.4 p.25", "IE": "ComReg 09/45R6 s.4.2",
    "DE": "AfuV Anlage 1", "NL": "nl.md s.1", "BE": "be.md s.1", "FR": "ARCEP 2012-1241",
    "ES": "Orden IET/1311/2013 art.14", "IT": "CCE art.140", "NO": "FOR-2026-03-23-465, mainland",
    "SE": "PTSFS 2025:1", "DK": "BEK 499/2026", "FI": "6 K/2019M; F2 s.6 Aland", "US": "47 CFR 97, Appendix 1",
    "CA": "RBR-4 Schedule I", "AU": "Class Licence 2023 s.10, s.17", "NZ": "GURL 2025 cl.4(1), 8(3)",
}


def countries_json(countries, status_of) -> list:
    return [{"iso": c.iso, "name": c.name, "regulator": c.regulator, "entities": list(c.entities),
             "regimes": list(c.regimes), "plan": c.plan, "region": c.region,
             "status": status_of[c.regimes[0]], "src": COUNTRY_SRC[c.iso]} for c in countries.WRITTEN]


def region_rules(countries) -> dict:
    """ohscore/countries.py region_of's exceptions, so a reader can give a
    DXCC entity its IARU region from cty.dat's continent without ohscore."""
    return {"byContinent": dict(countries._BY_CONTINENT),
            "region1InAsia": sorted(countries._REGION1_ASIA),
            "region3InAfrica": sorted(countries._REGION3_AFRICA),
            "region2InOceania": sorted(countries._REGION2_OCEANIA),
            "src": "ITU RR Art.5 Nos. 5.2-5.9 via ohscore/countries.py region_of"}


# ---------------------------------------------------------------- main
def git_head(repo: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nb", type=Path, default=Path.home() / "openhamshack-nb")
    ap.add_argument("--box", type=Path, default=Path.home() / "openhamshack")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "bandplan")
    ap.add_argument("--force", action="store_true", help="overwrite a dataset this script did not write")
    ap.add_argument("--nb-commit", default="", help="the openhamshack-nb commit --nb was archived from")
    ap.add_argument("--box-commit", default="", help="the openhamshack commit --box was archived from")
    a = ap.parse_args(argv)

    man = a.out / "manifest.json"
    if man.exists() and not a.force:
        if json.loads(man.read_text()).get("generatedBy") != GENERATED_BY:
            print(f"{man} was not written by this script: the JSON is the source of truth now. Use --force "
                  f"only if you mean to throw its hand edits away.", file=sys.stderr)
            return 1

    # Never leave bytecode in other sessions' repositories.
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(a.nb), str(a.box / "appliance")]
    licences = importlib.import_module("ohscore.licences")
    countries = importlib.import_module("ohscore.countries")
    plans = importlib.import_module("ohsradio.plans")
    repeaters = importlib.import_module("ohsradio.plans.repeaters")
    nb = importlib.import_module("ohsnb.bandplan")
    box = importlib.import_module("openhamshacklib.bands")

    if a.out.exists():
        shutil.rmtree(a.out)
    a.out.mkdir(parents=True)

    status_of = {}
    for rid, mod in licences.REGIMES.items():
        if rid == "ofcom-2024":
            reg = regime_json(mod, licences, "checked", False, "", uk_permits)
        else:
            iso = mod.COUNTRY[0].lower()
            reg = regime_json(mod, licences, "researched", True,
                              f"Researched on 08/10/2026 from the cited instrument and not yet second-checked; "
                              f"what the transcription left out is listed in its module docstring and in "
                              f"openhamshack-nb docs/ham/countries/{iso}.md")
        status_of[rid] = reg["status"]
        write(a.out, f"regimes/{rid}.json", reg)
    for pid, mod in plans.PLANS.items():
        # The RSGB plan's 13 cm, 9 cm and 6 cm rows were not in the line by
        # line check (licence-check.md covers 160 m-10 m, 2 m, 70 cm and
        # 23 cm), and the IARU plans have had none, so no plan is `checked`.
        write(a.out, f"plans/{pid}.json", plan_json(mod, "researched"))
    write(a.out, "satellite/qo100-nb.json", qo100_nb(nb))
    write(a.out, "satellite/qo100-wb.json", qo100_wb(box))
    write(a.out, "repeaters.json", repeaters_json(plans, repeaters, importlib.import_module("ohsradio.bands")))
    write(a.out, "presets.json", presets_json(nb))
    write(a.out, "vocab.json", vocab_json(licences))
    write(a.out, "countries.json", countries_json(countries, status_of))
    write(a.out, "regions.json", dict({"schema": SCHEMA}, **region_rules(countries)))
    (a.out / "cty").mkdir()
    for name in ("cty.dat", "cty.dat-copyright.txt", "cty-source.txt"):
        shutil.copyfile(a.nb / "ohscore" / "data" / name, a.out / "cty" / name)

    files = {}
    for p in sorted(a.out.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files[p.relative_to(a.out).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    write(a.out, "manifest.json", {
        "schema": SCHEMA, "dataVersion": DATA_VERSION, "generatedBy": GENERATED_BY,
        "generatedFrom": {"openhamshack-nb": a.nb_commit or git_head(a.nb), "openhamshack": a.box_commit or git_head(a.box),
                          "overrides": {p.relative_to(OVERRIDES.parent.parent).as_posix():
                                        hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in sorted(OVERRIDES.rglob("*.json"))}},
        "regimes": sorted(licences.REGIMES), "plans": sorted(plans.PLANS), "satellites": ["qo100-nb", "qo100-wb"],
        "files": files,
    })
    print(f"wrote {len(files) + 1} files to {a.out} (dataVersion {DATA_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
