"""IcomRig, the first Rig backend: its state, its commands, its supervisor,
the model table, and make_rig's profile rules.

Ported from OpenHamShack Icom's tools/selftest.py (test_commands_need_a_radio,
test_taking_the_radio_by_hand, test_radio_models, the rig half of
test_data_mode and test_spectrum) and the radio-level half of
selftest_lanstreams.py (audio and the scope against FakeIcom; the HTTP and
WebSocket half stays with the app). Transmit is test_ptt.py.
"""

import asyncio
import time

import pytest

import shack_rig
from helpers import OnFake, Wire, nowhere, payloads, say, sent, until, wired
from shack_rig.icom import civ, models
from shack_rig.icom.build import make_rig
from shack_rig.icom.link import LanLink
from shack_rig.icom.usb import UsbLink
from shack_rig.rigctl import _dump_state, modes_mask

NINE = models.model("ic-9700")
SIXTEEN = models.model("ic-7610")
FIVE = models.model("ic-705")


def test_it_is_a_rig_and_fills_every_state_key():
    r = nowhere(NINE)
    assert isinstance(r, shack_rig.Rig)
    assert set(shack_rig.STATE_KEYS) <= set(r.state)


# -- commands with no radio --------------------------------------------------

def test_a_command_that_reaches_no_radio_says_so_and_moves_nothing():
    r = nowhere()
    before = dict(r.state)
    assert r.send(b"\x00") is False
    assert r.set_freq(144_800_000) is False
    assert r.set_mode("FM") is False
    assert r.set_vfo("VFOB") is False
    assert r.set_split(True) is False
    assert r.set_scope(False) is False
    assert r.set_data(True) is False
    for k in ("freq", "mode", "vfo", "data_mode"):
        assert r.state[k] == before[k], f"the {k} shown must not move for a command that went nowhere"


def test_a_command_that_left_moves_the_cache_at_once():
    r = wired(NINE)
    assert r.set_freq(144_300_000) and sent(r) == civ.set_freq(144_300_000)
    assert r.state["freq"] == 144_300_000
    assert r.set_vfo("VFOB") and sent(r) == b"\x07\x01" and r.state["vfo"] == "VFOB"
    assert r.set_split(True) and sent(r) == b"\x0f\x01" and r.state["split"] is True


def test_a_mode_the_radio_has_not_got_is_not_sent():
    r = wired(SIXTEEN)
    assert r.set_mode("DD") is False and r._link.frames == []
    r = wired(NINE)
    assert r.set_mode("PSK") is False and r._link.frames == []


# -- reading what the radio says ---------------------------------------------

def test_frames_from_the_radio_fill_the_cache():
    r = wired(NINE)
    say(r, b"\x03" + civ.encode_freq(432_174_000))
    say(r, b"\x15\x02\x01\x20")
    say(r, b"\x0f\x01")
    assert r.state["freq"] == 432_174_000
    assert r.state["smeter"] == 120 and r.state["smeter_db"] == 0.0
    assert r.state["split"] is True
    say(r, b"\x0f\x12")                               # DUP+ with split off (guide p. 4)
    assert r.state["split"] is False
    say(r, b"\x00" + civ.encode_freq(432_175_000))    # the dial moved
    assert r.state["freq"] == 432_175_000


def test_our_own_echo_and_other_addresses_are_not_answers():
    r = wired(NINE)
    r._on_civ_data(civ.frame(b"\x03" + civ.encode_freq(1), to=0xA2, frm=0xE0))   # our echo
    r._on_civ_data(civ.frame(b"\x03" + civ.encode_freq(2), to=0xE0, frm=0x98))   # another rig
    assert r.state["freq"] is None


def test_a_listener_hears_each_change_once():
    r = wired(NINE)
    seen = []
    stop = r.listen(lambda st: seen.append(st["freq"]))
    say(r, b"\x03" + civ.encode_freq(144_174_000))
    say(r, b"\x03" + civ.encode_freq(144_174_000))
    assert seen == [144_174_000]
    stop()
    say(r, b"\x03" + civ.encode_freq(144_175_000))
    assert seen == [144_174_000]


# -- DATA on the rig ---------------------------------------------------------

def test_the_data_flag_off_the_wire():
    r = wired(NINE)
    say(r, bytes([0x26, civ.VFO_SELECTED, 0x01, 0x01, 0x02]))
    assert (r.state["mode"], r.state["data_mode"], r.state["filter"], r.state["mode_label"]) == \
        ("USB", True, "FIL2", "USB-D")
    say(r, bytes([0x26, civ.VFO_SELECTED, 0x01, 0x00, 0x02]))
    assert r.state["data_mode"] is False and r.state["mode_label"] == "USB"
    say(r, bytes([0x26, civ.VFO_SUB, 0x05, 0x00, 0x01]))
    assert r.state["mode"] == "USB", "a Sub-band frame is not the selected VFO"
    # The mode knob: 0x01 carries no data flag at all.
    say(r, bytes([0x26, civ.VFO_SELECTED, 0x01, 0x01, 0x01]))
    say(r, bytes([0x01, 0x00, 0x01]))                # to LSB
    assert r.state["mode"] == "LSB" and r.state["data_mode"] is True
    say(r, bytes([0x01, 0x03, 0x01]))                # to CW
    assert r.state["mode_label"] == "CW", "never CW-D"


