"""May this station transmit this signal? (BANDPLAN-DATA.md §6.)

A port of openhamshack-nb ohsradio/guard.py (licence_check, plan_check),
ohscore/countries.py (in_force), ohscore/licences (satellite_uplink),
ohsnb/bandplan.py (tx_blocked) and the box's openhamshacklib/bands.py
(qo100_blocked) onto the JSON dataset, with their semantics unchanged:

- Both edges of the occupied signal are judged, never the dial.
- The licence is law and always enforced; a frequency outside every
  allocation is refused, whether or not the regime is partial.
- The band plan is a code of practice: warn, enforce or off, except its
  reserved segments (beacons, satellites, protected), always refused.
- Power is advisory until measured (Brian, 05/10/2026): an estimate over
  the limit warns, a measured excess refuses.
- Satellite plans' reserved segments are always refused.

Every function is pure: `context()` resolves the profile once, `check()`
reads nothing but its arguments. tests/test_conformance.py runs the same
grid through ohsradio's guard and this module and requires identical
allow/refuse and limits, which is the "equivalence before retirement" of
BANDPLAN-DATA.md §9.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date

from . import cty as _cty
from .data import Dataset, bundled
from .messages import mhz, message, span, watts

EPS = 1e-6
OCCUPIED_FACTOR = 1.35          # DVB-S/S2 roll-off 0.35, the widest (openhamshacklib/bands.py OCCUPIED_FACTOR)
CUSTOM = "custom"
AUTO = "auto"
CUSTOM_CLASS = "own"


def dvb_occupied_hz(sr_ks: float) -> int:
    """Occupied width of a DVB-S/S2 signal at `sr_ks` kS/s: the symbol
    rate times 1.35 (PlutoDVB2 has no roll-off control, so the safe 0.35)."""
    return int(round(float(sr_ks) * 1000 * OCCUPIED_FACTOR))


# ---------------------------------------------------------------- the station
@dataclass
class Context:
    """What the profile resolves to, once (§6.1 `bp.context`)."""
    data: Dataset
    profile: dict
    today: str
    entity: str = ""
    prefix: str = ""
    country_iso: str = ""
    country: dict | None = None
    region: int | None = None
    at_sea: bool = False
    regime: dict | None = None
    regime_id: str = ""
    cls: str = ""
    why_none: dict | None = None        # the refusal when no regime is in force
    conditions: dict = field(default_factory=dict)
    permits: dict = field(default_factory=dict)   # id -> profile entry
    plan: dict | None = None
    notes: list = field(default_factory=list)

    @property
    def class_name(self) -> str:
        if self.regime is None:
            return self.cls
        return next((c["name"] for c in self.regime["classes"] if c["id"] == self.cls), self.cls)


def _custom_regime(segments: list, country: str) -> dict:
    """The operator's own licence as a regime (ohscore/licences custom()),
    from the profile's licence.custom ({lo, hi, watts, unit, satellite, note})."""
    where = f" in {country}" if country else ""
    allocs, sats = [], []
    for s in sorted(segments, key=lambda s: float(s["lo"])):
        lo, hi = int(round(float(s["lo"]))), int(round(float(s["hi"])))
        allocs.append({"loHz": lo, "hiHz": hi,
                       "limits": {CUSTOM_CLASS: [{"watts": float(s["watts"]), "unit": s.get("unit", "PEP")}]},
                       "note": str(s.get("note", "")), "src": "entered by the operator"})
        if s.get("satellite"):
            sats.append({"loHz": lo, "hiHz": hi, "direction": "both", "src": "entered by the operator"})
    return {"schema": 1, "id": CUSTOM, "title": f"your own licence{where}", "short": "Your own licence",
            "authority": "", "countries": [], "plan": "", "status": "checked", "partial": False,
            "classes": [{"id": CUSTOM_CLASS, "name": "Your"}], "edges": "closed", "allocations": allocs,
            "channelled": [], "emissions": [], "refused": [], "permits": [], "conditions": [],
            "satellite": sats, "notes": []}


def region_of(data: Dataset, ent: _cty.Entity | None) -> int | None:
    if ent is None:
        return None
    r = data.regions
    if ent.prefix in r["region1InAsia"]:
        return 1
    if ent.prefix in r["region3InAfrica"]:
        return 3
    if ent.prefix in r["region2InOceania"]:
        return 2
    if ent.prefix in data.by_entity:
        return data.by_entity[ent.prefix]["region"]
    return r["byContinent"].get(ent.continent)


def _choose_plan(data: Dataset, ctx: Context, override: str = "") -> dict | None:
    """ohsradio/plans choose(): the operator's choice, the country's national
    plan, the regime's own plan, the region's IARU plan, IARU R1."""
    regional = {1: "iaru-r1", 2: "iaru-r2", 3: "iaru-r3"}
    cands = [override, (ctx.country or {}).get("plan", ""), (ctx.regime or {}).get("plan", ""),
             regional.get(ctx.region or 0, ""), "iaru-r1"]
    for pid in cands:
        if pid and pid in data.plans:
            return data.plans[pid]
    return None


