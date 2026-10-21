"""Repo-relative globs for the allow-list and the include/exclude settings.

Not ``fnmatch`` itself: its ``*`` crosses ``/``, so ``ohsnb/*.py`` would
match ``ohsnb/ui/deep/x.py`` and an allow-list entry would quietly cover far
more than its author meant. I18N.md §3.3 asks for ``**`` to cross
directories, which only means something if ``*`` does not. ``*`` on its own
is the exception the contract names: every file.
"""

from __future__ import annotations

import functools
import re


@functools.lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def match(pattern: str, path: str) -> bool:
    """Whether a forward-slash path relative to the repo root matches."""
    pattern = pattern.strip()
    if pattern == "*":
        return True
    if pattern.startswith("./"):
        pattern = pattern[2:]
    return bool(_compile(pattern).match(path))
