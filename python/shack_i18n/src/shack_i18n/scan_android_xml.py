"""Android layout and menu XML (``res/layout*/``, ``res/menu*/``).

I18N.md §3.1 leaves XML layouts to Android Lint's ``HardcodedText`` raised
to an error. The beta session asked for this checker to cover them as well,
so a repo whose Gradle build has not run (or a Kotlin module without Lint
configured) still fails on ``android:text="Tune"``. Lint stays the second
net; the two agree on what a literal is: any value not starting with ``@``
(a resource) or ``?`` (a theme attribute).
"""

from __future__ import annotations

from . import _xml
from ._core import CatalogUse, Finding, Literal, ScanResult, is_user_text, quote

ATTRIBUTES = ("android:text", "android:hint", "android:contentDescription")


def scan(path: str, source: str, extra_attributes: tuple[str, ...] = ()) -> ScanResult:
    res = ScanResult()
    try:
        root = _xml.parse(source)
    except _xml.XMLError as err:
        res.findings.append(Finding(path, err.line, err.col, f"could not parse this XML file ({err}), so its strings were not checked"))
        return res
    names = ATTRIBUTES + tuple(a for a in extra_attributes if a not in ATTRIBUTES)
    for node in root.iter():
        for name, value in node.attrib.items():
            if value.startswith("@string/") or value.startswith("@plurals/"):
                res.uses.append(CatalogUse("android", value[1:], path, node.line, node.col, value.startswith("@plurals/")))
            if name not in names or value.startswith(("@", "?")):
                continue
            lit = Literal(value, (value,), node.line, node.col)
            if is_user_text(lit):
                res.findings.append(
                    Finding(
                        path, node.line, node.col,
                        f"user-visible literal {quote(value)} in {name} on <{node.tag}>; use @string/…",
                        literal=value,
                    )
                )  # fmt: skip
    return res
