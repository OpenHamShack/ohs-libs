"""The runtime codec loader (RADIO-SDR-PLAN §5): where each codec is
looked for, the child-process probe, and the status the app words.

**Nothing encumbered is ever ours.** faad2, fdk-aac and mbelib are opened
only from the user's own install (or, on a controller, the host library
the administrator installed), never shipped, linked or downloaded by any
build of ours; tools/check-encumbered.py fails a build that does.
codec2 is the exception we ship (Brian, 22/10/2026), and is still optional.

Where each codec is looked for, in order; the first file that passes wins:

1. `SHACK_CODEC_<ID>` (SHACK_CODEC_FAAD2=/path/libfaad.so.2), for tests and
   odd installs.
2. The app's codecs folder, which the user fills (`default_folder`); on a
   controller, the staged host library `$SHACK_HOSTLIBS/<name>/`, mounted
   read-only, in its place (§5.6: a controller has no user-writable codecs
   folder).
3. Only for a codec we may ship (codec2): the app's own bundle.
4. The system: Linux, the dynamic loader's cache and its folders; macOS,
   Homebrew's and MacPorts' folders; Windows, nothing (a bare name there is
   DLL search-order hijacking).

**The probe.** The first time a file is seen (by path, size, mtime and
sha256) it is opened in a child process, `python -m shack_sdr.codecs probe
<id> <path>`, which checks its entry points and version and decodes a
known input (codecs.rs). A crash, a hang (5 s) or a wrong answer refuses
the file - `crashed`, `timeout`, `bad_output` - until it changes. Only a
file that passed is ever opened in the app's own process. A frozen app has
no `python -m`: it passes `probe_argv`, a command line of its own that ends
up in `main(["probe", id, path])`.

**Status, never sentences.** Each codec is `found` (path, version),
`missing` (why `not_found`) or `refused` (why: the probe's code). `host` is
true on a controller, so the app says "ask your controller's
administrator" rather than "install it".
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import sysconfig
import threading
from pathlib import Path

from . import _native

PROBE_TIMEOUT_S = 5.0

#: Folders searched after the user's and the bundle's, per OS.
MAC_DIRS = ("/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib")


def _linux_dirs() -> list[str]:
    multi = sysconfig.get_config_var("MULTIARCH") or f"{os.uname().machine}-linux-gnu"
    return ["/usr/local/lib", f"/usr/lib/{multi}", f"/lib/{multi}", "/usr/lib64", "/lib64", "/usr/lib", "/lib"]


def default_folder(app: str = "openhamshack-radio", data_dir: str | os.PathLike | None = None) -> Path | None:
    """The user's codecs folder for `app` on this OS (`data_dir`/codecs when
    the app has its own data folder); None on a controller."""
    if os.environ.get("SHACK_HOSTLIBS"):
        return None
    if data_dir:
        return Path(data_dir) / "codecs"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app / "data" / "codecs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app / "codecs"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / app / "codecs"


class Loader:
    """The codecs one app can use. Most apps use the module-level one
    (`configure`, `status`, `path`)."""

    def __init__(self, folder: str | os.PathLike | None = None, bundle_dirs=(), cache_path=None, probe_argv=None):
        self.folder = Path(folder) if folder else None
        self.bundle_dirs = [Path(d) for d in bundle_dirs]
        self.cache_path = Path(cache_path) if cache_path else None
        self.probe_argv = list(probe_argv) if probe_argv else [sys.executable, "-m", "shack_sdr.codecs"]
        self._entries = {e["id"]: e for e in _native.codec_entries()}
        self._cache: dict[str, dict] = self._load_cache()
        self._status: list[dict] | None = None
        self._lock = threading.Lock()

    # -- the search --------------------------------------------------------------

    def candidates(self, codec: str) -> list[tuple[Path, str]]:
        """(path, where) for every file that exists, in search order."""
        e = self._entries[codec]
        names = e["names"]
        out: list[tuple[Path, str]] = []
        env = os.environ.get("SHACK_CODEC_" + codec.upper().replace("-", "_"))
        if env:
            out.append((Path(env), "env"))
        hostlibs = os.environ.get("SHACK_HOSTLIBS")
        if hostlibs and e["hostLibrary"]:
            out += [(p, "host") for p in _staged(Path(hostlibs) / e["hostLibrary"], names)]
        elif self.folder:
            out += [(self.folder / n, "folder") for n in names]
        if e["shippable"]:
            out += [(d / n, "bundle") for d in self.bundle_dirs for n in names]
        out += [(p, "system") for p in self._system(names)]
        seen, found = set(), []
        for p, where in out:
            if not p.is_absolute():
                continue
            key = str(p)
            if key in seen or not p.is_file():
                continue
            seen.add(key)
            found.append((p, where))
        return found

    @staticmethod
    def _system(names) -> list[Path]:
        if sys.platform == "win32":
            return []
        if sys.platform == "darwin":
            return [Path(d) / n for d in MAC_DIRS for n in names]
        out = []
        cache = _ldconfig()
        for n in names:
            out += cache.get(n, [])
        out += [Path(d) / n for d in _linux_dirs() for n in names]
        return out

    # -- the probe ---------------------------------------------------------------

    def _load_cache(self) -> dict:
        if self.cache_path and self.cache_path.is_file():
            try:
                return json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                return {}
        return {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, indent=1, sort_keys=True))
            os.replace(tmp, self.cache_path)
        except OSError:
            pass   # a cache we cannot write only costs a probe next time

    def check(self, codec: str, path: Path) -> dict:
        """The probe's verdict on one file: {"ok", "version"} or {"ok": False,
        "why", "detail"}, from the cache while the file is unchanged."""
        st = path.stat()
        key = f"{codec}:{path}"
        hit = self._cache.get(key)
        if hit and hit["size"] == st.st_size and hit["mtime"] == st.st_mtime_ns:
            return hit["result"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if hit and hit["sha256"] == digest:
            hit.update(size=st.st_size, mtime=st.st_mtime_ns)
            return hit["result"]
        result = self.probe(codec, path)
        self._cache[key] = {"size": st.st_size, "mtime": st.st_mtime_ns, "sha256": digest, "result": result}
        self._save_cache()
        return result

    def probe(self, codec: str, path: Path) -> dict:
        """Run the probe child on `path` now (no cache)."""
        env = dict(os.environ)
        # The child imports this same shack_sdr, wherever it was loaded from.
        here = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            r = subprocess.run(self.probe_argv + ["probe", codec, str(path)], capture_output=True, text=True,
                               timeout=PROBE_TIMEOUT_S, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "why": "timeout", "detail": None}
        line = (r.stdout.strip().splitlines() or [""])[-1]
        try:
            out = json.loads(line)
        except ValueError:
            out = None
        if r.returncode != 0 or not isinstance(out, dict):
            return {"ok": False, "why": "crashed", "detail": f"exit {r.returncode}"}
        return out

    # -- status ------------------------------------------------------------------

    def status(self, rescan: bool = False) -> list[dict]:
        """Every codec: {"id", "for", "state", "path", "version", "why",
        "detail", "where", "host", "shippable"}. Scans once; again only
        with `rescan` ("Look again")."""
        with self._lock:
            if self._status is not None and not rescan:
                return [dict(s) for s in self._status]
            host = bool(os.environ.get("SHACK_HOSTLIBS"))
            out = []
            for cid, e in self._entries.items():
                s = {"id": cid, "for": list(e["for"]), "state": "missing", "path": None, "version": None,
                     "why": "not_found", "detail": None, "where": None, "host": host and bool(e["hostLibrary"]),
                     "shippable": e["shippable"]}
                refused = None
                for p, where in self.candidates(cid):
                    r = self.check(cid, p)
                    if r.get("ok"):
                        s.update(state="found", path=str(p), version=r.get("version"), why=None, where=where)
                        break
                    if refused is None:
                        refused = dict(state="refused", path=str(p), why=r.get("why"), detail=r.get("detail"),
                                       where=where)
                else:
                    if refused:
                        s.update(refused)
                out.append(s)
            self._status = out
            return [dict(s) for s in out]

    def path(self, codec: str) -> str | None:
        """The file to open in-process for `codec`: one that passed the probe."""
        for s in self.status():
            if s["id"] == codec and s["state"] == "found":
                return s["path"]
        return None


def _staged(folder: Path, names) -> list[Path]:
    """A controller's staged copy of a host library (RADIO-SDR-PLAN §5.6),
    `$SHACK_HOSTLIBS/<name>/`: each known name first, then any versioned file
    of one of them (libfaad.so.2.0.0 for libfaad.so.2). The platform stages
    the package's files by pattern (`libfaad.so.2*`) and copies files, not
    links, so whether the soname itself arrives depends on how the package
    ships it; the versioned file is the same library either way."""
    out = [folder / n for n in names]
    try:
        files = sorted(p for p in folder.iterdir() if p.is_file())
    except OSError:
        return out
    out += [p for n in names for p in files if p.name.startswith(n + ".") and p not in out]
    return out


def _ldconfig() -> dict[str, list[Path]]:
    """soname -> paths, from the dynamic loader's cache (`ldconfig -p`)."""
    for exe in ("/sbin/ldconfig", "/usr/sbin/ldconfig"):
        if os.path.exists(exe):
            break
    else:
        return {}
    try:
        text = subprocess.run([exe, "-p"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[str, list[Path]] = {}
    for line in text.splitlines()[1:]:
        name, _, rest = line.strip().partition(" (")
        if "=>" in rest:
            out.setdefault(name, []).append(Path(rest.split("=>", 1)[1].strip()))
    return out


_loader: Loader | None = None


def configure(folder=None, *, app: str = "openhamshack-radio", data_dir=None, bundle_dirs=(), cache_path=None,
              probe_argv=None) -> Loader:
    """Set up the app's loader: its codecs folder (default from `app` and
    `data_dir`), its bundle folders (searched for codec2 only), a cache file
    for probe verdicts, and a probe command line for a frozen app."""
    global _loader
    _loader = Loader(folder if folder is not None else default_folder(app, data_dir), bundle_dirs, cache_path,
                     probe_argv)
    return _loader


def loader() -> Loader:
    return _loader or configure()


def status(rescan: bool = False) -> list[dict]:
    return loader().status(rescan)


def path(codec: str) -> str | None:
    return loader().path(codec)


def folder() -> Path | None:
    """The folder "Open codecs folder" opens; None on a controller."""
    return loader().folder


def main(argv: list[str] | None = None) -> int:
    """`probe <id> <path>`: the child's half (prints one JSON line).
    `status`: every codec's status, as JSON."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "probe":
        print(json.dumps(_native.codec_probe(argv[1], argv[2])), flush=True)
        return 0
    if argv[:1] == ["status"]:
        print(json.dumps(status(), indent=1))
        return 0
    print("usage: python -m shack_sdr.codecs probe <id> <path> | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
