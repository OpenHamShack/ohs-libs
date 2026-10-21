"""What an Icom gives a packet station: carrier sense from its squelch, a
simplex channel with no tones, FM with the data flag, and DATA MOD on the
codec or the network - read first, changed only through the model's own
table, and put back as found.

Against a wired rig (the bytes) and against FakeIcom (the bytes reaching a
radio that keeps what it is told). shack_packet.radios.IcomLink is the
caller; its own tests key the fake.
"""

import asyncio

import pytest

from helpers import OnFake, nowhere, payloads, say, sent, until, wired
from shack_rig.icom import civ, models

NINE = models.model("ic-9700")
FIVE = models.model("ic-705")


def test_the_squelch_is_read_as_carrier_sense():
    assert civ.read_squelch() == b"\x15\x01"
    assert civ.decode_squelch(b"\x15\x01\x01") is True
    assert civ.decode_squelch(b"\x15\x01\x00") is False
    assert civ.decode_squelch(b"\x15\x01\x00\x01") is True       # 0001, the guide's four digits
    with pytest.raises(civ.CivError):
        civ.decode_squelch(b"\x15\x02\x01")
    r = wired(NINE)
    assert r.state["busy"] is None
    say(r, b"\x15\x01\x01")
    assert r.state["busy"] is True
    say(r, b"\x15\x01\x00")
    assert r.state["busy"] is False
    r.watch_squelch(False)
    assert r.state["busy"] is None, "not watching is not knowing"


def test_simplex_and_every_tone_off_in_four_frames():
    r = wired(NINE)
    assert r.set_simplex() is True
    assert payloads(r) == [b"\x0f\x10", b"\x16\x42\x00", b"\x16\x43\x00", b"\x16\x4b\x00"]
    assert nowhere(NINE).set_simplex() is False


def test_data_mod_is_read_before_it_is_changed_and_put_back_after():
    r = wired(NINE)
    # Not read yet: nothing to put back, so nothing is written.
    assert r.set_data_mod("usb") is False
    assert r._link.frames == []
    assert "read" in r.data_mod_refusal("usb")
    assert r.read_data_mod() is True
    assert sent(r) == b"\x1a\x05\x01\x16"
    say(r, b"\x1a\x05\x01\x16\x00")                       # MIC, as shipped
    assert r.state["data_mod"] == "mic"
    assert r.set_data_mod("usb") is True
    assert sent(r) == b"\x1a\x05\x01\x16\x03"
    assert r.set_data_mod("lan") is True
    assert sent(r) == b"\x1a\x05\x01\x16\x05"
    assert r.set_data_mod("bluetooth") is False             # not a 9700 setting
    assert r.restore_data_mod() is True
    assert sent(r) == b"\x1a\x05\x01\x16\x00"
    assert r.state["data_mod"] == "mic"
    n = len(r._link.frames)
    assert r.restore_data_mod() is True and len(r._link.frames) == n, "already as found"


def test_a_radio_without_a_sourced_data_mod_row_is_never_written():
    r = wired(FIVE)
    assert FIVE.data_mod is None
    assert r.read_data_mod() is False
    assert r.set_data_mod("usb") is False
    assert r._link.frames == []
    why = r.data_mod_refusal("usb")
    assert "IC-705" in why and "USB" in why


def test_the_only_menu_write_builder_is_data_mods():
    assert civ.set_data_mod(116, 3) == b"\x1a\x05\x01\x16\x03"
    with pytest.raises(civ.CivError):
        civ.set_data_mod(116, 0x40)


def test_a_packet_channel_on_the_fake_radio():
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            rig.watch_squelch(True)
            assert await until(lambda: rig.state["busy"] is False, 5)
            radio.squelch_open = True
            assert await until(lambda: rig.state["busy"] is True, 5)
            radio.squelch_open = False
            assert await until(lambda: rig.state["busy"] is False, 5)

            assert rig.set_freq(144_800_000)
            assert rig.set_mode("FM", data=True)
            assert rig.set_simplex()
            assert rig.read_data_mod()
            assert await until(lambda: rig.state["data_mod"] == "mic", 5)
            assert rig.set_data_mod("lan")
            assert await until(lambda: radio.data_mod == 0x05, 5)
            assert await until(lambda: radio.mode == 0x05 and radio.data == 0x01, 5)
            assert radio.duplex == 0x10
            assert set(radio.tone_functions.values()) == {0x00}
            assert await until(lambda: rig.state["mode_label"] == "FM-D", 5)
            assert rig.restore_data_mod()
            assert await until(lambda: radio.data_mod == 0x00, 5)
            assert radio.data_mod_writes == [0x05, 0x00]
            assert radio.menu_writes == [], "no other menu item was written"
    asyncio.run(run())