def context(profile: dict, data: Dataset | None = None, today: str | None = None, plan: str = "") -> Context:
    """Resolve a station profile (SERVICES.md §2.1) against the dataset."""
    data = data or bundled()
    today = today or _date.today().isoformat()
    ctx = Context(data, profile, today)
    lic = profile.get("licence") or {}
    ctx.cls = lic.get("class", "") or ""
    countries = _cty.load(data.cty_path)
    call = _cty.normalise(profile.get("callsign", ""))
    ent = None
    if profile.get("dxccPrefix"):
        ent = countries.by_prefix.get(profile["dxccPrefix"])
    elif call and _cty.valid(call):
        if _cty.location_part(call) is None:
            ctx.at_sea = True
        else:
            ent = countries.lookup(call)
    if ent is not None:
        ctx.entity, ctx.prefix = ent.name, ent.prefix
    ctx.prefix = profile.get("dxccPrefix") or ctx.prefix
    if profile.get("country"):
        ctx.country_iso = profile["country"].upper()
        ctx.country = data.by_iso.get(ctx.country_iso)
    elif ent is not None and ent.prefix in data.by_entity:
        ctx.country = data.by_entity[ent.prefix]
        ctx.country_iso = ctx.country["iso"]
    ctx.region = profile.get("iaruRegion") or region_of(data, ent) or (ctx.country or {}).get("region")
    where = (ctx.country or {}).get("name") or ctx.entity or ctx.country_iso

    choice = lic.get("regime") or AUTO
    if choice == CUSTOM:
        segs = lic.get("custom") or []
        ctx.regime_id = CUSTOM
        if not segs:
            ctx.why_none = message("no_regime", "no_segments")
        else:
            ctx.regime = _custom_regime(segs, where)
            ctx.cls = CUSTOM_CLASS
    else:
        if choice == AUTO:
            rid = (ctx.country or {}).get("regimes", [""])[0]
            if not rid:
                if ctx.at_sea and not profile.get("country"):
                    ctx.why_none = message("at_sea", "at_sea")
                elif where:
                    ctx.why_none = message("no_regime", "no_regime_in", country=where)
                else:
                    ctx.why_none = message("no_country", "no_country")
        else:
            rid = choice
        if ctx.why_none is None:
            ctx.regime_id = rid
            ctx.regime = data.regimes.get(rid)
            if ctx.regime is None:
                ctx.why_none = message("no_regime", "unknown_regime", regime=rid)

    # Conditions: the operator's explicit answer, else what the DXCC
    # prefix says (GM is Scotland), else the default - the restrictive side
    # unless a decision says otherwise (BANDPLAN-DATA.md §4.4).
    answers = profile.get("conditions") or {}
    for c in (ctx.regime or {}).get("conditions", []):
        k = c["key"]
        if k in answers:
            ctx.conditions[k] = bool(answers[k])
        elif c.get("derive", {}).get("dxccPrefix") and ctx.prefix:
            ctx.conditions[k] = ctx.prefix in c["derive"]["dxccPrefix"]
        else:
            ctx.conditions[k] = bool(c["default"])
    for nov in profile.get("novs") or []:
        if nov.get("validTo") and today > nov["validTo"]:
            continue
        ctx.permits[nov.get("id", "")] = nov
    ctx.plan = _choose_plan(data, ctx, plan)
    return ctx


# ---------------------------------------------------------------- the signal
@dataclass
class Signal:
    """A transmission as the guard judges it. `occupied_bw_hz` sets the
    licence edges; `plan_bw_hz` (default the same) sets a satellite plan's
    edges where its edgeRule is symbolRate (QO-100 WB)."""
    centre_hz: float
    occupied_bw_hz: float = 0.0
    mode: str | None = None
    power_w: float | None = None            # PEP at the antenna; None: power not judged
    power_at: str = "antenna"
    measured: bool = False
    purpose: str = "terrestrial"            # terrestrial | satellite-uplink
    satellite: str | None = None            # dataset satellite id when purpose is satellite-uplink
    plan_bw_hz: float | None = None
    antenna_gain_dbi: float | None = None   # None: the profile's figure for the band, else 0
    # One signal's own width when it is known but not where inside its
    # edges it will sit: FT8's 55 Hz somewhere in a 3 kHz dial window, put
    # there by another program through a loopback device. The plans'
    # bandwidth limits judge this width, every position check still the
    # edges; 0 judges reach alone (nothing is known of the width). The
    # licence's own width limits always judge the edges. None: the edges'
    # width. openhamshack-nb's two guards worked this way (ohsnb/bandplan.py
    # tx_blocked width/widths, ohsradio/guard.py plan_check width); without
    # it, PTT for WSJT-X was refused in every 500 Hz segment.
    signal_bw_hz: float | None = None

    @classmethod
    def edges(cls, lo_hz: float, hi_hz: float, **kw) -> "Signal":
        """A signal by its occupied edges (an SSB signal is not centred on
        its dial)."""
        lo, hi = sorted((lo_hz, hi_hz))
        return cls(centre_hz=(lo + hi) / 2, occupied_bw_hz=hi - lo, **kw)

    @property
    def lo(self) -> float:
        return self.centre_hz - self.occupied_bw_hz / 2

    @property
    def hi(self) -> float:
        return self.centre_hz + self.occupied_bw_hz / 2


