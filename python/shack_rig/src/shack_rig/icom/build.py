"""Build an IcomRig from a radio profile: the one place a rig is constructed.

Moved from the part of OpenHamShack Icom's `app/registry.py` that turned a
stored profile into a `Radio` (`Radios._build` and `_usb_link`). The rest of
that file - the profile list, which radio is selected, the digital-modes
session per radio, attaching dependants - is the app's business and stays
there; this is only the rules about *a radio*, which every app that drives an
Icom needs identically.

The two rules worth reading, kept from the app:

- **What is a property of a radio has no fallback.** The model, the address,
  the user, the password and the CI-V address belong to a radio: a profile
  that does not state them does not get another radio's, and an empty CI-V
  address means the model's own factory value rather than whatever the last
  radio used. (With one slot and one password, replacing the radio and leaving
  the password box empty once handed the new radio the old one's credentials.)
- **The transmit stream is asked for only when the app may transmit at all.**
  `tx_enabled` decides both `allow_ptt` and whether the LAN link asks the
  radio for a transmit audio stream - one rule, not one per transport. It is
  a gate, not permission: keying still needs a lease (`shack_rig.lease`).

A profile is a dict with the Icom app's keys:

    model         ic-9700 | ic-7610 | ic-705 | ic-7600   (default ic-9700)
    transport     lan | usb                              (default lan)
    host, port, username, client_name                    LAN
    civ_device, civ_baud, audio_device                   USB; blank = probe
    audio_backend alsa | portaudio                       USB receive audio (default alsa)
    tx_audio      "" | alsa | portaudio                  USB transmit audio (default none;
                                                         only with tx_enabled)
    tx_audio_device                                      USB, portaudio: the output's name
    civ_address   "0x98" or blank for the model's default
    audio_rate    LAN: the rate to ask for (0 = no audio); USB: non-zero = audio on
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Callable

from ..usbaudio import DECODE_RATE
from . import models as radio_models
from .link import LanLink
from .rig import IcomRig
from .usb import UsbLink

log = logging.getLogger("shack_rig.icom")

TRANSPORTS = ("lan", "usb")
DEFAULT_PORT = 50001
DEFAULT_AUDIO_RATE = 12000


def resolve_model(key: str | None) -> radio_models.IcomModel:
    """The profile's model, or the default with an error logged.

    A typo in a stored profile falls back rather than refusing to build, so
    the page still has a radio to show the operator the mistake on.
    """
    try:
        return radio_models.model(str(key or ""))
    except ValueError as e:
        log.error("%s; falling back to %s", e, radio_models.DEFAULT_MODEL)
        return radio_models.model(radio_models.DEFAULT_MODEL)


def civ_address(raw, model: radio_models.IcomModel) -> int | None:
    """An explicit CI-V address, or None to follow the model's default.

    Left as None rather than filled in with the model's value, because a
    filled-in default is a value that wins when the model later changes.
    """
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        log.warning("CI-V address %r is not a number; using the %s default",
                    raw, model.name)
        return None


def make_rig(profile: dict, *, password: str = "", tx_enabled: bool = False,
             poll_interval: float = 0.25, scope: bool = True,
             client_name: str = "OpenHamShack",
             usb_cache: Path | str | None = None,
             busy_ports: Callable[[], tuple[str, ...]] | None = None) -> IcomRig:
    """An IcomRig for this profile, not yet started.

    `password` is passed apart from the profile because the app keeps it apart
    (a 0600 settings file, never the page payload). `usb_cache` is where the
    USB probe remembers what it found; `busy_ports` answers which serial ports
    other rigs in the app hold, evaluated when the probe runs - a port another
    rig holds must never be opened to see what is on it, because two openers
    of one tty each get a random half of the bytes and neither is told.
    """
    model = resolve_model(profile.get("model"))
    radio_addr = civ_address(profile.get("civ_address"), model)
    raw_rate = profile.get("audio_rate")
    audio_rate = int(raw_rate if raw_rate not in (None, "") else DEFAULT_AUDIO_RATE)

    transport = str(profile.get("transport") or "lan").strip().lower()
    if transport not in TRANSPORTS:
        log.warning("profile asks for transport %r, which this library has "
                    "not got; using the LAN", transport)
        transport = "lan"

    if transport == "usb":
        device = str(profile.get("civ_device") or "").strip()
        make_link = functools.partial(
            UsbLink,
            device=device,
            baud=int(profile.get("civ_baud") or 0),
            address=radio_addr if radio_addr is not None else model.civ_address,
            model_key=model.key,
            cache_path=usb_cache,
            busy_ports=busy_ports,
            audio_device=str(profile.get("audio_device") or "").strip(),
            audio=bool(audio_rate),
            audio_backend=str(profile.get("audio_backend") or "alsa").strip().lower(),
            # The transmit sink follows the same rule as the LAN's transmit
            # stream: asked for only when the app may transmit at all.
            tx_audio=str(profile.get("tx_audio") or "").strip().lower() if tx_enabled else "",
            tx_audio_device=str(profile.get("tx_audio_device") or "").strip(),
        )
        where = device
    else:
        host = str(profile.get("host") or "")
        port = int(profile.get("port") or DEFAULT_PORT)
        make_link = functools.partial(
            LanLink,
            host=host,
            port=port,
            username=str(profile.get("username") or ""),
            password=str(password or ""),
            comp_name=str(profile.get("client_name") or client_name),
            audio_rate=audio_rate,
            tx_rate=audio_rate if tx_enabled else 0,
        )
        where = f"{host}:{port}" if host else ""

    return IcomRig(
        make_link=make_link,
        where=where,
        model=model,
        radio_addr=radio_addr,
        poll_interval=max(0.1, poll_interval),
        allow_ptt=tx_enabled,
        # On the LAN the radio is *told* what rate to send and this is it. On
        # USB the rate is not a choice - the codec has one capture rate, and
        # asking ALSA for another put every decoded frequency 2.1% out (see
        # usbaudio.py) - so the profile's number means only whether audio is
        # wanted, and what arrives is always 12 kHz.
        audio_rate=(audio_rate if transport == "lan"
                    else (DECODE_RATE if audio_rate else 0)),
        scope=scope,
    )
