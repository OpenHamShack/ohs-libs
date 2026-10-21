"""APRS payloads, from the Icom selftest's corpus: positions every way they
are sent, mic-E, messages, objects, weather, telemetry, and malformed
payloads that must neither raise nor half-parse."""

from __future__ import annotations

import random

import pytest

from shack_packet import aprs, ax25

CORPUS = [
    "GM0ABC-9>APDW17,WIDE1-1,WIDE2-1:!5537.14N/00420.83W>Test position",
    "MM0XYZ>APDW17,GB7AY*,WIDE2-1:@092345z5537.14N/00420.83W-House",
    "G4ABC-1>APDW17:=5537.14N/00420.83W#PHG5360 Digipeater",
    "M0ABC-9>TQPX2X,WIDE1-1:`(_fn\"Oj/]\"4-}Testing mic-E",
    "MM3XYZ-7>T2SY4Y,WIDE1-1:`bR l#>/`\"4M}146.850MHz T100 -060",
    "2E0TST-7>APDW12:>Monitoring 144.800 in Ayrshire",
    "GB7AY>APN391:!5530.00NR00415.00W#WIDEn-N Digipeater Ayr",
    "G0WX-13>APDW17:@092345z5537.14N/00420.83W_180/010g015t045r000p010P010h50b10132",
    "G0WX-13>APDW17:_10090556c220s004g005t077r000p000P000h50b09900",
    "GM0ABC>APDW17::GM7XYZ   :Are you around this evening?{12",
    "GM7XYZ>APDW17::GM0ABC   :ack12",
    "GM0ABC>APDW17:;LEADHILLS*092345z5525.00N/00337.00WrSOTA GM/SS-176",
    "GM0ABC>APDW17:)AID#2!5537.14N/00420.83WA",
    "GM0ABC-11>APDW17:/092345h5537.14N/00420.83WO123/010/A=001234Balloon",
    "GM0ABC-9>APDW17:!5537.  N/00420.  W>Ambiguous to a minute",
    "GM0ABC-9>APDW17:!/5L!!<*e7> sT compressed",
    "GM0ABC-5>APDW17:T#123,010,020,030,040,050,10110000",
]


def packet(line_or_index):
    line = CORPUS[line_or_index] if isinstance(line_or_index, int) else line_or_index
    # Through the real bytes and the real parser, so the address decoder is
    # exercised as well.
    return aprs.parse(ax25.parse(ax25.Frame.from_monitor(line).encode()))


def close(a, b, tolerance=1e-5):
    return a is not None and abs(a - b) < tolerance


def test_a_position_with_and_without_a_timestamp():
    p = packet(0)
    assert p.kind == "position" and p.position.symbol == "/>" and p.symbol_name == "car"
    assert close(p.position.lat, 55.619) and close(p.position.lon, -4.3471666667)
    assert p.comment == "Test position" and p.position.uncertainty_m == 0.0
    p = packet(1)
    assert p.timestamp == "092345z" and close(p.position.lat, 55.619)
    assert p.comment == "House" and p.messaging


def test_ambiguity_is_the_middle_of_the_square_and_says_how_big():
    p = packet(14)
    assert close(p.position.lat, 55.625) and close(p.position.lon, -4.34166667)
    assert close(p.position.uncertainty_m, 926.0, 1.0)


def test_a_compressed_position():
    p = packet(15)
    assert p.position.compressed and p.position.symbol == "/>"
    assert close(p.position.lat, 49.5) and close(p.position.lon, -72.750004, 1e-4)


def test_mic_e_including_the_half_hidden_in_the_destination():
    p = packet(3)
    assert p.kind == "mic-e" and p.mic_e_message == "off-duty"
    assert close(p.position.lat, 41.138) and close(p.position.lon, -12.129)
    assert p.course_deg == 251 and close(p.speed_kmh, 37.0, 0.1)
    assert p.symbol_name == "jeep"
    # Behind a one-character radio-make prefix: lost by a decoder that looks
    # only at offset zero.
    assert close(p.altitude_m, 22.0, 0.5)
    assert p.comment == "Testing mic-E"
    p = packet(4)
    assert p.mic_e_message == "in-service" and close(p.position.lat, 42.6581666)
    assert close(p.altitude_m, 54.0, 0.5) and p.comment == "146.850MHz T100 -060"


def test_a_mic_e_packet_whose_destination_is_not_one_is_refused_not_guessed():
    p = packet("M0ABC-9>APRS,WIDE1-1:`(_fn\"Oj/]\"4-}no latitude")
    assert p.position is None and p.problem == "mic-e-destination"
    assert p.text.startswith("`")


def test_status_message_and_ack():
    p = packet(5)
    assert p.kind == "status" and p.status == "Monitoring 144.800 in Ayrshire" and p.position is None
    p = packet(9)
    assert (p.kind, p.addressee, p.message, p.message_id) == ("message", "GM7XYZ", "Are you around this evening?", "12")
    p = packet(10)
    assert (p.kind, p.addressee, p.ack, p.message) == ("ack", "GM0ABC", "12", "")


def test_an_object_and_an_item():
    p = packet(11)
    assert p.kind == "object" and p.name == "LEADHILLS" and p.live
    assert close(p.position.lat, 55.41666667) and p.comment == "SOTA GM/SS-176"
    p = packet(12)
    assert p.kind == "item" and p.name == "AID#2" and close(p.position.lat, 55.619)


