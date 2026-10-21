"""Every sentence the guard can say, whole, with named placeholders.

The rule carried from ohscore and ohsradio (I18N.md §1, rule 3): a sentence is
never assembled from fragments, so a catalog can translate it whole. Each
message has a stable key; a verdict carries the key, the params and the
English text, and `translate()` renders the same key in another language
from the `shack-bandplan` gettext domain when a catalog for it is installed.

Sentences that come from the dataset (a regime's refused range, a
condition's refusal, a permit variant) use the key "data": their msgid is
the English text itself, extracted into the same domain at build time
(I18N.md §2 "Dataset text ... is extracted into the shack-bandplan domain").

Wording is ohsradio/guard.py's and ohscore/countries.py's with two changes: the
product is OpenHamShack, not OpenHamShack, and the place to fix a setting is
"the station profile" (SERVICES.md §2), because every app shows the same
profile and no one app's Settings path is right for all of them.
"""
from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-bandplan"
LOCALEDIR = Path(__file__).resolve().parent / "locale"

T = {
    # who may transmit at all
    "no_class": ("No licence class chosen: transmit stays locked until you choose yours in the station profile"),
    "wrong_class": ("Your licence class belongs to a different licence than {regime}: choose your class again in "
                    "the station profile"),
    "no_regime": ("No licence is set up for this station's country yet: transmit stays locked until you choose a "
                  "licence or enter your own in the station profile"),
    "no_regime_in": ("No licence for {country} is written into OpenHamShack yet: transmit stays locked until you "
                     "choose a licence or enter your own licence's segments and power limits in the station profile"),
    "no_country": ("The station's country is not known: transmit stays locked until you set your callsign or "
                   "choose the country in the station profile, or enter your own licence"),
    "at_sea": ("Maritime or aeronautical mobile (/MM, /AM): no country's licence applies by itself. Transmit stays "
               "locked until you choose the licence you operate under in the station profile"),
    "no_segments": ("Your own licence has no segments yet: transmit stays locked until you enter them in the "
                    "station profile"),
    "unknown_regime": ("The licence {regime} is not one this version of OpenHamShack knows: choose yours again in "
                       "the station profile"),
    # where
    "not_amateur": "{span} is not an amateur band",
    "outside_own": ("{span} is outside every segment of your own licence: add the segment in the station profile "
                    "if your licence allows it"),
    "class_not_allowed": "{class} licence: {span} is not available",
    "left_allocation": "{class} licence: the signal would leave the allocation ({span})",
    "channel_class": "{class} licence: {label} is open only to {who} licensees",
    "channel_segment": "{label}: the signal must lie inside one of {where} ({span} does not)",
    "channel_width": "{label} allows {max} kHz at most; this signal is {width} kHz",
    "unconfirmed": ("{span} is marked unconfirmed in OpenHamShack's data for {regime}: it stays refused until it "
                    "has been checked against the licence"),
    "satellite_only": "{regime} allows {span} for the amateur-satellite service only, and this is not a satellite uplink",
    "permit_class": "{class} licence: the {permit} is open only to {who} licensees",
    # emissions
    "emission_mode": "{class} licence: {mode} is not permitted on {span}",
    "emission_width": "{class} licence: {span} allows signals up to {max} Hz; this one is {width} Hz",
    # satellites
    "satellite_unstated": "{regime} as written here says nothing about the amateur-satellite service",
    "satellite_not_permitted": "{regime} does not permit an amateur-satellite uplink on {span}",
    "satellite_unknown": "{satellite} is not a satellite in OpenHamShack's data",
    "satellite_outside": "{span} is outside the {title} ({uplink} up)",
    "satellite_neighbour": "The signal reaches the {label} ({uplink})",
    "satellite_reserved": "The signal would reach the {label} segment ({seg}), where nobody transmits",
    "satellite_mode": "{what} is not allowed in the {label} segment ({seg})",
    "satellite_seg_width": "The {label} segment allows {max} Hz at most; this signal is {width} Hz wide",
    "satellite_too_wide": "The signal is {width} kHz wide; the {title} allows {max} kHz at most",
    "satellite_below": "The signal reaches down to {edge}, into the {title}'s {label} segment",
    "satellite_above": "The signal reaches {edge}, above the top of the {title} ({top})",
    "satellite_terrestrial": ("The signal reaches the {title}'s uplink ({uplink}); only a satellite uplink to it "
                              "may transmit there"),
    # band plan
    "plan_reserved": "The signal would reach the {label} ({span}), where the band plan keeps everyone off",
    "plan_output": "Band plan: {span} is {label}: not for transmitting",
    "plan_mode": "Band plan: {what} is not what the {label} segment ({span}) is for",
    "plan_bandwidth": "Band plan: the {label} segment ({span}) is for signals up to {max} Hz; this one is {width} Hz",
    "plan_gap": "The {plan} data does not cover {span} yet: only the licence was checked there",
    "plan_unplanned": "The {plan} plan prints nothing for {span}: only the licence was checked there",
    # power
    "power_measured_over": "{class} licence: {limit} maximum {where} (measured {got})",
    "power_estimated_over": ("{class} licence: {limit} maximum {where}, and the power chain's estimate is {got}. An "
                             "estimate, so a warning, not a refusal: check it with a power meter"),
    "power_not_judged": "OpenHamShack's data holds no power limit for the {permit}: power was not checked",
    # data quality
    "data_researched": ("The data for {regime} was transcribed from its source but has not yet been second-checked: "
                        "check the licence itself before relying on it"),
    "permit_researched": ("The {permit} in OpenHamShack's data comes from a transcription that has not been checked "
                          "against the permit document"),
}


def mhz(f: float) -> str:
    s = f"{f / 1e6:.4f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def khz(f: float) -> str:
    return f"{f / 1e3:.1f}".rstrip("0").rstrip(".") if f < 30e6 else mhz(f)


def span(lo: float, hi: float) -> str:
    """'1850-2000 kHz', '430-431 MHz' (ohsradio/guard.py span)."""
    if hi < 30e6:
        return f"{khz(lo)}-{khz(hi)} kHz"
    return f"{mhz(lo)}-{mhz(hi)} MHz"


def watts(w: float) -> str:
    if w >= 10:
        return f"{w:.0f} W"
    if w >= 1:
        return f"{w:.1f} W"
    if w >= 1e-3:
        return f"{w * 1e3:.3g} mW"
    return f"{w * 1e6:.3g} µW"


def message(code: str, key: str, src: str = "", text: str | None = None, **params) -> dict:
    """A refusal or warning: {"code", "key", "text", "params", "src"}."""
    if text is None:
        text = T[key].format(**params)
    return {"code": code, "key": key, "text": text, "params": params, "src": src}


def translate(verdict: dict, locale: str) -> dict:
    """The verdict with every sentence in `locale`, where a catalog for it
    is installed; English otherwise. Keys and params are unchanged, so the
    caller may also render the code itself (CONTROLLER-CONTRACT.md §9)."""
    tr = gettext.translation(DOMAIN, localedir=str(LOCALEDIR), languages=[locale], fallback=True)

    def one(m):
        if m is None:
            return None
        if m["key"] == "data":
            text = tr.gettext(m["text"])
        else:
            text = tr.gettext(T[m["key"]]).format(**m["params"])
        return dict(m, text=text)
    out = dict(verdict)
    out["refusal"] = one(verdict.get("refusal"))
    out["warnings"] = [one(w) for w in verdict.get("warnings", [])]
    return out