def power_in(unit: str, pep_w: float, gain_dbi: float) -> float:
    """ERP is relative to a half-wave dipole (2.15 dBi), EIRP to isotropic."""
    if unit == "ERP":
        return pep_w * 10 ** ((gain_dbi - 2.15) / 10)
    if unit == "EIRP":
        return pep_w * 10 ** (gain_dbi / 10)
    return pep_w


def max_pep_for(unit: str, limit_w: float, gain_dbi: float) -> float:
    return limit_w / max(1e-12, power_in(unit, 1.0, gain_dbi))


def band_of(data: Dataset, hz: float) -> str:
    for b in data.vocab["bands"]:
        if b["loHz"] <= hz <= b["hiHz"]:
            return b["name"]
    return ""


def _in_force(row: dict, region: int | None, today: str) -> bool:
    if row.get("validFrom") and today < row["validFrom"]:
        return False
    if row.get("validTo") and today > row["validTo"]:
        return False
    if row.get("regions") and region is not None and region not in row["regions"]:
        return False
    return True


def _applicable(limits: list, mclass: str | None) -> list:
    return [lim for lim in limits if mclass is None or "modes" not in lim or mclass in lim["modes"]]


def _touch(lo: float, hi: float, a: float, b: float) -> bool:
    return lo < b and hi > a


class _Refused(Exception):
    def __init__(self, msg: dict):
        self.msg = msg


# ---------------------------------------------------------------- the check
def check(ctx: Context, sig: Signal, plan_guard: str = "warn") -> dict:
    """The verdict (BANDPLAN-DATA.md §6.2) for `sig` under `ctx`.
    `plan_guard`: warn | enforce | off for the band plan; the licence and
    every reserved segment are enforced whatever it says."""
    data = ctx.data
    v = {"allowed": True, "refusal": None, "warnings": [], "limit": None, "segment": None,
         "regime": ctx.regime_id or None, "class": ctx.cls or None,
         "dataVersion": data.data_version, "dataStatus": (ctx.regime or {}).get("status")}
    try:
        _check(ctx, sig, plan_guard, v)
    except _Refused as r:
        v["allowed"] = False
        v["refusal"] = r.msg
        # A refused verdict carries no limit or segment, except a measured
        # power refusal, whose limit is the point: one rule every port can
        # follow without reproducing the order in which this one learns
        # things.
        if r.msg["code"] != "power_measured_over":
            v["limit"] = None
        v["segment"] = None
    return v


