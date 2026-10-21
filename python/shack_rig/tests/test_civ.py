"""The CI-V codec: frames, BCD, meters, mode/data, and the spectrum.

The vector checks (vectors/civ.json) are new: every command builder against
its bytes, every frame wrapped, split and parsed back. The rest is ported
from OpenHamShack Icom's tools/selftest.py (test_bcd, test_frames,
test_meters, the codec half of test_data_mode, test_spectrum). Being wrong
here is silent - a plausible number, a mode the radio ignores - which is why
the checks are byte for byte.
"""

import json
from pathlib import Path

import pytest

from shack_rig.icom import civ, models

VECTORS = json.loads((Path(__file__).parent / "vectors" / "civ.json").read_text())
BUILDERS = {name: getattr(civ, name) for name in {v["build"] for v in VECTORS["commands"]}}


def h(text: str) -> bytes:
    return bytes.fromhex(text)


# -- vectors -----------------------------------------------------------------

@pytest.mark.parametrize("v", VECTORS["commands"], ids=lambda v: v["what"])
def test_each_command_builds_its_bytes_and_round_trips(v):
    payload = BUILDERS[v["build"]](*v["args"])
    assert payload == h(v["payload"])
    for to in (0xA2, 0x98, 0xA4):
        wire = civ.frame(payload, to=to, frm=0xE0)
        frames, rest = civ.split_frames(wire)
        assert frames == [wire] and rest == b""
        assert civ.parse(frames[0]) == (to, 0xE0, payload)


@pytest.mark.parametrize("v", VECTORS["frames"], ids=lambda v: v["what"])
def test_frames_wrap_and_parse(v):
    to, frm = int(v["to"], 16), int(v["from"], 16)
    assert civ.frame(h(v["payload"]), to=to, frm=frm) == h(v["frame"])
    assert civ.parse(h(v["frame"])) == (to, frm, h(v["payload"]))


@pytest.mark.parametrize("v", VECTORS["replies"], ids=lambda v: v["what"])
def test_replies_decode(v):
    p = h(v["payload"])
    if "freq" in v:
        assert civ.decode_freq(p[1:6]) == v["freq"]
        assert civ.set_freq(v["freq"])[1:] == p[1:6], "and encode back to the same bytes"
    if "mode" in v:
        got = civ.parse_mode_data(p)
        assert (got["mode"], got["data"], got["filter"]) == (v["mode"], v["data"], v["filter"])
    if "meter" in v:
        raw = civ.decode_meter(p[2:4])
        assert raw == v["meter"]
        if "smeter_db" in v:
            assert civ.smeter_db(raw) == v["smeter_db"]
        if "swr" in v:
            assert civ.swr(raw) == v["swr"]


def test_every_frequency_survives_the_round_trip():
    for hz in list(range(0, 10_000_000_000, 99_999_937)) + [9_999_999_999, 1, 10]:
        assert civ.decode_freq(civ.encode_freq(hz)) == hz


def test_every_meter_value_survives_the_round_trip():
    for raw in range(0, 256):
        digits = f"{raw:04d}"
        b = bytes([int(digits[0:2], 16), int(digits[2:4], 16)])
        assert civ.decode_meter(b) == raw


# -- BCD and framing ---------------------------------------------------------

def test_bcd_refuses_what_is_not_bcd():
    with pytest.raises(civ.CivError):
        civ.decode_freq(b"\xff\x00\x00\x00\x00")
    with pytest.raises(civ.CivError):
        civ.encode_freq(-1)
    with pytest.raises(civ.CivError):
        civ.encode_freq(10_000_000_000)
    with pytest.raises(civ.CivError):
        civ.decode_meter(b"\x0a\x00")
    with pytest.raises(civ.CivError):
        civ.bcd_byte(0x1A)


def test_frames_split_across_reads_and_packed_together():
    whole = h("fefea2e00300fd")
    part = h("fefee0a20300")
    frames, rest = civ.split_frames(whole + part)
    assert frames == [whole] and rest == part, "a partial frame is held back"
    frames, rest = civ.split_frames(whole + whole)
    assert len(frames) == 2 and rest == b""
    # A trailing single FE may be the first half of the next preamble.
    frames, rest = civ.split_frames(whole + b"\xfe")
    assert frames == [whole] and rest == b"\xfe"
    # Bytes before a preamble are a fragment of a frame we never saw.
    frames, rest = civ.split_frames(b"\x12\x34" + whole)
    assert frames == [whole]
    with pytest.raises(civ.CivError):
        civ.parse(h("fefea2fd"))


