"""Recordings as an app uses them: start, stop, list, file, delete; the
disk-use limit, the free-space floor, the metadata, and range requests."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import namedtuple
from pathlib import Path

import numpy as np
import pytest

from flacref import decode
from shack_rec import GB, Limits, RangeNotSatisfiable, Recordings, Refused, file_headers, parse_range

Usage = namedtuple("Usage", "total used free")


class Disk:
    """A disk whose free space a test sets."""

    def __init__(self, free: int = 100 * GB):
        self.free = free

    def __call__(self, path):
        return Usage(1000 * GB, 0, self.free)


def tone(seconds: float, rate: int = 12_000, channels: int = 1) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    x = (8000 * np.sin(2 * np.pi * 700 * t)).astype("<i2")
    return (np.repeat(x[:, None], channels, 1) if channels > 1 else x).tobytes()


def make(tmp_path, disk=None, kinds=("audio",), **limits) -> Recordings:
    return Recordings(tmp_path / "recordings", Limits(**limits), app="icom", kinds=kinds, disk_usage=disk or Disk(),
                      guard_every=0.05)


def feed(r, pcm: bytes, packet: int = 480) -> None:
    for i in range(0, len(pcm), packet):
        r.push(pcm[i:i + packet])


META = {"freqHz": 144_300_000, "mode": "USB", "station": "GM4BRI", "radio": "IC-9700", "appName": "OpenHamShack Icom",
        "processing": {"nr2": True, "nr2Level": 50}}


def test_start_feed_stop_gives_a_flac_and_its_sidecar(tmp_path):
    rec = make(tmp_path)
    r = rec.start_audio(12_000, 1, META)
    assert rec.status()["recording"] is True
    assert rec.status()["active"][0]["kind"] == "audio"
    pcm = tone(2.5)
    feed(r, pcm)
    r.note(freqHz=144_301_000)                                # retuned at 2.5 s
    feed(r, tone(0.5))
    rec.stop("audio", wait=5)
    assert rec.status()["recording"] is False
    assert rec.status()["last"]["id"] == r.id
    data = decode(Path(r.path).read_bytes())
    assert len(data["samples"]) == 36_000
    assert data["samples"][:30_000, 0].tobytes() == pcm
    assert data["tags"]["FREQUENCY"] == "144300000" and data["tags"]["MODE"] == "USB"
    assert data["tags"]["STATION"] == "GM4BRI" and data["tags"]["DATE"].endswith("Z")
    side = json.loads(Path(r.meta_path).read_text())
    assert side["kind"] == "audio" and side["format"] == "flac" and side["complete"] is True
    assert side["freqHz"] == 144_300_000 and side["mode"] == "USB" and side["station"] == "GM4BRI"
    assert side["sampleRate"] == 12_000 and side["channels"] == 1 and side["seconds"] == 3.0
    assert side["processing"] == {"nr2": True, "nr2Level": 50}
    assert side["startUtc"].endswith("Z") and side["endUtc"].endswith("Z")
    assert side["changes"] == [{"t": 0.0, "freqHz": 144_300_000, "mode": "USB"}, {"t": 2.5, "freqHz": 144_301_000}]
    assert side["stopped"] == "by the operator"


def test_the_list_shows_each_recording_once_newest_first_and_the_files_download(tmp_path):
    clock = iter([1_760_000_000.0 + i * 60 for i in range(10)])
    rec = make(tmp_path)
    rec.wall = lambda: next(clock)
    ids = []
    for f in (144_300_000, 432_200_000):
        r = rec.start_audio(12_000, 1, {**META, "freqHz": f})
        feed(r, tone(0.2))
        rec.stop(wait=5)
        ids.append(r.id)
    got = rec.listing()
    assert [e["id"] for e in got["recordings"]] == ids[::-1]
    e = got["recordings"][0]
    assert e["kind"] == "audio" and e["freqHz"] == 432_200_000 and e["mode"] == "USB" and e["complete"] is True
    assert e["parts"]["data"]["type"] == "audio/flac" and e["parts"]["meta"]["type"] == "application/json"
    assert e["bytes"] == e["parts"]["data"]["bytes"] + e["parts"]["meta"]["bytes"]
    assert "432.200000MHz-USB" in e["id"]
    path, ctype = rec.file(e["id"], "data")
    assert ctype == "audio/flac" and path.read_bytes()[:4] == b"fLaC"
    assert rec.file(e["id"], "meta")[0].suffix == ".json"


def test_one_audio_recording_at_a_time_and_only_the_kinds_the_app_has(tmp_path):
    rec = make(tmp_path)
    rec.start_audio(12_000, 1, META)
    with pytest.raises(Refused) as e:
        rec.start_audio(12_000, 1, META)
    assert (e.value.status, e.value.code) == (409, "already_recording")
    with pytest.raises(Refused) as e:
        rec.prepare("iq")
    assert (e.value.status, e.value.code) == (400, "kind_unavailable")
    with pytest.raises(Refused) as e:
        rec.prepare("video")
    assert e.value.code == "bad_kind"
    rec.stop(wait=5)


def test_below_the_free_space_floor_nothing_starts(tmp_path):
    disk = Disk(free=int(1.5 * GB))
    rec = make(tmp_path, disk, floor_bytes=2 * GB)
    with pytest.raises(Refused) as e:
        rec.start_audio(12_000, 1, META)
    assert (e.value.status, e.value.code) == (507, "low_disk")
    assert "1.5 GB free" in e.value.text and "2.0 GB" in e.value.text
    assert rec.listing()["recordings"] == []
    disk.free = 3 * GB
    rec.start_audio(12_000, 1, META)
    rec.stop(wait=5)


def test_a_recording_running_when_free_space_falls_below_the_floor_stops(tmp_path):
    disk = Disk(free=10 * GB)
    rec = make(tmp_path, disk, floor_bytes=2 * GB)
    r = rec.start_audio(12_000, 1, META)
    feed(r, tone(0.5))
    disk.free = GB
    assert r.done.wait(5)
    st = rec.status()
    assert st["recording"] is False
    assert "free disk space fell below 2.0 GB" in st["last"]["stopped"]
    side = json.loads(Path(r.meta_path).read_text())
    assert side["complete"] is True and "free disk space" in side["stopped"]


def _fill(rec, n: int, seconds: float = 1.0) -> list[str]:
    ids = []
    for _ in range(n):
        r = rec.start_audio(12_000, 1, META)
        feed(r, tone(seconds))
        rec.stop(wait=5)
        ids.append(r.id)
    return ids


def test_past_the_limit_the_oldest_are_deleted_and_never_the_running_one(tmp_path):
    clock = iter([1_760_000_000.0 + i for i in range(100)])
    rec = make(tmp_path, limit_bytes=10**9)
    rec.wall = lambda: next(clock)
    ids = _fill(rec, 4)
    each = rec.listing()["recordings"][0]["bytes"]
    rec.store.limit_bytes = int(each * 2.5)                  # room for two
    running = rec.start_audio(12_000, 1, META)               # prunes before it starts
    left = [e["id"] for e in rec.listing()["recordings"]]
    assert ids[0] not in left and ids[1] not in left
    assert ids[2] in left and ids[3] in left and running.id in left
    feed(running, tone(3.0))
    deadline = time.monotonic() + 5
    while ids[2] in [e["id"] for e in rec.listing()["recordings"]] and time.monotonic() < deadline:
        time.sleep(0.05)                                     # the guard prunes while it records
    left = [e["id"] for e in rec.listing()["recordings"]]
    assert ids[2] not in left and running.id in left
    rec.stop(wait=5)


def test_a_recording_stops_at_its_longest_duration(tmp_path):
    rec = make(tmp_path, audio_max_s=1.0)
    r = rec.start_audio(12_000, 1, META)
    feed(r, tone(3.0))
    assert r.done.wait(5)
    assert decode(Path(r.path).read_bytes())["total"] == 12_000
    assert rec.status()["last"]["stopped"] == "the time limit"
    assert r.push(tone(0.1)) is False


def test_a_recording_stops_at_the_size_it_was_given(tmp_path):
    rec = make(tmp_path, limit_bytes=20_000)
    r = rec.start_audio(12_000, 1, META)
    feed(r, (np.random.default_rng(0).integers(-20000, 20000, 12_000 * 5)).astype("<i2").tobytes())
    assert r.done.wait(5)
    assert rec.status()["last"]["stopped"] == "the size limit"
    assert Path(r.path).stat().st_size < 40_000


def test_on_close_runs_once_whoever_stops_it(tmp_path):
    rec = make(tmp_path, audio_max_s=0.5)
    r = rec.start_audio(12_000, 1, META)
    calls = []
    r.on_close(lambda: calls.append(1))
    feed(r, tone(1.0))                                       # the time limit closes it
    rec.stop(wait=5)                                         # and so does the operator
    assert calls == [1]
    r.on_close(lambda: calls.append(2))                      # too late: called at once
    assert calls == [1, 2]


def test_stereo_is_kept_as_two_channels(tmp_path):
    rec = make(tmp_path)
    r = rec.start_audio(24_000, 2, META)
    pcm = tone(0.5, 24_000, 2)
    feed(r, pcm, packet=960)
    rec.stop(wait=5)
    got = decode(Path(r.path).read_bytes())
    assert got["channels"] == 2 and got["samples"].tobytes() == pcm


def test_delete_removes_both_files_and_refuses_the_running_one(tmp_path):
    rec = make(tmp_path)
    done = _fill(rec, 1)[0]
    running = rec.start_audio(12_000, 1, META)
    with pytest.raises(Refused) as e:
        rec.delete(running.id)
    assert (e.value.status, e.value.code) == (409, "recording_active")
    gone = rec.delete(done)
    assert gone["id"] == done
    assert not {f"{done}.flac", f"{done}.json"} & set(os.listdir(rec.store.folder))
    with pytest.raises(Refused) as e:
        rec.delete(done)
    assert e.value.status == 404
    rec.stop(wait=5)


@pytest.mark.parametrize("rid", ["../etc/passwd", "/etc/passwd", "..", "", "a/b", "x" * 200, ".hidden"])
def test_no_id_reaches_outside_the_folder(tmp_path, rid):
    rec = make(tmp_path)
    (tmp_path / "secret.flac").write_bytes(b"fLaC")
    with pytest.raises(Refused) as e:
        rec.file(rid, "data")
    assert e.value.status == 404
    with pytest.raises(Refused):
        rec.delete(rid)


def test_a_sigmf_recording_lists_as_iq_with_its_facts(tmp_path):
    rec = make(tmp_path, kinds=("audio", "iq"))
    folder = rec.store.folder
    folder.mkdir(parents=True)
    base = "QO-100NB-20261012-100000Z-10489.750MHz-USB"
    (folder / f"{base}.sigmf-data").write_bytes(bytes(4800))
    (folder / f"{base}.sigmf-meta").write_text(json.dumps({
        "global": {"core:datatype": "ci16_le", "core:sample_rate": 600000.0, "core:author": "GM4BRI",
                   "openhamshack:start_utc": "2026-10-12T10:00:00.000000Z", "openhamshack:seconds": 0.002,
                   "openhamshack:complete": True, "openhamshack:stopped": "by the operator",
                   "openhamshack:receive": {"rxDial": 10489750000.0, "mode": "usb"}},
        "captures": [{"core:sample_start": 0, "core:frequency": 10489750000.0}], "annotations": []}))
    (e,) = rec.listing()["recordings"]
    assert e["kind"] == "iq" and e["format"] == "sigmf-ci16_le" and e["sampleRate"] == 600000.0
    assert e["freqHz"] == 10489750000.0 and e["mode"] == "USB" and e["station"] == "GM4BRI"
    assert e["startUtc"] == "2026-10-12T10:00:00Z" and e["complete"] is True
    assert rec.file(base, "data")[1] == "application/octet-stream"


def test_settings_come_from_the_environment_and_junk_keeps_the_default():
    lim = Limits.from_env({"RECORDINGS_LIMIT_GB": "12.5", "RECORDINGS_MIN_FREE_GB": "1",
                           "RECORDINGS_AUDIO_MAX_HOURS": "0", "RECORDINGS_IQ_MAX_MINUTES": "five"},
                          iq_max_s=300)
    assert lim.limit_bytes == int(12.5 * GB) and lim.floor_bytes == GB
    assert lim.audio_max_s == 4 * 3600                       # 0 is not a limit: the default stays
    assert lim.iq_max_s == 300


def test_a_slow_disk_drops_audio_rather_than_holding_up_the_radio(tmp_path, monkeypatch):
    rec = make(tmp_path)
    r = rec.start_audio(12_000, 1, META)
    gate = threading.Event()
    real = r.writer.write
    monkeypatch.setattr(r.writer, "write", lambda x: (gate.wait(5), real(x)))
    t0 = time.monotonic()
    feed(r, tone(10.0))                                      # far more than QUEUE_S
    assert time.monotonic() - t0 < 1.0
    assert r.status()["droppedSeconds"] > 0
    gate.set()
    rec.stop(wait=5)


# ------------------------------------------------------------------ ranges
@pytest.mark.parametrize("header,want", [
    (None, None), ("", None), ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-100", (900, 999)),
    ("bytes=990-2000", (990, 999)), ("bytes=0-0", (0, 0)), ("bytes=-5000", (0, 999)),
    ("bytes=0-1,5-6", None), ("items=0-1", None), ("bytes=50-10", None)])
def test_range_headers(header, want):
    assert parse_range(header, 1000) == want


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5000-6000", "bytes=-0"])
def test_a_range_past_the_end_is_416(header):
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, 1000)


def test_range_headers_say_what_part_of_the_file_follows():
    status, h = file_headers(1000, "audio/flac", "a b.flac", False, (100, 199))
    assert status == 206 and h["Content-Range"] == "bytes 100-199/1000" and h["Content-Length"] == "100"
    assert h["Accept-Ranges"] == "bytes" and h["Content-Disposition"].startswith('inline; filename="a b.flac"')
    status, h = file_headers(1000, "audio/flac", "a.flac", True, None)
    assert status == 200 and h["Content-Length"] == "1000" and h["Content-Disposition"].startswith("attachment")


def test_when_a_recording_ends_the_folder_is_brought_back_within_its_limit(tmp_path):
    clock = iter([1_760_000_000.0 + i for i in range(100)])
    rec = make(tmp_path)
    rec.wall = lambda: next(clock)
    old = _fill(rec, 2)
    rec.store.limit_bytes = rec.listing()["recordings"][0]["bytes"] + 1
    r = rec.start_audio(12_000, 1, META)                     # prunes the first, keeps the second
    feed(r, tone(1.0))
    rec.stop(wait=5)
    # The second went when this one ended; the one just made is never the one to go.
    assert [e["id"] for e in rec.listing()["recordings"]] == [r.id]
    assert old[1] not in [e["id"] for e in rec.listing()["recordings"]]
