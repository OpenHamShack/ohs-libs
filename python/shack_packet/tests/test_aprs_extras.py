"""APRS beyond the station's beacon (0.3.0): GPS sentences, the iGate's
rules both ways, weather stations, objects, items and telemetry - each
formatted here and read back by our own parser, which direwolf's decoder
already agrees with (test_direwolf.py) - and the schedule that sends
them, over the simulated channel."""

from __future__ import annotations

import socket
import struct
import threading

from stations import World

from shack_packet import aprs, aprsis, aprswx, nmea
from shack_packet.aprs_services import AprsSchedule, PlacedObject, ScheduleConfig
from shack_packet.ax25 import Address, Frame

T0 = 1_790_000_000.0


# -- GPS ----------------------------------------------------------------------

def nmea_sentence(body: str) -> str:
    total = 0
    for ch in body:
        total ^= ord(ch)
    return f"${body}*{total:02X}"


def test_nmea_rmc_and_gga_make_one_fix():
    r = nmea.NmeaReader()
    gga = nmea_sentence("GNGGA,123519,5557.000,N,00311.400,W,1,08,0.9,45.4,M,46.9,M,,")
    rmc = nmea_sentence("GPRMC,123520,A,5557.000,N,00311.400,W,022.4,084.4,201026,003.1,W")
    fix = r.line(gga)
    assert round(fix.lat, 4) == 55.95 and round(fix.lon, 4) == -3.19 and fix.altitude_m == 45.4
    fix = r.line(rmc)
    assert round(fix.speed_kmh, 1) == 41.5 and fix.course_deg == 84.4 and fix.altitude_m == 45.4
    assert fix.time == 1_792_499_720.0          # 20/10/2026 12:35:20 UTC
    assert r.line(rmc[:-2] + "00") is None       # a damaged checksum
    assert r.line(nmea_sentence("GPRMC,123521,V,,,,,,,201026,,")) is None and r.fix is None


def test_gpsd_tpv():
    fix = nmea.from_gpsd('{"class":"TPV","mode":3,"lat":55.95,"lon":-3.19,"speed":10.0,"track":90,'
                         '"altMSL":40,"time":"2026-10-20T12:00:00.000Z"}')
    assert (fix.lat, fix.lon, fix.speed_kmh, fix.course_deg, fix.altitude_m) == (55.95, -3.19, 36.0, 90.0, 40.0)
    assert nmea.from_gpsd('{"class":"TPV","mode":1}') is None
    assert nmea.from_gpsd('{"class":"SKY"}') is None and nmea.from_gpsd("garbage") is None


# -- the iGate ------------------------------------------------------------------

def test_passcode_and_login():
    assert aprsis.passcode("N0CALL") == 13023 and aprsis.passcode("n0call-10") == 13023
    assert aprsis.login_line("GM4BRI-10", 21156, filter="m/50") == \
        "user GM4BRI-10 pass 21156 vers OHS-Packet 0.3.0 filter m/50"


def frame(source, info, via=(), dest="APRS"):
    f = Frame.ui(source, dest, info, via=[Address.from_text(v) for v in via])
    return f


def test_rf_to_is_rules():
    now = [100.0]
    gate = aprsis.IGate("GM4BRI-10", clock=lambda: now[0])
    line = gate.rf_to_is(frame("G0ABC-9", "!5557.00N/00311.40W>moving", via=["MB7UE*", "WIDE2-1"]))
    assert line == "G0ABC-9>APRS,MB7UE*,WIDE2-1,qAR,GM4BRI-10:!5557.00N/00311.40W>moving"
    # The same packet again through another digipeater within 30 s: a duplicate.
    assert gate.rf_to_is(frame("G0ABC-9", "!5557.00N/00311.40W>moving", via=["MB7UF*"])) is None
    now[0] += 31
    assert gate.rf_to_is(frame("G0ABC-9", "!5557.00N/00311.40W>moving")) is not None
    for path in (["TCPIP*"], ["NOGATE"], ["RFONLY"], ["TCPXX*"]):
        assert gate.rf_to_is(frame("G4XYZ", ">status", via=path)) is None
    assert gate.rf_to_is(frame("G4XYZ", "}G0AAA>APRS,TCPIP*:>third party")) is None
    assert gate.rf_to_is(frame("G4XYZ", "?APRS?")) is None
    assert gate.rf_to_is(frame("GM4BRI-10", ">myself")) is None