def _check(ctx: Context, sig: Signal, plan_guard: str, v: dict) -> None:
    data = ctx.data
    lo, hi = sorted((sig.lo, sig.hi))
    sat_up = sig.purpose == "satellite-uplink"
    if ctx.why_none is not None:
        raise _Refused(ctx.why_none)
    reg = ctx.regime
    cls = ctx.cls
    if not cls:
        raise _Refused(message("no_class", "no_class"))
    if cls not in [c["id"] for c in reg["classes"]]:
        raise _Refused(message("no_class", "wrong_class", regime=reg["short"]))
    name = ctx.class_name
    mclass = data.mode_class(sig.mode)
    gain = sig.antenna_gain_dbi
    if gain is None:
        gain = float((ctx.profile.get("antennaGainDbi") or {}).get(band_of(data, sig.centre_hz), 0.0))

    def class_names(ids):
        return " or ".join(next((c["name"] for c in reg["classes"] if c["id"] == i), i) for i in ids)

    # Refused ranges, unless a permit the station holds opens one.
    held_spans = []        # (lo, hi, permit)
    for row in reg.get("refused", []):
        if not _touch(lo, hi, row["loHz"], row["hiHz"]):
            continue
        pid = row.get("permit")
        permit = next((p for p in reg.get("permits", []) if p["id"] == pid), None) if pid else None
        if permit is None:
            raise _Refused(message("refused_range", "data", src=row["src"], text=row["text"]))
        if pid not in ctx.permits:
            raise _Refused(message("permit_needed", "data", src=row["src"], text=row["text"]))
        if cls not in permit["classes"]:
            raise _Refused(message("class_not_allowed", "permit_class", src=permit["src"], **{
                "class": name, "permit": permit["label"], "who": class_names(permit["classes"])}))
        p_lo, p_hi, why = permit["loHz"], permit["hiHz"], None
        for var in permit.get("variants", []):
            if ctx.conditions.get(var["condition"], True) == var["when"]:
                p_lo, p_hi = var.get("loHz", p_lo), var.get("hiHz", p_hi)
                why = var
        # A variant narrows the permit (146.9375 MHz in Scotland): a signal
        # reaching past its edge is refused with the variant's sentence.
        if why is not None and (hi > p_hi + EPS or lo < p_lo - EPS):
            raise _Refused(message("edge_above" if hi > p_hi + EPS else "edge_below", "data", src=permit["src"],
                                   text=why["text"]))
        held_spans.append((p_lo, p_hi, permit))

    # Channelled allocations (60 m): the signal must sit inside one channel.
    for ch in reg.get("channelled", []):
        if not _touch(lo, hi, ch["loHz"], ch["hiHz"]):
            continue
        if cls not in ch["limits"]:
            raise _Refused(message("class_not_allowed", "channel_class", src=ch["src"], **{
                "class": name, "label": ch["label"], "who": class_names(ch["limits"])}))
        if not any(a <= lo and hi <= b for a, b in ch["segments"]):
            raise _Refused(message("outside_allocations", "channel_segment", src=ch["src"],
                                   label=ch["label"], where=ch["where"], span=span(lo, hi)))
        if ch.get("maxBwHz") is not None and hi - lo > ch["maxBwHz"] + EPS:
            raise _Refused(message("emission", "channel_width", src=ch["src"], label=ch["label"],
                                   max=f"{ch['maxBwHz'] / 1e3:g}", width=f"{(hi - lo) / 1e3:.1f}"))
        lims = ch["limits"][cls]
        first = lims[0]
        v["limit"] = {"watts": first["watts"], "unit": first["unit"],
                      "maxPepAtAntennaW": round(min(max_pep_for(x["unit"], x["watts"], gain) for x in lims), 3)}
        if sig.power_w is not None:
            for lim in lims:
                got = power_in(lim["unit"], sig.power_w, gain)
                if got > lim["watts"] * 1.0001:
                    _power_over(v, sig, name, lim, f"on {ch['label']}", got, ch["src"])
                    break
        _after_licence(ctx, sig, lo, hi, plan_guard, v, mclass, sat_up)
        return

    rows = sorted((r for r in reg["allocations"]
                   if (_touch(lo, hi, r["loHz"], r["hiHz"]) or (lo == hi and r["loHz"] <= lo <= r["hiHz"]))
                   and _in_force(r, ctx.region, ctx.today)), key=lambda r: r["loHz"])
    # The class must hold every part of [lo, hi], contiguously, from its
    # allocations and any permit it holds.
    pieces = [(r["loHz"], r["hiHz"], r) for r in rows if cls in r["limits"]]
    pieces += [(a, b, None) for a, b, _p in held_spans]
    pieces.sort(key=lambda p: p[0])
    covered, mine, used_permit = lo, [], False
    for a, b, r in pieces:
        if a > covered + EPS:
            break
        covered = max(covered, b)
        if r is None:
            used_permit = True
        else:
            mine.append(r)
    if (not mine and not used_permit) or covered < hi - EPS:
        if not rows and not held_spans:
            if reg["id"] == CUSTOM:
                raise _Refused(message("outside_allocations", "outside_own", span=span(lo, hi)))
            raise _Refused(message("outside_allocations", "not_amateur", span=span(lo, hi)))
        if rows and not any(cls in r["limits"] for r in rows) and not held_spans:
            where = rows[0]
            raise _Refused(message("class_not_allowed", "class_not_allowed", src=where["src"],
                                   **{"class": name, "span": span(where["loHz"], where["hiHz"])}))
        code = "edge_below" if pieces and lo < pieces[0][0] - EPS else "edge_above"
        raise _Refused(message(code, "left_allocation", src=(rows[0]["src"] if rows else ""),
                               **{"class": name, "span": span(lo, hi)}))
    for r in mine:
        if r.get("unconfirmed"):
            raise _Refused(message("unconfirmed", "unconfirmed", src=r["src"],
                                   span=span(r["loHz"], r["hiHz"]), regime=reg["short"]))
        if r.get("satelliteOnly") and not sat_up:
            raise _Refused(message("satellite_not_permitted", "satellite_only", src=r["src"],
                                   span=span(r["loHz"], r["hiHz"]), regime=reg["short"]))
    extra = []
    for c in reg.get("conditions", []):
        if "loHz" not in c or not _touch(lo, hi, c["loHz"], c["hiHz"]):
            continue
        if ctx.conditions.get(c["key"], c["default"]) != c.get("when", True):
            continue
        if "refuse" in c:
            raise _Refused(message("condition", "data", src=c["src"], text=c["refuse"]))
        extra.append((c["limit"], f"({c['label']})", c["src"]))
    if sig.mode is not None:
        for e in reg.get("emissions", []):
            if not _touch(lo, hi, e["loHz"], e["hiHz"]):
                continue
            allowed = e["modes"].get(cls)
            if allowed is None:
                continue
            if mclass not in allowed:
                raise _Refused(message("emission", "emission_mode", src=e["src"], **{
                    "class": name, "mode": sig.mode.upper(), "span": span(e["loHz"], e["hiHz"])}))
            if e.get("maxBwHz") is not None and hi - lo > e["maxBwHz"] + EPS:
                raise _Refused(message("emission", "emission_width", src=e["src"], **{
                    "class": name, "span": span(e["loHz"], e["hiHz"]), "max": f"{e['maxBwHz']:.0f}",
                    "width": f"{hi - lo:.0f}"}))

    # Power: the tightest limit among the rows touched, of those that apply
    # to the mode, and any a condition adds.
    cands = [(lim, f"on {span(r['loHz'], r['hiHz'])}", r["src"]) for r in mine
             for lim in _applicable(r["limits"][cls], mclass)]
    cands += [(lim, where, src) for lim, where, src in extra if mclass is None or "modes" not in lim
              or mclass in lim["modes"]]
    worst = None
    for lim, where, src in cands:
        allowed_pep = max_pep_for(lim["unit"], lim["watts"], gain)
        if worst is None or allowed_pep < worst[0]:
            worst = (allowed_pep, lim, where, src)
    if worst is not None:
        allowed_pep, lim, where, src = worst
        v["limit"] = {"watts": lim["watts"], "unit": lim["unit"], "maxPepAtAntennaW": round(allowed_pep, 3)}
        if sig.power_w is not None:
            got = power_in(lim["unit"], sig.power_w, gain)
            if got > lim["watts"] * 1.0001:
                _power_over(v, sig, name, lim, where, got, src)
    for _a, _b, permit in held_spans:
        if permit["limits"] is None and sig.power_w is not None:
            v["warnings"].append(message("power_not_judged", "power_not_judged", src=permit["src"],
                                         permit=permit["label"]))
        if permit.get("status") != "checked":
            v["warnings"].append(message("data_researched", "permit_researched", src=permit["src"],
                                         permit=permit["label"]))
    if sat_up:
        _satellite_licence(ctx, lo, hi, v)
    _after_licence(ctx, sig, lo, hi, plan_guard, v, mclass, sat_up)