def test_setting_the_data_flag():
    r = wired(NINE)
    say(r, bytes([0x26, civ.VFO_SELECTED, 0x05, 0x00, 0x01]))    # FM, no data
    assert r.set_mode("USB", data=True) and sent(r).hex(" ") == "26 00 01 01 01"
    assert r.state["mode_label"] == "USB-D"
    assert r.set_mode("USB-D") and sent(r)[3] == 0x01
    assert r.set_data(False) and sent(r).hex(" ") == "26 00 01 00 01"
    r.set_mode("USB", data=True)
    # Changing the filter re-sends the mode, and must not drop the flag.
    assert r.set_mode(r.state["mode"], 2) and sent(r).hex(" ") == "26 00 01 01 02"
    before = len(r._link.frames)
    assert r.set_mode("CW", data=True) is False and len(r._link.frames) == before


# -- the spectrum on the rig -------------------------------------------------

def test_a_spectrum_off_the_wire_reaches_listeners_not_the_state():
    levels = bytes(range(160)) + bytes(315)
    fixed = (b"\x27\x00\x00\x01\x01\x01" + civ.encode_freq(144_000_000)
             + civ.encode_freq(146_000_000) + b"\x00" + levels)
    r = nowhere()
    seen = []
    stop = r.listen_scope(seen.append)
    r._on_civ_data(civ.frame(fixed, to=0xE0, frm=r.radio_addr))
    assert len(seen) == 1 and seen[0]["points"] == civ.SCOPE_LEN_MAX
    assert r.spectrum() is not None
    stats = r.scope_stats()
    assert stats["spectra"] == 1 and stats["start"] == 144_000_000
    assert r.state["scope_points"] == civ.SCOPE_LEN_MAX and "data" not in r.state
    stop()
    r._on_civ_data(civ.frame(fixed, to=0xE0, frm=r.radio_addr))
    assert len(seen) == 1


# -- connecting and disconnecting by hand ------------------------------------

def test_connect_and_disconnect_are_intentions_the_supervisor_obeys():
    async def run():
        built = []

        def factory():
            w = Wire()
            w.last_received = time.monotonic()
            built.append(w)
            return w

        r = shack_rig.icom.IcomRig(make_link=factory, where="192.0.2.1:50001")
        await r.start()
        try:
            await asyncio.sleep(0.1)
            assert r.state["connected"] and r.state["link_state"] == "connected"
            await r.connect()
            await r.connect()
            await asyncio.sleep(0.1)
            assert len(built) == 1, "Connect on a connected radio makes no second link"
            await r.disconnect()
            assert r.state["connected"] is False and built[0].connected is False
            assert r.state["link_state"] == "disconnected"
            await asyncio.sleep(1.5)                 # past the first backoff
            assert len(built) == 1 and not r.state["connected"], "and it stays down"
            await r.connect()
            await asyncio.sleep(0.1)
            assert r.state["connected"] and len(built) == 2
        finally:
            await r.stop()

    asyncio.run(run())


def test_disconnecting_mid_handshake_leaves_nothing_running():
    class Slow(Wire):
        async def start(self):
            await asyncio.sleep(0.3)

    async def run():
        made = []

        def slowly():
            w = Slow()
            w.last_received = time.monotonic()
            made.append(w)
            return w

        r = shack_rig.icom.IcomRig(make_link=slowly, where="192.0.2.1:50001")
        await r.start()
        try:
            await asyncio.sleep(0.05)
            await r.disconnect()
            await asyncio.sleep(0.5)
            assert made[0].connected is False
            assert r._link is None and r.state["link_state"] == "disconnected"
        finally:
            await r.stop()

    asyncio.run(run())


def test_the_link_word_and_a_reason_that_does_not_blink_out():
    r = nowhere()
    r._set(wanted=True, connected=False, error=None)
    assert r.state["link_state"] == "connecting"
    r._set(error="the radio is in use by SHACK-PC")
    assert r.state["link_state"] == "failed"
    r._set(connected=True)
    assert r.state["link_state"] == "connected"
    link = Wire()
    link.connected = False
    link.error = "the radio rejected that username and password"
    r._follow(link)
    assert r.state["error"] == link.error
    link.error = None
    r._follow(link)
    assert r.state["error"] == "the radio rejected that username and password"
    link.connected = True
    r._follow(link)
    assert r.state["error"] is None and r.state["link_state"] == "connected"


# -- the model table ---------------------------------------------------------

def test_the_radios_differ_in_the_table_and_nowhere_else():
    assert {"ic-9700", "ic-7610", "ic-705"} <= set(models.MODELS)
    assert len({NINE.civ_address, SIXTEEN.civ_address, FIVE.civ_address}) == 3
    with pytest.raises(ValueError):
        models.model("ic-7300")
    assert not NINE.can_transmit(14_074_000) and SIXTEEN.can_transmit(14_074_000)
    assert FIVE.can_transmit(14_074_000) and not SIXTEEN.can_transmit(1_296_200_000)
    assert FIVE.covers(98_000_000) and not NINE.covers(98_000_000)
    assert NINE.transmit_bands_text() == "144-146 MHz, 430-440 MHz, 1240-1300 MHz"
    assert NINE.receive_bands_text() == "144-148 MHz, 430-450 MHz, 1240-1300 MHz"
    assert NINE.has_mode("DD") and not SIXTEEN.has_mode("DD")
    assert SIXTEEN.has_mode("PSK") and not NINE.has_mode("PSK")
    assert FIVE.has_mode("WFM")
    for m in ("USB", "LSB", "FM", "AM"):
        assert NINE.has_data_mode(m)
    for m in ("CW", "CWR", "RTTY", "DV", "DD"):
        assert not NINE.has_data_mode(m)
    assert not SIXTEEN.has_data_mode("PSK")
    assert NINE.public()["civ_address"] == "0xA2" and NINE.public()["maker"] == "icom"
    assert isinstance(NINE, shack_rig.RigModel)