def test_is_to_rf_only_messages_for_locals_and_not_too_many():
    now = [100.0]
    gate = aprsis.IGate("GM4BRI-10", aprsis.IGateConfig(tx=True, max_per_min=2), clock=lambda: now[0])
    msg = "W1AW>APRS,TCPIP*,qAC,T2X::{to}:hello{n}{{1"
    # Nobody heard locally yet: nothing goes on the air.
    assert gate.is_to_rf(msg.format(to="G0ABC-9  ", n=0)) is None
    gate.heard(frame("G0ABC-9", ">here"))
    gate.heard(frame("G4FAR", ">far", via=["MB7UE*"]))            # heard only through a digipeater
    out = gate.is_to_rf(msg.format(to="G0ABC-9  ", n=1))
    assert out == ("}W1AW>APRS,TCPIP,GM4BRI-10*::G0ABC-9  :hello1{1", ["WIDE1-1"])
    assert gate.is_to_rf(msg.format(to="G4FAR    ", n=2)) is None
    assert gate.is_to_rf(msg.format(to="G0ABC-9  ", n=1)) is None            # the same again
    assert gate.is_to_rf("W1AW>APRS,TCPIP*:!4140.00N/07200.00W-position") is None
    assert gate.is_to_rf(msg.format(to="G0ABC-9  ", n=3)) is not None
    assert gate.is_to_rf(msg.format(to="G0ABC-9  ", n=4)) is None             # two a minute
    now[0] += 61
    assert gate.is_to_rf(msg.format(to="G0ABC-9  ", n=5)) is not None
    # The addressee turns out to be on APRS-IS itself: it gets it there.
    gate.is_to_rf("G0ABC-9>APRS,TCPIP*:>on the internet too")
    assert gate.is_to_rf(msg.format(to="G0ABC-9  ", n=6)) is None
    # Out of the local window: nothing.
    receive_only = aprsis.IGate("GM4BRI-10", clock=lambda: now[0])
    receive_only.heard(frame("G0ABC-9", ">here"))
    assert receive_only.is_to_rf(msg.format(to="G0ABC-9  ", n=7)) is None


def test_client_logs_in_and_passes_lines_both_ways():
    a, b = socket.socketpair()
    got, states = [], []
    client = aprsis.AprsIsClient("GM4BRI-10", 21156, on_line=got.append, filter="m/50",
                                 connect=lambda server, timeout: a, on_state=states.append)
    client.start()
    b.settimeout(5)
    login = b.recv(200)
    assert login == b"user GM4BRI-10 pass 21156 vers OHS-Packet 0.3.0 filter m/50\r\n"
    b.sendall(b"# aprsc 2.1\r\n# logresp GM4BRI-10 verified, server T2TEST\r\nW1AW>APRS:>hi\r\n")
    done = threading.Event()
    for _ in range(100):
        if len(got) >= 3:
            done.set()
            break
        threading.Event().wait(0.02)
    assert done.is_set() and got[-1] == "W1AW>APRS:>hi" and client.state == "verified"
    assert client.send("G0ABC>APRS,qAR,GM4BRI-10:>gated")
    assert b.recv(200) == b"G0ABC>APRS,qAR,GM4BRI-10:>gated\r\n"
    client.stop()
    b.close()
    assert "verified" in states and states[-1] == "idle"


# -- weather ------------------------------------------------------------------

def test_weather_report_round_trips_through_our_parser():
    wx = {"wind_dir_deg": 225, "wind_kmh": 16.1, "gust_kmh": 32.2, "temp_c": -2.0, "rain_hour_mm": 0.0,
          "humidity_pct": 100, "pressure_hpa": 1013.2}
    text = aprs.format_weather(55.95, -3.19, wx, "OHS")
    assert text == "!5557.00N/00311.40W_225/010g020t028r000h00b10132OHS"
    back = aprs.parse(frame("GM4BRI", text)).weather
    assert back["wind_dir_deg"] == 225 and back["temp_c"] == -2.2 and back["humidity_pct"] == 100
    assert back["pressure_hpa"] == 1013.2
    # No sensor for something: dots, which the parser skips.
    assert aprs.format_weather(55.95, -3.19, {"temp_c": 10}).endswith("_.../...g...t050")


def test_peet_ultimeter_line():
    # Wind 20.0 km/h at 0x80 (180°), 50.0 °F, pressure 1013.2, humidity 81.0 %,
    # today's rain 0.12 in, one-minute average 15.0 km/h.
    line = "!!" + "".join(f"{v:04X}" for v in (200, 0x80, 500, 1234, 10132, 700, 810, 450, 0x0B0A, 600, 12, 150))
    got = aprswx.peet_line(line)
    assert got == {"wind_kmh": 15.0, "gust_kmh": 20.0, "wind_dir_deg": 180, "temp_c": 10.0,
                   "pressure_hpa": 1013.2, "humidity_pct": 81, "rain_midnight_mm": 3.0}
    assert "temp_c" not in aprswx.peet_line(line[:10] + "----" + line[14:])
    assert aprswx.peet_line("hello") is None


def loop_packet(**over):
    p = bytearray(99)
    p[0:3] = b"LOO"
    p[3] = 0xFF            # bar trend: none
    p[4] = 0               # packet type 0
    vals = {"bar": 29921, "temp": 504, "wind": 10, "avg": 8, "dir": 270, "hum": 66, "rate": 5, "day": 20}
    vals.update(over)
    struct.pack_into("<H", p, 7, vals["bar"])
    struct.pack_into("<h", p, 12, vals["temp"])
    p[14], p[15] = vals["wind"], vals["avg"]
    struct.pack_into("<H", p, 16, vals["dir"])
    p[33] = vals["hum"]
    struct.pack_into("<H", p, 41, vals["rate"])
    struct.pack_into("<H", p, 50, vals["day"])
    p[95], p[96] = 0x0A, 0x0D
    crc = aprswx._crc_ccitt(bytes(p[:97]))
    p[97], p[98] = crc >> 8, crc & 0xFF
    return bytes(p)


