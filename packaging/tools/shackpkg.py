#!/usr/bin/env python3
"""Read an app's shack-app.toml and stage everything its installers need.

Every packaging script in this kit (deb, AppImage, Windows, macOS) starts
from one staged folder rather than from the descriptor itself, for two
reasons: the descriptor names things only this machine has (the brand kit
in ~/openhamshack/brand, source paths relative to the app's repo), and the
Windows and macOS builders run on other machines, in PowerShell 5.1 and
with no Python of their own until they fetch one. So the descriptor is
read once, here, and resolved into plain JSON and files:

    <stage>/app.json         the resolved descriptor (below)
    <stage>/src/             every [python] paths entry, merged
    <stage>/launcher.py      the frozen program's entry script
    <stage>/requirements.txt [python] requirements, for the frozen builds
    <stage>/icons/           <size>.png (16-1024), app.ico, app.svg
    <stage>/LICENSE          the app's licence text
    <stage>/THIRD-PARTY.txt  [windows] notices, when the app bundles programs

Usage:
    shackpkg.py stage <shack-app.toml> <stage-dir> [--brand DIR]
    shackpkg.py show  <shack-app.toml>            (the resolved JSON)

The format is in packaging/README.md; the example is
packaging/example/shack-app.toml. Stdlib only (tomllib: Python 3.11+).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

DEFAULT_BRAND = Path(os.environ.get("SHACK_BRAND", Path.home() / "openhamshack" / "brand"))
ICON_SIZES = (16, 32, 48, 64, 128, 256, 512)

# REPO-LAYOUT.md §5: desktop app id com.openhamshack.<kind>, executable
# shack-<kind>. The kit enforces both, so no app ships under a stray id
# that a later rename would then have to migrate.
ID_RE = re.compile(r"^com\.openhamshack\.[a-z0-9]+$")
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.+~-][0-9A-Za-z.+~-]+)?$")
GUID_RE = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$")
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# An archive member or licence file name: relative, no "..", no drive.
MEMBER_RE = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$))[A-Za-z0-9_.+ -][A-Za-z0-9_.+ /-]*$")


class DescriptorError(ValueError):
    pass


def _pinned(spec: object, where: str) -> dict:
    """{urls, sha256} - a download the Windows builder fetches and refuses
    unless its SHA-256 matches. https only: a pin protects the bytes, but
    a plain-http fallback would still tell anyone on the path what we build."""
    if not isinstance(spec, dict):
        raise DescriptorError(f"{where}: needs urls and sha256")
    urls = spec.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if not urls or not all(isinstance(u, str) and u.startswith("https://") for u in urls):
        raise DescriptorError(f"{where}: urls must be one or more https:// addresses")
    sha = str(spec.get("sha256", "")).lower()
    if not SHA256_RE.match(sha):
        raise DescriptorError(f"{where}: sha256 must be the file's 64-hex SHA-256 (it is checked at every build)")
    return {"urls": list(urls), "sha256": sha}


def _bundle(entries: object) -> list[dict]:
    """[[windows.bundle]]: third-party programs the Windows installer
    carries beside the app's executable (packaging/README.md). Checked here,
    on Linux, so a typo fails the stage in a second rather than a Windows
    build twenty minutes in."""
    if not isinstance(entries, list):
        raise DescriptorError("windows.bundle is a list of [[windows.bundle]] tables")
    out, names = [], set()
    for i, e in enumerate(entries):
        where = f"windows.bundle[{i}]"
        if not isinstance(e, dict):
            raise DescriptorError(f"{where}: each bundle is a [[windows.bundle]] table")
        name = str(e.get("name", ""))
        if not BUNDLE_NAME_RE.match(name) or name in ("licences", "_internal"):
            raise DescriptorError(f"{where}: name {name!r} must be a plain lower-case folder name")
        if name in names:
            raise DescriptorError(f"{where}: two bundles are both called {name}")
        names.add(name)
        where = f"windows.bundle {name}"
        pin = _pinned(e, where)
        archive = str(e.get("archive") or pin["urls"][0].rsplit("/", 1)[-1])
        if not archive.endswith((".7z", ".zip")) or "/" in archive:
            raise DescriptorError(f"{where}: archive {archive!r} must be a .7z or .zip file name")
        files = e.get("files")
        if not files or not isinstance(files, list) or not all(isinstance(f, str) and MEMBER_RE.match(f) for f in files):
            raise DescriptorError(f"{where}: files lists the archive members to carry, as relative paths")
        leaves = [f.rsplit("/", 1)[-1] for f in files]
        if len(set(leaves)) != len(leaves):
            raise DescriptorError(f"{where}: files are taken out flat, so their names must differ")
        licences = {}
        for dest, src in dict(e.get("licences", {})).items():
            if not MEMBER_RE.match(dest) or "/" in dest:
                raise DescriptorError(f"{where}: licence name {dest!r} must be a plain file name")
            if isinstance(src, str):
                if not MEMBER_RE.match(src):
                    raise DescriptorError(f"{where}: licence {dest} names archive member {src!r}")
                licences[dest] = {"member": src}
            else:
                licences[dest] = _pinned(src, f"{where} licence {dest}")
        if not licences:
            # Every bundled program is somebody else's, under their
            # licence; the GPL builds' in particular must travel with it.
            raise DescriptorError(f"{where}: licences is required (the texts the program's licence says must ship with it)")
        check = e.get("check")
        if check is not None:
            if not isinstance(check, dict) or not isinstance(check.get("args", []), list) \
                    or not isinstance(check.get("expect", []), list):
                raise DescriptorError(f"{where}: check is {{ args = [...], expect = [...] }}")
            check = {"args": [str(a) for a in check.get("args", [])], "expect": [str(x) for x in check.get("expect", [])]}
        out.append({"name": name, "archive": archive, **pin, "files": list(files), "licences": licences,
                    "check": check})
    return out


def _need(table: dict, key: str, where: str) -> object:
    if key not in table:
        raise DescriptorError(f"{where}: {key} is required")
    return table[key]


def _version(spec: object, root: Path) -> str:
    """version = "1.2.3" | { file = "VERSION" } | { python = "src/pkg/__init__.py" }"""
    if isinstance(spec, str):
        v = spec
    elif isinstance(spec, dict) and "file" in spec:
        v = (root / spec["file"]).read_text().strip()
    elif isinstance(spec, dict) and "python" in spec:
        m = re.search(r'^__version__ = "([^"]+)"', (root / spec["python"]).read_text(), re.M)
        if not m:
            raise DescriptorError(f"no __version__ in {spec['python']}")
        v = m.group(1)
    else:
        raise DescriptorError("app.version must be a string, {file = ...} or {python = ...}")
    if not VERSION_RE.match(v):
        raise DescriptorError(f"app.version {v!r} is not a version (1.2.3, 1.2.3-beta.1)")
    return v


def load(path: Path) -> dict:
    """The resolved descriptor: what app.json holds."""
    path = Path(path).resolve()
    root = path.parent
    d = tomllib.loads(path.read_text())
    app = _need(d, "app", str(path))
    for k in ("id", "kind", "name", "summary", "version", "entry", "icon"):
        _need(app, k, "[app]")
    if not ID_RE.match(app["id"]):
        raise DescriptorError(f"app.id {app['id']!r} must be com.openhamshack.<kind> (REPO-LAYOUT §5)")
    if not KIND_RE.match(app["kind"]):
        raise DescriptorError(f"app.kind {app['kind']!r} is not a kind")
    if app["id"] != f"com.openhamshack.{app['kind'].replace('-', '')}":
        raise DescriptorError(f"app.id {app['id']} does not match kind {app['kind']}")
    module, _, func = app["entry"].partition(":")
    if not module:
        raise DescriptorError("app.entry is module or module:function")
    py = d.get("python", {})
    linux = d.get("linux", {})
    win = d.get("windows", {})
    mac = d.get("macos", {})
    guid = _need(win, "app_guid", "[windows]")
    if not GUID_RE.match(guid):
        raise DescriptorError("windows.app_guid is a {GUID} in braces; make one once and never change it")
    bundle = _bundle(win.get("bundle", []))
    if bundle and not win.get("notices"):
        raise DescriptorError("windows.notices is required with windows.bundle: the installer must say what "
                              "it carries, under which licence, and where its source is")
    name = app["name"]
    version = _version(app["version"], root)
    executable = app.get("executable", f"shack-{app['kind']}")
    # Installer file names, REPO-LAYOUT §5: OpenHamShack-<Name>-<version>-<os>-<arch>.<ext>,
    # Name being the product name after "OpenHamShack " with spaces as hyphens.
    file_name = re.sub(r"\s+", "-", name.removeprefix("OpenHamShack ").strip())
    return {
        "schema": 1,
        "id": app["id"],
        "kind": app["kind"],
        "name": name,
        "summary": app["summary"],
        "description": app.get("description", app["summary"]).strip(),
        "version": version,
        "executable": executable,
        "entry_module": module,
        "entry_function": func or "",
        "icon": app["icon"],
        "categories": app.get("categories", ["HamRadio"]),
        "keywords": app.get("keywords", []),
        "license": app.get("license", "GPL-3.0-or-later"),
        "homepage": app.get("homepage", "https://openhamshack.com/"),
        "publisher": app.get("publisher", "360 Technology"),
        "maintainer": app.get("maintainer", "Brian Henderson <noreply@openhamshack.com>"),
        "file_name": f"OpenHamShack-{file_name}-{version}",
        "python": {
            "paths": [str((root / p).resolve()) for p in py.get("paths", ["src"])],
            "requirements": list(py.get("requirements", [])),
        },
        "linux": {
            "depends": list(linux.get("depends", ["python3 (>= 3.11)"])),
            "recommends": list(linux.get("recommends", [])),
            "section": linux.get("section", "hamradio"),
            "vendor": list(linux.get("vendor", py.get("requirements", []))),
        },
        "windows": {
            "app_guid": guid,
            "app_user_model_id": win.get("app_user_model_id", "OpenHamShack." + "".join(
                p.capitalize() for p in re.split(r"[-_]", app["kind"]))),
            "bundle": bundle,
        },
        "macos": {
            "bundle_id": mac.get("bundle_id", app["id"]),
            "minimum_system": mac.get("minimum_system", "12.0"),
        },
        "license_file": str((root / app.get("license_file", "LICENSE")).resolve()),
        # An app's own PyInstaller spec, in place of the kit's
        # pyinstaller/shack-app.spec: the kit's is for web-UI apps (it
        # leaves Qt out and pulls in GTK's bindings), and a Qt app - QO-100
        # NB, Radio, DATV, ATV - knows its own data files and lazily
        # imported modules. Staged as app.spec; the builders prefer it.
        "spec_file": str((root / d["pyinstaller"]["spec"]).resolve()) if d.get("pyinstaller", {}).get("spec") else "",
        # And its own list of libraries the AppImage leaves to the host, in
        # place of appimage/host-libs.txt (staged as host-libs.txt).
        "host_libs_file": str((root / linux["host_libs"]).resolve()) if linux.get("host_libs") else "",
        # The third-party notice the Windows installer puts beside the
        # executable when it carries bundled programs (staged as
        # THIRD-PARTY.txt).
        "notices_file": str((root / win["notices"]).resolve()) if win.get("notices") else "",
    }


def _brand_icon(brand: Path, icon: str) -> dict[str, Path]:
    png = brand / "icons" / "png" / icon
    found = {str(s): png / f"{icon}-{s}.png" for s in ICON_SIZES}
    found["1024"] = png / "ios-1024.png"
    found["ico"] = png / f"{icon}.ico"
    found["svg"] = brand / "icons" / "svg" / f"{icon}.svg"
    missing = [str(p) for p in found.values() if not p.is_file()]
    if missing:
        raise DescriptorError(f"brand kit icon {icon!r} is incomplete under {brand}: missing {', '.join(missing)}")
    return found


# The frozen program's entry script. Bundled programs ([windows] bundle)
# sit in folders beside the executable and go first on PATH before the app
# is imported, so it finds them as it finds any program (shutil.which,
# subprocess with a bare name) and needs no code of its own per OS: on
# Linux and macOS, and in the .deb (not frozen), there are no such folders
# and PATH is left alone. First, not last: a tester's own older ffmpeg on
# PATH must not stand in for the build the app was proved with.
LAUNCHER = '''"""Entry point of the frozen {name} (written by packaging/tools/shackpkg.py)."""
import os
import sys

BUNDLED = {bundled!r}
if getattr(sys, "frozen", False) and BUNDLED:
    _here = os.path.dirname(os.path.abspath(sys.executable))
    _found = [os.path.join(_here, d) for d in BUNDLED if os.path.isdir(os.path.join(_here, d))]
    if _found:
        os.environ["PATH"] = os.pathsep.join(_found + [os.environ.get("PATH", "")])

{imp}

if __name__ == "__main__":
    sys.exit({call})
'''


def stage(descriptor: Path, out: Path, brand: Path = DEFAULT_BRAND) -> dict:
    meta = load(descriptor)
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "src").mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.egg-info", "tests")
    for p in meta["python"]["paths"]:
        src = Path(p)
        if not src.is_dir():
            raise DescriptorError(f"python.paths: {src} is not a folder")
        for child in sorted(src.iterdir()):
            if child.name in ("__pycache__",) or child.name.endswith(".egg-info"):
                continue
            dest = out / "src" / child.name
            if dest.exists():
                raise DescriptorError(f"python.paths: {child.name} comes from two paths")
            if child.is_dir():
                shutil.copytree(child, dest, ignore=ignore)
            else:
                shutil.copy2(child, dest)
    if not (out / "src" / meta["entry_module"].split(".")[0]).exists():
        raise DescriptorError(f"app.entry: {meta['entry_module']} is not in python.paths")
    icons = out / "icons"
    icons.mkdir()
    for key, src in _brand_icon(Path(brand), meta["icon"]).items():
        name = {"ico": "app.ico", "svg": "app.svg"}.get(key, f"{key}.png")
        shutil.copy2(src, icons / name)
    if meta["spec_file"]:
        spec = Path(meta["spec_file"])
        if not spec.is_file():
            raise DescriptorError(f"pyinstaller.spec: no spec at {spec}")
        shutil.copy2(spec, out / "app.spec")
    if meta["host_libs_file"]:
        hl = Path(meta["host_libs_file"])
        if not hl.is_file():
            raise DescriptorError(f"linux.host_libs: no list at {hl}")
        shutil.copy2(hl, out / "host-libs.txt")
    if meta["notices_file"]:
        nf = Path(meta["notices_file"])
        if not nf.is_file():
            raise DescriptorError(f"windows.notices: no notice at {nf}")
        shutil.copy2(nf, out / "THIRD-PARTY.txt")
    lic = Path(meta["license_file"])
    if not lic.is_file():
        raise DescriptorError(f"no licence text at {lic}")
    shutil.copy2(lic, out / "LICENSE")
    (out / "requirements.txt").write_text("".join(f"{r}\n" for r in meta["python"]["requirements"]))
    call = f"{meta['entry_function']}()" if meta["entry_function"] else "None"
    imp = (f"from {meta['entry_module']} import {meta['entry_function']}" if meta["entry_function"]
           else f"import {meta['entry_module']}")
    (out / "launcher.py").write_text(LAUNCHER.format(
        name=meta["name"], bundled=[b["name"] for b in meta["windows"]["bundle"]], imp=imp, call=call))
    # Paths on this machine mean nothing on the build hosts: app.json
    # carries only what was resolved into the stage.
    public = {k: v for k, v in meta.items() if k not in ("license_file", "spec_file", "host_libs_file", "notices_file")}
    public["spec"] = "app.spec" if meta["spec_file"] else ""
    public["python"] = {"requirements": meta["python"]["requirements"],
                        "packages": sorted(p.name for p in (out / "src").iterdir() if p.is_dir())}
    (out / "app.json").write_text(json.dumps(public, indent=2) + "\n")
    return public


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("descriptor", type=Path)
    s.add_argument("out", type=Path)
    s.add_argument("--brand", type=Path, default=DEFAULT_BRAND)
    sh = sub.add_parser("show")
    sh.add_argument("descriptor", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "stage":
            meta = stage(a.descriptor, a.out, a.brand)
            print(f"staged {meta['name']} {meta['version']} in {a.out}")
        else:
            print(json.dumps(load(a.descriptor), indent=2))
    except (DescriptorError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"shackpkg: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