def test_the_9700_tunes_its_hardware_coverage_and_keys_only_its_licence_floor():
    # Receive is what an IC-9700 tunes - the widest version in Icom's service
    # manual - and transmit stays the Region 1 floor. 146.1 is where Brian's
    # radio sat, from its own panel, while the app refused to step it up.
    for hz in (146_100_000, 146_500_000, 147_999_999, 445_000_000):
        assert NINE.covers(hz) and not NINE.can_transmit(hz), hz
    for hz in (143_999_999, 148_000_001, 429_999_999, 450_000_001, 1_300_000_001):
        assert not NINE.covers(hz), hz
    assert NINE.can_transmit(145_500_000) and NINE.can_transmit(1_296_200_000)
    # The band buttons follow transmit, so nothing here grew a fourth band.
    assert [b["from"] for b in NINE.public()["bands"]] == [144_000_000, 430_000_000, 1_240_000_000]


def test_the_table_reaches_what_a_hamlib_client_is_told():
    masks = {k: modes_mask(models.model(k).modes) for k in ("ic-9700", "ic-7610", "ic-705")}
    assert len(set(masks.values())) == 3
    assert masks["ic-9700"] & 0x1000000 and not masks["ic-7610"] & 0x1000000
    for key in ("ic-9700", "ic-7610", "ic-705"):
        rig = models.model(key)
        dump = _dump_state(rig)
        for lo, hi in rig.rx_ranges + rig.tx_ranges:
            line = next((ln for ln in dump.splitlines() if ln.startswith(f"{lo} {hi} ")), None)
            assert line is not None and len(line.split()) == 7, (key, lo, hi, line)


def test_band_buttons_land_on_activity_not_the_middle():
    names = {b["name"]: b["hz"] for b in shack_rig.band_buttons(NINE)}
    assert names == {"2m": 144_300_000, "70cm": 432_200_000, "23cm": 1_296_200_000}


# -- make_rig: a profile becomes a rig ---------------------------------------

def test_make_rig_takes_the_model_and_its_address_from_the_profile():
    r = make_rig({"model": "ic-7610", "host": "192.0.2.1"})
    assert r.model is SIXTEEN and r.radio_addr == 0x98
    assert r.where() == "192.0.2.1:50001"
    r = make_rig({"model": "ic-7610", "host": "192.0.2.1", "civ_address": "0x99"})
    assert r.radio_addr == 0x99, "an explicit address wins"
    r = make_rig({"model": "ic-7610", "civ_address": "nonsense"})
    assert r.radio_addr == 0x98, "an unreadable one falls back to the model's"
    r = make_rig({"model": "ic-9999"})
    assert r.model is NINE, "an unknown model falls back rather than refusing to build"


def test_make_rig_asks_for_a_transmit_stream_only_when_transmit_is_enabled():
    off = make_rig({"host": "192.0.2.1", "audio_rate": 12000})._make_link()
    on = make_rig({"host": "192.0.2.1", "audio_rate": 12000}, tx_enabled=True)._make_link()
    assert isinstance(off, LanLink) and off.want_tx_rate == 0 and off.audio_rate == 12000
    assert on.want_tx_rate == 12000
    assert make_rig({}, tx_enabled=False).allow_ptt is False
    assert make_rig({}, tx_enabled=True).allow_ptt is True


def test_make_rig_builds_a_usb_link_that_will_probe():
    seen = []
    r = make_rig({"model": "ic-7610", "transport": "usb", "audio_rate": 48000},
                 busy_ports=lambda: seen.append(1) or ("/dev/ttyUSB9",))
    link = r._make_link()
    assert isinstance(link, UsbLink) and link.address == 0x98 and link.model_key == "ic-7610"
    assert r.audio_rate == 12000, "USB audio is always 12 kHz out of usbaudio"
    assert link.device == "" and link.baud == 0, "blank means probe for it"
    assert seen == [], "busy_ports is asked when the probe runs, not when the rig is built"
    assert make_rig({"transport": "carrier-pigeon", "host": "192.0.2.1"})._make_link().__class__ is LanLink


# -- end to end on FakeIcom --------------------------------------------------

def test_on_the_fake_radio_the_poll_fills_the_state_and_tuning_reaches_it():
    async def run():
        async with OnFake() as (rig, radio):
            assert rig.state["freq"] == 144_174_000
            assert await until(lambda: rig.state["mode_label"] == "USB-D", 2)
            assert rig.state["radio"] is not None
            assert rig.set_freq(432_174_000)
            assert await until(lambda: radio.freq == 432_174_000, 2)
            req = radio.stream_requests[-1]
            assert req["rx_enable"] == 1 and req["rx_codec"] == 0x04 and req["rx_rate"] == 12000

    asyncio.run(run())


def test_a_wedged_civ_stream_is_logged_out_of_and_in_again(monkeypatch):
    """The 10/10/2026 failure on ohs-box: after a module was killed without
    logging out, the radio handed its successor a CI-V stream that pinged and
    idled and never carried a frame. Asking again changed nothing; a clean
    logout and a fresh login did. The fake wedges the first session the same
    way, and the rig must recover by itself and say why meanwhile."""
    from shack_rig.icom import lan
    from shack_rig.icom.fake import FakeIcom
    monkeypatch.setattr(lan, "CIV_WEDGED_S", 0.8)
    monkeypatch.setattr(shack_rig.icom.IcomRig, "WEDGE_PAUSE_S", 0.2)

    async def run():
        radio = FakeIcom(username="op", password="secret")
        radio.wedge_civ_sessions = 1
        await radio.start()
        rig = make_rig({"model": "ic-9700", "host": "127.0.0.1",
                        "port": radio.control_port, "username": "op"},
                       password="secret", poll_interval=0.1)
        errors = []
        rig.listen(lambda st: errors.append(st.get("error")))
        await rig.start()
        try:
            assert await until(lambda: rig.state["freq"] == 144_174_000, 8), rig.state
            assert radio.civ_sessions == 2, "one wedged session, then one that works"
            assert radio.logouts >= 1, "the wedged session was logged out of, not abandoned"
            assert 0x01 in radio.tokens, "and its token handed back"
            assert any(e and "CI-V" in e for e in errors), "the page was told why"
        finally:
            await rig.stop()
            await radio.stop()

    asyncio.run(run())


