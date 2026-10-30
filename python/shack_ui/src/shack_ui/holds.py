"""Holds whose stop needs what their start returned (SCREENS.md §6).

A hold's `stop` is a call the client makes with only `$client` to fill in
(§5.2: nothing else is substituted), so a start that answers with an id
(Radio's `POST api/decoders/sessions` answers the new session's id, and
`DELETE api/decoders/sessions/{id}` stops it) has no way into the stop.
Rather than add a placeholder every renderer must implement, the box
keeps the id: the screen's hold calls a route of the app's that this
class answers, keyed by hold name and client.

    holds = Holds()
    holds.define("dmr", start=lambda client, req: radio.start_session("dmr", client)["id"],
                        stop=lambda sid: radio.stop_session(sid))
    s.hold("dmr", *holds.actions("dmr"))          # POST / DELETE ui/v2/hold/dmr

    # the app's route for ui/v2/hold/<name>, any framework:
    resp = holds.answer(method, name, client, req)    # a serve.Response

    # presence says a client has gone (its 30 s expiry): stop what it held
    holds.gone(client)

A start repeated while the hold is held (the screen came back to the
front, the socket reconnected) returns the id it has rather than opening
a second session. A refusal is `raise HoldRefused(409, sentence, code,
actions=[...])` in `start`, which answer() sends as the contract's error
with its remedy actions (§5.3), for the hold banner.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable

from .build import A
from .i18n import _
from .serve import Response

ROUTE = "ui/v2/hold"
_CLIENT = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class HoldRefused(Exception):
    """A start the box refuses: `status` and the contract's error body
    (CONTROLLER-CONTRACT §9), with remedy `actions` for the banner."""

    def __init__(self, status: int, error: str, code: str, actions: list[dict] | None = None):
        super().__init__(error)
        self.status, self.error, self.code, self.actions = status, error, code, actions


def _json(status: int, body: Any) -> Response:
    return Response(status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
                    json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class Holds:
    def __init__(self, route: str = ROUTE):
        self.route = route.strip("/")
        self._lock = threading.Lock()
        self._defs: dict[str, tuple[Callable, Callable]] = {}
        self._ids: dict[tuple[str, str], Any] = {}

    def define(self, name: str, start: Callable[[str, Any], Any], stop: Callable[[Any], None]) -> None:
        """`start(client, req)` opens what the hold keeps and returns its id
        (anything); `stop(id)` closes it. Both are the app's own code."""
        self._defs[name] = (start, stop)

    def actions(self, name: str, *, level: str = "control", timeout_s: float = 30) -> tuple[dict, dict]:
        """The (start, stop) actions for Screen.hold(name, ...)."""
        path = f"{self.route}/{name}"
        return (A.post(path, {"client": A.CLIENT}, level=level, timeout_s=timeout_s),
                A.delete(f"{path}?client={{$client}}", level=level))

    def start(self, name: str, client: str, req: Any = None) -> Any:
        start, _ = self._defs[name]
        with self._lock:
            if (name, client) in self._ids:
                return self._ids[(name, client)]
        hid = start(client, req)            # outside the lock: it may take seconds
        with self._lock:
            if (name, client) in self._ids:  # a second start raced this one: keep the first
                extra, hid = hid, self._ids[(name, client)]
            else:
                self._ids[(name, client)] = hid
                return hid
        self._defs[name][1](extra)
        return hid

    def stop(self, name: str, client: str) -> bool:
        with self._lock:
            hid = self._ids.pop((name, client), None)
        if hid is None:
            return False
        self._defs[name][1](hid)
        return True

    def held(self, name: str, client: str) -> Any:
        with self._lock:
            return self._ids.get((name, client))

    def gone(self, client: str) -> list[str]:
        """Stop every hold `client` has (its presence expired or it said
        goodbye); the names stopped."""
        with self._lock:
            names = [n for (n, c) in self._ids if c == client]
        return [n for n in names if self.stop(n, client)]

    def answer(self, method: str, name: str, client: str | None, req: Any = None) -> Response:
        """The app's `ui/v2/hold/<name>` route: POST starts (`client` from
        the body), DELETE stops (`client` from the query)."""
        if name not in self._defs:
            return _json(404, {"error": _("There is no such hold here."), "code": "not_found"})
        if not isinstance(client, str) or not _CLIENT.match(client):
            return _json(400, {"error": _("A hold needs the client's id."), "code": "bad_params"})
        if method == "POST":
            try:
                hid = self.start(name, client, req)
            except HoldRefused as e:
                body = {"error": e.error, "code": e.code}
                if e.actions:
                    body["actions"] = e.actions
                return _json(e.status, body)
            return _json(200, {"held": name, "id": hid})
        if method == "DELETE":
            return _json(200, {"stopped": self.stop(name, client)})
        return _json(405, {"error": _("A hold is started with POST and stopped with DELETE."), "code": "bad_method"})
