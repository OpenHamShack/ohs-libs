"""The `dab` decoder through shack_sdr's Python face, as Radio will drive
it: the catalogue's entry and its partial availability, a session fed the
transmitter model's IQ (`_native.testsig_dab`), the feed's ensemble and
service list in the session's meta, a service chosen live, its sound
pulled at Radio's 12 kHz mono, and DAB+ both without faad2 and through a
stand-in libfaad built at test time (our own trivial C exporting faad2's
entry points: no codec, nothing encumbered built or loaded). The stand-in
answers each AU with a frame of its third byte, the AU's index in its
super frame, so the test hears the AUs arrive in order."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

import shack_sdr
from shack_sdr import DecoderError, Host, _native, codecs, dab

STUB = r"""
#include <stdlib.h>
#include <string.h>
typedef struct { unsigned long bytesconsumed, samples; unsigned char channels, error; unsigned long samplerate;
                 unsigned char rest[64]; } info_t;
static short pcm[4096];
static int dab_plus;
int NeAACDecGetVersion(char **id, char **c) { static char v[] = "2.11.2"; *id = v; *c = v; return 0; }
unsigned long NeAACDecGetCapabilities(void) { return 1; }
void *NeAACDecOpen(void) { return malloc(16); }
char NeAACDecInit2(void *h, unsigned char *b, unsigned long n, unsigned long *rate, unsigned char *ch) {
    /* DAB+'s config carries SBR (5 bytes): answer as HE-AAC stereo 48 kHz. */
    dab_plus = n > 2; *rate = 48000; *ch = dab_plus ? 2 : 1; return 0; }
void *NeAACDecDecode(void *h, info_t *i, unsigned char *b, unsigned long n) {
    unsigned long s = dab_plus ? 1920 * 2 : 1024;
    memset(pcm, 0, sizeof pcm);
    if (dab_plus) for (unsigned long k = 0; k < s; k++) pcm[k] = 1000 * (n > 2 ? b[2] + 1 : 0);
    i->samples = s; i->channels = dab_plus ? 2 : 1; i->samplerate = 48000; i->error = 0; i->bytesconsumed = n;
    return pcm; }
void NeAACDecClose(void *h) { free(h); }
"""


def stub_faad(tmp: Path) -> Path:
    c = shutil.which("cc") or shutil.which("gcc")
    if not c or sys.platform == "win32":
        pytest.skip("no C compiler for the stand-in library")
    d = tmp / "stub"
    d.mkdir(exist_ok=True)
    (d / "faad.c").write_text(STUB)
    out = d / "libfaad.so.2"
    subprocess.run([c, "-shared", "-fPIC", "-O1", "-o", str(out), str(d / "faad.c")], check=True)
    return out


@pytest.fixture
def no_faad(monkeypatch, tmp_path):
    """This process's loader sees no faad2 at all."""
    monkeypatch.delenv("SHACK_CODEC_FAAD2", raising=False)
    monkeypatch.delenv("SHACK_HOSTLIBS", raising=False)
    monkeypatch.setattr(codecs.Loader, "_system", staticmethod(lambda names: []))
    codecs.configure(tmp_path / "codecs", cache_path=tmp_path / "cache.json")
    codecs.publish()
    yield
    _native.codec_use("faad2", None)


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """faad2 is the stand-in, found through SHACK_CODEC_FAAD2 and probed."""
    lib = stub_faad(tmp_path)
    monkeypatch.setenv("SHACK_CODEC_FAAD2", str(lib))
    monkeypatch.setattr(codecs.Loader, "_system", staticmethod(lambda names: []))
    codecs.configure(tmp_path / "codecs", cache_path=tmp_path / "cache.json")
    yield lib
    _native.codec_use("faad2", None)


def iq(frames: int = 16, **kw) -> np.ndarray:
    return np.frombuffer(_native.testsig_dab(frames, snr_db=kw.pop("snr_db", 15.0), **kw), np.complex64)


def feed(host: Host, sid: str, x: np.ndarray, rate: float = dab.IQ_RATE, start: int = 0) -> int:
    """Feed `x` in 100 ms blocks from sample `start`; the next sample's index."""
    block = int(rate / 10)
    for k in range(0, len(x), block):
        host.feed(sid, x[k:k + block], 100.0 + (start + k) / rate, start + k)
    return start + len(x)