def test_davis_loop_packet():
    got = aprswx.davis_loop(loop_packet())
    assert got == {"pressure_hpa": 1013.2, "temp_c": 10.2, "wind_kmh": 12.9, "gust_kmh": 16.1,
                   "wind_dir_deg": 270, "humidity_pct": 66, "rain_hour_mm": 1.3, "rain_midnight_mm": 5.1}
    bad = bytearray(loop_packet())
    bad[20] ^= 1
    assert aprswx.davis_loop(bytes(bad)) is None


def test_weather_from_a_file_in_any_units():
    assert aprswx.from_file('{"temp_f": 50, "wind_mph": 10, "pressure_inhg": 29.92, "junk": 1}') == \
        {"temp_c": 10.0, "wind_kmh": 16.1, "pressure_hpa": 1013.2}
    assert aprswx.from_file("time,temp_c,humidity\n1,9.5,80\n2,10.5,81\n") == {"temp_c": 10.5, "humidity_pct": 81.0}
    assert aprswx.from_file("not json {") in (None, {})


# -- objects, items, telemetry -----------------------------------------------------

def test_object_and_item_formats_parse_back():
    text = aprs.format_object("NET", 55.95, -3.19, "/r", "Net 2000 on 145.500", when=T0)
    assert text == ";NET      *211413z5557.00N/00311.40WrNet 2000 on 145.500"
    p = aprs.parse(frame("GM4BRI", text))
    assert p.kind == "object" and p.name == "NET" and p.live and round(p.position.lat, 2) == 55.95
    dead = aprs.parse(frame("GM4BRI", aprs.format_object("NET", 55.95, -3.19, "/r", live=False, when=T0)))
    assert dead.kind == "object" and not dead.live
    item = aprs.parse(frame("GM4BRI", aprs.format_item("CAR1", 55.9, -3.2, "/>", "parked")))
    assert item.kind == "item" and item.name == "CAR1" and item.live


def test_telemetry_formats_parse_back():
    t = aprs.format_telemetry(1005, [12, 300, -4, 7.6], "10100000")
    assert t == "T#005,012,255,000,008,000,10100000"
    assert aprs.parse(frame("GM4BRI", t)).telemetry == {"sequence": "005", "analogue": [12, 255, 0, 8, 0],
                                                        "digital": "10100000"}
    parm = aprs.format_telemetry_meta("GM4BRI-10", "PARM", ["RxPkts", "TxPkts"])
    assert parm == ":GM4BRI-10:PARM.RxPkts,TxPkts"
    assert aprs.parse(frame("GM4BRI-10", parm)).kind == "message"


def test_the_schedule_keeps_its_gaps_and_kills_three_times():
    world = World()
    me = world.station("GM4BRI-10")
    ear = world.station("G0ABC")
    heard = []
    ear.monitors.append(lambda f: heard.append((world.clock(), f.text())))
    sched = AprsSchedule(me, ScheduleConfig(object_every_s=60, weather_every_s=60, telemetry_every_s=60),
                         wall_clock=lambda: T0 + world.clock())
    assert sched.config.object_every_s == 600 and sched.config.weather_every_s == 300
    readings = {"temp_c": 10.0}
    sched.weather_source = lambda: (55.95, -3.19, readings, T0 + world.clock())
    sched.telemetry_source = lambda: [1, 2, 3, 4, 5]
    sched.place(PlacedObject("NET", 55.95, -3.19, "/r", "Net 2000"))
    sched.place(PlacedObject("CAR1", 55.9, -3.2, "/>", item=True))
    world.run(700)
    texts = [t for _, t in heard]
    objects = [t for t in texts if t.startswith(";NET")]
    assert len(objects) == 2                         # at start, then 600 s later
    assert any(t.startswith(")CAR1!") for t in texts)
    assert any(t.startswith("!5557.00N/00311.40W_") for t in texts)
    assert any(t.startswith("T#000,001,002,003,004,005") for t in texts)
    assert any(t.startswith(":GM4BRI-10:PARM.RxPkts") for t in texts)
    beacons = [t for t, x in heard if not x.startswith(":")]
    gaps = [b - a for a, b in zip(beacons, beacons[1:])]
    assert min(gaps) >= 28.0          # 30 s apart at the sender, give or take frame lengths
    heard.clear()
    sched.kill("NET")
    world.run(400)
    kills = [t for _, t in heard if t.startswith(";NET      _")]
    assert len(kills) == 3 and "NET" not in sched.objects
    # A dead weather station sends nothing rather than old numbers.
    sched.weather_source = lambda: (55.95, -3.19, readings, T0)
    heard.clear()
    world.run(900)
    assert not any("_" in t[18:20] for _, t in heard if t.startswith("!"))
