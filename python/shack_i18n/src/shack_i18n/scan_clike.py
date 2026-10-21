"""Kotlin, Swift and JavaScript via one small tokenizer (I18N.md §3.1).

None of the three has a parser in the standard library, and a full parser
for each would be most of this package. What the check needs is narrower:
know where comments and string literals begin and end (so a quote inside a
comment, or ``//`` inside a string, never confuses it), split template
strings into literal parts and placeholders, and see enough punctuation to
find a sink and the expression after it. That is a tokenizer, shared by the
three languages with a table of the differences.

Sinks are written exactly as I18N.md's table writes them (``Text(``,
``.setText(``, ``text =``, ``Text(verbatim:``) and tokenized with the same
tokenizer, so a repo's ``.i18n-check.toml`` adds one in the same spelling.
The last token says what follows: ``(`` a call whose arguments are checked,
``=`` an assignment whose right-hand side is, ``:`` one labelled argument.

Only string literals at the top level of the checked expression count:
``Text("Tune")`` and ``title = if (tx) "On air" else "Idle"`` are findings,
``Text(stringResource(R.string.tune))`` and ``Text(name)`` are not, and a
literal inside a nested call (``Text(format("…"))``) is that call's business.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import scan_html
from ._core import CatalogUse, Finding, Literal, ScanResult, is_user_text, quote


@dataclass
class Token:
    kind: str  # ident, string, number, op, regex
    text: str
    line: int
    col: int  # 1-based
    nl_before: bool = False
    parts: tuple[str, ...] = ()  # string: literal pieces, placeholders removed
    value: str = ""  # string: literal with placeholders as written
    inner: list[list["Token"]] = field(default_factory=list)  # template expressions


@dataclass(frozen=True)
class Lang:
    name: str
    nested_comments: bool
    fix: str
    # Kotlin "$name", "${…}"; JS "${…}"; Swift "\(…)".
    dollar_ident: bool = False
    dollar_brace: bool = False
    backslash_paren: bool = False
    backtick_template: bool = False  # JS
    single_quote_string: bool = False  # JS; in Kotlin and Swift ' is not a string
    raw_hash_strings: bool = False  # Swift #"…"#
    regex_literals: bool = False  # JS
    backtick_ident: bool = False  # Kotlin and Swift `name`


KOTLIN = Lang("kotlin", nested_comments=True, fix="stringResource()", dollar_ident=True, dollar_brace=True, backtick_ident=True)
SWIFT = Lang("swift", nested_comments=True, fix="String(localized:)", backslash_paren=True, raw_hash_strings=True, backtick_ident=True)
JAVASCRIPT = Lang(
    "javascript", nested_comments=False, fix="t()", dollar_brace=True, backtick_template=True,
    single_quote_string=True, regex_literals=True,
)  # fmt: skip

_OPS = sorted(
    """>>>= === !== **= <<= >>= >>> ... ?. ?: ??= &&= ||= == != <= >= && || ?? ++ -- += -= *= /= %= &= |= ^= => -> :: ** <<""".split(),
    key=len,
    reverse=True,
)
# After these a "/" starts a regex rather than dividing (JS only). "}" is
# taken as the end of an expression, the common case; the rare block-then-
# regex at statement start costs a missed literal, not a false finding.
_REGEX_AFTER_WORDS = frozenset(
    {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "instanceof", "yield", "await"}
)


class TokenizeError(Exception):
    def __init__(self, message: str, line: int, col: int):
        super().__init__(message)
        self.line, self.col = line, col


class _Tokenizer:
    def __init__(self, src: str, lang: Lang):
        self.s, self.lang, self.i, self.line, self.col0 = src, lang, 0, 1, 0

    # position helpers -----------------------------------------------------
    def _col(self) -> int:
        return self.i - self.col0 + 1

    def _advance(self, n: int = 1) -> None:
        for _ in range(n):
            if self.i < len(self.s) and self.s[self.i] == "\n":
                self.line += 1
                self.col0 = self.i + 1
            self.i += 1

    def _peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < len(self.s) else ""

    # main loop ------------------------------------------------------------
    def tokens(self, until_close: str | None = None) -> list[Token]:
        """Tokens up to the end, or up to an unmatched ``until_close``."""
        out: list[Token] = []
        depth = 0
        nl = False
        s, lang = self.s, self.lang
        while self.i < len(s):
            c = s[self.i]
            if c == "\n":
                nl = True
                self._advance()
                continue
            if c.isspace():
                self._advance()
                continue
            if s.startswith("//", self.i):
                while self.i < len(s) and s[self.i] != "\n":
                    self._advance()
                continue
            if s.startswith("/*", self.i):
                self._block_comment()
                continue
            line, col = self.line, self._col()
            if until_close and c == until_close and depth == 0:
                self._advance()
                return out
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            tok = self._token(out)
            tok.line, tok.col, tok.nl_before = line, col, nl
            nl = False
            out.append(tok)
        if until_close:
            raise TokenizeError("a template expression is not closed", self.line, self._col())
        return out

    def _block_comment(self) -> None:
        start_line, start_col = self.line, self._col()
        self._advance(2)
        level = 1
        while self.i < len(self.s):
            if self.lang.nested_comments and self.s.startswith("/*", self.i):
                level += 1
                self._advance(2)
            elif self.s.startswith("*/", self.i):
                level -= 1
                self._advance(2)
                if level == 0:
                    return
            else:
                self._advance()
        raise TokenizeError("a block comment is not closed", start_line, start_col)

    def _token(self, prev: list[Token]) -> Token:
        s, lang, c = self.s, self.lang, self.s[self.i]
        if lang.raw_hash_strings and c == "#":
            j = self.i
            while j < len(s) and s[j] == "#":
                j += 1
            if j < len(s) and s[j] == '"':
                return self._string(hashes=j - self.i)
        if c == '"':
            return self._string()
        if c == "'" and lang.single_quote_string:
            return self._string(quote="'")
        if c == "'":
            return self._char()
        if c == "`" and lang.backtick_template:
            return self._string(quote="`")
        if c == "`" and lang.backtick_ident:
            start = self.i
            self._advance()
            while self.i < len(s) and s[self.i] not in "`\n":
                self._advance()
            self._advance()
            return Token("ident", s[start + 1 : self.i - 1], 0, 0)
        if c.isalpha() or c == "_" or (c == "$" and lang.name == "javascript"):
            start = self.i
            while self.i < len(s) and (s[self.i].isalnum() or s[self.i] in "_$"):
                self._advance()
            return Token("ident", s[start : self.i], 0, 0)
        if c.isdigit():
            start = self.i
            while self.i < len(s) and (s[self.i].isalnum() or s[self.i] in "._"):
                self._advance()
            return Token("number", s[start : self.i], 0, 0)
        if c == "/" and lang.regex_literals and self._regex_allowed(prev):
            return self._regex()
        for op in _OPS:
            if s.startswith(op, self.i):
                self._advance(len(op))
                return Token("op", op, 0, 0)
        self._advance()
        return Token("op", c, 0, 0)

    def _regex_allowed(self, prev: list[Token]) -> bool:
        if not prev:
            return True
        p = prev[-1]
        if p.kind == "ident":
            return p.text in _REGEX_AFTER_WORDS
        if p.kind in ("string", "number", "regex"):
            return False
        return p.text not in (")", "]", "}", "++", "--")

    def _regex(self) -> Token:
        start, in_class = self.i, False
        self._advance()
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\\":
                self._advance(2)
                continue
            if ch == "\n":
                break
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                self._advance()
                while self.i < len(self.s) and self.s[self.i].isalpha():
                    self._advance()
                return Token("regex", self.s[start : self.i], 0, 0)
            self._advance()
        # Not a regex after all (a division the heuristic misread): treat the
        # slash as an operator and carry on from just after it.
        self.i = start  # the scan stopped before any newline, so line/col still hold
        self._advance()
        return Token("op", "/", 0, 0)

    def _char(self) -> Token:
        start = self.i
        self._advance()
        while self.i < len(self.s) and self.s[self.i] not in "'\n":
            self._advance(2 if self.s[self.i] == "\\" else 1)
        self._advance()
        return Token("number", self.s[start : self.i], 0, 0)  # a char is data, never text

    def _string(self, quote: str = '"', hashes: int = 0) -> Token:
        s, lang = self.s, self.lang
        start_line, start_col, start = self.line, self._col(), self.i
        self._advance(hashes)
        triple = quote == '"' and s.startswith('"""', self.i)
        delim = '"""' if triple else quote
        self._advance(len(delim))
        close = delim + "#" * hashes
        escape = "\\" + "#" * hashes
        raw = lang.name == "kotlin" and triple  # Kotlin """…""" has no escapes
        parts: list[str] = []
        shown: list[str] = []
        buf: list[str] = []
        inner: list[list[Token]] = []

        def flush():
            parts.append("".join(buf))
            shown.append("".join(buf))
            buf.clear()

        while True:
            if self.i >= len(s):
                raise TokenizeError("a string is not closed", start_line, start_col)
            if s.startswith(close, self.i):
                self._advance(len(close))
                break
            ch = s[self.i]
            if ch == "\n" and not triple and quote != "`":
                raise TokenizeError("a string is not closed on its line", start_line, start_col)
            if lang.backslash_paren and s.startswith(escape + "(", self.i):
                flush()
                expr_start = self.i
                self._advance(len(escape) + 1)
                inner.append(self.tokens(until_close=")"))
                shown.append(s[expr_start : self.i])
                continue
            if lang.dollar_brace and s.startswith("${", self.i) and (lang.name != "javascript" or quote == "`"):
                flush()
                expr_start = self.i
                self._advance(2)
                inner.append(self.tokens(until_close="}"))
                shown.append(s[expr_start : self.i])
                continue
            if lang.dollar_ident and ch == "$" and (self._peek(1).isalpha() or self._peek(1) == "_"):
                flush()
                j = self.i + 1
                while j < len(s) and (s[j].isalnum() or s[j] == "_"):
                    j += 1
                shown.append(s[self.i : j])
                self._advance(j - self.i)
                continue
            if not raw and s.startswith(escape, self.i) and self.i + len(escape) < len(s):
                nxt = s[self.i + len(escape)]
                buf.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(nxt, nxt))
                self._advance(len(escape) + 1)
                continue
            buf.append(ch)
            self._advance()
        flush()
        tok = Token("string", s[start : self.i], 0, 0)
        tok.parts = tuple(p for p in parts)
        tok.value = "".join(shown)
        tok.inner = inner
        return tok