def test_a_refused_login_is_not_tried_again_until_asked():
    """GM0OTB's IC-705 on 19/10/2026 with a wrong password: the rig logged in
    again every 1.25 s for as long as the app ran. Now one refusal stops it,
    the reason stays on the page, and connect() (the app's Reconnect) asks
    once more."""
    from shack_rig.icom.fake import FakeIcom
    from shack_rig.icom.lan import AUTH_REJECTED

    async def run():
        radio = FakeIcom(username="op", password="secret")
        tries = []
        accepts = radio.accepts
        radio.accepts = lambda u, p: tries.append(1) or accepts(u, p)
        await radio.start()
        rig = make_rig({"model": "ic-9700", "host": "127.0.0.1", "port": radio.control_port, "username": "op"},
                       password="wrong", poll_interval=0.1)
        await rig.start()
        try:
            assert await until(lambda: rig.state["error"] == AUTH_REJECTED, 5), rig.state
            await asyncio.sleep(3.0)
            assert len(tries) == 1 and rig.state["error"] == AUTH_REJECTED
            assert rig.state["link_state"] == "failed" and rig.wanted
            await rig.connect()
            assert await until(lambda: len(tries) == 2, 5)
            assert await until(lambda: rig.state["error"] == AUTH_REJECTED, 5)
        finally:
            await rig.stop()
            await radio.stop()

    asyncio.run(run())


def test_other_failures_back_off_and_a_connection_resets_the_backoff():
    """A link that fails with any other reason is rebuilt after 2, 4, 8 ...
    seconds (to a minute), not every 1.25 s: the pause only goes back to
    the start once a link has connected."""
    async def run():
        built = []

        def factory():
            w = Wire()
            w.connected = False
            w.error = "the radio is in use by SHACK-PC"
            w.last_received = time.monotonic()
            built.append(w)
            return w

        r = shack_rig.icom.IcomRig(make_link=factory, where="192.0.2.1:50001")
        await r.start()
        try:
            await asyncio.sleep(7.5)
            # Attempts at about 0, 2.25 and 6.5 s; the old loop made six.
            assert len(built) == 3, len(built)
        finally:
            await r.stop()

    asyncio.run(run())


def test_on_the_fake_radio_receive_audio_is_an_unbroken_run_of_what_it_sent():
    async def run():
        async with OnFake() as (rig, radio):
            got = bytearray()
            assert await until(lambda: rig.receivers(), 3)
            stop = rig.listen_audio(got.extend)
            await asyncio.sleep(1.0)
            stop()
            sent_pcm = bytes(radio.pcm_sent)
            assert len(got) > 12000, len(got)              # ~1 s at 12 kHz, 16-bit
            assert bytes(got) in sent_pcm, "a gap, repeat or reordering would not be a run"
            assert rig.audio_stats()["bytes"] >= len(got)

    asyncio.run(run())


def test_on_the_fake_radio_the_scope_output_is_on_only_while_watched(monkeypatch):
    monkeypatch.setattr(shack_rig.icom.IcomRig, "SCOPE_RELEASE_S", 1.0)

    async def run():
        async with OnFake() as (rig, radio):
            await asyncio.sleep(1.5)
            assert not [c for c in radio.civ_commands if c[:2] == b"\x27\x11"]
            spectra = []
            stop = rig.listen_scope(spectra.append)
            assert await until(lambda: spectra, 5)
            s = spectra[-1]
            assert (s["start"], s["end"]) == (144_149_000, 144_199_000), "edges, not centre"
            stop()
            assert await until(lambda: not radio.scope_output, 5)
            assert radio.scope_display, "the radio's own scope is the operator's; never switched off"

    asyncio.run(run())


# -- power on and off, and a radio in standby ----------------------------------

def test_power_goes_out_with_the_links_preamble_and_needs_a_link():
    assert nowhere().set_power(True) is False

    r = wired(NINE)
    assert r.set_power(False) and r._link.frames[-1] == civ.frame(b"\x18\x00")
    # A Wire has no preamble of its own, like the LAN: the frame goes as is.
    assert r.set_power(True) and r._link.frames[-1] == civ.frame(b"\x18\x01")

    # A serial link names its run, and only a power-on carries it.
    r._link.wake_preamble = lambda: b"\xfe" * 13
    assert r.set_power(True)
    assert r._link.frames[-1] == b"\xfe" * 13 + civ.frame(b"\x18\x01")
    assert r.set_power(False) and r._link.frames[-1] == civ.frame(b"\x18\x00")
    for k in ("ptt", "freq", "mode"):
        assert r.state[k] == nowhere(NINE).state[k], "power touches nothing else"


def test_a_radio_answering_only_ng_is_asleep(monkeypatch):
    # The real 9700 in standby answers every poll with a bare NG.
    r = wired(NINE)
    r._connected_at = time.monotonic() - 10
    say(r, b"\xfa")
    r._watch_sleep()
    assert r.state["asleep"] is True, "NG alone is not a radio that is awake"
    say(r, civ.read_freq() + civ.encode_freq(144_174_000))
    assert r.state["asleep"] is False and r.state["freq"] == 144_174_000


