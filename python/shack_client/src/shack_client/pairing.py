"""Pairing: exchanging a user's password for a device token, and the TV's
six-digit code (CONTROLLER-CONTRACT.md §4.2, §4.3).

The token is the only credential a paired app holds. It appears in the
exchange's answer once; the library writes it to pairing.json (0600)
beside the store and never logs it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .transport import HttpTransport, Offline


class PairingError(Exception):
    """The controller refused: `code` is its machine code (totp_required,
    bad_credentials, account_locked, use_tv_pairing, pair_expired ...),
    `message` its whole sentence, safe to show."""

    def __init__(self, code: str, message: str, status: int = 0, body: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.body = body or {}


@dataclass
class Token:
    token: str
    device_id: str
    user: str
    max_level: str
    controller_id: str
    admin: bool = False


def _refusal(status: int, body: dict) -> PairingError:
    return PairingError(body.get("code") or f"http_{status}",
                        body.get("error") or "The controller refused to pair this device.", status, body)


def exchange_password(transport, username: str, password: str, *, totp: str | None = None,
                      device_name: str, device_kind: str = "desktop", client_id: str, app: str) -> Token:
    """POST /api/shack/token. Raises PairingError (code totp_required means:
    ask for the code and call again) or transport.Offline."""
    body = {"username": username, "password": password, "deviceName": device_name, "deviceKind": device_kind,
            "clientId": client_id, "app": app}
    if totp:
        body["totp"] = totp
    status, ans = transport.request("POST", "api/shack/token", body)
    if status != 201 or not ans.get("token"):
        raise _refusal(status, ans)
    return Token(ans["token"], ans.get("deviceId", ""), ans.get("user", username), ans.get("maxLevel", ""),
                 ans.get("controllerId", ""), bool(ans.get("admin")))


@dataclass
class TvPairing:
    pair_id: str
    pair_secret: str
    code: str
    expires_in: int
    poll_every: float


def tv_start(transport, *, device_name: str, client_id: str, app: str) -> TvPairing:
    """POST /api/shack/pair/start: the code the TV shows."""
    status, ans = transport.request("POST", "api/shack/pair/start", {"deviceName": device_name, "deviceKind": "tv",
                                                                 "clientId": client_id, "app": app})
    if status not in (200, 201) or "pairId" not in ans:
        raise _refusal(status, ans)
    return TvPairing(ans["pairId"], ans["pairSecret"], ans["code"], int(ans.get("expiresIn", 300)),
                     float(ans.get("pollEvery", 2)))


def tv_poll(transport, pairing: TvPairing) -> tuple[str, Token | None]:
    """One poll: ("pending" | "approved" | "denied" | "expired", token)."""
    status, ans = transport.request("POST", "api/shack/pair/poll", {"pairId": pairing.pair_id,
                                                                "pairSecret": pairing.pair_secret})
    if status == 410:
        return "expired", None
    if status != 200:
        raise _refusal(status, ans)
    state = ans.get("state", "pending")
    if state == "approved":
        return state, Token(ans["token"], ans.get("deviceId", ""), ans.get("user", ""), ans.get("maxLevel", ""),
                            ans.get("controllerId", ""))
    return state, None


__all__ = ["HttpTransport", "Offline", "PairingError", "Token", "TvPairing", "exchange_password", "tv_poll", "tv_start"]