def tokenize(src: str, lang: Lang) -> list[Token]:
    return _Tokenizer(src, lang).tokens()


# --------------------------------------------------------------------------
# Sinks


@dataclass(frozen=True)
class Sink:
    spec: str
    pattern: tuple[str, ...]
    mode: str  # call, assign, arg
    shown: str
    fix: str | None = None
    html: bool = False  # literal parts are markup (innerHTML)
    attr_filter: frozenset[str] | None = None  # setAttribute: first arg must be one of these


def make_sink(spec: str, lang: Lang, **kw) -> Sink:
    toks = tokenize(spec, lang)
    if not toks or toks[-1].text not in ("(", "=", ":"):
        raise ValueError(f'The sink "{spec}" must end in "(", "=" or ":".')
    mode = {"(": "call", "=": "assign", ":": "arg"}[toks[-1].text]
    pattern = tuple(t.text for t in toks)
    if mode == "call":
        shown = "passed to " + spec.strip() + ")"
    elif mode == "arg":
        shown = "passed to " + spec.strip() + ")"
    else:
        shown = "assigned to " + spec.strip()[:-1].strip()
    return Sink(spec, pattern, mode, shown, **kw)


KOTLIN_SINKS = (
    "Text(", "text =", "label =", "title =", "placeholder =", "contentDescription =", "Toast.makeText(",
    ".setText(", ".setTitle(", ".setMessage(", ".setContentTitle(", ".setContentText(",
)  # fmt: skip
SWIFT_SINKS = (".text =", ".title =", ".body =", "setTitle(", "UILabel(", "UIAlertController(", "UIAlertAction(")
JAVASCRIPT_SINKS = (".textContent =", ".innerText =", ".title =", ".placeholder =", "alert(", "confirm(", "prompt(", "new Notification(")


