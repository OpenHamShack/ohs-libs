"""Python sinks via ``ast`` (I18N.md §3.1, first row of the table)."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from ._core import CatalogUse, Finding, Literal, ScanResult, is_user_text, quote

QT_METHODS = frozenset(
    {
        "setText", "setWindowTitle", "setToolTip", "setStatusTip", "setPlaceholderText", "setTitle",
        "setHeaderLabels", "addItem", "addItems", "insertItem", "setItemText", "addAction", "addMenu",
        "addTab", "setTabText", "showMessage", "setLabelText",
    }
)  # fmt: skip
QT_CONSTRUCTORS = frozenset(
    {"QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox", "QAction", "QMenu", "QToolButton"}
)
QMESSAGEBOX_STATICS = frozenset({"information", "warning", "critical", "question", "about"})

# Calls whose string argument is a catalog key, never a finding, and which
# §3.2 checks against the catalog. Recognised by name only: a module that
# imports gettext as something else is not seen, and that is a reason to
# keep the conventional names rather than a reason to resolve imports.
_GETTEXT_SINGULAR = frozenset({"_", "gettext", "N_", "lazy_gettext"})
_GETTEXT_PLURAL = frozenset({"ngettext"})
_GETTEXT_CONTEXT = frozenset({"pgettext"})
_GETTEXT_CONTEXT_PLURAL = frozenset({"npgettext"})


@dataclass
class PythonConfig:
    sinks: frozenset[str] = frozenset()
    user_facing_exceptions: frozenset[str] = frozenset()


def _final_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _str_const(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


@dataclass
class _Scope:
    in_method: bool = False


@dataclass
class _Visitor(ast.NodeVisitor):
    path: str
    lines: list[str]
    config: PythonConfig
    result: ScanResult = field(default_factory=ScanResult)
    # Assignment targets known to hold a Qt widget, so a finding can say
    # "QLabel.setText" rather than the bare method name, as I18N.md's own
    # example does. Module-wide and by spelling ("self.status"): good enough
    # for a message, and a wrong guess costs nothing but wording.
    widget_types: dict[str, str] = field(default_factory=dict)
    scopes: list[_Scope] = field(default_factory=lambda: [_Scope()])
    seen: set[tuple[int, int]] = field(default_factory=set)

    # -- positions ---------------------------------------------------------
    def _col(self, node: ast.AST) -> int:
        # ast columns are UTF-8 byte offsets; editors and §3's examples count
        # characters, from 1.
        line = self.lines[node.lineno - 1] if 0 < node.lineno <= len(self.lines) else ""
        return len(line.encode("utf-8")[: node.col_offset].decode("utf-8", errors="replace")) + 1

    # -- literal extraction ------------------------------------------------
    def _literals(self, node: ast.AST):
        """Every literal in an argument that would reach the screen as written.

        Descends through concatenation, %-formatting, ``.format`` on a
        literal, conditionals, ``or`` and list/tuple displays, and stops at
        any other call: a catalog call's argument is a key, and an arbitrary
        function's argument is that function's business.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield Literal(node.value, (node.value,), node.lineno, self._col(node))
        elif isinstance(node, ast.JoinedStr):
            parts, shown = [], []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                    shown.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    shown.append("{" + ast.unparse(v.value) + "}")
            yield Literal("".join(shown), tuple(parts), node.lineno, self._col(node))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            yield from self._literals(node.left)
            yield from self._literals(node.right)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            yield from self._literals(node.left)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and _str_const(node.func.value) is not None
        ):
            yield from self._literals(node.func.value)
        elif isinstance(node, ast.IfExp):
            yield from self._literals(node.body)
            yield from self._literals(node.orelse)
        elif isinstance(node, ast.BoolOp):
            for v in node.values:
                yield from self._literals(v)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for e in node.elts:
                yield from self._literals(e)

    def _report(self, args, where: str, fix: str) -> None:
        for arg in args:
            for lit in self._literals(arg):
                if (lit.line, lit.col) in self.seen or not is_user_text(lit):
                    continue
                self.seen.add((lit.line, lit.col))
                self.result.findings.append(
                    Finding(
                        self.path, lit.line, lit.col,
                        f"user-visible literal {quote(lit.value)} {where}; use {fix}",
                        literal=lit.value,
                    )
                )  # fmt: skip

    def _qt_fix(self) -> str:
        return "self.tr()" if self.scopes[-1].in_method else "QCoreApplication.translate()"

    # -- scopes ------------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scopes.append(_Scope(in_method=False))
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(stmt, method=True)
            else:
                self.visit(stmt)
        self.scopes.pop()

    def _function(self, node, method: bool) -> None:
        args = node.args.posonlyargs + node.args.args
        in_method = method and bool(args) and args[0].arg == "self"
        # A nested function inside a method still has ``self`` in reach.
        self.scopes.append(_Scope(in_method=in_method or self.scopes[-1].in_method))
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node, method=False)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    # -- assignments (for messages only) -----------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call):
            cls = _final_name(node.value.func)
            if cls and len(cls) > 1 and cls[0] == "Q" and cls[1].isupper():
                for target in node.targets:
                    self.widget_types[ast.unparse(target)] = cls
        self.generic_visit(node)

    # -- sinks -------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        self._catalog_use(node)
        func = node.func
        name = _final_name(func)
        args = list(node.args) + [k.value for k in node.keywords]
        if isinstance(func, ast.Attribute) and (func.attr in QT_METHODS or func.attr in self.config.sinks):
            if func.attr in QMESSAGEBOX_STATICS and _final_name(func.value) == "QMessageBox":
                pass  # handled below with its own wording
            else:
                owner = self.widget_types.get(ast.unparse(func.value))
                shown = f"{owner}.{func.attr}" if owner else func.attr
                self._report(args, f"passed to {shown}", self._qt_fix())
        if isinstance(func, ast.Attribute) and func.attr in QMESSAGEBOX_STATICS and _final_name(func.value) == "QMessageBox":
            self._report(args, f"passed to QMessageBox.{func.attr}", self._qt_fix())
        elif name in QT_CONSTRUCTORS or (isinstance(func, ast.Name) and name in self.config.sinks):
            self._report(args, f"passed to {name}()", self._qt_fix())
        for arg in args:
            if isinstance(arg, ast.Dict):
                self._error_dict(arg)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        # ``return {"error": ...}`` and ``return {"error": ...}, 400``: the
        # two shapes the family's aiohttp/Flask-style handlers use. A dict
        # built up in a variable first is not followed; one assignment of
        # data flow would be the start of a type checker.
        value = node.value
        candidates = value.elts if isinstance(value, ast.Tuple) else [value]
        for c in candidates:
            if isinstance(c, ast.Dict):
                self._error_dict(c)
        self.generic_visit(node)

    def _error_dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if _str_const(key) == "error":
                self._report([value], 'sent as a JSON "error"', "the server's catalog (_())")

    def visit_Raise(self, node: ast.Raise) -> None:
        exc = node.exc
        if isinstance(exc, ast.Call):
            name = _final_name(exc.func)
            if name in self.config.user_facing_exceptions:
                self._report(list(exc.args), f"raised in {name}()", "the package's catalog (_())")
        self.generic_visit(node)

    # -- catalog uses (§3.2) -----------------------------------------------
    def _catalog_use(self, node: ast.Call) -> None:
        func, args = node.func, node.args
        name = _final_name(func)

        def use(system: str, key_node, plural: bool = False, context: str | None = None) -> None:
            key = _str_const(key_node)
            if key is not None:
                self.result.uses.append(
                    CatalogUse(system, key, self.path, key_node.lineno, self._col(key_node), plural, context)
                )

        if isinstance(func, ast.Attribute) and name == "tr" and args:
            plural = len(args) >= 3 or any(k.arg == "n" for k in node.keywords)
            use("qt", args[0], plural)
        elif isinstance(func, ast.Attribute) and name == "translate" and len(args) >= 2:
            plural = len(args) >= 4 or any(k.arg == "n" for k in node.keywords)
            use("qt", args[1], plural, _str_const(args[0]))
        elif name in _GETTEXT_SINGULAR and args:
            use("gettext", args[0])
        elif name in _GETTEXT_PLURAL and args:
            use("gettext", args[0], plural=True)
        elif name in _GETTEXT_CONTEXT and len(args) >= 2:
            use("gettext", args[1], context=_str_const(args[0]))
        elif name in _GETTEXT_CONTEXT_PLURAL and len(args) >= 2:
            use("gettext", args[1], plural=True, context=_str_const(args[0]))


def scan(path: str, source: str, config: PythonConfig) -> ScanResult:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as err:
        # A file the check cannot read is a finding, not a pass: silently
        # skipping it would let any literal through by breaking the parse.
        res = ScanResult()
        res.findings.append(
            Finding(path, err.lineno or 1, err.offset or 1, f"could not parse this Python file ({err.msg}), so its strings were not checked")
        )
        return res
    v = _Visitor(path, source.splitlines(), config)
    v.visit(tree)
    return v.result
