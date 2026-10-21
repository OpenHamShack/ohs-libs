"""``.i18n-check.toml``: per-repo additions to the built-in rules.

Everything is additive. A repo can name more sinks, more user-facing
exception types, include/exclude globs and another allow-list path; it
cannot remove a built-in sink, because a repo that wants a sink gone wants
the check gone for that API, and that belongs in review, not in a setting.

Unknown settings are refused rather than ignored: a misspelt
``user_facing_exception`` would otherwise switch a check off without a word.

    include = ["ohsnb/**"]            # default: every file of a known kind
    exclude = ["**/tests/**"]
    allow_list = "i18n-allow.txt"
    [python]
    sinks = ["setSubtitle", "Banner"]          # method or callable names
    user_facing_exceptions = ["ValueError"]
    [kotlin]
    sinks = ["Banner(", "subtitle ="]          # same spelling as I18N.md's table
    [swift]
    sinks = [".subtitle ="]
    [javascript]
    sinks = ["toast(", "banner("]
    [html]
    attributes = ["data-tip"]
    [android_xml]
    attributes = ["android:title"]
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NAME = ".i18n-check.toml"


class ConfigError(Exception):
    """A configuration the check cannot run with; the message is a sentence."""


@dataclass
class Config:
    path: Path | None = None
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    allow_list: str = "i18n-allow.txt"
    python_sinks: list[str] = field(default_factory=list)
    user_facing_exceptions: list[str] = field(default_factory=list)
    kotlin_sinks: list[str] = field(default_factory=list)
    swift_sinks: list[str] = field(default_factory=list)
    javascript_sinks: list[str] = field(default_factory=list)
    html_attributes: list[str] = field(default_factory=list)
    android_xml_attributes: list[str] = field(default_factory=list)


_TOP = {"include", "exclude", "allow_list"}
_SECTIONS = {
    "python": {"sinks": "python_sinks", "user_facing_exceptions": "user_facing_exceptions"},
    "kotlin": {"sinks": "kotlin_sinks"},
    "swift": {"sinks": "swift_sinks"},
    "javascript": {"sinks": "javascript_sinks"},
    "html": {"attributes": "html_attributes"},
    "android_xml": {"attributes": "android_xml_attributes"},
}


def _strings(value: object, where: str, name: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{name}: {where} must be a list of strings.")
    return list(value)


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise ConfigError(f"Could not read {path}: {err.strerror}.") from err
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path} is not valid TOML: {err}.") from err
    cfg = Config(path=path)
    for key, value in data.items():
        if key in _SECTIONS:
            if not isinstance(value, dict):
                raise ConfigError(f"{path}: [{key}] must be a table.")
            for sub, sub_value in value.items():
                attr = _SECTIONS[key].get(sub)
                if attr is None:
                    known = ", ".join(_SECTIONS[key])
                    raise ConfigError(f'{path}: [{key}] has no setting "{sub}"; the settings there are {known}.')
                setattr(cfg, attr, _strings(sub_value, f"[{key}] {sub}", path))
        elif key in ("include", "exclude"):
            setattr(cfg, key, _strings(value, key, path))
        elif key == "allow_list":
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{path}: allow_list must be a path relative to the repo root.")
            cfg.allow_list = value
        else:
            known = ", ".join(sorted(_TOP | set(_SECTIONS)))
            raise ConfigError(f'{path} has an unknown setting "{key}"; the known ones are {known}.')
    return cfg
