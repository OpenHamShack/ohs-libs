#!/usr/bin/env python3
"""Write the English gettext templates of the Python packages.

I18N.md §2 names `pybabel extract` for this; Babel is not installed on the
build hosts and the libraries are stdlib-only, so this does the same for
the one pattern they use, a literal inside `_()`, plus the two catalogs
that are not calls:

- shack-client, shack-cw, shack-desktop, shack-packet, shack-pad, shack-rec, shack-rig: every `_("...")` in the package's
  source, subpackages included. Each package has its own domain (I18N.md
  §2, one domain per package), so a template lists only its own package's
  sentences.
- shack-bandplan: every sentence template in shack_bandplan.messages.T and
  every sentence in the dataset (refused ranges, condition refusals,
  permit variants, plan advisories), which verdicts carry as key "data" with the English as
  msgid (I18N.md §2 "Dataset text ... is extracted into the
  shack-bandplan domain at build time").

Run it after changing a sentence; `tools/test.sh --check-pot` fails when a
committed template is out of date, so a sentence cannot reach an app
without reaching the catalog.

Usage: tools/extract_pot.py [--check]
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def po_quote(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render(domain: str, entries: dict[str, list[str]]) -> str:
    out = [f'# English template for the {domain} domain. Written by tools/extract_pot.py; do not edit.',
           'msgid ""', 'msgstr ""', '"Content-Type: text/plain; charset=UTF-8\\n"', f'"X-Domain: {domain}\\n"', '']
    for msgid in sorted(entries):
        for where in sorted(set(entries[msgid])):
            out.append(f"#: {where}")
        out += [f"msgid {po_quote(msgid)}", 'msgstr ""', ""]
    return "\n".join(out)


def underscore_entries(package: str) -> dict:
    entries: dict[str, list[str]] = {}
    src = ROOT / "python" / package / "src" / package
    for p in sorted(src.rglob("*.py")):
        tree = ast.parse(p.read_text(), str(p))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                entries.setdefault(node.args[0].value, []).append(str(p.relative_to(ROOT)))
    return entries


def bandplan_entries() -> dict:
    sys.path.insert(0, str(ROOT / "python" / "shack_bandplan" / "src"))
    from shack_bandplan import messages, load
    entries: dict[str, list[str]] = {}
    for key, text in messages.T.items():
        entries.setdefault(text, []).append(f"shack_bandplan.messages:{key}")
    data = load(ROOT / "data" / "bandplan")
    for rid, reg in data.regimes.items():
        for row in reg.get("refused", []):
            entries.setdefault(row["text"], []).append(f"regimes/{rid}.json:refused")
        for c in reg.get("conditions", []):
            if "refuse" in c:
                entries.setdefault(c["refuse"], []).append(f"regimes/{rid}.json:conditions.{c['key']}")
        for p in reg.get("permits", []):
            for v in p.get("variants", []):
                entries.setdefault(v["text"], []).append(f"regimes/{rid}.json:permits.{p['id']}")
    for pid, pl in data.plans.items():
        for a in pl.get("advisories", []):
            entries.setdefault(a["text"], []).append(f"plans/{pid}.json:advisories")
    return entries


TARGETS = [
    *[(ROOT / "python" / pkg / "src" / pkg / "locale" / f"{pkg.replace('_', '-')}.pot", pkg.replace("_", "-"),
       lambda pkg=pkg: underscore_entries(pkg))
      for pkg in ("shack_client", "shack_cw", "shack_desktop", "shack_packet", "shack_pad", "shack_rec", "shack_rig")],
    (ROOT / "python" / "shack_bandplan" / "src" / "shack_bandplan" / "locale" / "shack-bandplan.pot",
     "shack-bandplan", bandplan_entries),
]


def main(argv=None) -> int:
    check = "--check" in (argv if argv is not None else sys.argv[1:])
    stale = []
    for path, domain, entries in TARGETS:
        text = render(domain, entries())
        if check:
            if not path.is_file() or path.read_text() != text:
                stale.append(path.relative_to(ROOT))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    for p in stale:
        print(f"{p} is out of date: run tools/extract_pot.py")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
