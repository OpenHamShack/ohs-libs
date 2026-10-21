"""Catalog checks (I18N.md §3.2) for every catalog the run finds.

Five formats, one model. Each catalog file becomes a ``Catalog`` of
``Entry``s keyed the way the code asks for them (a Qt source text, a gettext
msgid, a web key, ``string/name`` on Android, an Xcode key), and the same
four rules run over all of them:

1. every key the code uses is in the English catalog for its mechanism;
2. every English entry has text, and plural entries have ``one`` and
   ``other`` (all English needs);
3. each translation uses the same placeholders as the English, so a
   translation cannot crash a format call or drop a value;
4. English keys no checked code uses are warnings, never failures (a key
   only another platform's kind uses is legitimate).

Rules 1 and 4 need the whole picture, so they only run where the run can
see it: rule 1 needs an English catalog for that mechanism under the checked
paths, and rule 4 needs a whole-repo run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import _xml
from ._core import CatalogUse, Finding, quote

PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")
ENGLISH_PLURALS = ("one", "other")


@dataclass
class Entry:
    key: str
    line: int
    # Plain entries have forms {"": text}; plural ones a form per category
    # (Qt and gettext forms are numbered, so they use "0", "1", ...).
    forms: dict[str, str]
    plural: bool = False
    # gettext keeps the plural English apart from the msgid.
    extra_english: tuple[str, ...] = ()


@dataclass
class Catalog:
    system: str
    path: str
    lang: str
    english: bool
    entries: dict[str, Entry] = field(default_factory=dict)


SYSTEM_NAMES = {
    "qt": ("Qt catalog (*_en.ts)", "run pylupdate6 to extract it"),
    "gettext": ("gettext template (*.pot)", "run pybabel extract to update it"),
    "web": ("web catalog (static/i18n/en.json)", "add it there"),
    "android": ("Android catalog (res/values/strings.xml)", "add it there"),
    "apple": ("String Catalog (Localizable.xcstrings)", "build in Xcode to extract it"),
}


# --------------------------------------------------------------------------
# Placeholders

# One pattern for every mechanism's placeholders. They do not collide in
# practice, and a single reading means a translator moving between catalogs
# meets one rule. Order matters: "%%" first (a literal percent), positional
# printf ("%1$s") before Qt's bare "%1".
_PLACEHOLDER = re.compile(
    r"""
    (?P<pct>%%)
  | (?P<pyname>%\([^)]+\)[-\#+ 0]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa])
  | (?P<subst>%\#@\w+@)
  | (?P<printf>%(?:\d+\$)?[-\#+ 0,(']*\d*(?:\.\d+)?(?:hh|h|ll|l|L|q|j|z|t)?[diouxXeEfFgGaAcsp@])
  | (?P<qt>%L?(?:n|\d{1,2}))
  | (?P<dollar>\$\{[^}]*\})
  | (?P<esc>\{\{|\}\})
  | (?P<brace>\{[A-Za-z_]?[A-Za-z0-9_.]*(?:![rsa])?(?::[^{}]*)?\})
    """,
    re.VERBOSE,
)


def placeholders(text: str) -> frozenset[str]:
    out = set()
    for m in _PLACEHOLDER.finditer(text):
        kind = m.lastgroup
        if kind in ("pct", "esc"):
            continue
        token = m.group()
        if kind == "brace":
            # "{freq:.3f}" and "{freq}" name the same value.
            token = "{" + re.split(r"[!:]", token[1:-1], maxsplit=1)[0] + "}"
        out.add(token)
    return frozenset(out)


def _show(ph: frozenset[str]) -> str:
    return ", ".join(sorted(ph)) if ph else "none"


# --------------------------------------------------------------------------
# Parsers. Each returns a Catalog or raises ValueError with a sentence.


def _lang_from_tag(tag: str) -> str:
    return tag.replace("_", "-").split(".")[0]


def parse_qt(path: str, text: str) -> Catalog | None:
    try:
        root = _xml.parse(text)
    except _xml.XMLError:
        return None  # a TypeScript file, most likely; not ours
    if root.tag != "TS":
        return None
    lang = _lang_from_tag(root.attrib.get("language", ""))
    if not lang:
        m = re.search(r"_([A-Za-z]{2,3}(?:[_-][A-Za-z]+)?)\.ts$", path)
        lang = _lang_from_tag(m.group(1)) if m else ""
    english = lang.split("-")[0].lower() == "en"
    cat = Catalog("qt", path, lang, english)
    for ctx in root.find_all("context"):
        for msg in ctx.find_all("message"):
            src = msg.first("source")
            if src is None:
                continue
            tr = msg.first("translation")
            plural = msg.attrib.get("numerus") == "yes"
            state = tr.attrib.get("type", "") if tr is not None else "unfinished"
            if state in ("vanished", "obsolete"):
                continue
            if plural:
                forms = {str(i): n.text for i, n in enumerate(tr.find_all("numerusform"))} if tr is not None else {}
            else:
                forms = {"": tr.text if tr is not None else ""}
            if state == "unfinished" and not english:
                forms = {}  # not translated yet: Qt falls back to the source
            entry = Entry(src.text, src.line, forms, plural)
            cat.entries.setdefault(src.text, entry)
    return cat


def _po_unquote(s: str) -> str:
    return json.loads(s) if s.startswith('"') else ""


def parse_po(path: str, text: str) -> Catalog:
    is_pot = path.endswith(".pot")
    lang = "en" if is_pot else ""
    entries: list[dict] = []
    cur: dict = {}
    field_name = None

    def finish():
        nonlocal cur
        if "msgid" in cur:
            entries.append(cur)
        cur = {}

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            finish()
            field_name = None
            continue
        if line.startswith("#"):
            if any(k.startswith("msgstr") for k in cur):
                finish()
            if line.startswith("#,") and "fuzzy" in line:
                cur["fuzzy"] = True
            continue
        m = re.match(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)\s+(".*")$', line)
        if m:
            name = m.group(1)
            if name in ("msgctxt", "msgid") and any(k.startswith("msgstr") for k in cur):
                finish()
            field_name = name
            if name == "msgid":
                cur["line"] = n
            try:
                cur[name] = _po_unquote(m.group(3))
            except json.JSONDecodeError as err:
                raise ValueError(f"line {n} has a string that is not properly quoted") from err
        elif line.startswith('"') and field_name:
            try:
                cur[field_name] += _po_unquote(line)
            except json.JSONDecodeError as err:
                raise ValueError(f"line {n} has a string that is not properly quoted") from err
        else:
            raise ValueError(f"line {n} is not a gettext keyword, string or comment")
    finish()
    for e in entries:
        if e["msgid"] == "":
            m = re.search(r"^Language:\s*(\S+)", e.get("msgstr", ""), re.MULTILINE)
            if m and not lang:
                lang = _lang_from_tag(m.group(1))
    if not lang:
        parts = path.split("/")
        if "LC_MESSAGES" in parts and parts.index("LC_MESSAGES") >= 1:
            lang = parts[parts.index("LC_MESSAGES") - 1]
        else:
            lang = parts[-1].rsplit(".", 1)[0]
        lang = _lang_from_tag(lang)
    english = lang.split("-")[0].lower() == "en"
    cat = Catalog("gettext", path, lang, english)
    for e in entries:
        if e["msgid"] == "":
            continue  # the header
        plural = "msgid_plural" in e
        if plural:
            forms = {k[7:-1]: v for k, v in e.items() if k.startswith("msgstr[")}
        else:
            forms = {"": e.get("msgstr", "")}
        if e.get("fuzzy") and not english:
            forms = {}  # gettext ignores fuzzy translations at runtime, so do we
        extra = (e["msgid_plural"],) if plural else ()
        cat.entries.setdefault(e["msgid"], Entry(e["msgid"], e.get("line", 1), forms, plural, extra))
    return cat


def _json_line(lines: list[str], key: str) -> int:
    needle = re.compile(re.escape(json.dumps(key, ensure_ascii=False)) + r"\s*:")
    for i, line in enumerate(lines, 1):
        if needle.search(line):
            return i
    needle = re.compile(re.escape(json.dumps(key)) + r"\s*:")
    for i, line in enumerate(lines, 1):
        if needle.search(line):
            return i
    return 1


def parse_web(path: str, text: str) -> Catalog:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError(f"it is not valid JSON ({err.msg} at line {err.lineno})") from err
    if not isinstance(data, dict):
        raise ValueError("it must be a JSON object of keys")
    lang = _lang_from_tag(path.rsplit("/", 1)[-1][: -len(".json")])
    cat = Catalog("web", path, lang, lang.split("-")[0].lower() == "en")
    lines = text.splitlines()

    def walk(obj: dict, prefix: str):
        for k, v in obj.items():
            key = f"{prefix}{k}"
            line = _json_line(lines, k)
            if isinstance(v, str):
                cat.entries[key] = Entry(key, line, {"": v})
            elif isinstance(v, dict) and v and set(v) <= set(PLURAL_CATEGORIES) and all(isinstance(x, str) for x in v.values()):
                cat.entries[key] = Entry(key, line, dict(v), plural=True)
            elif isinstance(v, dict):
                # Nested objects are namespaces ("tx": {"blocked": …} is the
                # key "tx.blocked"), which is how shack-i18n.js resolves them.
                walk(v, key + ".")
            else:
                cat.entries[key] = Entry(key, line, {"": None})  # type: ignore[dict-item]

    walk(data, "")
    return cat


_ANDROID_LANG = re.compile(r"^values-([a-z]{2,3})(?:-r([A-Z]{2}))?$|^values-b\+([a-z]{2,3})(?:\+(\w+))?$")


def _android_text(node: _xml.Node) -> str:
    t = node.text.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), t)


def parse_android(path: str, text: str) -> Catalog | None:
    folder = path.split("/")[-2]
    if folder == "values":
        lang, english = "en", True
    else:
        m = _ANDROID_LANG.match(folder)
        if not m:
            return None  # values-night, values-land …: configuration, not a language
        lang = "-".join(p for p in (m.group(1) or m.group(3), m.group(2) or m.group(4)) if p)
        english = lang.split("-")[0] == "en"
    try:
        root = _xml.parse(text)
    except _xml.XMLError as err:
        raise ValueError(f"it is not well-formed XML ({err} at line {err.line})") from err
    cat = Catalog("android", path, lang, english)
    for node in root.children:
        name = node.attrib.get("name")
        if not name:
            continue
        if node.tag == "string":
            cat.entries[f"string/{name}"] = Entry(f"string/{name}", node.line, {"": _android_text(node)})
        elif node.tag == "plurals":
            forms = {i.attrib.get("quantity", ""): _android_text(i) for i in node.find_all("item")}
            cat.entries[f"plurals/{name}"] = Entry(f"plurals/{name}", node.line, forms, plural=True)
    return cat


def _unit_value(loc: dict) -> str | None:
    unit = loc.get("stringUnit")
    return unit.get("value") if isinstance(unit, dict) else None


def parse_xcstrings(path: str, text: str) -> list[Catalog]:
    """One Catalog per language in the file (a String Catalog holds them all)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError(f"it is not valid JSON ({err.msg} at line {err.lineno})") from err
    source = data.get("sourceLanguage", "en")
    strings = data.get("strings", {})
    if not isinstance(strings, dict):
        raise ValueError('its "strings" member must be an object')
    lines = text.splitlines()
    cats: dict[str, Catalog] = {}

    def cat(lang: str) -> Catalog:
        if lang not in cats:
            cats[lang] = Catalog("apple", path, lang, lang == source)
        return cats[lang]

    english = cat(source)
    for key, item in strings.items():
        item = item if isinstance(item, dict) else {}
        line = _json_line(lines, key)
        locs = item.get("localizations", {}) or {}
        src = locs.get(source)
        if src is None:
            # Xcode's convention: with no source-language entry the key itself
            # is the English text.
            english.entries[key] = Entry(key, line, {"": key})
        for lang, loc in locs.items():
            if not isinstance(loc, dict):
                continue
            plural = (loc.get("variations") or {}).get("plural")
            if isinstance(plural, dict):
                forms = {k: _unit_value(v) or "" for k, v in plural.items() if isinstance(v, dict)}
                cat(lang).entries[key] = Entry(key, line, forms, plural=True)
            else:
                value = _unit_value(loc)
                if value is None and lang != source:
                    continue
                cat(lang).entries[key] = Entry(key, line, {"": value or ""})
    return list(cats.values())


# --------------------------------------------------------------------------
# The rules


def _english_findings(cat: Catalog) -> list[Finding]:
    out = []
    for e in cat.entries.values():
        if cat.system == "gettext":
            continue  # the msgid is the English; checked for plurals against uses
        if cat.system == "qt" and not e.plural:
            continue  # the source text is the English, and cannot be empty
        if e.plural:
            have = {k for k, v in e.forms.items() if v}
            if cat.system == "qt":
                missing = len(have) < 2
            else:
                missing = not set(ENGLISH_PLURALS) <= have
            if missing:
                need = "both numerusforms" if cat.system == "qt" else '"one" and "other"'
                out.append(Finding(cat.path, e.line, 1, f"the English plural {quote(e.key)} needs {need} filled in"))
        elif e.forms.get("") is None:
            out.append(Finding(cat.path, e.line, 1, f"the entry {quote(e.key)} must be a string or a plural object"))
        elif not e.forms.get("", "").strip():
            out.append(Finding(cat.path, e.line, 1, f"the English text for {quote(e.key)} is empty"))
    return out


def _english_placeholders(e: Entry) -> frozenset[str]:
    # An Xcode entry with an empty English value shows its key, so the key's
    # placeholders are what a translation must match.
    texts = [v for v in e.forms.values() if v] or [e.key]
    return frozenset().union(*(placeholders(t) for t in [*texts, *e.extra_english]))


def _translation_findings(cat: Catalog, english: dict[str, Entry]) -> list[Finding]:
    out = []
    for key, e in cat.entries.items():
        src = english.get(key)
        if src is None:
            continue
        if cat.system in ("qt", "gettext"):
            # The source text (and gettext's msgid_plural) is the English.
            want = placeholders(key).union(*(placeholders(x) for x in src.extra_english))
        else:
            want = _english_placeholders(src)
        for form, text in e.forms.items():
            if not text:
                continue  # untranslated: the English shows instead
            got = placeholders(text)
            # A plural form may leave a placeholder out ("one station" with no
            # number is good English and good Russian); it may never add one.
            bad = got != want if not (e.plural or src.plural) else not got <= want
            if bad:
                which = f' (plural form "{form}")' if form else ""
                out.append(
                    Finding(
                        cat.path, e.line, 1,
                        f"the {cat.lang} translation of {quote(key)}{which} has placeholders {_show(got)} "
                        f"where the English has {_show(want)}; a format call would fail or lose a value",
                    )
                )  # fmt: skip
    return out


def check(catalogs: list[Catalog], uses: list[CatalogUse], full_run: bool) -> tuple[list[Finding], list[Finding]]:
    """(findings, warnings) for the catalogs and the keys the code used."""
    findings: list[Finding] = []
    warnings: list[Finding] = []
    by_system: dict[str, list[Catalog]] = {}
    for c in catalogs:
        by_system.setdefault(c.system, []).append(c)

    for system, cats in by_system.items():
        english: dict[str, Entry] = {}
        for c in cats:
            if c.english:
                findings.extend(_english_findings(c))
                for k, e in c.entries.items():
                    english.setdefault(k, e)
        for c in cats:
            if not c.english:
                findings.extend(_translation_findings(c, english))

    used_keys: dict[str, set[str]] = {}
    first_use: dict[str, CatalogUse] = {}
    for u in uses:
        used_keys.setdefault(u.system, set()).add(u.key)
        first_use.setdefault(u.system, u)
        english_cats = [c for c in by_system.get(u.system, []) if c.english]
        desc, fix = SYSTEM_NAMES[u.system]
        if not english_cats:
            continue
        entry = next((c.entries[u.key] for c in english_cats if u.key in c.entries), None)
        where = ", ".join(sorted({c.path for c in english_cats}))
        shown = "@" + u.key if u.system == "android" else quote(u.key)
        if entry is None:
            findings.append(Finding(u.path, u.line, u.col, f"{shown} is not in the English catalog ({where}); {fix}"))
        elif u.plural and not entry.plural:
            findings.append(
                Finding(u.path, u.line, u.col, f"{shown} is used as a plural but its English entry in {where} has no plural forms")
            )

    # One finding, not one per use, when a mechanism is used and its English
    # catalog is nowhere in the repo: the fix is a single file.
    if full_run:
        for system, u in first_use.items():
            if not any(c.english for c in by_system.get(system, [])):
                desc, _ = SYSTEM_NAMES[system]
                shown = "@" + u.key if system == "android" else quote(u.key)
                findings.append(
                    Finding(u.path, u.line, u.col, f"this code uses catalog keys such as {shown}, but the repo has no English {desc}")
                )
        for system, cats in by_system.items():
            used = used_keys.get(system, set())
            for c in cats:
                if not c.english:
                    continue
                for key, e in c.entries.items():
                    if key not in used:
                        warnings.append(
                            Finding(c.path, e.line, 1, f"{quote(key)} in the English catalog is not used by any checked code", warning=True)
                        )
    return findings, warnings
