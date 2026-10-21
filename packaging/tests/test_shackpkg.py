"""packaging/tools/shackpkg.py: the descriptor is checked and staged as
every builder expects. Uses the example app and a small stand-in brand
kit, so it runs without ~/openhamshack."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("shackpkg", KIT / "tools" / "shackpkg.py")
shackpkg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shackpkg)


@pytest.fixture
def brand(tmp_path):
    png = tmp_path / "brand" / "icons" / "png" / "platform"
    png.mkdir(parents=True)
    for s in shackpkg.ICON_SIZES:
        (png / f"platform-{s}.png").write_bytes(b"png")
    (png / "ios-1024.png").write_bytes(b"png")
    (png / "platform.ico").write_bytes(b"ico")
    svg = tmp_path / "brand" / "icons" / "svg"
    svg.mkdir(parents=True)
    (svg / "platform.svg").write_text("<svg/>")
    return tmp_path / "brand"


def test_the_example_stages_completely(tmp_path, brand):
    out = tmp_path / "stage"
    meta = shackpkg.stage(KIT / "example" / "shack-app.toml", out, brand)
    app = json.loads((out / "app.json").read_text())
    assert app == meta
    assert app["id"] == "com.openhamshack.hello" and app["executable"] == "shack-hello"
    assert app["file_name"] == f"OpenHamShack-Hello-{app['version']}"
    assert app["windows"]["app_user_model_id"] == "OpenHamShack.Hello"
    assert app["python"]["packages"] == ["shack_desktop", "shack_hello"]
    # Nothing in app.json points back at this machine.
    assert str(KIT) not in (out / "app.json").read_text()
    assert (out / "src" / "shack_hello" / "static" / "index.html").is_file()
    assert not list(out.rglob("__pycache__"))
    assert {p.name for p in (out / "icons").iterdir()} >= {"16.png", "1024.png", "app.ico", "app.svg"}
    assert "from shack_hello.__main__ import main" in (out / "launcher.py").read_text()
    assert (out / "requirements.txt").read_text() == "pywebview==6.2.1\n"


def write(tmp_path, **over):
    d = tmp_path / "app"
    shutil.copytree(KIT / "example", d, ignore=shutil.ignore_patterns("__pycache__"))
    text = (d / "shack-app.toml").read_text()
    for k, v in over.items():
        text = text.replace(k, v)
    text = text.replace('license_file = "../../LICENSE"', 'license_file = "VERSION"')
    text = text.replace('"../../python/shack_desktop/src"', f'"{(KIT.parent / "python" / "shack_desktop" / "src").as_posix()}"')
    (d / "shack-app.toml").write_text(text)
    return d / "shack-app.toml"


@pytest.mark.parametrize("old, new", [
    ('id = "com.openhamshack.hello"', 'id = "uk.co.example.hello"'),       # not the family's id space
    ('id = "com.openhamshack.hello"', 'id = "com.openhamshack.icom"'),     # id and kind disagree
    ('app_guid = "{BE29FBA6-32D9-4CB8-BA53-38130357937A}"', 'app_guid = "BE29FBA6"'),
    ('version = { file = "VERSION" }', 'version = "one"'),
])
def test_bad_descriptors_are_refused(tmp_path, old, new):
    with pytest.raises(shackpkg.DescriptorError):
        shackpkg.load(write(tmp_path, **{old: new}))


def test_a_missing_brand_icon_is_refused(tmp_path, brand):
    with pytest.raises(shackpkg.DescriptorError, match="brand kit icon"):
        shackpkg.stage(write(tmp_path, **{'icon = "platform"': 'icon = "nosuch"'}), tmp_path / "s", brand)


def test_an_app_may_bring_its_own_spec(tmp_path, brand):
    # Qt apps (QO-100 NB, Radio) freeze with their own spec: the kit's
    # leaves Qt out. The spec is staged as app.spec and named in app.json.
    toml = write(tmp_path)
    (toml.parent / "qt.spec").write_text("# a spec\n")
    toml.write_text(toml.read_text() + '\n[pyinstaller]\nspec = "qt.spec"\n')
    out = tmp_path / "stage"
    app = shackpkg.stage(toml, out, brand)
    assert app["spec"] == "app.spec"
    assert (out / "app.spec").read_text() == "# a spec\n"
    assert str(tmp_path) not in (out / "app.json").read_text()


def test_a_missing_spec_is_refused(tmp_path, brand):
    toml = write(tmp_path)
    toml.write_text(toml.read_text() + '\n[pyinstaller]\nspec = "nope.spec"\n')
    with pytest.raises(shackpkg.DescriptorError, match="no spec"):
        shackpkg.stage(toml, tmp_path / "stage", brand)


def test_without_one_the_kits_spec_is_used(tmp_path, brand):
    app = shackpkg.stage(write(tmp_path), tmp_path / "stage", brand)
    assert app["spec"] == "" and not (tmp_path / "stage" / "app.spec").exists()


def test_an_app_may_bring_its_own_host_libs_list(tmp_path, brand):
    toml = write(tmp_path)
    (toml.parent / "hl.txt").write_text("libglib-2.0.so*\n")
    text = toml.read_text().replace("[linux]", '[linux]\nhost_libs = "hl.txt"', 1)
    toml.write_text(text)
    out = tmp_path / "stage"
    shackpkg.stage(toml, out, brand)
    assert (out / "host-libs.txt").read_text() == "libglib-2.0.so*\n"
    assert str(tmp_path) not in (out / "app.json").read_text()


# --- [[windows.bundle]]: third-party programs the Windows installer carries

SHA = "ab" * 32
BUNDLE = f'''
notices = "NOTICE.txt"

[[windows.bundle]]
name = "ffmpeg"
urls = ["https://example.org/ffmpeg-9.0.2-essentials_build.7z"]
sha256 = "{SHA}"
files = ["ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe", "ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe"]
licences = {{ "ffmpeg-LICENSE.txt" = "ffmpeg-9.0.2-essentials_build/LICENSE" }}
check = {{ args = ["-hide_banner", "-encoders"], expect = ["libx264"] }}

[[windows.bundle]]
name = "mpv"
urls = ["https://example.org/mpv.7z", "https://mirror.example.org/mpv.7z"]
sha256 = "{SHA.upper()}"
files = ["mpv.exe"]
licences = {{ "mpv-LICENSE.GPL" = {{ urls = ["https://example.org/LICENSE.GPL"], sha256 = "{SHA}" }} }}
'''


def with_bundle(tmp_path, text=BUNDLE):
    toml = write(tmp_path)
    (toml.parent / "NOTICE.txt").write_text("what this installer carries\n")
    toml.write_text(toml.read_text().replace("\n[macos]", text + "\n[macos]", 1))
    return toml


def test_bundled_programs_are_staged_for_the_windows_builder(tmp_path, brand):
    out = tmp_path / "stage"
    app = shackpkg.stage(with_bundle(tmp_path), out, brand)
    ff, mpv = app["windows"]["bundle"]
    assert ff["name"] == "ffmpeg" and ff["archive"] == "ffmpeg-9.0.2-essentials_build.7z"
    assert ff["licences"] == {"ffmpeg-LICENSE.txt": {"member": "ffmpeg-9.0.2-essentials_build/LICENSE"}}
    assert ff["check"] == {"args": ["-hide_banner", "-encoders"], "expect": ["libx264"]}
    assert mpv["sha256"] == SHA and mpv["urls"][1].startswith("https://mirror.")
    assert mpv["licences"]["mpv-LICENSE.GPL"] == {"urls": ["https://example.org/LICENSE.GPL"], "sha256": SHA}
    assert mpv["check"] is None
    # The notice travels in the stage; no path to this machine does.
    assert (out / "THIRD-PARTY.txt").read_text() == "what this installer carries\n"
    assert str(tmp_path) not in (out / "app.json").read_text()


def test_an_app_without_bundles_stages_as_before(tmp_path, brand):
    out = tmp_path / "stage"
    app = shackpkg.stage(write(tmp_path), out, brand)
    assert app["windows"]["bundle"] == [] and not (out / "THIRD-PARTY.txt").exists()


@pytest.mark.parametrize("old, new, why", [
    ('notices = "NOTICE.txt"\n', "", "notices is required"),
    ('sha256 = "' + SHA + '"\nfiles', 'sha256 = "abc"\nfiles', "sha256"),
    ('urls = ["https://example.org/ffmpeg', 'urls = ["http://example.org/ffmpeg', "https"),
    ('name = "mpv"', 'name = "ffmpeg"', "two bundles"),
    ('name = "mpv"', 'name = "licences"', "folder name"),
    ('files = ["mpv.exe"]', 'files = ["../mpv.exe"]', "relative"),
    ('files = ["mpv.exe"]', 'files = ["a/mpv.exe", "b/mpv.exe"]', "flat"),
    ('licences = { "mpv-LICENSE.GPL"', 'licenses = { "mpv-LICENSE.GPL"', "licences is required"),
    ('mpv.7z", "https://mirror', 'mpv.tar.gz", "https://mirror', "7z or .zip"),
])
def test_bad_bundles_are_refused(tmp_path, old, new, why):
    assert old in BUNDLE
    with pytest.raises(shackpkg.DescriptorError, match=why):
        shackpkg.load(with_bundle(tmp_path, BUNDLE.replace(old, new, 1)))


def run_launcher(stage: Path, executable: Path, frozen: bool) -> list[str]:
    """The staged launcher.py run as the frozen program would run it (with
    sys.frozen and sys.executable as PyInstaller sets them), its entry
    module replaced by one that prints PATH."""
    stub = stage / "stubs"
    (stub / "shack_hello").mkdir(parents=True, exist_ok=True)
    (stub / "shack_hello" / "__init__.py").write_text("")
    (stub / "shack_hello" / "__main__.py").write_text(
        "import os\ndef main():\n    print(os.environ['PATH'])\n    return 0\n")
    code = (f"import sys, runpy; sys.path.insert(0, {str(stub)!r}); "
            + (f"sys.frozen = True; sys.executable = {str(executable)!r}; " if frozen else "")
            + f"runpy.run_path({str(stage / 'launcher.py')!r}, run_name='__main__')")
    res = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30,
                         env={"PATH": "/usr/bin"}, check=True)
    return res.stdout.strip().split(os.pathsep)


def test_the_frozen_launcher_puts_bundled_programs_first_on_path(tmp_path, brand):
    # The app then finds them as it finds any program (shutil.which), and a
    # tester's own older ffmpeg further down PATH never stands in for them.
    stage = tmp_path / "stage"
    shackpkg.stage(with_bundle(tmp_path), stage, brand)
    install = tmp_path / "install"
    (install / "ffmpeg").mkdir(parents=True)
    (install / "mpv").mkdir()
    exe = install / "shack-hello.exe"
    assert run_launcher(stage, exe, frozen=True) == [str(install / "ffmpeg"), str(install / "mpv"), "/usr/bin"]


def test_the_launcher_leaves_path_alone_where_nothing_is_bundled(tmp_path, brand):
    # Linux and macOS builds have no bundle folders; the .deb's launcher is
    # not frozen at all; an app without bundles has nothing to add.
    stage = tmp_path / "stage"
    shackpkg.stage(with_bundle(tmp_path), stage, brand)
    (tmp_path / "install" / "ffmpeg").mkdir(parents=True)
    exe = tmp_path / "install" / "shack-hello.exe"
    assert run_launcher(stage, exe, frozen=False) == ["/usr/bin"]
    assert run_launcher(stage, tmp_path / "elsewhere" / "shack-hello", frozen=True) == ["/usr/bin"]


# --- windows/bundle.ps1, run for real where PowerShell and 7-Zip are found
# (PowerShell 7 on Linux runs it as Windows' 5.1 does; SHACK_PWSH and
# SHACK_7Z name them when they are not on PATH). Offline: the archives are
# made here and put in the build's cache, which is where bundle.ps1 looks
# before it downloads - and a cached file that does not match its pin is
# thrown away, never used.

PWSH = os.environ.get("SHACK_PWSH") or shutil.which("pwsh")
SEVEN = os.environ.get("SHACK_7Z") or shutil.which("7zz") or shutil.which("7z")
needs_pwsh = pytest.mark.skipif(not (PWSH and SEVEN), reason="needs PowerShell (pwsh) and 7-Zip (7zz/7z)")


def tool_zip(path: Path, says: str) -> str:
    """A stand-in for FFmpeg's build: a program that prints `says`, and a
    licence, in folders as the real archive has them. Its SHA-256."""
    import hashlib
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        exe = zipfile.ZipInfo("tool-1.0/bin/tool.exe")
        exe.create_system, exe.external_attr = 3, 0o100755 << 16
        z.writestr(exe, f"#!/bin/sh\necho '{says}'\n")
        z.writestr("tool-1.0/LICENSE", "GPL-3.0 text\n")
        z.writestr("tool-1.0/doc/manual.html", "not carried\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_bundle(tmp_path, brand, says="encoders: libx264 libx265", expect='["libx264", "libx265"]', seed=True):
    import hashlib
    work = tmp_path / "work"
    (work / "cache").mkdir(parents=True)
    sha = tool_zip(tmp_path / "tool-1.0.zip", says)
    if seed is True:
        shutil.copy(tmp_path / "tool-1.0.zip", work / "cache" / "tool-1.0.zip")
    elif seed:
        (work / "cache" / "tool-1.0.zip").write_bytes(seed)
    gpl = b"the GPL, fetched on its own\n"
    (work / "cache" / "tool-COPYING").write_bytes(gpl)
    text = f'''
notices = "NOTICE.txt"

[[windows.bundle]]
name = "tool"
urls = ["https://example.invalid/tool-1.0.zip"]
sha256 = "{sha}"
files = ["tool-1.0/bin/tool.exe"]
licences = {{ "tool-LICENSE.txt" = "tool-1.0/LICENSE", "COPYING" = {{ urls = ["https://example.invalid/COPYING"], sha256 = "{hashlib.sha256(gpl).hexdigest()}" }} }}
check = {{ args = ["-encoders"], expect = {expect} }}
'''
    stage = tmp_path / "stage"
    shackpkg.stage(with_bundle(tmp_path, text), stage, brand)
    dest = tmp_path / "dist" / "shack-hello"
    dest.mkdir(parents=True)
    res = subprocess.run([PWSH, "-NoProfile", "-File", str(KIT / "windows" / "bundle.ps1"), "-Stage", str(stage),
                          "-Dest", str(dest), "-Work", str(work), "-SevenZip", SEVEN],
                         capture_output=True, text=True, timeout=120)
    return res, dest


@needs_pwsh
def test_bundle_ps1_carries_the_programs_licences_and_notice(tmp_path, brand):
    res, dest = run_bundle(tmp_path, brand)
    assert res.returncode == 0, res.stdout + res.stderr
    assert sorted(p.name for p in (dest / "tool").iterdir()) == ["tool.exe"]     # flat, and only what was named
    assert (dest / "licences" / "tool-LICENSE.txt").read_text() == "GPL-3.0 text\n"
    assert (dest / "licences" / "COPYING").read_text() == "the GPL, fetched on its own\n"
    assert (dest / "THIRD-PARTY.txt").read_text() == "what this installer carries\n"
    assert "tool has: libx264, libx265" in res.stdout


@needs_pwsh
def test_bundle_ps1_refuses_a_program_that_lacks_what_the_app_needs(tmp_path, brand):
    res, _ = run_bundle(tmp_path, brand, says="encoders: libx264")
    assert res.returncode != 0 and "lacks what the app needs: libx265" in res.stdout + res.stderr


@needs_pwsh
def test_bundle_ps1_never_uses_an_archive_that_does_not_match_its_pin(tmp_path, brand):
    # A cached archive that has changed since it was pinned is deleted and
    # fetched again; the pinned address being unreachable, the build stops.
    res, dest = run_bundle(tmp_path, brand, seed=b"something else")
    assert res.returncode != 0 and "could not be fetched as pinned" in res.stdout + res.stderr
    assert not (tmp_path / "work" / "cache" / "tool-1.0.zip").exists()
    assert not (dest / "tool").exists()