def wait(host: Host, sid: str, pred, secs: float = 20.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        st = host.status(sid)
        if pred(st):
            return st
        time.sleep(0.02)
    return host.status(sid)


def test_the_catalogue_says_dab_needs_faad2_for_dab_plus_only(no_faad):
    e = next(e for e in shack_sdr.catalogue() if e["id"] == "dab")
    assert e["family"] == "broadcast" and e["inputs"] == ["iq"] and e["panel"] == "meta"
    assert e["rateHz"] == e["minRateHz"] == 2_048_000 and e["bandwidthHz"] == 1_536_000
    assert e["needs"] == ["faad2"] and e["needsFor"] == {"faad2": ["dab+"]}
    # Missing faad2 leaves it available, partly.
    assert e["available"] and e["why"] is None and e["partial"] and e["missing"] == ["faad2"]
    assert {p["key"]: (p["type"], p["default"]) for p in e["params"]} == {"service": ("text", ""), "block": ("text", "")}
    assert codecs.status()[1]["for"] == ["dab", "drm"]


def test_with_faad2_the_entry_is_whole(stub):
    e = next(e for e in shack_sdr.catalogue() if e["id"] == "dab")
    assert e["available"] and not e["partial"] and e["missing"] == []


def test_a_session_lists_the_ensemble_and_plays_old_dab_at_radios_rate(no_faad):
    with Host(cpu_max=0.0, ring_s=4.0) as host:
        sid = host.start("dab", {"block": "11D"}, "iq", dab.IQ_RATE)
        x = iq(28)
        half = 12 * 196_608
        at = feed(host, sid, x[:half])
        st = wait(host, sid, lambda s: s["meta"] and len(s["meta"]["services"]) == 2)
        m = st["meta"]
        assert m["sync"] is True and m["why"] is None and m["playing"] is None and m["block"] == "11D"
        assert m["ensemble"] == {"id": "C1CE", "label": "Shack Test Mux", "shortLabel": "ShackTes"}
        assert [(s["sid"], s["label"], s["bitrate"], s["kind"]) for s in m["services"]] == [
            ("C221", "Shack MP2 Tone", 128, "dab"), ("C3A7", "Shack DAB+ Data", 64, "dab+")]
        assert m["services"][1]["why"] == "needs_codec:faad2" and m["services"][1]["needs"] == ["faad2"]
        assert st["stats"]["sync"] is True and abs(st["stats"]["offsetHz"]) < 5
        # Choose the old DAB service live; its sound, at 12 kHz mono.
        host.configure(sid, {"service": "C221"})
        feed(host, sid, x[half:], start=at)
        # 16 frames after the choice, less 15 CIFs of interleaving: ~48 MP2 frames.
        wait(host, sid, lambda s: s["audio"]["queued"] > 50_000)
        assert host.status(sid)["meta"]["playing"] == "C221"
        chunks = host.audio(sid, 2.0, rate=12_000, channels=1)
        pcm = np.concatenate([c["pcm"] for c in chunks])
        assert all((c["rate"], c["channels"]) == (12_000, 1) for c in chunks)
        assert len(pcm) > 12_000
        # The mono mix of 1 kHz and 1.5 kHz tones at 0.5 each: 0.25 apiece.
        y = pcm[2000:]
        n = np.arange(len(y))
        for f in (1000, 1500):
            amp = 2 * abs(np.dot(y, np.exp(-2j * np.pi * f * n / 12_000))) / len(y)
            assert abs(amp - 0.25) < 0.02, (f, amp)
        assert host.poll()  # meta and state results were queued meanwhile


def test_without_faad2_a_dab_plus_service_says_so_and_the_list_still_works(no_faad):
    with Host(cpu_max=0.0) as host:
        sid = host.start("dab", {"service": "C3A7"}, "iq", dab.IQ_RATE)
        feed(host, sid, iq(16))
        st = wait(host, sid, lambda s: s["why"] == "needs_codec:faad2")
        assert st["state"] == "running" and st["why"] == "needs_codec:faad2"
        assert st["meta"]["why"] == "needs_codec:faad2" and st["meta"]["playing"] == "C3A7"
        assert len(st["meta"]["services"]) == 2
        assert st["stats"]["superframes"] > 0 and host.audio(sid) == []


def test_dab_plus_plays_through_the_users_faad2(stub):
    with Host(cpu_max=0.0, ring_s=4.0) as host:
        sid = host.start("dab", {"service": "C3A7"}, "iq", dab.IQ_RATE)
        feed(host, sid, iq(20))
        st = wait(host, sid, lambda s: s["audio"]["queued"] >= 1920 * 6)
        assert st["why"] is None and st["meta"]["audio"] == {"codec": "he-aac"}
        assert st["meta"]["services"][1]["why"] is None
        (c,) = host.audio(sid, 4.0)
        assert (c["rate"], c["channels"]) == (48_000, 2)
        # The stand-in's frames say which AU each came from: 1, 2, 3 in turn.
        frames = c["pcm"].reshape(-1, 1920 * 2)[:, 0]
        idx = np.round(frames * 32768 / 1000).astype(int)
        assert len(idx) >= 6
        start = list(idx).index(1)
        assert list(idx[start:start + 6]) == [1, 2, 3, 1, 2, 3]


def test_refusals_are_the_existing_codes(no_faad):
    with Host() as host:
        with pytest.raises(DecoderError) as e:
            host.start("dab", {}, "iq", 1_536_000)
        assert e.value.args == ("bad_input", "rate")
        with pytest.raises(DecoderError) as e:
            host.start("dab", {"service": "XYZ"}, "iq", dab.IQ_RATE)
        assert e.value.args[0] == "bad_params"
        sid = host.start("dab", {}, "iq", 2_400_000)
        with pytest.raises(DecoderError) as e:
            host.configure(sid, {"block": "14Z"})
        assert e.value.args == ("bad_params", "bad_param")


def test_blocks_and_span():
    assert dab.block_centre("11d") == 222_064_000 and dab.block_at(222_064_100) == "11D"
    assert len(dab.BLOCKS) == 41 and dab.BLOCKS["5A"] == 174_928_000
    assert dab.span_check(222_064_000, 222_064_000, 2_048_000) is None
    assert dab.span_check(222_064_000, 222_064_000 + 300_000, 2_048_000) == "outside_span"
    assert dab.span_check(222_064_000, 222_064_000 + 300_000, 2_400_000) is None


def test_a_pluto_rate_is_resampled(no_faad):
    with Host(cpu_max=0.0) as host:
        sid = host.start("dab", {}, "iq", 2_400_000)
        feed(host, sid, iq(10, rate=2_400_000.0), 2_400_000)
        st = wait(host, sid, lambda s: s["meta"] and s["meta"]["ensemble"])
        assert st["meta"]["ensemble"]["label"] == "Shack Test Mux"
