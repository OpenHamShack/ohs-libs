"""The library's questions beyond the conformance vectors: the country gate,
limits and bands, channels, presets, the licence in force, translation."""
import shack_bandplan as bp

DATA = bp.load()
TODAY = "2026-10-09"
GM = {"callsign": "GM4BRI", "licence": {"regime": "auto", "class": "full"}}


def ctx(profile, **kw):
    return bp.context(profile, DATA, today=TODAY, **kw)


def test_switching_the_country_changes_the_answer():
    """BETA-PLAN §5 gate 1: the same signal, a UK profile and a German one."""
    sig = bp.Signal(144_300_000, 2_700, mode="usb", power_w=900, measured=True)
    uk = bp.check(ctx(GM), sig)
    de = bp.check(ctx(dict(GM, country="DE", dxccPrefix="DL", licence={"regime": "auto", "class": "de_a"})), sig)
    assert uk["allowed"] and uk["limit"]["watts"] == 1000 and uk["regime"] == "ofcom-2024"
    assert not de["allowed"] and de["refusal"]["code"] == "power_measured_over"
    assert de["limit"]["watts"] == 750 and de["regime"] == "bnetza-2024"


def test_a_german_station_gets_the_iaru_plan_and_a_researched_warning():
    c = ctx({"callsign": "DL1ABC", "licence": {"regime": "auto", "class": "de_a"}})
    assert (c.country_iso, c.region, c.plan["id"]) == ("DE", 1, "iaru-r1")
    v = bp.check(c, bp.Signal(145_500_000, 12_000, mode="fm"))
    assert "data_researched" in [w["code"] for w in v["warnings"]]


def test_a_chosen_regime_beats_the_callsigns_country():
    c = ctx({"callsign": "GM4BRI", "licence": {"regime": "bnetza-2024", "class": "de_a"}})
    assert c.regime_id == "bnetza-2024" and c.entity == "Scotland"


def test_scotland_derived_from_the_prefix_and_overridden_by_the_operator():
    assert ctx(GM).conditions["scotland40km"] is True
    assert ctx(dict(GM, callsign="G4ABC")).conditions["scotland40km"] is False
    assert ctx(dict(GM, conditions={"scotland40km": False})).conditions["scotland40km"] is False


def test_limits_shade_where_the_class_may_transmit():
    lim = bp.limits(ctx(GM), 144_000_000, 148_000_000)
    assert [(s["loHz"], s["hiHz"]) for s in lim["allowed"]] == [(144_000_000, 146_000_000)]
    nov = bp.limits(ctx(dict(GM, novs=[{"id": "gb-146"}])), 144_000_000, 148_000_000)
    assert (146_000_000, 146_937_500) in [(s["loHz"], s["hiHz"]) for s in nov["allowed"]]
    assert any(s["kind"] == "satellite" for s in lim["segments"])


def test_bands_for_a_foundation_licensee():
    b = {x["name"]: x for x in bp.bands(ctx({"callsign": "M7ABC", "licence": {"regime": "auto", "class": "foundation"}}))}
    assert b["2m"]["allowed"] and b["2m"]["limit"]["watts"] == 25
    assert not b["23cm"]["allowed"] and not b["60m"]["allowed"]


def test_qo100_wb_channels_match_the_bats_plan():
    ch = bp.channels(DATA, "qo100-wb", 1500)
    assert [c["downlinkHz"] for c in ch] == [10_493_250_000, 10_494_750_000, 10_496_250_000]
    assert all(c["preferred"] for c in ch) and ch[0]["uplinkHz"] == 2_403_750_000
    narrow = bp.channels(DATA, "qo100-wb", 333)
    assert len(narrow) == 14 and not narrow[0]["preferred"] and narrow[-1]["preferred"]
    assert bp.channels(DATA, "qo100-wb", 2000) == []


def test_presets_are_shown_disabled_with_the_reason_never_hidden():
    rows = {p["label"]: p for p in bp.presets(ctx(GM), "datv")}
    assert set(rows) == {"2 m NoV DATV", "70 cm DATV", "23 cm DATV"}
    assert not rows["2 m NoV DATV"]["enabled"] and "Notice of Variation" in rows["2 m NoV DATV"]["why"]
    assert rows["23 cm DATV"]["enabled"]
    held = {p["label"]: p for p in bp.presets(ctx(dict(GM, novs=[{"id": "gb-146"}])), "datv")}
    assert held["2 m NoV DATV"]["enabled"]
    # A German station gets no UK presets: nothing more specific applies.
    assert bp.presets(ctx({"callsign": "DL1ABC", "licence": {"regime": "auto", "class": "de_a"}}), "datv") == []
    assert len(bp.presets(ctx(GM), "qo100nb")) == 12


