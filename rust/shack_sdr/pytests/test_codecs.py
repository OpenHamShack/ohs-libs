"""The codec loader (RADIO-SDR-PLAN §5) against stand-in libraries built
from C at test time - a good one, one missing a symbol, one that crashes,
one that hangs, one too old, one whose output is wrong (§8.3) - through
the real child-process probe, plus the search order on this OS. The
stand-ins export the codecs' entry-point names with our own trivial
bodies: they are no codec, and nothing encumbered is built or loaded.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shack_sdr import codecs

FAAD = r"""
#include <stdlib.h>
#include <signal.h>
#include <unistd.h>
#include <string.h>
typedef struct { unsigned long bytesconsumed, samples; unsigned char channels, error; unsigned long samplerate;
                 unsigned char rest[64]; } info_t;
static short pcm[2048];
int NeAACDecGetVersion(char **id, char **c) { static char v[] = VERSION; *id = v; *c = v; return 0; }
unsigned long NeAACDecGetCapabilities(void) { return 1; }
void *NeAACDecOpen(void) {
#ifdef CRASH
    raise(SIGSEGV);
#endif
#ifdef HANG
    for (;;) sleep(1);
#endif
    return malloc(16);
}
#ifndef MISSING
char NeAACDecInit2(void *h, unsigned char *b, unsigned long n, unsigned long *rate, unsigned char *ch) {
    *rate = 48000; *ch = 1; return 0; }
#endif
void *NeAACDecDecode(void *h, info_t *i, unsigned char *b, unsigned long n) {
    memset(pcm, 0, sizeof pcm);
#ifdef LOUD
    pcm[5] = 12000;
#endif
    i->samples = 1024; i->channels = 1; i->error = 0; i->bytesconsumed = n; return pcm; }
void NeAACDecClose(void *h) { free(h); }
"""

MBE = r"""
#include <string.h>
#include <math.h>
void mbe_printVersion(char *s) { strcpy(s, "1.3.0"); }
void mbe_initMbeParms(void *a, void *b, void *c) { memset(a, 0, 1200); memset(b, 0, 1200); memset(c, 0, 1200); }
void mbe_processAmbe2450Dataf(float *out, int *e1, int *e2, char *es, char *d, void *a, void *b, void *c, int q) {
    for (int i = 0; i < 160; i++) out[i] = 0.0f; *e1 = *e2 = 0; }
void mbe_processAmbe3600x2400Framef(void) {}
void mbe_processAmbe3600x2450Framef(void) {}
void mbe_processImbe7200x4400Framef(void) {}
"""

FDK = r"""
#include <stdlib.h>
#include <string.h>
typedef struct { const char *title, *date, *time; int module_id, version; unsigned flags; char vs[32]; } info_t;
int aacDecoder_GetLibInfo(info_t *i) { i[0].module_id = 2; i[1].module_id = 3; i[1].version = (3 << 24) | (2 << 16);
    strcpy(i[1].vs, "3.2.0"); return 0; }
void *aacDecoder_Open(int tt, unsigned n) { return malloc(16); }
int aacDecoder_ConfigRaw(void *h, unsigned char **c, const unsigned *l) { return 0; }
int aacDecoder_Fill(void *h, unsigned char **b, const unsigned *s, unsigned *v) { *v = 0; return 0; }
int aacDecoder_DecodeFrame(void *h, short *pcm, int n, unsigned f) { memset(pcm, 0, n * 2); return 0; }
void aacDecoder_Close(void *h) { free(h); }
void *aacDecoder_GetStreamInfo(void *h) { return 0; }
"""


def build(tmp: Path, name: str, source: str, *defines: str) -> Path:
    c = shutil.which("cc") or shutil.which("gcc")
    if not c or sys.platform == "win32":
        pytest.skip("no C compiler for the stand-in libraries")
    src = tmp / f"{name}.c"
    src.write_text(source)
    out = tmp / name
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([c, "-shared", "-fPIC", "-O1", "-o", str(out), str(src), "-lm"] + [f"-D{d}" for d in defines],
                   check=True)
    return out


def faad(tmp: Path, *defines: str, version: str = "2.11.2", name: str = "libfaad.so.2") -> Path:
    d = tmp / ("faad-" + "-".join(defines or ("good",)) + version)
    d.mkdir(parents=True, exist_ok=True)
    return build(d, name, FAAD, f'VERSION="{version}"', *defines)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("SHACK_HOSTLIBS", "SHACK_CODEC_FAAD2", "SHACK_CODEC_FDK_AAC", "SHACK_CODEC_MBELIB",
              "SHACK_CODEC_CODEC2"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def no_system(monkeypatch):
    """Keep this host's own libraries out of the search."""
    monkeypatch.setattr(codecs.Loader, "_system", staticmethod(lambda names: []))


@pytest.mark.parametrize("defines,version,why", [
    ((), "2.11.2", None),
    (("MISSING",), "2.11.2", "missing_symbol"),
    (("CRASH",), "2.11.2", "crashed"),
    ((), "2.7.0", "too_old"),
    (("LOUD",), "2.11.2", "bad_output"),
])
def test_the_probe_judges_each_stand_in(tmp_path, defines, version, why):
    lib = faad(tmp_path, *defines, version=version)
    r = codecs.Loader().probe("faad2", lib)
    if why is None:
        assert r == {"ok": True, "version": "2.11.2"}
    else:
        assert r["ok"] is False and r["why"] == why, r
    if why == "missing_symbol":
        assert r["detail"] == "NeAACDecInit2"
    if why == "too_old":
        assert r["detail"] == "2.7.0"