def builtin_sinks(lang: Lang) -> list[Sink]:
    if lang is KOTLIN:
        return [make_sink(s, lang) for s in KOTLIN_SINKS]
    if lang is SWIFT:
        # Text(verbatim:) is for data that is already formatted (§2's Apple
        # row); letters in a literal there mean text that skipped the catalog.
        return [make_sink(s, lang) for s in SWIFT_SINKS] + [
            make_sink("Text(verbatim:", lang, fix='a String Catalog key: Text("…")')
        ]
    return [make_sink(s, lang) for s in JAVASCRIPT_SINKS] + [
        make_sink(".innerHTML =", lang, html=True),
        make_sink("setAttribute(", lang, attr_filter=frozenset({"title", "placeholder", "aria-label"})),
    ]


_DECLARATION_WORDS = frozenset({"val", "var", "let", "const", "fun", "func", "function", "def"})
# A newline ends an assignment's right-hand side unless the expression is
# visibly unfinished: the line ended in an operator, or the next starts with
# one. Kotlin, Swift and JS without semicolons all read it that way.
_CONTINUES_AFTER = frozenset({"+", "-", "*", "/", "?", ":", "?:", "=", "&&", "||", "??", ".", "?.", ",", "(", "[", "{"})
_CONTINUES_BEFORE = frozenset({"+", ".", "?.", "?", ":", "?:", "&&", "||", "??"})


