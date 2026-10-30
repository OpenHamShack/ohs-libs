"""Answering a screen request (SCREENS.md §2, §2.3, §8.1, §10, §11), for
any web framework: headers in, (status, headers, body) out.

    req = Req.from_headers(request.headers)          # what the client is
    resp = serve_document(request.headers, build)    # build(req) -> Screen
    write(resp.status, resp.headers, resp.body)

Radio's server is http.server and Icom's aiohttp, so nothing here knows
either: an adapter is the three lines above. Headers may be any mapping
(http.server's Message, aiohttp's CIMultiDict, a dict); names are matched
without regard to case.

The level comes from the platform's proxy (`X-OpenHamShack-Level`,
CONTROLLER-CONTRACT §5.3). Only trust it on a request the app has already
checked carries its proxy secret: that check is the app's, before this.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, NamedTuple

from .build import UI_MAJOR, UI_MINOR, UI_VERSION, Screen

FORMS = ("tv", "phone", "tablet")
LEVELS = ("view", "control", "transmit")


class Unsupported(ValueError):
    """The client draws a ui major this box does not serve (§2.3)."""

    def __init__(self, asked: str):
        super().__init__(f"ui {asked!r} is not served here; this box serves {UI_VERSION}")
        self.asked = asked


def _lower(headers: Mapping | None) -> dict[str, str]:
    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else headers
    return {str(k).lower(): str(v) for k, v in items}


@dataclass(frozen=True)
class Req:
    """What the requesting client is, from its headers (§11): the form and
    window it draws in, the newest ui minor it draws, its language, its
    client id, and the device's effective level for this app."""
    form: str = "tv"
    w: int | None = None
    h: int | None = None
    minor: int = UI_MINOR
    lang: str = "en"
    client: str | None = None
    device: str | None = None
    level: str = "view"
    transmit: str = "none"

    @classmethod
    def from_headers(cls, headers: Mapping | None) -> Req:
        """Raises Unsupported for another ui major. A missing
        `X-OpenHamShack-UI` is read as this package's own minor (a tool or
        a test, not a client: every client sends it), a missing form as a
        TV and a missing level as view, the most restricted reading of
        each, so a request that says nothing gets nothing it should not."""
        h = _lower(headers)
        minor = UI_MINOR
        ui = h.get("x-openhamshack-ui", "").strip()
        if ui:
            m = re.fullmatch(r"(\d+)\.(\d+)", ui)
            if not m or int(m.group(1)) != UI_MAJOR:
                raise Unsupported(ui)
            minor = int(m.group(2))
        form, w, hh = "tv", None, None
        parts = [x.strip() for x in h.get("x-openhamshack-form", "").split(";") if x.strip()]
        if parts and parts[0] in FORMS:
            form = parts[0]
        for x in parts[1:]:
            k, _, v = x.partition("=")
            if k.strip() in ("w", "h") and v.strip().isdigit():
                if k.strip() == "w":
                    w = int(v)
                else:
                    hh = int(v)
        level = h.get("x-openhamshack-level", "view").strip()
        transmit = h.get("x-openhamshack-transmit", "").strip()
        if transmit not in ("none", "digital", "full"):
            # An older controller sends no transmit header: decide by the
            # level alone (CONTROLLER-CONTRACT §5.3).
            transmit = "full" if level == "transmit" else "none"
        device = h.get("x-openhamshack-device", "").strip()
        return cls(form=form, w=w, h=hh, minor=minor, lang=_language(h.get("accept-language", "")),
                   client=h.get("x-openhamshack-client", "").strip() or None,
                   device=None if device in ("", "-") else device,
                   level=level if level in LEVELS else "view", transmit=transmit)


def _language(accept: str) -> str:
    """The first language the client asks for, as a tag (`en-GB`); the app
    chooses what it has from it (I18N.md §1). q-values are honoured."""
    best, best_q = "en", -1.0
    for piece in accept.split(","):
        tag, _, params = piece.strip().partition(";")
        tag = tag.strip()
        if not tag or tag == "*":
            continue
        q = 1.0
        m = re.search(r"q=([0-9.]+)", params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if q > best_q:
            best, best_q = tag, q
    return best


class Response(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


def _json_body(v: Any) -> bytes:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def unsupported_response(asked: str = "") -> Response:
    return Response(406, {"Content-Type": "application/json; charset=utf-8"}, _json_body(
        {"error": f"This box serves ui {UI_VERSION}, not {asked}".strip(), "code": "ui_unsupported",
         "ui": [UI_VERSION]}))


def serve_document(headers: Mapping | None, build: Callable[[Req], Screen | dict]) -> Response:
    """One `GET ui/v2/screen/<id>`: 406 for another major, 304 when the
    client's `If-None-Match` is the document's rev, else 200 with the
    document. `build` gets the Req and returns a Screen (validated and
    downgraded here, by Screen.json()) or a dict already made by one.
    A document that fails validation raises build.UIError: a server error is
    the honest answer, and the app's own tests should have caught it."""
    try:
        req = Req.from_headers(headers)
    except Unsupported as e:
        return unsupported_response(e.asked)
    built = build(req)
    if isinstance(built, Screen):
        doc, lang = built.json(), built.lang or req.lang
    elif isinstance(built, dict) and isinstance(built.get("rev"), str):
        doc, lang = built, req.lang
    else:
        raise TypeError("build must return a Screen, or a document from Screen.json()")
    etag = f'"{doc["rev"]}"'
    out = {"ETag": etag, "Cache-Control": "no-cache",
           "Vary": "Accept-Language, X-OpenHamShack-UI, X-OpenHamShack-Form",
           "Content-Language": lang}
    inm = _lower(headers).get("if-none-match", "")
    if etag in [t.strip() for t in inm.split(",")] or inm.strip() == "*":
        return Response(304, out, b"")
    return Response(200, {"Content-Type": "application/json; charset=utf-8"} | out, _json_body(doc))


def serve_json(value: Any, status: int = 200) -> Response:
    """Any other ui/2 answer (a descriptor, a status or a poll answer)."""
    return Response(status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"},
                    _json_body(value))
