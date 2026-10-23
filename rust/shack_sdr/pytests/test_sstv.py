"""The `sstv` decoder through shack_sdr's Python face, as Radio will drive
it: the catalogue's entry, a session fed the test transmitter's audio and
IQ, the picture arriving as `image` results with its `meta`, and the
finished PNG kept by the host for `api/decoders/files` - written to the
files folder when there is one, served by name, refused for a name that
could leave the folder."""

from __future__ import annotations

import time

import numpy as np

import shack_sdr
from shack_sdr import DecoderError, Host, sstv


def run(host: Host, x: np.ndarray, kind: str = "audio", rate: float = 12_000.0, params: dict | None = None):
    """Feed `x` to a new session at once (the ring holds it all) and
    collect its results until the picture is done."""
    sid = host.start("sstv", params or {}, kind, rate)
    block = int(rate // 10)
    for k in range(0, len(x), block):
        host.feed(sid, x[k:k + block], 1_800_000_000.0 + k / rate, k)
    items, end = [], time.monotonic() + 30
    while time.monotonic() < end:
        items += host.poll(5000)
        if any(i["type"] == "image" and i["event"] == "done" for i in items):
            break
        time.sleep(0.02)
    return sid, items


def test_the_catalogue_lists_it():
    e = next(e for e in shack_sdr.catalogue() if e["id"] == "sstv")
    assert e["engine"] == "shack_sdr" and e["available"] and e["family"] == "amateur"
    assert e["panel"] == "image" and set(e["outputs"]) == {"image", "meta"}
    assert set(e["inputs"]) == {"audio", "iq"}
    assert [p["key"] for p in e["params"]] == ["mode", "syncStart", "afcRangeHz"]


def test_modes():
    ids = [m["id"] for m in sstv.modes()]
    assert ids == ["martin1", "martin2", "scottie1", "scottie2", "scottiedx", "robot36", "robot72",
                   "pd50", "pd90", "pd120", "pd160", "pd180", "pd240", "pd290"]
    r36 = next(m for m in sstv.modes() if m["id"] == "robot36")
    assert (r36["vis"], r36["width"], r36["height"], r36["lineMs"]) == (8, 320, 240, 150.0)


def test_a_picture_through_the_host_lands_in_the_files_folder(tmp_path):
    x, sent = sstv.testsig("robot36", snr_db=20.0, tail_s=2.0)
    with Host(cpu_max=0.0, ring_s=60.0, files_dir=tmp_path) as host:
        sid, items = run(host, x)
        image = [i for i in items if i["type"] == "image"]
        assert image[0]["event"] == "start" and (image[0]["width"], image[0]["height"]) == (320, 240)
        assert image[0]["format"] == "rgb"
        rows = sum(len(i["rgb"]) // (320 * 3) for i in image if i["event"] == "lines")
        assert rows >= 240
        done = image[-1]
        assert done["event"] == "done" and done["file"].startswith(sid + "_sstv_") and done["file"].endswith(".png")
        assert done["path"] == str(tmp_path / done["file"])
        png = host.file(done["file"])
        assert png == (tmp_path / done["file"]).read_bytes()
        got = sstv.png_rgb(png)
        s = sstv.score(sent, got)
        assert s["inSync"] == 1.0 and s["psnr"] > 18.0, s
        meta = [i["fields"] for i in items if i["type"] == "meta"]
        assert meta[0]["state"] == "receiving" and meta[-1]["state"] == "done"
        assert meta[-1]["mode"] == "robot36" and meta[-1]["vis"] == 8 and meta[-1]["file"] == done["file"]
        assert abs(meta[-1]["started"] - (1_800_000_000.0 + 0.5)) < 0.01
        assert abs(meta[-1]["snrDb"] - 20.0) < 1.5
        assert host.status(sid)["meta"] == meta[-1]
        listed = host.files()
        assert [(f["name"], f["session"], f["decoder"], f["size"]) for f in listed] == \
            [(done["file"], sid, "sstv", len(png))]
        for bad in ("../x.png", "/etc/passwd", ".x.png", "a/b.png"):
            assert host.file(bad) is None


def test_iq_and_memory_only():
    # IQ centred on 1900 Hz; no files folder, so the picture is kept in memory.
    x, sent = sstv.testsig("martin2", snr_db=20.0, tail_s=2.0, iq=True)
    assert x.dtype == np.complex64
    with Host(cpu_max=0.0, ring_s=90.0) as host:
        _, items = run(host, x, "iq")
        done = next(i for i in items if i["type"] == "image" and i["event"] == "done")
        assert done["path"] is None
        s = sstv.score(sent, sstv.png_rgb(host.file(done["file"])))
        assert s["inSync"] == 1.0 and s["psnr"] > 20.0, s


def test_parameters_are_checked():
    with Host(cpu_max=0.0) as host:
        for bad in ({"mode": "wraase"}, {"afcRangeHz": 500}, {"slant": True}):
            try:
                host.start("sstv", bad, "audio", 12_000)
            except DecoderError as e:
                assert e.args[0] == "bad_params"
            else:
                raise AssertionError(bad)
        # Audio too slow to carry 2300 Hz: the decoder refuses its first
        # block, and the session says so.
        sid = host.start("sstv", {}, "audio", 4_000)
        host.feed(sid, np.zeros(400, np.float32), 0.0, 0)
        end = time.monotonic() + 5
        while host.status(sid)["state"] != "error" and time.monotonic() < end:
            time.sleep(0.02)
        assert (host.status(sid)["state"], host.status(sid)["why"]) == ("error", "rate")


def test_the_score_and_test_card():
    x, sent = sstv.testsig("pd50", lead_s=0.0, tail_s=0.0)
    assert sent.shape == (256, 320, 3) and x.dtype == np.float32
    s = sstv.score(sent, sent)
    assert s["psnr"] == 99.0 and s["inSync"] == 1.0
    tail = sstv.score(sent, sent[100:], partial=True)
    assert (tail["rowOffset"], tail["rows"]) == (100, 156)
