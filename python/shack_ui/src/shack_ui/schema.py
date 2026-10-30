"""spec/schemas/ui2.schema.json from inside the wheel.

    schema()                       -> dict    the schema itself
    schema_errors(value, "op")     -> [str]   jsonschema's complaints, [] if none

The schema checks shape (and the rules that are shape); check.py's rules
check what needs the whole document. An app's tests may run both; the
server itself runs check.py (Screen.json()), which needs nothing outside
the standard library. schema_errors needs jsonschema (the `test` extra)
and says so if it is missing, rather than passing silently.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources


@cache
def schema() -> dict:
    return json.loads(resources.files("shack_ui").joinpath("data", "schemas", "ui2.schema.json")
                      .read_text(encoding="utf-8"))


def schema_errors(value, ref: str | None = None) -> list[str]:
    """Check `value` against the schema, or one of its $defs (`ref`: "op",
    "frame", "pollAnswer", "descriptor", "statusAnswer", "symbolSet", ...).
    Each error as "<json path>: <message>", in jsonschema's order."""
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError as e:            # pragma: no cover - depends on the environment
        raise ImportError("schema_errors needs jsonschema: install shack-ui[test]") from e
    s = schema()
    target = s if ref is None else {"$ref": f"{s['$id']}#/$defs/{ref}"}
    v = jsonschema.Draft202012Validator(target, registry=Registry().with_resource(s["$id"], Resource.from_contents(s)))
    return [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(value)]