def test_rf_power_round_trips_and_rejects_bad_answers():
    assert civ.read_rf_power() == h("14 0a")
    assert civ.set_rf_power(0) == h("14 0a 00 00")
    assert civ.set_rf_power(255) == h("14 0a 02 55")
    assert civ.set_rf_power(128) == h("14 0a 01 28")
    for level in (0, 1, 128, 254, 255):
        answer = h("14 0a") + civ.set_rf_power(level)[2:]
        assert civ.decode_rf_power(answer) == level
    with pytest.raises(civ.CivError):
        civ.set_rf_power(-1)
    with pytest.raises(civ.CivError):
        civ.set_rf_power(256)
    with pytest.raises(civ.CivError):
        civ.decode_rf_power(h("15 11 00 00")), "the wrong command"
    with pytest.raises(civ.CivError):
        civ.decode_rf_power(h("14 0a 0a 00")), "not BCD"


def test_meter_scaling():
    assert civ.smeter_db(0) == -54.0
    assert civ.smeter_db(120) == 0.0
    assert round(civ.smeter_db(241)) == 60
    assert all(civ.smeter_db(i) <= civ.smeter_db(i + 1) for i in range(255))
    assert (civ.swr(0), civ.swr(48), civ.swr(80), civ.swr(120)) == (1.0, 1.5, 2.0, 3.0)
    assert all(civ.swr(i) <= civ.swr(i + 1) for i in range(255))


# -- DATA is a flag, not a mode ----------------------------------------------

def test_there_is_no_data_mode_byte():
    assert not any(name.endswith("-D") for name in models.ALL_MODES.values())
    with pytest.raises(civ.CivError):
        civ.set_mode_data("USB-D")
    with pytest.raises(civ.CivError):
        civ.set_mode_data("USB", True, 0x07), "a filter outside FIL1-3 is refused"
    with pytest.raises(civ.CivError):
        civ.parse_mode_data(h("04 01 01"))


def test_naming_the_pair():
    assert civ.mode_label("USB", True) == "USB-D"
    assert civ.mode_label("USB", False) == "USB"
    assert civ.mode_label(None, True) is None
    assert civ.split_mode_label("USB-D") == ("USB", True)
    assert civ.split_mode_label("usb") == ("USB", False)
    assert civ.split_mode_label("DD") == ("DD", False), "DD is not D with a data flag"


# -- the spectrum ------------------------------------------------------------

LEVELS = bytes((i * 3) % 161 for i in range(civ.SCOPE_LEN_MAX))


def wave(mode: int, a: int, b: int, oor: int = 0, levels: bytes = LEVELS,
         seq: int = 0x01, seq_max: int = 0x01) -> bytes:
    return (bytes([0x27, 0x00, 0x00, seq, seq_max, mode]) + civ.encode_freq(a)
            + civ.encode_freq(b) + bytes([oor]) + levels)


def test_a_spectrum_arriving_whole_as_over_the_lan():
    s = civ.ScopeAssembler().add(wave(0x01, 144_000_000, 146_000_000))
    assert s["points"] == civ.SCOPE_LEN_MAX and s["data"] == list(LEVELS)
    assert s["mode"] == "fixed" and (s["start"], s["end"]) == (144_000_000, 146_000_000)
    assert s["out_of_range"] is False


def test_centre_mode_reports_edges_not_centre_and_half_span():
    s = civ.ScopeAssembler().add(wave(0x00, 145_000_000, 50_000))
    assert (s["start"], s["end"]) == (144_950_000, 145_050_000)
    assert s["mode"] == "center"