def test_the_lan_sends_no_preamble_and_usb_sends_one_for_its_speed():
    assert LanLink("192.0.2.1", 50001, "u", "p").wake_preamble() == b""
    assert UsbLink(device="/dev/null", baud=9600).wake_preamble() == b"\xfe" * 13


def test_on_the_fake_radio_standby_reads_as_asleep_and_power_on_wakes_it(monkeypatch):
    monkeypatch.setattr(shack_rig.icom.IcomRig, "ASLEEP_AFTER_S", 0.5)

    async def run():
        async with OnFake() as (rig, radio):
            assert rig.state["asleep"] is False
            radio.powered = False                  # the operator pressed standby
            assert await until(lambda: rig.state["asleep"], 5)
            assert rig.state["connected"], "the LAN side stays up through standby"
            radio.freq = 432_100_000
            assert rig.set_power(True)
            assert await until(lambda: not rig.state["asleep"], 5)
            assert await until(lambda: rig.state["freq"] == 432_100_000, 5)
            assert radio.power_commands == [True]
            assert radio.ptt_commands == [], "waking never keys"
            # On the LAN the frame reached the radio with no FE run in front.
            assert b"\x18\x01" in radio.civ_commands

    asyncio.run(run())


# -- the receive front end: preamp and attenuator ----------------------------

def test_the_table_says_which_preamps_and_attenuators_each_radio_has():
    assert NINE.public()["preamps"] == ["P.AMP"] and NINE.public()["attenuators"] == [10]
    assert SIXTEEN.public()["preamps"] == ["P.AMP1", "P.AMP2"]
    assert SIXTEEN.public()["attenuators"][0] == 3 and SIXTEEN.public()["attenuators"][-1] == 45
    assert FIVE.public()["attenuators"] == [20]
    assert NINE.has_preamp(1) and not NINE.has_preamp(2) and SIXTEEN.has_preamp(2)
    assert NINE.has_att(0) and NINE.has_att(10) and not NINE.has_att(20)


def test_the_front_end_off_the_wire():
    r = wired(NINE)
    assert r.state["preamp"] is None and r.state["att"] is None, "unknown until the radio says"
    say(r, b"\x16\x02\x03")
    assert r.state["preamp"] == 1 and r.state["preamp_ext"] is True
    say(r, b"\x11\x10")
    assert r.state["att"] == 10
    r = wired(SIXTEEN)
    say(r, b"\x16\x02\x02")
    assert r.state["preamp"] == 2 and r.state["preamp_ext"] is None


def test_a_9700_preamp_set_keeps_the_external_preamp_as_the_radio_has_it():
    r = wired(NINE)
    assert r.set_preamp(1) is False and r._link.frames == [], \
        "with the external preamp unknown, a set would have to guess its supply"
    say(r, b"\x16\x02\x02")                       # external on, internal off
    assert r.set_preamp(1)
    assert payloads(r)[0] == b"\x16\x02\x03", "the external bit goes back as it was"
    # ...and the radio is asked straight away what it now has.
    assert b"\x16\x02" in payloads(r)[1:] and b"\x11" in payloads(r)[1:]
    assert r.state["preamp"] == 1
    r._link.frames.clear()
    assert r.set_preamp(0) and payloads(r)[0] == b"\x16\x02\x02"


def test_levels_the_model_does_not_list_are_not_sent():
    r = wired(NINE)
    say(r, b"\x16\x02\x00")
    assert r.set_preamp(2) is False and r.set_att(20) is False and r.set_att(6) is False
    assert r._link.frames == []
    assert r.set_att(10) and payloads(r)[0] == b"\x11\x10" and r.state["att"] == 10
    r = wired(SIXTEEN)
    assert r.set_preamp(2) and payloads(r)[0] == b"\x16\x02\x02", "no external bit to wait for"
    assert r.set_att(45) and payloads(r)[-3] == b"\x11\x45"
    assert nowhere(NINE).set_att(10) is False, "no link, nothing claimed"
    r = wired(None)
    assert r.set_preamp(1) is False and r.set_att(10) is False, "no model, no levels to trust"


def test_on_the_fake_radio_preamp_and_att_are_read_set_and_kept_per_band():
    async def run():
        async with OnFake() as (rig, radio):
            assert await until(lambda: rig.state["preamp"] == 0 and rig.state["att"] == 0, 3)
            assert rig.state["preamp_ext"] is False
            assert rig.set_preamp(1) and rig.set_att(10)
            assert await until(lambda: radio.preamp == 0x01 and radio.att == 10, 2)
            # The panel's key sends nothing unasked; the poll finds it.
            radio.preamp = 0x00
            assert await until(lambda: rig.state["preamp"] == 0, 3)
            # Another band has its own settings in the radio, read at once.
            assert rig.set_freq(1_296_200_000)
            assert await until(lambda: rig.state["att"] == 0 and rig.state["freq"] == 1_296_200_000, 2)
            assert rig.set_preamp(1)
            assert await until(lambda: radio.preamp == 0x01, 2)
            assert rig.set_freq(144_300_000)
            assert await until(lambda: rig.state["att"] == 10 and rig.state["preamp"] == 0, 2)
            assert radio.ptt_commands == [], "receive settings never key"
            # A value the radio has not got is refused on the radio's side too.
            assert rig.send(civ.set_att(20))
            await asyncio.sleep(0.3)
            assert radio.att == 10

    asyncio.run(run())


# -- the IC-9700's external preamp: whole settings, gated by the radio's menu --