def _power_over(v: dict, sig: Signal, name: str, lim: dict, where: str, got: float, src: str) -> None:
    unit = "" if lim["unit"] == "PEP" else " " + lim["unit"]
    params = {"class": name, "limit": f"{watts(lim['watts'])} {lim['unit']}", "where": where,
              "got": f"{watts(got)}{unit}"}
    if sig.measured:
        raise _Refused(message("power_measured_over", "power_measured_over", src=src, **params))
    v["warnings"].append(message("power_estimated_over", "power_estimated_over", src=src, **params))


def _satellite_licence(ctx: Context, lo: float, hi: float, v: dict) -> None:
    """ohscore/licences satellite_uplink: the regime must permit the
    amateur-satellite service Earth to space over the whole signal. A
    regime with no satellite table says nothing either way: a warning, as
    OpenHamShack NB treats it (radio.py _uplink_refusal: a check that cannot be
    made is not a check failed)."""
    reg = ctx.regime
    sats = reg.get("satellite")
    if sats is None:
        v["warnings"].append(message("satellite_unstated", "satellite_unstated", regime=reg["title"]))
        return
    covered = lo
    for s in sorted((s for s in sats if _touch(lo, hi, s["loHz"], s["hiHz"])), key=lambda s: s["loHz"]):
        if s["loHz"] > covered + EPS or s["direction"] == "down":
            break
        covered = max(covered, s["hiHz"])
    if covered < hi - EPS:
        raise _Refused(message("satellite_not_permitted", "satellite_not_permitted", regime=reg["title"],
                               span=span(lo, hi)))


def _after_licence(ctx: Context, sig: Signal, lo: float, hi: float, plan_guard: str, v: dict,
                   mclass: str | None, sat_up: bool) -> None:
    _satellite_plans(ctx, sig, v, mclass, sat_up)
    _band_plan(ctx, lo, hi, plan_guard, v, mclass, sat_up, sig.signal_bw_hz)
    reg = ctx.regime
    if reg.get("status") != "checked":
        v["warnings"].append(message("data_researched", "data_researched", regime=reg["short"]))


def _satellite_plans(ctx: Context, sig: Signal, v: dict, mclass: str | None, sat_up: bool) -> None:
    """Every satellite plan in the dataset applies to any signal touching
    its uplink (as the box's qo100_blocked judges every signal touching the
    transponders), and the addressed satellite's plan to a satellite uplink
    wherever it is."""
    data = ctx.data
    target = sig.satellite if sat_up else None
    if target and target not in data.satellites:
        raise _Refused(message("satellite_not_permitted", "satellite_unknown", satellite=target))
    # The addressed satellite's edge rule sets the width for every
    # transponder: the box judges a QO-100 WB signal's reach into the
    # narrowband transponder at the symbol rate too (qo100_blocked).
    bw = sig.occupied_bw_hz
    if target and data.satellites[target]["edgeRule"] == "symbolRate" and sig.plan_bw_hz is not None:
        bw = sig.plan_bw_hz
    lo, hi = sig.centre_hz - bw / 2, sig.centre_hz + bw / 2
    for sid, s in data.satellites.items():
        up = s["uplink"]
        touches = lo < up["hiHz"] - EPS and hi > up["loHz"] + EPS
        if sid != target:
            if touches:
                if target and target in data.satellites:
                    raise _Refused(message("satellite_reserved", "satellite_neighbour", src=s["source"]["key"],
                                           label=s["title"], uplink=_mhz_span(up["loHz"], up["hiHz"])))
                raise _Refused(message("satellite_reserved", "satellite_terrestrial", src=s["source"]["key"],
                                       title=s["title"], uplink=_mhz_span(up["loHz"], up["hiHz"])))
            continue
        if s["segmentsIn"] == "uplink":
            _judge_uplink_plan(s, lo, hi, touches, v)
        else:
            _judge_downlink_plan(s, sig, lo, hi, v, mclass)