def test_a_spectrum_in_eleven_bcd_numbered_pieces_as_over_serial():
    asm = civ.ScopeAssembler()
    assert asm.add(wave(0x01, 430_000_000, 440_000_000, levels=b"", seq_max=0x11)) is None
    out, pos = None, 0
    for i in range(2, 12):
        take = 50 if i < 11 else 25
        seq = ((i // 10) << 4) | (i % 10)            # the tenth frame is 0x10
        out = asm.add(bytes([0x27, 0x00, 0x00, seq, 0x11]) + LEVELS[pos:pos + take])
        pos += take
        if i == 10:
            assert out is None, "the tenth of eleven is not the end"
    assert out["data"] == list(LEVELS[:475])
    assert (out["start"], out["end"]) == (430_000_000, 440_000_000)


def test_the_ways_a_spectrum_goes_wrong():
    asm = civ.ScopeAssembler()
    assert asm.add(b"\x27\x00\x00\x02\x11" + LEVELS[:50]) is None
    assert asm.dropped == 1, "a continuation with no beginning is dropped and counted"
    s = asm.add(wave(0x01, 1_240_000_000, 1_300_000_000, oor=1, levels=b""))
    assert s["out_of_range"] and s["data"] == [0] * civ.SCOPE_LEN_MAX
    assert not civ.is_scope_data(b"\x27\x10\x01") and asm.add(b"\x27\x10\x01") is None
    with pytest.raises(civ.CivError):
        asm.add(b"\x27\x00\x00\x01\x01\x01\x00")
    with pytest.raises(civ.CivError):
        asm.add(b"\x27\x00\x00\x03\x02" + LEVELS[:5]), "sequence 3 of 2"
    # A continuation stream whose header was lost must not grow for ever.
    asm = civ.ScopeAssembler()
    asm.add(wave(0x01, 1, 2, levels=b"", seq_max=0x99))
    with pytest.raises(civ.CivError, match="ran past"):
        for i in range(2, 99):
            asm.add(bytes([0x27, 0x00, 0x00, ((i // 10) << 4) | (i % 10), 0x99]) + bytes(100))


# -- power ---------------------------------------------------------------------

def test_power_is_0x18_and_the_wake_preamble_follows_the_line_speed():
    assert civ.set_power(True) == b"\x18\x01"
    assert civ.set_power(False) == b"\x18\x00"
    # wfview's table: about 100 ms of FE at each speed, the longest when the
    # speed is 115200 or not known at all.
    assert civ.wake_preamble(9600) == b"\xfe" * 13
    assert civ.wake_preamble(57600) == b"\xfe" * 75
    assert civ.wake_preamble(115200) == b"\xfe" * 150
    assert civ.wake_preamble(None) == b"\xfe" * 150
    # Whatever the run, a reader still finds exactly one frame behind it.
    frames, rest = civ.split_frames(civ.wake_preamble(9600) + civ.frame(civ.set_power(True)))
    assert [civ.parse(f)[2] for f in frames] == [b"\x18\x01"] and rest == b""


def test_the_front_end_bytes_refuse_what_no_radio_has():
    # BCD both ways: 0x10 is ten dB, and a byte that is not BCD is an error
    # rather than a plausible number of decibels.
    assert civ.decode_att(h("11 10")) == 10 and civ.decode_att(h("11 00")) == 0
    with pytest.raises(civ.CivError):
        civ.decode_att(h("11 0a"))
    with pytest.raises(civ.CivError):
        civ.set_att(100)
    with pytest.raises(civ.CivError):
        civ.set_preamp(4)


def test_the_9700_preamp_byte_carries_the_external_preamp_and_the_7610s_does_not():
    nine, sixteen = models.model("ic-9700"), models.model("ic-7610")
    assert nine.preamp_byte(1, ext=True) == 0x03 and nine.preamp_byte(0, ext=True) == 0x02
    assert nine.preamp_from_byte(0x02) == (0, True) and nine.preamp_from_byte(0x01) == (1, False)
    # On the HF radio 02 is PRE2, never "external".
    assert sixteen.preamp_byte(2, ext=True) == 0x02 and sixteen.preamp_from_byte(0x02) == (2, None)


def test_a_menu_item_is_read_as_four_bcd_digits_and_its_answer_decoded():
    # SET > Connectors > External P.AMP > 144M is item 0093 (CI-V guide).
    assert civ.read_menu(93) == h("1a 05 00 93")
    assert civ.read_menu(1200) == h("1a 05 12 00")
    assert civ.decode_menu(h("1a 05 00 95 01")) == (95, 1)
    with pytest.raises(civ.CivError):
        civ.decode_menu(h("1a 05 00 9a 01"))
    with pytest.raises(civ.CivError):
        civ.read_menu(10000)