def test_the_9700_offers_four_whole_settings_and_the_hf_radios_their_levels():
    assert NINE.public()["preamp_modes"] == ["OFF", "P.AMP", "EXT", "P.AMP+EXT"]
    assert SIXTEEN.public()["preamp_modes"] == ["OFF", "P.AMP1", "P.AMP2"]
    assert FIVE.public()["preamp_modes"] == ["OFF", "P.AMP1", "P.AMP2"]
    assert nowhere(NINE).state["ext_preamp_menu"] == {"144": None, "430": None, "1200": None}
    assert nowhere(SIXTEEN).state["ext_preamp_menu"] is None, "no such menu on an HF radio"


def test_the_preamp_mode_is_the_byte_and_the_menu_is_read_off_the_wire():
    r = wired(NINE)
    say(r, b"\x16\x02\x02")
    assert r.state["preamp_mode"] == 2 and r.state["preamp"] == 0 and r.state["preamp_ext"] is True
    say(r, b"\x1a\x05\x00\x94\x01")
    say(r, b"\x1a\x05\x00\x93\x00")
    say(r, b"\x1a\x05\x00\x96\x01")              # another menu item: not ours
    assert r.state["ext_preamp_menu"] == {"144": False, "430": True, "1200": None}
    r = wired(SIXTEEN)
    say(r, b"\x16\x02\x02")
    assert r.state["preamp_mode"] == 2, "on an HF radio the mode is the level"


def test_ext_is_sent_only_where_the_radios_menu_says_one_is_fitted():
    r = wired(NINE)
    say(r, b"\x03" + civ.encode_freq(144_300_000))
    for mode in (2, 3):
        assert r.set_preamp_mode(mode) is False and r._link.frames == [], \
            "the menu has not answered: EXT would be a guess at a DC supply"
    assert "not said yet" in r.preamp_mode_refusal(2)
    say(r, b"\x1a\x05\x00\x93\x00")
    assert r.set_preamp_mode(3) is False and r._link.frames == []
    assert "off in the radio's menu" in r.preamp_mode_refusal(3)
    # Internal-only settings need nothing from the menu, and nothing read first.
    assert r.preamp_mode_refusal(1) is None
    assert r.set_preamp_mode(1) and payloads(r)[0] == b"\x16\x02\x01"
    assert r.state["preamp_mode"] == 1 and r.state["preamp_ext"] is False
    r._link.frames.clear()
    say(r, b"\x1a\x05\x00\x93\x01")
    assert r.set_preamp_mode(2) and payloads(r)[0] == b"\x16\x02\x02"
    assert r.state["preamp"] == 0 and r.state["preamp_ext"] is True
    # 70 cm has its own menu entry, still unknown here.
    say(r, b"\x03" + civ.encode_freq(432_100_000))
    r._link.frames.clear()
    assert r.set_preamp_mode(3) is False and r._link.frames == []
    assert r.set_preamp_mode(4) is False and r.set_preamp_mode(-1) is False
    assert r._link.frames == []


def test_an_hf_radio_takes_its_levels_as_modes_with_no_menu_to_ask():
    r = wired(SIXTEEN)
    assert r.set_preamp_mode(2) and payloads(r)[0] == b"\x16\x02\x02"
    assert r.state["preamp"] == 2 and r.state["preamp_mode"] == 2
    assert all(not p.startswith(b"\x1a\x05") for p in payloads(r))
    assert r.set_preamp_mode(3) is False
    assert wired(None).set_preamp_mode(1) is False


def test_on_the_fake_radio_every_setting_round_trips_and_each_band_keeps_its_own():
    async def run():
        async with OnFake() as (rig, radio):
            assert await until(lambda: rig.state["ext_preamp_menu"] == {
                "144": False, "430": False, "1200": False}, 3)
            assert rig.set_preamp_mode(2) is False, "menu off: EXT is not offered"
            # The operator enables the 2 m mast-head preamp in the radio's
            # SET menu; the next connection reads it.
            radio.ext_preamp_menu[93] = 0x01
            await rig.disconnect()
            await rig.connect()
            assert await until(lambda: rig.state["connected"]
                               and rig.state["ext_preamp_menu"]["144"] is True, 10)
            for mode in (1, 2, 3, 0):
                assert rig.set_preamp_mode(mode)
                assert await until(lambda: radio.preamp == mode
                                   and rig.state["preamp_mode"] == mode, 2)
            assert rig.set_preamp_mode(3)
            assert await until(lambda: radio.preamp == 3, 2)
            # 70 cm keeps its own setting, and its menu entry is off.
            assert rig.set_freq(432_100_000)
            assert await until(lambda: rig.state["preamp_mode"] == 0, 2)
            assert rig.set_preamp_mode(2) is False
            assert rig.set_preamp_mode(1)
            assert await until(lambda: radio.preamp == 1, 2)
            # Back on 2 m the radio gives that band's setting back.
            assert rig.set_freq(144_300_000)
            assert await until(lambda: rig.state["preamp_mode"] == 3
                               and rig.state["preamp_ext"] is True, 2)
            assert radio.menu_writes == [], "the External P.AMP menu is never written"
            assert radio.ptt_commands == [], "receive settings never key"

    asyncio.run(run())


# -- the CW pitch (14 09), for a CW decoder ----------------------------------

def test_the_cw_pitch_off_the_wire():
    # The guide's three points: 0000 = 300 Hz, 0128 = 600 Hz, 0255 = 900 Hz.
    r = wired(NINE)
    assert r.state["cw_pitch"] is None, "unknown until the radio says"
    for level, hz in ((b"\x00\x00", 300), (b"\x01\x28", 600), (b"\x02\x55", 900), (b"\x00\x85", 500)):
        say(r, b"\x14\x09" + level)
        assert r.state["cw_pitch"] == hz
    with pytest.raises(civ.CivError):
        civ.cw_pitch_hz(256)