def _mhz_span(a: float, b: float) -> str:
    return f"{mhz(a)}-{mhz(b)} MHz"


def _judge_uplink_plan(s: dict, lo: float, hi: float, touches: bool, v: dict) -> None:
    """QO-100 WB (openhamshacklib/bands.py qo100_blocked): edges at the symbol
    rate; below the users' segment is the beacon's, above it the top."""
    up = s["uplink"]
    key = s["source"]["key"]
    if not touches:
        raise _Refused(message("satellite_edge", "satellite_outside", src=key, span=_mhz_span(lo, hi),
                               title=s["title"], uplink=_mhz_span(up["loHz"], up["hiHz"])))
    users = [g for g in s["segments"] if g["modes"] is not None]
    u_lo = min(g["loHz"] for g in users)
    u_hi = max(g["hiHz"] for g in users)
    if lo < u_lo - EPS:
        seg = next(g for g in s["segments"] if g["loHz"] <= lo < g["hiHz"] or g["hiHz"] == u_lo)
        raise _Refused(message("satellite_reserved", "satellite_below", src=key, edge=f"{mhz(lo)} MHz",
                               title=s["title"], label=seg["label"]))
    if hi > u_hi + EPS:
        raise _Refused(message("satellite_edge", "satellite_above", src=key, edge=f"{mhz(hi)} MHz",
                               title=s["title"], top=f"{mhz(u_hi)} MHz"))
    seg = next((g for g in users if g["loHz"] <= (lo + hi) / 2 < g["hiHz"]), users[0])
    v["segment"] = {"plan": s["id"], "label": seg["label"]}


def _judge_downlink_plan(s: dict, sig: Signal, lo: float, hi: float, v: dict, mclass: str | None) -> None:
    """QO-100 NB (ohsnb/bandplan.py tx_blocked), in downlink Hz."""
    off = s["uplinkToDownlinkHz"]
    key = s["source"]["key"]
    dlo, dhi = lo + off, hi + off
    width = hi - lo if sig.signal_bw_hz is None else sig.signal_bw_hz
    if s.get("maxBwHz") is not None and width > s["maxBwHz"] + EPS:
        raise _Refused(message("satellite_mode", "satellite_too_wide", src=key, width=f"{width / 1000:.2f}",
                               title=s["title"], max=f"{s['maxBwHz'] / 1000:g}"))
    dl = s["downlink"]
    if dlo < dl["loHz"] or dhi > dl["hiHz"]:
        up = s["uplink"]
        raise _Refused(message("satellite_edge", "satellite_outside", src=key, span=_mhz_span(lo, hi),
                               title=s["title"], uplink=_mhz_span(up["loHz"], up["hiHz"])))
    words = {"ssb": "SSB voice", "am": "SSB voice", "cw": "CW", "digi": "Digital modes", "dv": "Digital modes",
             "image": "Digital modes"}
    for g in s["segments"]:
        if not ((dlo < g["hiHz"] - EPS and dhi > g["loHz"] + EPS) or (dlo == dhi and g["loHz"] <= dlo < g["hiHz"])):
            continue
        seg = _mhz_span(g["loHz"], g["hiHz"]).replace(" MHz", "")
        if g["modes"] is None:
            raise _Refused(message("satellite_reserved", "satellite_reserved", src=g["src"], label=g["label"],
                                   seg=seg))
        if mclass is not None and mclass not in g["modes"]:
            raise _Refused(message("satellite_mode", "satellite_mode", src=g["src"],
                                   what=words.get(mclass, (sig.mode or "").upper()), label=g["label"], seg=seg))
        if g["maxBwHz"] is not None and width > g["maxBwHz"] + EPS:
            raise _Refused(message("satellite_mode", "satellite_seg_width", src=g["src"], label=g["label"],
                                   max=f"{g['maxBwHz']:.0f}", width=f"{width:.0f}"))
        v["segment"] = {"plan": s["id"], "label": g["label"]}


