"""The skimmer's callsign list: the `history` the spot rule asks
(calls.py). Two sources, either enough:

* the calls the RBN spotted on CW in a recent week, which ship with the
  neural engine's language model (nn/cwlm.npz `calls`, ~17,700 calls,
  built by tools/cw_lm/build.py from the RBN's public archive). Always
  there, so the rule never waits on a download; and
* Super Check Partial's MASTER.SCP, if the operator has one (below).

Measured on the CW survey's RBN-labelled recordings with both engines and
the LM (tools/skim_eval.py; calls.py has the table): the RBN week alone
spotted 70 RBN calls, MASTER.SCP alone 66, both 72. They overlap little
(5,700 calls): the contest list holds contesters who were not on that
week, the RBN week casual operators who never send a log.

MASTER.SCP is the contest community's list of active calls (about 50,000,
built twice a week from contributed Cabrillo logs; one call a line, `#`
comments, a `!!Order` header). It is not bundled with any app: neither
supercheckpartial.com nor the file states terms that allow passing it on
(read 14/10/2026), and Brian's skimmer decision 4 (CW-SKIMMER-PLAN §10)
was "never bundled until its terms are read". The site's developer guide
does invite logging software to download it itself - a conditional GET,
no more than once a day, a User-Agent naming the software, and a way for
the user to turn that off - and `fetch` does exactly that, into the app's
own data folder, for an app whose operator has switched it on. An
operator may also put their own copy there by hand.

    hist = Scp([data_dir / "MASTER.SCP"], base=bundled_calls())
    hist("GM4BRI")   # True / False; None only with no list of either kind

`Scp` re-reads the file when it changes (checked at most every
RECHECK_S), so a list fetched after the skimmer started, or one an
operator drops in, is used without a restart. It is handed to the
skimmer's worker process by name: ("shack_cw.skim.scp:history", [path]).
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..i18n import _
from .calls import home_of

URL = "https://supercheckpartial.com/downloads/MASTER.SCP"
FILENAME = "MASTER.SCP"
RECHECK_S = 60.0
FETCH_EVERY_S = 86_400.0     # the site's guide: no more than once a day
MIN_CALLS = 1_000            # a file with fewer is a broken download, not a list


def parse(text: str) -> frozenset[str]:
    """The calls of a MASTER.SCP (or any one-call-a-line list)."""
    out = set()
    for ln in text.splitlines():
        w = ln.strip().upper()
        if w and not w.startswith(("#", "!")) and " " not in w:
            out.add(w)
    return frozenset(out)


def bundled_calls() -> frozenset[str]:
    """The RBN week's calls shipped in the language model's table, or an
    empty set if this build has none."""
    try:
        import numpy as np

        from ..nn.lm import TABLE
        with np.load(TABLE, allow_pickle=False) as z:
            return frozenset(bytes(z["calls"]).decode().split()) if "calls" in z.files else frozenset()
    except (OSError, KeyError, ValueError, ImportError):
        return frozenset()


class Scp:
    """A callsign list: `base` (a set) and the first of `paths` that exists
    (see the module). Calling it answers whether a call is in either: the
    call as printed, or its home call (G4ABC for G4ABC/P); None while there
    is neither, so the spot rule falls back to the call's shape."""

    def __init__(self, paths, recheck_s: float = RECHECK_S, base: frozenset = frozenset()):
        self.paths = [Path(p) for p in paths if p]
        self.base = frozenset(base)
        self.recheck_s = float(recheck_s)
        self.calls: frozenset[str] | None = None
        self.source: Path | None = None
        self._stamp = None
        self._checked = -1e18
        self._load()

    def _load(self) -> None:
        self._checked = time.monotonic()
        for p in self.paths:
            try:
                st = p.stat()
            except OSError:
                continue
            stamp = (str(p), st.st_mtime_ns, st.st_size)
            if stamp == self._stamp:
                return
            try:
                calls = parse(p.read_text(encoding="latin-1"))
            except OSError:
                continue
            if len(calls) < MIN_CALLS:
                continue
            self.calls, self.source, self._stamp = calls, p, stamp
            return
        self.calls, self.source, self._stamp = None, None, None

    def __call__(self, call: str) -> bool | None:
        if time.monotonic() - self._checked >= self.recheck_s:
            self._load()
        if self.calls is None and not self.base:
            return None
        home = home_of(call)
        return any(c in s for s in (self.base, self.calls or ()) for c in (call, home))

    def __len__(self) -> int:
        return len(self.base | (self.calls or frozenset()))


def history(*paths) -> Scp:
    """The runner's factory: ("shack_cw.skim.scp:history", [path, ...]):
    the bundled RBN week plus the first of the files that exists."""
    flat = []
    for p in paths:
        flat += list(p) if isinstance(p, (list, tuple)) else [p]
    return Scp(flat, base=bundled_calls())


def fetch(path, user_agent: str, *, url: str = URL, every_s: float = FETCH_EVERY_S, timeout: float = 20.0,
          opener=None) -> str:
    """Bring `path` up to date from supercheckpartial.com, the way the
    site's developer guide asks: nothing if the file is younger than
    `every_s`, else a conditional GET with the ETag kept beside it
    (`<path>.etag`). Returns "fresh" (too young to ask), "unchanged" (304),
    "fetched", or raises OSError/ValueError with the reason. A download
    that is not a list (under MIN_CALLS calls) never replaces the file.
    Blocking: call it from a thread of its own, never the receive path."""
    path = Path(path)
    tag_path = path.with_name(path.name + ".etag")
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        age = None
    if age is not None and age < every_s:
        return "fresh"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    if age is not None:
        try:
            req.add_header("If-None-Match", tag_path.read_text().strip())
        except OSError:
            pass
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=timeout) as r:
            body = r.read()
            etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            os.utime(path)      # asked today: the next ask is in a day
            return "unchanged"
        raise
    text = body.decode("latin-1")
    if len(parse(text)) < MIN_CALLS:
        raise ValueError(_("{url} gave {n} bytes that are not a callsign list").format(url=url, n=len(body)))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    if etag:
        tag_path.write_text(etag)
    return "fetched"
