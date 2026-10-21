# shack-i18n

OpenHamShack's internationalisation tooling (contract: `openhamshack/docs/contract/I18N.md`).

- `shack-i18n-check [--config .i18n-check.toml] [paths...]`: fails on any
  user-visible string literal outside the catalogs (§3), checks the catalogs
  it finds (§3.2) and enforces the repo's `i18n-allow.txt` (§3.3).
- `shack_i18n.fmt`: frequencies, offsets, numbers and SWR in a locale (§4),
  pinned by `spec/vectors/i18n/format.json`, which the Kotlin, Swift and JS
  ports run too.

Standard library only at runtime; Python 3.11+. GPL-3.0-or-later.

## Using the check in a repo

Add `shack-i18n-check` to the repo's test suite. Optional files at the repo root:

- `.i18n-check.toml`: extra sinks per language, user-facing exception types,
  include/exclude globs, another allow-list path (see `shack_i18n/config.py`).
- `i18n-allow.txt`: `glob | "JSON literal" | reason`, one per line. Entries
  need a reason and must match something, or they are findings themselves.

Exit status: 0 clean, 1 findings, 2 the check could not run.

## Developing

```
pip install -e python/shack_i18n
python -m pytest python/shack_i18n/tests -q
python -m build --wheel python/shack_i18n
```