def _band_plan(ctx: Context, lo: float, hi: float, guard: str, v: dict, mclass: str | None, sat_up: bool,
               signal_bw: float | None = None) -> None:
    """ohsradio/guard.py plan_check. A satellite uplink is judged by its
    satellite's plan instead, so the band plan's satellite segments (which
    keep terrestrial stations off) do not refuse it; its beacons still do."""
    plan = ctx.plan
    if plan is None:
        return
    segs = [s for s in plan["segments"]
            if (lo < s["hiHz"] - EPS and hi > s["loHz"] + EPS) or (lo == hi and s["loHz"] <= lo < s["hiHz"])]
    reserved = ctx.data.vocab.get("reservedKinds", ["beacon", "satellite", "protected"])
    for s in segs:
        if s["kind"] in reserved and not (sat_up and s["kind"] == "satellite"):
            raise _Refused(message("plan_reserved", "plan_reserved", src=s["src"], label=s["label"],
                                   span=span(s["loHz"], s["hiHz"])))
    if sat_up:
        return
    if segs and v["segment"] is None:
        v["segment"] = {"plan": plan["id"], "label": segs[0]["label"]}
    for g in plan.get("gaps", []):
        if _touch(lo, hi, g["loHz"], g["hiHz"]) or (lo == hi and g["loHz"] <= lo < g["hiHz"]):
            v["warnings"].append(message("plan_gap", "plan_gap", src=g["why"], plan=plan["short"],
                                         span=span(max(lo, g["loHz"]), min(hi, g["hiHz"])) if lo < hi
                                         else span(g["loHz"], g["hiHz"])))
            break
    # Where the source itself prints nothing the plan has no opinion, which
    # the operator should know as much as a transcription gap: same
    # warning position, its own sentence, and the file is not partial.
    for g in plan.get("unplanned", []):
        if _touch(lo, hi, g["loHz"], g["hiHz"]) or (lo == hi and g["loHz"] <= lo < g["hiHz"]):
            v["warnings"].append(message("plan_unplanned", "plan_unplanned", src=g["why"], plan=plan["short"],
                                         span=span(max(lo, g["loHz"]), min(hi, g["hiHz"])) if lo < hi
                                         else span(g["loHz"], g["hiHz"])))
            break
    if guard == "off":
        return
    fallback = ctx.data.vocab.get("fallbackClass", {})
    words = ctx.data.vocab.get("classWord", {})
    width = hi - lo if signal_bw is None else signal_bw
    # A plan's recommendation that is not one of its rows (RSGB 630 m
    # Note-1: 472-475 kHz CW only at 200 Hz, inside a 472-479 kHz row at
    # 500 Hz). Encoding it as a row would refuse under "enforce" what the
    # plan only recommends (session decision, 09/10/2026), so it warns
    # under warn and enforce alike, and never refuses.
    for a in plan.get("advisories", []):
        if not (_touch(lo, hi, a["loHz"], a["hiHz"]) or (lo == hi and a["loHz"] <= lo < a["hiHz"])):
            continue
        off_mode = mclass is not None and a["modes"] is not None and mclass not in a["modes"]
        too_wide = a["maxBwHz"] is not None and width > a["maxBwHz"] + EPS
        if off_mode or too_wide:
            v["warnings"].append(message("plan_advice", "data", src=a["src"], text=a["text"]))
    why = None
    for s in segs:
        if s["modes"] is not None and not s["modes"]:
            why = ("plan_mode", "plan_output", {"span": span(s["loHz"], s["hiHz"]), "label": s["label"]}, s["src"])
            break
        seg_modes = s["modes"] or []
        if mclass is None:
            m_ok = True
        else:
            m_ok = mclass in seg_modes or (fallback.get(mclass) in seg_modes and not
                                           any(m in seg_modes for m in ("datv", "atv")))
        if not m_ok:
            why = ("plan_mode", "plan_mode", {"what": words.get(mclass, (mclass or "").upper()), "label": s["label"],
                                              "span": span(s["loHz"], s["hiHz"])}, s["src"])
            break
        if s["maxBwHz"] is not None and width > s["maxBwHz"] + EPS:
            why = ("plan_bandwidth", "plan_bandwidth", {"label": s["label"], "span": span(s["loHz"], s["hiHz"]),
                                                       "max": f"{s['maxBwHz']:.0f}", "width": f"{width:.0f}"},
                   s["src"])
            break
    if why is None:
        return
    code, key, params, src = why
    msg = message(code, key, src=src, **params)
    if guard == "enforce":
        raise _Refused(msg)
    v["warnings"].append(msg)


# ---------------------------------------------------------------- other questions
def in_force(profile: dict, data: Dataset | None = None, today: str | None = None) -> dict:
    """The licence in force and why not (SERVICES.md §2.2 `derived`)."""
    ctx = context(profile, data, today)
    why = ""
    if ctx.why_none is not None:
        why = ctx.why_none["text"]
    elif not ctx.cls:
        why = message("no_class", "no_class")["text"]
    elif ctx.cls not in [c["id"] for c in ctx.regime["classes"]]:
        why = message("no_class", "wrong_class", regime=ctx.regime["short"])["text"]
    return {"entity": ctx.entity, "country": ctx.country_iso, "region": ctx.region,
            "regimeId": ctx.regime_id, "regimeShort": (ctx.regime or {}).get("short", ""),
            "className": ctx.class_name if ctx.regime else "", "canTransmit": not why, "why": why,
            "plan": (ctx.plan or {}).get("id", ""), "bandplanDataVersion": ctx.data.data_version}


