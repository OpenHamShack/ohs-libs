"""The fast modes as app settings (fastsettings.py): checked without
opening anything, and turned into what PlutoLink, the guard, the channel
and the data link take - and those objects accept them."""

from __future__ import annotations

from sdrhelp import FakePluto

from shack_packet import Station, adapt
from shack_packet.engine import Engine
from shack_packet.fastsettings import PROBLEMS, FastSettings, wide_data_notes, wide_data_refusal
from shack_packet.radios import PlutoLink


def test_defaults_are_sane_and_bad_values_are_named():
    assert FastSettings().problems() == []
    bad = FastSettings(window=64, paclen=4096, ptt_output="gpio", relay_lead_ms=900, max_tx_gain_db=3,
                       turnaround_ms=-1)
    assert set(bad.problems()) == {"fast-window", "fast-paclen", "fast-ptt-output", "fast-relay", "fast-drive-cap",
                                   "fast-turnaround"}
    assert FastSettings(mode="8psk").problems() == ["fast-unknown-mode"]
    assert FastSettings(adapt=True, top_mode="g3ruh9600", floor_mode="4fsk76800").problems() == ["fast-ladder"]
    assert FastSettings(adapt=True, adapt_start="4fsk38400").problems() == ["fast-adapt-start"]
    assert all(k in PROBLEMS for k in bad.problems())
    s = FastSettings(adapt=True, window=63)
    assert FastSettings.from_dict({**s.to_dict(), "unknown": 1}) == s


def test_the_band_plan_check_follows_the_widest_mode_used():
    assert FastSettings(mode="gfsk76800").problems(1_298_500_000) == ["fast-out-of-plan"]
    assert FastSettings(mode="gfsk76800").problems(1_299_525_000) == []
    assert FastSettings(adapt=True).problems(1_299_525_000) == []                     # 4fsk76800, 141 kHz
    assert FastSettings(adapt=True, top_mode="gfsk153600").problems(1_299_525_000) == ["fast-out-of-plan"]
    assert FastSettings(adapt=True, top_mode="gfsk153600").problems(2_312_000_000) == []
    assert FastSettings(mode="g3ruh9600").problems(432_650_000) == []                # 10 kHz: not this check's
    assert wide_data_refusal(1_299_700_000, "4fsk76800")                             # over the top edge
    assert wide_data_notes(1_299_525_000, "4fsk76800", 200_000) == []
    assert "image" in wide_data_notes(1_299_225_000, "4fsk76800", 200_000)[0]


def test_the_objects_take_what_the_settings_make():
    s = FastSettings(adapt=True, window=63, paclen=1024, ptt_output="gpo1", max_tx_gain_db=-20.0,
                     adapt_start="gfsk76800", turnaround_ms=30)
    radio = FakePluto()
    link = PlutoLink("ip:192.168.2.1", 1_299_525_000, iio_module=radio.module(), allow_unguarded=True,
                     tx_gain_db=-25.0, **s.pluto_kwargs())
    assert link.modes == adapt.ladder() and link.ptt_output == "gpo1" and link.max_tx_gain_db == -20.0
    assert link.turnaround_s == 0.03
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link, channel=s.channel_config(), link=s.link_config())
    assert eng.link_config.adapt == adapt.ladder() and eng.link_config.adapt_start == "gfsk76800"
    assert eng.link_config.k == 63 and eng.link_config.modulo == 128 and eng.channel.adaptive
    assert s.guard_kwargs() == {"gpo": True, "max_gain_db": -20.0}
    fixed = FastSettings(mode="g3ruh38400", window=7)
    link = PlutoLink("ip:192.168.2.1", 432_650_000, iio_module=FakePluto().module(), allow_unguarded=True,
                     **fixed.pluto_kwargs())
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link, channel=fixed.channel_config(),
                 link=fixed.link_config())
    assert eng.channel.mode.name == "g3ruh38400" and eng.link_config.modulo == 8 and not eng.channel.adaptive