def _match_at(toks: list[Token], i: int, pattern: tuple[str, ...]) -> bool:
    if i + len(pattern) > len(toks):
        return False
    for k, p in enumerate(pattern):
        t = toks[i + k]
        if t.kind == "string":
            return False
        if t.text == p:
            continue
        # "x +=" appends text exactly as "x =" sets it.
        if k == len(pattern) - 1 and p == "=" and t.text == "+=":
            continue
        return False
    return True


def _split_args(toks: list[Token], start: int) -> tuple[list[list[Token]], int]:
    """Arguments of a call whose "(" is just before ``start``; returns the
    argument token lists and the index after the closing ")"."""
    args, cur, depth, i = [], [], 0, start
    while i < len(toks):
        t = toks[i]
        if t.kind == "op" and t.text in "([{" and len(t.text) == 1:
            depth += 1
        elif t.kind == "op" and t.text in ")]}" and len(t.text) == 1:
            if depth == 0:
                if cur:
                    args.append(cur)
                return args, i + 1
            depth -= 1
        elif t.kind == "op" and t.text == "," and depth == 0:
            args.append(cur)
            cur = []
            i += 1
            continue
        cur.append(t)
        i += 1
    if cur:
        args.append(cur)
    return args, i


def _strip_label(arg: list[Token]) -> list[Token]:
    """Drop a leading ``name =`` (Kotlin) or ``label:`` (Swift) from an argument."""
    if len(arg) >= 2 and arg[0].kind == "ident" and arg[1].kind == "op" and arg[1].text in ("=", ":"):
        return arg[2:]
    return arg


def _expression(toks: list[Token], start: int, stop_at_colon: bool = False) -> list[Token]:
    out, depth = [], 0
    for i in range(start, len(toks)):
        t = toks[i]
        if t.kind == "op" and len(t.text) == 1 and t.text in "([{":
            depth += 1
        elif t.kind == "op" and len(t.text) == 1 and t.text in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and t.kind == "op" and t.text in (";", ","):
            break
        elif depth == 0 and t.nl_before and out:
            prev = out[-1]
            if not (prev.kind == "op" and prev.text in _CONTINUES_AFTER) and not (t.kind == "op" and t.text in _CONTINUES_BEFORE):
                break
        out.append(t)
    return out


def _top_strings(expr: list[Token]) -> list[Token]:
    out, depth = [], 0
    for t in expr:
        if t.kind == "op" and len(t.text) == 1 and t.text in "([{":
            depth += 1
        elif t.kind == "op" and len(t.text) == 1 and t.text in ")]}":
            depth -= 1
        elif t.kind == "string" and depth == 0:
            out.append(t)
    return out