def test_weather_with_a_position_and_without():
    p = packet(7)
    assert p.kind == "weather"
    assert close(p.weather["temp_c"], 7.2, 0.1) and close(p.weather["pressure_hpa"], 1013.2, 0.1)
    assert p.weather["humidity_pct"] == 50 and close(p.weather["rain_24h_mm"], 2.5, 0.1)
    # For a weather station the pair after the symbol is the wind.
    assert p.weather["wind_dir_deg"] == 180 and p.course_deg is None and p.speed_kmh is None
    assert p.comment == ""
    p = packet(8)
    assert p.kind == "weather" and p.position is None
    assert p.weather["wind_dir_deg"] == 220 and close(p.weather["temp_c"], 25.0, 0.1)


def test_phg_course_speed_altitude_and_telemetry():
    p = packet(2)
    assert p.telemetry["power_w"] == 25 and close(p.telemetry["height_m"], 24.4, 0.1)
    assert p.telemetry["gain_db"] == 6 and p.comment == " Digipeater"
    p = packet(13)
    assert p.course_deg == 123 and close(p.speed_kmh, 18.5, 0.1) and close(p.altitude_m, 376.1, 0.2)
    assert p.comment == "Balloon" and p.timestamp == "092345h"
    p = packet(16)
    assert p.kind == "telemetry" and p.telemetry["analogue"] == [10, 20, 30, 40, 50]
    assert p.telemetry["digital"] == "10110000"


def test_an_overlay_digipeater_is_named():
    assert packet(6).symbol_name == "digipeater"


MALFORMED = ["!not a position at all", "!12345", "@", "@notatime5537.14N/00420.83W>", "/092345z", ":short:",
             ";TOOSHORT", ")", "!9937.14N/09920.83W>", "T#not,telemetry", "_", "\x00\x01\x02\x03", "!" + "\xff" * 40]


@pytest.mark.parametrize("text", MALFORMED)
def test_a_malformed_payload_is_kept_as_text_and_never_half_parsed(text):
    p = packet("GM0ABC>APDW17:" + text)
    assert p.text == text
    assert p.problem in aprs.PROBLEMS or p.problem == ""
    if p.position is not None:
        assert -90 <= p.position.lat <= 90 and -180 <= p.position.lon <= 180
    assert not text.startswith("!9") or p.position is None


def test_every_problem_and_name_is_a_key_not_a_sentence():
    """The library carries no user-visible text (I18N.md): apps translate
    these keys. A sentence slipping in here would reach a screen in English."""
    for key in list(aprs.PROBLEMS) + list(aprs.SYMBOLS.values()) + aprs.MIC_E_STANDARD + aprs.MIC_E_CUSTOM:
        assert key == key.lower() and " " not in key, key


def test_random_payloads_never_raise():
    rng = random.Random(9)
    starts = list(aprs.TYPES)
    for _ in range(5000):
        body = rng.choice(starts) + "".join(chr(rng.randrange(0, 256)) for _ in range(rng.randrange(0, 60)))
        frame = ax25.Frame.ui("GM0ABC", rng.choice(["APDW17", "TQPX2X", "T2SY4Y", "S32U6T"]), body)
        p = aprs.parse(frame)
        assert p.text == body
        assert p.problem != "exception", p.error
        if p.position is not None:
            assert -90 <= p.position.lat <= 90 and -180 <= p.position.lon <= 180


def test_distance_and_bearing_agree_with_a_known_pair_of_points():
    # Glasgow to London: 555 km on a great circle at about 147 degrees.
    km, bearing = aprs.distance_bearing(55.8642, -4.2518, 51.5074, -0.1278)
    assert abs(km - 555) < 6 and abs(bearing - 147) < 3


def test_what_we_write_reads_back():
    position = aprs.format_position(-33.865, 151.209, "\\#", " digi", course_deg=0, speed_kmh=1.852,
                                    ambiguity=0)
    packet = aprs.parse(ax25.Frame.from_monitor("VK2ABC>APZOHS:" + position))
    assert packet.position.lat == pytest.approx(-33.865, abs=1e-4)
    assert packet.position.lon == pytest.approx(151.209, abs=1e-4)
    assert packet.position.symbol == "\\#" and packet.messaging
    assert packet.course_deg == 360 and packet.speed_kmh == pytest.approx(1.852, abs=0.05)
    vague = aprs.parse(ax25.Frame.from_monitor("G0ABC>APZOHS:" + aprs.format_position(55.6, -4.3, ambiguity=2)))
    assert vague.position.uncertainty_m > 900
    message = aprs.parse(ax25.Frame.from_monitor("G0ABC>APZOHS:" + aprs.format_message("GM4BRI-7", "hi", "7")))
    assert (message.kind, message.addressee, message.message, message.message_id) == ("message", "GM4BRI-7", "hi", "7")
    with pytest.raises(ValueError):
        aprs.format_message("GM4BRI", "a {brace}")
    with pytest.raises(ValueError):
        aprs.format_position(91.0, 0.0)
    assert aprs.clean_message("x" * 70) == ["x" * 67, "xxx"]
    assert aprs.clean_message("a|b~c{d") == ["a-b-c-d"]
