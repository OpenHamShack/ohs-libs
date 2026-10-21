"""``i18n-allow.txt`` and the shared technical-terms list (I18N.md §3.3).

The allow-list is the one place a repo records why a literal at a sink is
not language, so it is strict: an entry needs a reason, and an entry that
matched nothing this run is a finding, so the list shrinks as code changes
instead of rotting. There are deliberately no inline ignore comments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ._core import Finding, quote
from ._glob import match

_FORMAT = 'write glob | "literal" | reason'


@dataclass
class Entry:
    glob: str
    literal: str
    reason: str
    line: int
    used: bool = False


def technical_terms() -> frozenset[str]:
    text = resources.files("shack_i18n").joinpath("data/technical-terms.txt").read_text(encoding="utf-8")
    return frozenset(line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))


def load(path: Path, shown: str) -> tuple[list[Entry], list[Finding]]:
    """Entries from the file and findings for its malformed lines."""
    entries: list[Entry] = []
    problems: list[Finding] = []
    if not path.is_file():
        return entries, problems
    decoder = json.JSONDecoder()
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        glob, bar, rest = line.partition("|")
        glob, rest = glob.strip(), rest.lstrip()
        if not bar or not glob:
            problems.append(Finding(shown, n, 1, f"this line is not an allow-list entry; {_FORMAT}"))
            continue
        # The literal is JSON so it can hold "|", quotes and any character
        # exactly; split on "|" only after it has been read.
        try:
            literal, end = decoder.raw_decode(rest)
        except json.JSONDecodeError:
            literal, end = None, 0
        if not isinstance(literal, str):
            problems.append(
                Finding(shown, n, 1, f'the literal in this allow-list entry must be a JSON string in double quotes; {_FORMAT}')
            )
            continue
        tail = rest[end:].strip()
        if tail and not tail.startswith("|"):
            problems.append(Finding(shown, n, 1, f"this line has text after the literal that is not a reason; {_FORMAT}"))
            continue
        reason = tail[1:].strip()
        if not reason:
            problems.append(
                Finding(shown, n, 1, f"the allow-list entry for {quote(literal)} has no reason; add one after a second \"|\" saying why it is not user text")
            )
            # Still honoured: one missing reason should produce one finding,
            # not that finding plus every literal the entry was covering.
        entries.append(Entry(glob, literal, reason, n))
    return entries, problems


def apply(
    findings: list[Finding],
    entries: list[Entry],
    terms: frozenset[str],
    scanned: set[str],
    full_run: bool,
    shown: str,
) -> list[Finding]:
    """Findings that survive the terms list and the allow-list, plus stale entries.

    An entry is judged stale only where this run could have seen its
    literal: on a whole-repo run, or when its glob matches a file that was
    scanned. Running the check on one file must not condemn every entry for
    the rest of the repo.
    """
    kept: list[Finding] = []
    for f in findings:
        if f.literal is None:
            kept.append(f)
            continue
        if f.literal in terms:
            continue
        hit = False
        for e in entries:
            if e.literal == f.literal and match(e.glob, f.path):
                e.used = hit = True
        if not hit:
            kept.append(f)
    for e in entries:
        if e.used:
            continue
        if full_run or any(match(e.glob, p) for p in scanned):
            kept.append(
                Finding(shown, e.line, 1, f"the allow-list entry for {quote(e.literal)} in {e.glob} matched nothing in this run; remove it")
            )
    return kept