def limits(ctx: Context, lo_hz: float, hi_hz: float, mode: str | None = None) -> dict:
    """Tuning bounds and shading over [lo, hi]: where this class may
    transmit (with the limit) and the plan's segments there."""
    spans = []
    reg = ctx.regime
    if reg is not None and ctx.cls:
        mclass = ctx.data.mode_class(mode)
        for r in reg["allocations"]:
            if _touch(lo_hz, hi_hz, r["loHz"], r["hiHz"]) and ctx.cls in r["limits"] \
                    and _in_force(r, ctx.region, ctx.today) and not r.get("unconfirmed") \
                    and not r.get("satelliteOnly"):
                lims = _applicable(r["limits"][ctx.cls], mclass) or r["limits"][ctx.cls]
                tight = min(lims, key=lambda x: max_pep_for(x["unit"], x["watts"], 0.0))
                spans.append({"loHz": r["loHz"], "hiHz": r["hiHz"], "limit": tight, "src": r["src"]})
        for ch in reg.get("channelled", []):
            if ctx.cls in ch["limits"]:
                for a, b in ch["segments"]:
                    if _touch(lo_hz, hi_hz, a, b):
                        spans.append({"loHz": a, "hiHz": b, "limit": ch["limits"][ctx.cls][0], "src": ch["src"]})
        for p in reg.get("permits", []):
            if p["id"] in ctx.permits and ctx.cls in p["classes"] and _touch(lo_hz, hi_hz, p["loHz"], p["hiHz"]):
                p_hi = p["hiHz"]
                for var in p.get("variants", []):
                    if ctx.conditions.get(var["condition"], True) == var["when"]:
                        p_hi = var.get("hiHz", p_hi)
                spans.append({"loHz": p["loHz"], "hiHz": p_hi,
                              "limit": (p["limits"] or [None])[0], "src": p["src"]})
        for c in reg.get("conditions", []):
            if "refuse" in c and "loHz" in c and ctx.conditions.get(c["key"], c["default"]) == c.get("when", True):
                spans = [s for s in spans if not (s["loHz"] >= c["loHz"] and s["hiHz"] <= c["hiHz"])]
    spans.sort(key=lambda s: s["loHz"])
    segs = [s for s in (ctx.plan or {}).get("segments", []) if _touch(lo_hz, hi_hz, s["loHz"], s["hiHz"])]
    return {"allowed": spans, "segments": segs, "plan": (ctx.plan or {}).get("id", ""),
            "dataVersion": ctx.data.data_version}


def bands(ctx: Context) -> list:
    """Named bands (ADIF names): whether this class has any of it, the
    highest limit there, and the plan in force."""
    out = []
    for b in ctx.data.vocab["bands"]:
        lim = limits(ctx, b["loHz"], b["hiHz"])
        top = None
        for s in lim["allowed"]:
            if s["limit"] and (top is None or s["limit"]["watts"] > top["watts"]):
                top = s["limit"]
        out.append({"name": b["name"], "loHz": b["loHz"], "hiHz": b["hiHz"], "allowed": bool(lim["allowed"]),
                    "limit": top, "plan": lim["plan"]})
    return out


def channels(data: Dataset | None, satellite: str, sr_ks: float) -> list:
    """A satellite's channels for a symbol rate (openhamshacklib/bands.py
    qo100_channels): uplink and downlink centres, and whether it is where
    that rate should go first."""
    data = data or bundled()
    s = data.satellites[satellite]
    sr = round(float(sr_ks))
    down = []
    for c in s.get("channels", []):
        if sr in c["srKs"]:
            if "downlinkHz" in c:
                down = list(c["downlinkHz"])
            else:
                down = [c["downlinkFirstHz"] + c["stepHz"] * k for k in range(c["count"])]
            break
    pref = s.get("preferred", {})
    off = s["uplinkToDownlinkHz"]
    return [{"downlinkHz": d, "uplinkHz": d - off,
             "preferred": sr > pref.get("belowKs", 0) or d > pref.get("aboveDownlinkHz", 0)} for d in down]


def presets(ctx: Context, kind: str) -> list:
    """Default channels for an app kind, most specific first: the
    station's country, then its region, then everywhere. Each carries
    `enabled` and, when disabled, the verdict's sentence: never hidden
    (BANDPLAN-DATA.md §4.6)."""
    entries = [e for e in ctx.data.presets if e["kind"] == kind]
    pick = (next((e for e in entries if e.get("country") == ctx.country_iso and ctx.country_iso), None)
            or next((e for e in entries if e.get("region") == ctx.region and ctx.region), None)
            or next((e for e in entries if e.get("everywhere")), None))
    if pick is None:
        return []
    out = []
    for p in pick["presets"]:
        row = dict(p)
        if "txHz" in p:
            sat = p.get("satellite")
            verdict = check(ctx, Signal(p["txHz"], 0.0, mode=p.get("mode"),
                                        purpose="satellite-uplink" if sat else "terrestrial", satellite=sat))
            row["enabled"] = verdict["allowed"]
            row["why"] = verdict["refusal"]["text"] if verdict["refusal"] else ""
        else:
            row["enabled"], row["why"] = True, ""
        out.append(row)
    return out
