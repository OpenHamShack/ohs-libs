"""Equivalence before retirement (BANDPLAN-DATA.md §9): the same grid of
signals through OpenHamShack NB's Python guards and through shack_bandplan
must give the same allow/refuse and the same power limit. Only when this
holds may ohscore/licences and ohsradio/plans be retired from their apps.
(The same check against Classic's openhamshacklib/bands.py - the QO-100 WB
guard and channel table - ran here until the family rename of 21/10/2026
archived and removed Classic's own repo; there is nothing left to stay
equivalent with, so that half of this file went with it.)

Needs the OpenHamShack NB source (read-only): SHACK_OPENHAMSHACK_NB (default
~/openhamshack-nb). Skipped where it is not checked out, so the suite still
runs on a build host without it.

Differences that are decisions, not drift, and so are excluded:
- QO-100's uplinks (2400.0-2410.0 MHz) are refused to terrestrial signals
  by the dataset's satellite plans; OpenHamShack HAM's guard did not know them.
- Text: the sentences say OpenHamShack and "the station profile".
- The reserved segments ohs-libs transcribed from the plans' PDFs on
  09/10/2026 (data/bandplan-overrides), which ohsradio's plan modules never
  carried: INTENDED below. The grid compares only outside them, and
  test_intended_differences proves each one is a difference - ohsradio
  allowed it, the dataset refuses it as plan_reserved - so a correction
  is a listed decision, never drift that happens to be excused.

Corrections that change no allow/refuse under plan_guard "off" (2321-2322
MHz at 20 kHz, the 3 cm and LF/MF rows, the 630 m advisory) need no entry:
this test does not judge the plan's codes of practice.
"""
import os
import sys
from pathlib import Path

import pytest

import shack_bandplan as bp

NB = Path(os.environ.get("SHACK_OPENHAMSHACK_NB", Path.home() / "openhamshack-nb"))
pytestmark = pytest.mark.skipif(not (NB / "ohsradio" / "guard.py").is_file(), reason="openhamshack-nb is not checked out")

DATA = bp.load()
TODAY = "2026-10-09"

# plan -> (loHz, hiHz, why): reserved segments the dataset has and ohsradio's
# plan of the same id does not. Each cites the override row it comes from.
INTENDED = {
    "rsgb-2026": [
        (2_320_750_000, 2_320_800_000, "13 cm local beacons (R1 p.25)"),
        (2_320_990_000, 2_321_000_000, "13 cm beacons exclusive to 2321 MHz, the IARU column (R1 p.25)"),
        (2_400_000_000, 2_450_000_000, "13 cm satellites and ATV repeater outputs (R1 p.25)"),
        (3_400_750_000, 3_400_800_000, "9 cm local beacons (R1 p.26)"),
        (5_650_000_000, 5_668_000_000, "6 cm satellite uplinks (R1 p.27)"),
        (5_760_750_000, 5_760_995_000, "6 cm local and propagation beacons (R1 p.27)"),
        (5_830_000_000, 5_850_000_000, "6 cm satellite downlinks (R1 p.27)"),
        (10_368_750_000, 10_368_995_000, "3 cm local and propagation beacons (R1 p.28)"),
    ],
    "iaru-r1": [
        (10_368_800_000, 10_368_990_000, "3 cm beacons (I2 p.51)"),
    ],
}


def intended(plan_id: str, lo: float, hi: float) -> bool:
    return any(lo < b and hi > a for a, b, _ in INTENDED.get(plan_id, []))


@pytest.fixture(scope="module")
def openhamshack():
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(NB)]
    try:
        from ohscore import licences
        from ohsradio import guard, plans
        from ohsnb import bandplan
        yield licences, guard, plans, bandplan
    finally:
        del sys.path[:1]