def test_a_hanging_library_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(codecs, "PROBE_TIMEOUT_S", 1.0)
    lib = faad(tmp_path, "HANG")
    t = time.monotonic()
    assert codecs.Loader().probe("faad2", lib)["why"] == "timeout"
    assert time.monotonic() - t < 3.0


def test_fdk_aac_and_mbelib_stand_ins_pass(tmp_path):
    assert codecs.Loader().probe("fdk-aac", build(tmp_path, "libfdk-aac.so.2", FDK)) == {"ok": True,
                                                                                          "version": "3.2.0"}
    assert codecs.Loader().probe("mbelib", build(tmp_path, "libmbe.so.1", MBE)) == {"ok": True, "version": "1.3.0"}


def test_status_finds_refuses_and_reports_missing(tmp_path, no_system):
    folder = tmp_path / "codecs"
    folder.mkdir()
    shutil.copy(faad(tmp_path, "CRASH"), folder / "libfaad.so.2")
    ld = codecs.Loader(folder)
    st = {s["id"]: s for s in ld.status()}
    assert st["faad2"]["state"] == "refused" and st["faad2"]["why"] == "crashed"
    assert st["faad2"]["path"] == str(folder / "libfaad.so.2") and st["faad2"]["where"] == "folder"
    assert st["mbelib"] == {"id": "mbelib", "for": ["dmr", "dstar", "ysf", "p25", "nxdn"], "state": "missing",
                            "path": None, "version": None, "why": "not_found", "detail": None, "where": None,
                            "host": False, "shippable": False}
    assert ld.path("faad2") is None
    # "Look again" after the user replaced the file with a good one.
    shutil.copy(faad(tmp_path), folder / "libfaad.so.2")
    st = {s["id"]: s for s in ld.status(rescan=True)}
    assert st["faad2"]["state"] == "found" and st["faad2"]["version"] == "2.11.2"
    assert ld.path("faad2") == str(folder / "libfaad.so.2")


def test_the_environment_overrides_and_a_passed_file_is_not_probed_again(tmp_path, no_system, monkeypatch):
    good = faad(tmp_path)
    monkeypatch.setenv("SHACK_CODEC_FAAD2", str(good))
    ld = codecs.Loader(tmp_path / "empty", cache_path=tmp_path / "cache.json")
    calls = []
    real = ld.probe
    monkeypatch.setattr(ld, "probe", lambda c, p: calls.append(p) or real(c, p))
    assert ld.path("faad2") == str(good)
    ld.status(rescan=True)
    assert len(calls) == 1
    # A new loader (the app restarted) reads the verdict from the cache file.
    ld2 = codecs.Loader(tmp_path / "empty", cache_path=tmp_path / "cache.json")
    monkeypatch.setattr(ld2, "probe", lambda c, p: pytest.fail("probed again"))
    assert ld2.path("faad2") == str(good)


def test_encumbered_codecs_are_never_taken_from_the_bundle(tmp_path, no_system):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy(faad(tmp_path), bundle / "libfaad.so.2")
    ld = codecs.Loader(tmp_path / "empty", bundle_dirs=[bundle])
    assert ld.candidates("faad2") == []
    st = {s["id"]: s for s in ld.status()}
    assert st["faad2"]["state"] == "missing"
    assert st["codec2"]["shippable"] and not st["faad2"]["shippable"]


def test_on_a_controller_the_host_library_replaces_the_folder(tmp_path, no_system, monkeypatch):
    staged = tmp_path / "hostlibs" / "faad2"
    staged.mkdir(parents=True)
    shutil.copy(faad(tmp_path), staged / "libfaad.so.2")
    monkeypatch.setenv("SHACK_HOSTLIBS", str(tmp_path / "hostlibs"))
    assert codecs.default_folder() is None
    st = {s["id"]: s for s in codecs.Loader(None).status()}
    assert st["faad2"]["state"] == "found" and st["faad2"]["where"] == "host" and st["faad2"]["host"]
    assert st["mbelib"]["host"] and not st["codec2"]["host"]


def test_the_folders_are_where_the_plan_says(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    if sys.platform.startswith("linux"):
        assert codecs.default_folder("openhamshack-nb") == tmp_path / "openhamshack-nb" / "codecs"
    assert codecs.default_folder(data_dir=tmp_path / "data") == tmp_path / "data" / "codecs"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's loader cache")
def test_this_hosts_codec2_if_installed():
    """The real codec2 where the system has it (FreeDV's; we may ship it)."""
    st = {s["id"]: s for s in codecs.Loader(None).status()}
    if st["codec2"]["state"] == "missing":
        pytest.skip("no codec2 on this host")
    assert st["codec2"]["state"] == "found", st["codec2"]
    assert st["codec2"]["where"] == "system"


def test_the_probe_child_is_runnable_and_refuses_bare_names():
    import json
    out = subprocess.run([sys.executable, "-m", "shack_sdr.codecs", "probe", "faad2", "libfaad.so.2"],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out.splitlines()[-1]) == {"ok": False, "why": "not_absolute", "detail": None}