def test_in_force_says_why_not():
    assert bp.in_force(GM, DATA, TODAY)["canTransmit"]
    ja = bp.in_force({"callsign": "JA1ABC", "licence": {"regime": "auto", "class": "full"}}, DATA, TODAY)
    assert not ja["canTransmit"] and "Japan" in ja["why"]
    none = bp.in_force(dict(GM, licence={"regime": "auto", "class": ""}), DATA, TODAY)
    assert not none["canTransmit"] and none["why"].startswith("No licence class chosen")


def test_translate_falls_back_to_english_with_the_same_codes():
    v = bp.check(ctx(GM), bp.Signal(2_370_000_000, 0))
    t = bp.translate(v, "de")
    assert t["refusal"]["code"] == "refused_range" and t["refusal"]["text"] == v["refusal"]["text"]


def test_dvb_occupied_width():
    assert bp.dvb_occupied_hz(333) == 449_550 and bp.dvb_occupied_hz(1500) == 2_025_000


def test_an_edge_signal_by_its_edges():
    """An LSB signal dialled on 1850.5 kHz puts speech below 1850 kHz, where
    an Intermediate licensee has 100 W rather than 32 W (ohsradio/guard.py)."""
    inter = ctx({"callsign": "2E0ABC", "licence": {"regime": "auto", "class": "intermediate"}})
    v = bp.check(inter, bp.Signal.edges(1_847_800, 1_850_500, mode="lsb"))
    assert v["limit"]["watts"] == 32


def codes(v):
    return [w["code"] for w in v["warnings"]]


def test_the_630m_recommendation_warns_and_never_refuses():
    """RSGB 2026 Note-1 (472-475 kHz CW only, 200 Hz) is a recommendation
    inside a 472-479 kHz, 500 Hz row: a warning under warn and enforce
    alike, and nothing for a signal that keeps to it."""
    wide = bp.Signal(473_000, 400, mode="cw")
    for guard in ("warn", "enforce"):
        v = bp.check(ctx(GM), wide, plan_guard=guard)
        assert v["allowed"] and "plan_advice" in codes(v), guard
        advice = next(w for w in v["warnings"] if w["code"] == "plan_advice")
        assert advice["key"] == "data" and "Note-1" in advice["src"]
    assert "plan_advice" not in codes(bp.check(ctx(GM), bp.Signal(473_000, 150, mode="cw")))
    assert "plan_advice" not in codes(bp.check(ctx(GM), bp.Signal(473_000, 400, mode="cw"), plan_guard="off"))
    # Above 475 kHz the recommendation does not reach; digital modes are fine.
    assert "plan_advice" not in codes(bp.check(ctx(GM), bp.Signal(477_000, 400, mode="ft8")))
    # The table row itself still judges: 600 Hz is over its 500 Hz.
    assert not bp.check(ctx(GM), bp.Signal(477_000, 600, mode="ft8"), plan_guard="enforce")["allowed"]


def test_where_the_source_prints_nothing_the_plan_says_so():
    """2315-2320 MHz is in the UK licence and RSGB 2026 gives it no usage:
    allowed on the licence alone, with a warning that is not plan_gap
    (the transcription is not missing anything there)."""
    v = bp.check(ctx(GM), bp.Signal(2_317_000_000, 12_000, mode="fm"))
    assert v["allowed"] and "plan_unplanned" in codes(v) and "plan_gap" not in codes(v)
    unplanned = next(w for w in v["warnings"] if w["code"] == "plan_unplanned")
    assert unplanned["params"]["plan"] == "RSGB 2026" and "2315-2320 MHz" in unplanned["src"]


def test_corrected_rsgb_rows_change_the_answers_they_should():
    # 2321-2322 MHz is 20 kHz in the PDF (it was 25 kHz).
    v = bp.check(ctx(GM), bp.Signal(2_321_500_000, 25_000, mode="fm"))
    assert v["allowed"] and "plan_bandwidth" in codes(v)
    assert "plan_bandwidth" not in codes(bp.check(ctx(GM), bp.Signal(2_321_500_000, 16_000, mode="fm")))
    # 3 cm narrow band: planned now, so no gap warning; its beacons refuse.
    v = bp.check(ctx(GM), bp.Signal(10_368_100_000, 2_700, mode="usb"))
    assert v["allowed"] and "plan_gap" not in codes(v) and v["segment"]["label"] == "narrow band"
    v = bp.check(ctx(GM), bp.Signal(10_368_900_000, 500, mode="cw"), plan_guard="off")
    assert not v["allowed"] and v["refusal"]["code"] == "plan_reserved"


def test_iaru_r1_now_plans_3cm_and_630m():
    de = ctx({"callsign": "DL1ABC", "licence": {"regime": "auto", "class": "de_a"}})
    v = bp.check(de, bp.Signal(10_368_200_000, 2_700, mode="usb"))
    assert v["allowed"] and "plan_gap" not in codes(v)
    assert not bp.check(de, bp.Signal(473_000, 2_700, mode="usb"), plan_guard="enforce")["allowed"]