def edges(reg: dict) -> list[float]:
    pts = set()
    for r in reg["allocations"] + reg["channelled"] + reg["refused"]:
        for f in (r["loHz"], r["hiHz"]):
            pts.update((f - 1000, f + 1000))
        pts.add((r["loHz"] + r["hiHz"]) // 2)
    for ch in reg["channelled"]:
        for a, b in ch["segments"]:
            pts.update((a + 1500, b - 1500))
    return sorted(p for p in pts if p > 0)


def regime_cases():
    for rid, reg in DATA.regimes.items():
        yield rid


@pytest.mark.parametrize("rid", list(regime_cases()))
def test_licence_and_reserved_segments_match_ohsradio(openhamshack, rid):
    licences, guard, plans, _nb = openhamshack
    reg = DATA.regimes[rid]
    mod = licences.get(rid)
    iso = reg["countries"][0]
    country = DATA.by_iso.get(iso)
    region = country["region"] if country else 1
    plan = plans.get(plans.choose("auto", country.get("plan", "") if country else "", getattr(mod, "PLAN", ""),
                                  region))
    mismatches = []
    n = 0
    for cls in mod.CLASSES:
        ctx = bp.context({"country": iso, "iaruRegion": region, "licence": {"regime": rid, "class": cls}}, DATA,
                         today=TODAY)
        assert ctx.plan["id"] == plan.ID
        for f in edges(reg):
            for width in (0.0, 2700.0):
                lo, hi = f - width / 2, f + width / 2
                if lo < 2_410_000_000 and hi > 2_400_000_000:
                    continue
                if intended(plan.ID, lo, hi):
                    continue
                for mode in (None, "usb", "fm"):
                    n += 1
                    old = guard.licence_check(cls, lo, hi, None, lic=mod, mode=mode, region=region, today=TODAY)
                    if old.ok:
                        old_plan = guard.plan_check(mode or "usb", lo, hi, guard="off", plan=plan)
                        old_ok = old_plan.ok
                    else:
                        old_ok = False
                    new = bp.check(ctx, bp.Signal(f, width, mode=mode), plan_guard="off")
                    old_lim = (old.limit_w, old.limit_kind) if old_ok and old.limit_w is not None else None
                    new_lim = (new["limit"]["watts"], new["limit"]["unit"]) if new["limit"] else None
                    if old_ok != new["allowed"] or (old_ok and old_lim != new_lim):
                        mismatches.append((cls, f, width, mode, old.refused or (old_ok, old_lim),
                                           new["refusal"] and new["refusal"]["text"], new_lim))
    assert n > 100
    assert not mismatches, f"{len(mismatches)} of {n} differ, e.g. {mismatches[:5]}"


def _regime_allowing(plan_id: str, hz: float):
    """A regime whose plan is `plan_id` and whose licence allows `hz`, with
    the class that may: the differences are the plan's, so the licence must
    not refuse first (a plan_reserved refusal means the licence passed)."""
    for rid, reg in DATA.regimes.items():
        for cls in [c["id"] for c in reg["classes"]]:
            c = bp.context({"country": reg["countries"][0], "licence": {"regime": rid, "class": cls}}, DATA,
                           today=TODAY)
            if not c.plan or c.plan["id"] != plan_id:
                continue
            v = bp.check(c, bp.Signal(hz, 0), plan_guard="off")
            if v["allowed"] or v["refusal"]["code"] == "plan_reserved":
                return rid, cls, c
    raise AssertionError(f"no regime under {plan_id} allows {hz} Hz")


@pytest.mark.parametrize("plan_id,lo,hi,why", [(p, a, b, w) for p, rows in INTENDED.items() for a, b, w in rows],
                         ids=[w for rows in INTENDED.values() for _a, _b, w in rows])
def test_intended_differences(openhamshack, plan_id, lo, hi, why):
    licences, guard, plans, _nb = openhamshack
    hz = (lo + hi) / 2
    rid, cls, c = _regime_allowing(plan_id, hz)
    old_lic = guard.licence_check(cls, hz, hz, None, lic=licences.get(rid), mode="cw", region=c.region, today=TODAY)
    assert old_lic.ok, (rid, cls, old_lic.refused)
    assert guard.plan_check("cw", hz, hz, guard="off", plan=plans.get(plan_id)).ok, f"ohsradio already refused {why}"
    v = bp.check(c, bp.Signal(hz, 0, mode="cw"), plan_guard="off")
    assert not v["allowed"] and v["refusal"]["code"] == "plan_reserved", (why, v["refusal"])


def test_qo100_nb_plan_matches_ohsnb(openhamshack):
    _l, _g, _p, nb = openhamshack
    ctx = bp.context({"callsign": "GM4BRI", "licence": {"regime": "auto", "class": "full"}}, DATA, today=TODAY)
    # One mode per NB class mapping that changed vocabulary (am under ssb,
    # freedv and sstv under digital), plus the plain ones.
    modes = ["usb", "am", "cw", "ft8", "freedv", "sstv"]
    mismatches, n = [], 0
    # 250 Hz steps land on every segment edge (all are multiples of 500 Hz).
    for step in range(-12, 2012):
        dl_centre = nb.DL_LO + step * 250.0
        for width in (55.0, 500.0, 2400.0, 2700.0, 3000.0):
            for mode in modes:
                n += 1
                lo, hi = dl_centre - width / 2, dl_centre + width / 2
                old_ok = nb.tx_blocked(lo, hi, mode) == ""
                v = bp.check(ctx, bp.Signal(nb.ul(dl_centre), width, mode=mode, purpose="satellite-uplink",
                                            satellite="qo100-nb"))
                if old_ok != v["allowed"]:
                    mismatches.append((dl_centre, width, mode, nb.tx_blocked(lo, hi, mode),
                                       v["refusal"] and v["refusal"]["text"]))
    assert not mismatches, f"{len(mismatches)} of {n} differ, e.g. {mismatches[:5]}"


# The QO-100 WB guard and channel table were checked here against Classic's
# openhamshacklib/bands.py (the box's own code) until its repo was archived
# and removed in the family rename of 21/10/2026 - nothing is left to stay
# equivalent with, so those two tests (test_qo100_wb_guard_matches_the_box,
# test_qo100_wb_channels_match_the_box) went with it.
