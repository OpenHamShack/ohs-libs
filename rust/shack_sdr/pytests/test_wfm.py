"""Broadcast FM through shack_sdr's Python face, as Radio and the band
scanner will drive it: the stream receiver on the test transmitter's IQ
(`_native.testsig_wfm`) with stereo PCM, RDS fields and their times, a
retune clearing them, the 12 kHz mono form, the composite input; the
`wfm` Host session with its sound in the ring and RDS as meta; the band
scan on a synthetic capture; PTY names through the catalog; refusals as
codes; and the GIL released while it works."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

import shack_sdr
from shack_sdr import DecoderError, Host, _native, wfm
from shack_sdr import i18n

T0 = 1_792_663_200.0


def iq(**kw) -> np.ndarray:
    return np.frombuffer(_native.testsig_wfm(**kw), np.complex64)


def tone(x: np.ndarray, hz: float, rate: float = 48_000.0) -> float:
    n = np.arange(len(x))
    return 2 * abs(np.sum(x * np.exp(-2j * np.pi * hz * n / rate))) / len(x)


def run(rx: wfm.Receiver, x: np.ndarray, block: int = 4_800) -> np.ndarray:
    return np.concatenate([rx.process(x[k:k + block], T0 + k / rx.rate) for k in range(0, len(x), block)])


def test_the_receiver_plays_stereo_and_reads_rds():
    rx = wfm.Receiver()
    pcm = run(rx, iq(seconds=5.0, cnr_db=30.0, left_hz=1_000.0))
    assert pcm.dtype == np.float32 and pcm.shape == (5 * 48_000, 2)
    l, r = tone(pcm[96_000:, 0], 1_000.0), tone(pcm[96_000:, 1], 1_000.0)
    assert abs(l - 0.5 * 0.87 * 0.5) < 0.005 and 20 * np.log10(l / r) > 40
    f = rx.rds()
    assert (f["pi"], f["ps"], f["pty"], f["ptyName"]) == ("C201", "TEST FM ", 10, "Pop music")
    assert f["rt"] == "Hello from the test transmitter"
    assert f["stereo"] is True and f["tp"] is False and f["ta"] is False
    # Times from the samples' t0, within what was fed.
    assert T0 < f["ts"] <= T0 + 5.0
    st = rx.status()
    assert st["stereo"] and st["blend"] == 1.0 and st["rdsSync"] and st["blockErrors"] == 0
    assert 38.0 < st["snrDb"] < 50.0
    rx.reset()
    f = rx.rds()
    assert f["pi"] is None and f["ps"] is None and f["ptyName"] is None and f["af"] == []
    assert not rx.status()["stereo"]


def test_no_pilot_is_mono_and_the_12_khz_mono_form_keeps_wfm_py_s_level():
    rx = wfm.Receiver(audio_rate=12_000, channels=1)
    pcm = run(rx, iq(seconds=2.0, left_hz=1_000.0, right_hz=1_000.0, pilot=False, pi=None))
    assert pcm.shape == (24_000, 1)
    # A full-scale programme is 0.87 of the deviation; wfm.py's 0.5 for full deviation.
    assert abs(tone(pcm[6_000:, 0], 1_000.0, 12_000.0) - 0.5 * 0.87 * 0.5) < 0.005
    assert not rx.status()["stereo"] and rx.rds()["pi"] is None


def test_the_composite_in_gives_the_same_station():
    rx = wfm.Receiver()
    mpx = np.frombuffer(_native.testsig_wfm(seconds=3.0, composite=True), np.float32)
    pcm = rx.process_composite(mpx, T0)
    assert pcm.shape == (3 * 48_000, 2)
    assert rx.rds()["ps"] == "TEST FM " and rx.status()["levelDb"] is None


def test_refusals_are_codes():
    with pytest.raises(DecoderError) as e:
        wfm.Receiver(rate=250_000)
    assert e.value.args == ("bad_input", "rate")
    with pytest.raises(DecoderError) as e:
        wfm.Receiver(audio_rate=44_100)
    assert e.value.args == ("bad_input", "audio_rate")
    rx = wfm.Receiver()
    with pytest.raises(DecoderError) as e:
        rx.set_deemphasis(500)
    assert e.value.args == ("bad_params", "deemphasis")
    rx.set_deemphasis(75)
    rx.set_stereo(False)
    rx.set_blend(False)


def test_pty_names_are_the_european_table_through_the_catalog():
    assert wfm.pty_name(1) == "News" and wfm.pty_name(31) == "Alarm" and wfm.pty_name(0) == "No programme type"
    # RBDS would call 10 "Top 40"; the European table's is pop music.
    assert wfm.pty_name(10) == "Pop music"
    assert wfm.pty_name(None) is None and wfm.pty_name(32) is None
    i18n.use(["xx"])           # no such catalog: English, never a failure
    try:
        assert wfm.pty_name(5) == "Education"
    finally:
        i18n.use(None)


def test_the_gil_is_released_while_it_works():
    x = iq(seconds=20.0)
    rx = wfm.Receiver()
    done = threading.Event()

    def work():
        rx.process(x, T0)
        done.set()

    th = threading.Thread(target=work)
    count = 0
    th.start()
    while not done.is_set():
        count += 1
    th.join()
    # 20 s of signal takes this host a few hundred ms; Python kept counting.
    assert count > 100_000, count


def test_the_wfm_host_session_fills_its_audio_ring_and_reports_rds_as_meta():
    entry = next(e for e in shack_sdr.catalogue() if e["id"] == "wfm")
    assert entry["family"] == "broadcast" and entry["panel"] == "meta" and entry["inputs"] == ["iq", "audio"]
    assert entry["available"] and entry["fixed"] and entry["rateHz"] == 240_000.0
    assert {p["key"] for p in entry["params"]} == {"deemphasis", "stereo", "blend", "rds"}
    x = iq(seconds=4.0, cnr_db=30.0, left_hz=1_000.0)
    with Host() as host:
        sid = host.start("wfm", {"deemphasis": 50}, "iq", 240_000)
        pcm, items = [], []
        for k in range(0, len(x), 24_000):
            host.feed(sid, x[k:k + 24_000], T0 + k / 240_000, k)
            end = time.monotonic() + 5
            # Radio's 12 kHz mono, converted by the host from 48 kHz stereo.
            while sum(len(p) for p in pcm) < (k + 24_000) // 20 - 50 and time.monotonic() < end:
                items += host.poll()
                pcm += [a["pcm"] for a in host.audio(sid, rate=12_000, channels=1)]
                time.sleep(0.002)
        items += host.poll()
        y = np.concatenate(pcm)
        assert abs(tone(y[24_000:], 1_000.0, 12_000.0) - 0.5 * 0.87 * 0.25) < 0.01
        assert not any(i["type"] == "audio" for i in items)
        metas = [i for i in items if i["type"] == "meta"]
        last = metas[-1]["fields"]
        assert (last["pi"], last["ps"], last["pty"], last["stereo"]) == ("C201", "TEST FM ", 10, True)
        assert wfm.pty_name(last["pty"]) == "Pop music"
        assert T0 < metas[-1]["t"] <= T0 + 4.0
        st = host.status(sid)
        assert st["meta"]["ps"] == "TEST FM " and st["stats"]["stereo"] == 1 and st["stats"]["blocks"] > 100
        host.configure(sid, {"stereo": False, "deemphasis": 75})
        with pytest.raises(DecoderError):
            host.configure(sid, {"deemphasis": 60})


def test_the_band_scan_names_every_station():
    stations = [(-1_500_000.0, -25.0, 0xC201, "RADIO 1 ", 10, True),
                (-300_000.0, -45.0, 0xC202, "RADIO 2 ", 1, False),
                (1_200_000.0, -35.0, 0xC2D5, "LOCAL   ", 3, True)]
    x = np.frombuffer(_native.testsig_fm_band(stations, rate=4_000_000.0, seconds=2.0, noise_db=-55.0), np.complex64)
    r = wfm.scan_band(x, 4_000_000, 98_000_000, budget_s=20.0, report=True)
    assert set(r) == {"stations", "usedS", "timedOut", "cpuS", "wallS", "noiseDb"}
    found = r["stations"]
    assert [s["hz"] for s in found] == [96_500_000, 97_700_000, 99_200_000]
    for s, (off, level, pi, ps, pty, stereo) in zip(found, stations):
        assert s["pi"] == f"{pi:04X}" and s["ps"] == ps and s["pty"] == pty and s["stereo"] == stereo
        assert s["ptyName"] == wfm.pty_name(pty) and s["complete"] and abs(s["levelDb"] - level) < 1.5
    assert not r["timedOut"] and r["usedS"] < 2.0
    # The plain form is the list.
    assert [s["hz"] for s in wfm.scan_band(x, 4_000_000, 98_000_000)] == [96_500_000, 97_700_000, 99_200_000]
    with pytest.raises(DecoderError):
        wfm.scan_band(x, 100_000, 98_000_000)