@dataclass
class _Scanner:
    path: str
    lang: Lang
    sinks: list[Sink]
    result: ScanResult = field(default_factory=ScanResult)
    seen: set[tuple[int, int, str]] = field(default_factory=set)

    def _report(self, tok: Token, value: str, parts: tuple[str, ...], sink: Sink) -> None:
        lit = Literal(value, parts, tok.line, tok.col)
        if (tok.line, tok.col, value) in self.seen or not is_user_text(lit):
            return
        self.seen.add((tok.line, tok.col, value))
        fix = sink.fix or self.lang.fix
        self.result.findings.append(
            Finding(self.path, tok.line, tok.col, f"user-visible literal {quote(value)} {sink.shown}; use {fix}", literal=value)
        )

    def _check_expr(self, expr: list[Token], sink: Sink) -> None:
        strings = _top_strings(_strip_label(expr) if sink.mode == "call" else expr)
        if sink.html:
            self._check_markup(expr, strings, sink)
            return
        for s in strings:
            self._report(s, s.value, s.parts, sink)

    def _check_markup(self, expr: list[Token], strings: list[Token], sink: Sink) -> None:
        # "<b>" + t("x") + "</b>" must not be a finding while "<b>Ready</b>"
        # must be: join the literal parts with a marker where code goes, read
        # the result as HTML, and test only the text between the tags.
        if not strings:
            return
        joined, last_was_code = [], False
        for t in expr:
            if t.kind == "string":
                joined.append("\x00".join(t.parts))
                last_was_code = False
            elif not last_was_code and not (t.kind == "op" and t.text == "+"):
                joined.append("\x00")
                last_was_code = True
        for text, _where, _fix in scan_html.text_runs("".join(joined)):
            self._report(strings[0], text.replace("\x00", "{…}"), tuple(text.split("\x00")), sink)

    def scan(self, toks: list[Token]) -> None:
        for i, t in enumerate(toks):
            if t.kind == "string" and t.inner:
                for inner in t.inner:
                    self.scan(inner)
            self._catalog_use(toks, i)
            for sink in self.sinks:
                if not _match_at(toks, i, sink.pattern):
                    continue
                if i > 0 and toks[i - 1].kind == "ident" and toks[i - 1].text in _DECLARATION_WORDS:
                    continue  # "val title = …" declares a variable; it shows nothing
                after = i + len(sink.pattern)
                if sink.mode == "call":
                    args, _ = _split_args(toks, after)
                    if sink.attr_filter is not None:
                        if len(args) < 2 or not (len(args[0]) == 1 and args[0][0].kind == "string" and args[0][0].value in sink.attr_filter):
                            continue
                        args = args[1:]
                    for arg in args:
                        self._check_expr(arg, sink)
                else:
                    self._check_expr(_expression(toks, after), sink)

    def _catalog_use(self, toks: list[Token], i: int) -> None:
        t = toks[i]
        if t.kind != "ident":
            return
        nxt = toks[i + 1 : i + 6]
        texts = [x.text for x in nxt]
        if self.lang is KOTLIN and t.text == "R" and len(nxt) >= 4 and texts[0] == "." and texts[1] in ("string", "plurals") and texts[2] == "." and nxt[3].kind == "ident":
            # Android keys carry their resource type ("string/tune"): R.string
            # and R.plurals are separate namespaces with separate catalog tags.
            kind = texts[1]
            self.result.uses.append(CatalogUse("android", f"{kind}/{nxt[3].text}", self.path, nxt[3].line, nxt[3].col, kind == "plurals"))
        elif self.lang is JAVASCRIPT and t.text == "t" and len(nxt) >= 2 and texts[0] == "(" and nxt[1].kind == "string" and not nxt[1].inner:
            if i == 0 or toks[i - 1].text != "function":
                self.result.uses.append(CatalogUse("web", nxt[1].value, self.path, nxt[1].line, nxt[1].col))
        elif self.lang is SWIFT:
            key = None
            if t.text == "String" and texts[:3] == ["(", "localized", ":"] and len(nxt) >= 4 and nxt[3].kind == "string":
                key = nxt[3]
            elif t.text in ("Text", "NSLocalizedString", "LocalizedStringKey") and texts[:1] == ["("] and len(nxt) >= 2 and nxt[1].kind == "string":
                # SwiftUI's Text("…") is a catalog key (§2's Apple row) that
                # Xcode extracts; one with interpolation becomes a format key
                # ("%lld stations") we do not reconstruct, so it is skipped.
                key = nxt[1]
            if key is not None and not key.inner:
                self.result.uses.append(CatalogUse("apple", key.value, self.path, key.line, key.col))


def sinks_for(lang: Lang, extra: list[str] = ()) -> list[Sink]:
    """Built-in sinks plus a repo's own; ValueError names a malformed one."""
    return builtin_sinks(lang) + [make_sink(s, lang) for s in extra]


def scan(path: str, source: str, lang: Lang, sinks: list[Sink] | None = None) -> ScanResult:
    sc = _Scanner(path, lang, sinks if sinks is not None else sinks_for(lang))
    try:
        toks = tokenize(source, lang)
    except TokenizeError as err:
        sc.result.findings.append(
            Finding(path, err.line, err.col, f"could not read this file ({err}), so its strings were not checked")
        )
        return sc.result
    sc.scan(toks)
    return sc.result