def test_on_the_fake_radio_the_cw_pitch_is_read_in_cw_only():
    async def run():
        async with OnFake() as (rig, radio):
            assert await until(lambda: rig.state["mode"] == "USB", 2)
            await asyncio.sleep(1.2)
            assert b"\x14\x09" not in radio.civ_commands, "nothing asks for it outside CW"
            assert rig.state["cw_pitch"] is None
            radio.mode, radio.data = civ.MODE_CODES["CW"], 0
            radio.cw_pitch_level = 85                       # 500 Hz
            assert await until(lambda: rig.state["cw_pitch"] == 500, 3)
            radio.mode = civ.MODE_CODES["CWR"]
            radio.cw_pitch_level = 170                      # 700 Hz
            assert await until(lambda: rig.state["cw_pitch"] == 700, 3)

    asyncio.run(run())


# -- RF POWER (14 0A), for shack_packet's minimum-power test step -----------

def test_on_the_fake_radio_rf_power_sets_reads_back_and_can_be_ignored():
    async def run():
        async with OnFake() as (rig, radio):
            radio.rf_power_level = 200
            got = await rig.ask(civ.read_rf_power(), b"\x14\x0a")
            assert civ.decode_rf_power(got) == 200
            assert rig.send(civ.set_rf_power(0))
            got = await rig.ask(civ.read_rf_power(), b"\x14\x0a")
            assert civ.decode_rf_power(got) == 0
            assert radio.rf_power_writes == [0]
            # A radio that acks the write but never acts on it - the shape
            # shack_packet's minimum-power step must refuse to key against.
            radio.ignore_rf_power_writes = True
            assert rig.send(civ.set_rf_power(150))
            got = await rig.ask(civ.read_rf_power(), b"\x14\x0a")
            assert civ.decode_rf_power(got) == 0, "the write was ignored, so the level did not move"
            assert radio.rf_power_writes == [0], "no second write recorded"

    asyncio.run(run())


def test_the_fake_radios_receive_audio_can_be_a_tests_own_signal():
    # What an app's decoder selftest feeds through the real path: here a
    # ramp, so a listener can be checked against it sample for sample.
    async def run():
        async with OnFake() as (rig, radio):
            made = bytearray()

            def source(rate, n):
                pcm = bytes((len(made) + i) % 251 for i in range(2 * n))
                made.extend(pcm)
                return pcm

            radio.audio_source = source
            assert await until(lambda: rig.receivers(), 3)
            await asyncio.sleep(0.1)       # counter packets already on their way land first
            got = bytearray()
            stop = rig.listen_audio(got.extend)
            await asyncio.sleep(0.6)
            stop()
            assert len(got) > 4000 and bytes(got) in bytes(made)

    asyncio.run(run())


# -- the scope's zoom, reference level and speed (CI-V 27 14-1D) ----------------

def test_the_table_says_what_each_scope_takes():
    scope = NINE.public()["scope"]
    assert scope["spans_hz"][0] == 5_000 and scope["spans_hz"][-1] == 1_000_000
    assert scope["edges"] == 3 and scope["ref_db"] == {"min": -20.0, "max": 20.0, "step": 0.5}
    assert scope["averaging"] == [0, 2, 3, 4]
    assert SIXTEEN.public()["scope"]["edges"] == 4
    assert models.model("ic-7600").public()["scope"] is None
    assert nowhere(models.model("ic-7600")).state["scope_settings"] is None


def test_scope_settings_go_out_as_the_guide_spells_them():
    r = wired(NINE)
    assert r.set_scope_settings({"span_hz": 5_000, "mode": "center", "edge": 2,
                                 "ref_db": -5.5, "speed": "slow", "vbw": "wide",
                                 "averaging": 3})
    sent_ = payloads(r)
    # Half-span +-2.5 kHz as a frequency, the reference as BCD with its sign,
    # averaging through its own menu item and nothing else of 1A 05's.
    assert b"\x27\x15\x00\x00\x25\x00\x00\x00" in sent_
    assert b"\x27\x14\x00\x00" in sent_ and b"\x27\x16\x00\x02" in sent_
    assert b"\x27\x19\x00\x05\x50\x01" in sent_
    assert b"\x27\x1a\x00\x02" in sent_ and b"\x27\x1d\x00\x01" in sent_
    assert b"\x1a\x05\x01\x92\x02" in sent_
    assert r.state["scope_settings"]["ref_db"] == -5.5
    assert r.state["scope_settings"]["span_hz"] == 5_000


def test_a_bad_scope_setting_sends_nothing_at_all():
    r = wired(NINE)
    for bad in ({"span_hz": 7_000}, {"ref_db": 21}, {"ref_db": float("nan")},
                {"speed": "warp"}, {"edge": 4}, {"averaging": 1}, {"mode": "scroll-C"},
                {"zoom": 2}, {}, {"span_hz": 5_000, "edge": 9}, {"span_hz": True}):
        assert r.scope_setting_refusal(bad), bad
        assert r.set_scope_settings(bad) is False
    assert r._link.frames == []
    assert nowhere(NINE).set_scope_settings({"speed": "fast"}) is False, "no link, nothing sent"


