"""``shack-i18n-check``: fail on user-visible literals outside the catalogs.

I18N.md §3 is the specification; this module finds the files, hands each to
its language's scanner, and prints one line per finding. Exit status 0 when
clean, 1 when anything was found, 2 when the check could not run (bad
arguments or configuration), so a CI log can tell "your code has strings in
it" from "the check is broken".
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import allow, catalogs, scan_android_xml, scan_clike, scan_html, scan_python
from ._core import CatalogUse, Finding
from ._glob import match
from .config import DEFAULT_NAME, Config, ConfigError, load

# Directories nobody means when they say "check this repo": VCS metadata,
# dependencies and build output. Excluding them here rather than in each
# repo's config keeps a fresh repo's first run meaningful.
_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", "build", "dist",
        ".gradle", ".idea", "Pods", "DerivedData", ".mypy_cache", ".pytest_cache", "site-packages",
    }
)  # fmt: skip

EXIT_CLEAN, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


def classify(rel: str) -> str | None:
    """Which scanner a repo-relative path belongs to, if any."""
    name = rel.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    parts = rel.split("/")
    if suffix == "ts":
        return "cat_qt"  # or TypeScript; catalogs.parse_qt tells them apart
    if suffix in ("po", "pot"):
        return "cat_po"
    if suffix == "xcstrings":
        return "cat_xcstrings"
    if suffix == "json" and len(parts) >= 3 and parts[-3] == "static" and parts[-2] == "i18n":
        return "cat_web"
    if name == "strings.xml" and len(parts) >= 3 and parts[-3] == "res" and parts[-2].startswith("values"):
        return "cat_android"
    if suffix == "py":
        return "python"
    if suffix in ("kt", "kts"):
        return "kotlin"
    if suffix == "swift":
        return "swift"
    if suffix in ("js", "mjs", "cjs") and not name.endswith(".min.js"):
        # Minified bundles are build output; their source is what gets checked.
        return "javascript"
    if suffix in ("html", "htm"):
        return "html"
    if suffix == "xml" and len(parts) >= 3 and parts[-3] == "res" and parts[-2].split("-")[0] in ("layout", "menu"):
        return "android_xml"
    return None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_FINDINGS if self.findings else EXIT_CLEAN

    def lines(self) -> list[str]:
        key = lambda f: (f.path, f.line, f.col, f.message)  # noqa: E731
        return [f.render() for f in sorted(self.findings + self.warnings, key=key)]


def find_root(anchor: Path) -> Path:
    """The repo root: nearest directory with a config file, else with .git."""
    anchor = anchor if anchor.is_dir() else anchor.parent
    for marker in (DEFAULT_NAME, ".git"):
        for d in [anchor, *anchor.parents]:
            if (d / marker).exists():
                return d
    return anchor


def _rel(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def _wanted(rel: str, cfg: Config) -> bool:
    if cfg.include and not any(match(p, rel) for p in cfg.include):
        return False
    return not any(match(p, rel) for p in cfg.exclude)


def _walk(target: Path):
    if target.is_file():
        yield target
        return
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for f in sorted(filenames):
            yield Path(dirpath) / f


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _parse_catalog(kind: str, rel: str, text: str) -> list[catalogs.Catalog]:
    if kind == "cat_qt":
        cat = catalogs.parse_qt(rel, text)
    elif kind == "cat_po":
        cat = catalogs.parse_po(rel, text)
    elif kind == "cat_web":
        cat = catalogs.parse_web(rel, text)
    elif kind == "cat_android":
        cat = catalogs.parse_android(rel, text)
    else:
        return catalogs.parse_xcstrings(rel, text)
    return [cat] if cat is not None else []


def run(paths: list[str] | None = None, config: str | None = None, cwd: Path | None = None) -> Report:
    """Run the check and return the report; ``main`` prints it.

    Raises ConfigError when the check cannot run.
    """
    cwd = (cwd or Path.cwd()).resolve()
    targets = [(cwd / p).resolve() for p in (paths or [])]
    if config:
        cfg_path = (cwd / config).resolve()
        if not cfg_path.is_file():
            raise ConfigError(f"The configuration file {config} does not exist.")
        root = cfg_path.parent
        cfg = load(cfg_path)
    else:
        root = find_root(targets[0] if targets else cwd)
        cfg = load(root / DEFAULT_NAME) if (root / DEFAULT_NAME).is_file() else Config()
    for t in targets:
        if not t.exists():
            raise ConfigError(f"The path {_rel(t, cwd)} does not exist.")
    # A whole-repo run can judge every allow-list entry; a partial one only
    # those whose files it saw (allow.apply).
    full_run = not targets or root in targets
    if not targets:
        targets = [root]

    py_cfg = scan_python.PythonConfig(
        sinks=frozenset(cfg.python_sinks), user_facing_exceptions=frozenset(cfg.user_facing_exceptions)
    )
    try:
        sinks = {
            lang.name: scan_clike.sinks_for(lang, extra)
            for lang, extra in (
                (scan_clike.KOTLIN, cfg.kotlin_sinks),
                (scan_clike.SWIFT, cfg.swift_sinks),
                (scan_clike.JAVASCRIPT, cfg.javascript_sinks),
            )
        }
    except (ValueError, scan_clike.TokenizeError) as err:
        raise ConfigError(f"{cfg.path}: {err}") from err
    report = Report()
    uses: list[CatalogUse] = []
    found_catalogs: list[catalogs.Catalog] = []
    seen: set[Path] = set()
    scanned: set[str] = set()
    for target in targets:
        for path in _walk(target):
            if path in seen:
                continue
            seen.add(path)
            rel = _rel(path, root)
            kind = classify(rel)
            if kind is None or not _wanted(rel, cfg):
                continue
            scanned.add(rel)
            text = _read(path)
            if text is None:
                report.findings.append(Finding(rel, 1, 1, "this file is not UTF-8, so its strings were not checked"))
                continue
            if kind.startswith("cat_"):
                try:
                    found_catalogs.extend(_parse_catalog(kind, rel, text))
                except ValueError as err:
                    report.findings.append(Finding(rel, 1, 1, f"this catalog could not be read because {err}"))
                continue
            if kind == "python":
                result = scan_python.scan(rel, text, py_cfg)
            elif kind == "kotlin":
                result = scan_clike.scan(rel, text, scan_clike.KOTLIN, sinks["kotlin"])
            elif kind == "swift":
                result = scan_clike.scan(rel, text, scan_clike.SWIFT, sinks["swift"])
            elif kind == "javascript":
                result = scan_clike.scan(rel, text, scan_clike.JAVASCRIPT, sinks["javascript"])
            elif kind == "html":
                result = scan_html.scan(rel, text, tuple(cfg.html_attributes))
            else:
                result = scan_android_xml.scan(rel, text, tuple(cfg.android_xml_attributes))
            report.findings.extend(result.findings)
            uses.extend(result.uses)

    cat_findings, cat_warnings = catalogs.check(found_catalogs, uses, full_run)
    report.findings.extend(cat_findings)
    report.warnings.extend(cat_warnings)

    allow_path = root / cfg.allow_list
    allow_shown = _rel(allow_path, root)
    entries, problems = allow.load(allow_path, allow_shown)
    report.findings = allow.apply(report.findings, entries, allow.technical_terms(), scanned, full_run, allow_shown)
    report.findings.extend(problems)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shack-i18n-check",
        description="Fail on user-visible string literals outside the translation catalogs (OpenHamShack I18N.md section 3).",
    )
    parser.add_argument("--config", help=f"configuration file (default: {DEFAULT_NAME} at the repo root, if present)")
    parser.add_argument("paths", nargs="*", help="files or directories to check (default: the whole repo)")
    args = parser.parse_args(argv)
    try:
        report = run(args.paths, args.config)
    except ConfigError as err:
        print(f"shack-i18n-check: {err}", file=sys.stderr)
        return EXIT_ERROR
    for line in report.lines():
        print(line)
    if report.findings or report.warnings:
        print(f"Findings: {len(report.findings)}. Warnings: {len(report.warnings)}.", file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