def test_a_reference_level_between_steps_is_rounded_to_half_a_db():
    r = wired(NINE)
    assert r.set_scope_settings({"ref_db": 3.3})
    assert b"\x27\x19\x00\x03\x50\x00" in payloads(r)
    assert civ.decode_ref_db(civ.encode_ref_db(-20.0)) == -20.0
    assert civ.decode_ref_db(civ.encode_ref_db(0.0)) == 0.0


def test_the_radios_answers_fill_scope_settings():
    r = wired(NINE)
    say(r, b"\x27\x15\x00\x00\x00\x05\x00\x00")       # +-50 kHz
    say(r, b"\x27\x19\x00\x12\x50\x01")               # -12.5 dB
    say(r, b"\x27\x14\x00\x01")
    say(r, b"\x27\x1a\x00\x00")
    say(r, b"\x27\x1d\x00\x01")
    say(r, b"\x27\x16\x00\x03")
    say(r, b"\x1a\x05\x01\x92\x03")
    assert r.state["scope_settings"] == {"receiver": 0, "mode": "fixed", "span_hz": 100_000,
                                         "edge": 3, "ref_db": -12.5, "speed": "fast",
                                         "vbw": "wide", "averaging": 4}
    say(r, b"\x27\x19\x01\x05\x00\x00")               # the sub scope's: not ours
    assert r.state["scope_settings"]["ref_db"] == -12.5


def test_on_the_fake_radio_zoom_and_reference_reach_the_radio_and_the_spectrum():
    async def run():
        async with OnFake() as (rig, radio):
            spectra = []
            stop = rig.listen_scope(spectra.append)
            assert await until(lambda: rig.state["scope_settings"]["span_hz"] == 50_000, 5), \
                rig.state["scope_settings"]
            # The reference level is its own frame, answered after the span.
            assert await until(lambda: rig.state["scope_settings"]["ref_db"] == 0.0, 3), \
                rig.state["scope_settings"]
            assert rig.set_scope_settings({"span_hz": 10_000, "ref_db": 7.5, "speed": "slow",
                                           "averaging": 2})
            assert await until(lambda: radio.scope_half_span == 5_000, 2)
            assert await until(
                lambda: spectra and spectra[-1]["end"] - spectra[-1]["start"] == 10_000, 3)
            assert radio.scope_ref == b"\x07\x50\x00" and radio.scope_speed == 0x02
            assert radio.scope_averaging == 0x01
            assert radio.menu_writes == [], "no other menu item was written"
            # And what the radio says wins: the operator turns REF on the panel.
            radio.scope_ref = b"\x02\x00\x01"
            assert await until(lambda: rig.state["scope_settings"]["ref_db"] == -2.0, 3)
            stop()
            assert radio.ptt_commands == []

    asyncio.run(run())


# -- D-STAR: the call sign of the station being heard ------------------------

def test_dv_blocks_are_read_as_the_guide_lays_them_out():
    block = (b"\x00\x00" + b"G4ABC   " + b"/P  " + b"CQCQCQ  " + b"GB7XX  B" + b"GB7XX  G")
    kind, got = civ.parse_dv(b"\x20\x00\x02" + block)
    assert kind == "callsigns"
    assert (got["caller"], got["suffix"], got["ur"], got["rpt1"], got["rpt2"]) == \
        ("G4ABC", "/P", "CQCQCQ", "GB7XX  B", "GB7XX  G")
    kind, got = civ.parse_dv(b"\x20\x01\x01" + b"hello from Leeds    " + b"G4ABC   " + b"/P  ")
    assert kind == "message" and got["message"] == "hello from Leeds" and got["caller"] == "G4ABC"
    assert civ.parse_dv(b"\x20\x00\x02\xff") == ("callsigns", None)
    assert civ.parse_dv(b"\x20\x02\x02\x40")[1]["voice"] is True
    assert civ.parse_dv(b"\x20\x00\x00\x01") is None, "the output setting's answer is not a call"


def test_a_new_dv_over_moves_heard_at_and_a_repeat_read_does_not():
    r = wired(NINE)
    block = b"\x00\x00" + b"G4ABC   " + b"    " + b"CQCQCQ  " + b" " * 16
    say(r, b"\x20\x00\x02" + block)
    first = r.state["dv"]["heard_at"]
    assert r.state["dv"]["caller"] == "G4ABC" and first
    say(r, b"\x20\x00\x02" + block)
    assert r.state["dv"]["heard_at"] == first, "the same over read twice"
    r._link.frames.clear()
    say(r, b"\x20\x02\x02\x50")                     # a voice call starts
    assert r.state["dv"]["receiving"] and r.state["dv"]["heard_at"] >= first
    assert b"\x20\x00\x02" in payloads(r), "the call signs are read at once"


def test_on_the_fake_radio_dv_calls_are_followed_and_the_radio_left_alone():
    async def run():
        async with OnFake() as (rig, radio):
            await asyncio.sleep(0.5)
            assert not [c for c in radio.civ_commands if c[:1] == b"\x20"], "not in DV, not asked"
            radio.mode = 0x17                                   # DV
            assert await until(lambda: rig.state["mode"] == "DV", 3)
            radio.dv_call("M0XYZ", "/M", rpt1="GB7DV  B", rpt2="GB7DV  G",
                          message="mobile on the A1")
            assert await until(lambda: (rig.state["dv"] or {}).get("caller") == "M0XYZ", 3)
            assert await until(lambda: rig.state["dv"]["message"] == "mobile on the A1", 3)
            assert rig.state["dv"]["suffix"] == "/M" and rig.state["dv"]["receiving"]
            radio.dv_end()
            assert await until(lambda: not rig.state["dv"]["receiving"], 3)
            assert radio.dv_output_writes == [], "the radio's own output settings untouched"
            assert radio.ptt_commands == []

    asyncio.run(run())
