"""Find my radio: which radios are on this computer's serial ports, and
which sound devices are theirs - read-only, bounded, and saying why.

Asked for by Brian on 20/10/2026 for Packet Radio 0.3.0 ("detect, then
confirm"): the setup page should find the radio and fill every field,
rather than ask an operator to know that an IC-9700 is a CP210x on COM4 at
115200 and that its sound device is the second "USB Audio CODEC". Ray
(GM0OTB)'s PC is the case it was written against (HANDOVER, 12/10 and
20/10/2026): the IC-9700 on a CP210x COM4 with "IC9700 Microphone" and
"IC9700 Speakers (USB Audio CODEC)" (he had renamed them), and other
radios' CP210x/CP2105 bridges and a "2- USB AUDIO CODEC" beside it.

**What is sent, and to what.** Every port pyserial lists may be anything
- a GPS, a rotator, a console - so only reads, as icom/usbprobe.py
argues: CI-V `19 00` ("who are you", seven bytes, no line terminator, so
a line-reading device never sees a command) to each factory address in
icom/models.py at 115200, 19200 and 9600 baud, then `03` (the
frequency) once to the address that answered, so the card says
something a person recognises. A radio does not answer CI-V's broadcast
address (measured on the IC-7610: usbprobe.py), hence the sweep. Where no
Icom answers, Yaesu's `ID;` at each Yaesu model's CAT speed: an answer
names the model by its ID (the FTX-1 says 0840). Where neither answers,
a Kenwood PC port (the TM-V71's, kenwood/), at each speed its menu offers.

**A Kenwood is asked last, and asked "TY" before "ID".** Its commands end
in a carriage return, so unlike CI-V and Yaesu's ID; a line-reading device
does see them - and "ID" is a command on a TNC-2-style TNC in command mode
that sends an identification packet, which keys its radio. So: Ctrl-X
first (a TNC-2's cancel-line, ending whatever the earlier probes left in a
line buffer without running it), then `TY` and `FV 0`, which no TNC
command matches; only a port that answers one of those like a Kenwood is
asked `ID`. A port opened for this is never left mid-line. A port that will not
open because something else has it is skipped and said ("in use") - it
is never taken from the program using it.

**Bounded.** Ports are asked in parallel, each with its own deadline,
and the whole scan has one (`deadline_s`); an unanswered question costs
ASK_S. A radio that answers at 9600 is still reported, with the advice to
set 115200 (the app's guidance).

**Sound devices** (`match_audio`): a device whose name has the model in
it wins (Ray's "IC9700 Speakers"); otherwise the CODEC on the same USB
hub as the radio's serial port (usbplace.radio_codec, Linux and Windows);
otherwise none, and the app asks - two radios' CODECs are identical by
name and guessing has already played a tone into the wrong radio once
(usbplace.py). A TM-V71 has no sound device of its own: the interface on
its DATA jack is matched the same way, by hub, under the names interfaces
go by (`INTERFACE_WORDS`). Where the interface's own serial port is also
the PC port's (a Digirig with a CAT cable to the radio) the hub is the
same by construction; a separate USB-serial cable on the same hub as the
interface matches too, and one elsewhere leaves the choice to the person.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

from . import usbplace
from .icom import civ
from .icom import models as icom_models
from .icom.usbprobe import IcomCiv
from .serialio import PortBusy, PortDenied, SerialError, SerialPort
from .kenwood import cat as kenwood_cat
from .kenwood import models as kenwood_models
from .yaesu import models as yaesu_models

log = logging.getLogger("shack_rig.findradio")

#: Fastest first: the app wants 115200 (the scope fits nowhere slower, and
#: Icom's USB CI-V defaults to "Auto", which follows the computer).
ICOM_BAUDS = (115200, 19200, 9600)
ASK_S = 0.25
DEADLINE_S = 20.0
#: USB bridges a radio sits behind (Silicon Labs CP210x, FTDI, Prolific,
#: WCH): such ports are asked first. Others are still asked.
RADIO_BRIDGES = {0x10C4, 0x0403, 0x067B, 0x1A86}


@dataclass
class Found:
    """One radio found. `kind` is the model key ("ic-9700", "ftx-1"...)."""
    kind: str
    name: str
    port: str = ""
    baud: int = 0
    civ_address: int | None = None
    frequency_hz: int | None = None
    #: The radio's sound devices, and how they were matched: "name" (the
    #: model is in the device's name), "usb" (the same USB hub), or "" (not
    #: known: ask).
    audio_in: str = ""
    audio_out: str = ""
    audio_how: str = ""
    evidence: str = ""

    def public(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class PortNote:
    """What happened on one port, for the transcript."""
    port: str
    description: str = ""
    outcome: str = ""            # "radio", "silent", "in use", "denied", "error", "skipped"
    detail: str = ""


@dataclass
class Scan:
    found: list[Found] = field(default_factory=list)
    ports: list[PortNote] = field(default_factory=list)
    seconds: float = 0.0


def list_ports() -> list[dict]:
    """Every serial port pyserial can see: device, description, vid, pid,
    location (the USB path, where the system gives one)."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    out = []
    for p in lp.comports():
        out.append({"device": p.device, "description": p.description or "", "vid": p.vid, "pid": p.pid,
                    "location": getattr(p, "location", None) or "", "hwid": p.hwid or ""})
    return out


def _rank(p: dict) -> tuple:
    return (0 if p.get("vid") in RADIO_BRIDGES else 1, p["device"])


def scan(ports: list[dict] | None = None, *, deadline_s: float = DEADLINE_S, opener=SerialPort,
         ask_s: float = ASK_S, cancel: threading.Event | None = None) -> Scan:
    """Ask every port (module docstring); the radios found and a note per port."""
    ports = sorted(list_ports() if ports is None else ports, key=_rank)
    result = Scan()
    lock = threading.Lock()
    started = time.monotonic()
    end = started + deadline_s

    def one(p):
        note = PortNote(p["device"], p.get("description", ""))
        try:
            found = _ask_port(p, opener, end, ask_s, cancel, note)
        except Exception as e:                      # noqa: BLE001 - one odd port never ends the scan
            log.warning("find my radio: %s: %s", p["device"], e)
            note.outcome, note.detail, found = "error", str(e), None
        with lock:
            result.ports.append(note)
            if found is not None:
                result.found.append(found)
    threads = [threading.Thread(target=one, args=(p,), name=f"findradio-{p['device']}", daemon=True)
               for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, end - time.monotonic()) + 1.0)
    result.found.sort(key=lambda f: f.port)
    result.ports.sort(key=lambda n: n.port)
    result.seconds = round(time.monotonic() - started, 2)
    return result


def _ask_port(p: dict, opener, end: float, ask_s: float, cancel, note: PortNote) -> Found | None:
    device = p["device"]
    tried = []
    for baud in ICOM_BAUDS:
        if time.monotonic() > end or (cancel is not None and cancel.is_set()):
            note.outcome = note.outcome or "skipped"
            note.detail = note.detail or "out of time"
            return None
        port = opener(device, baud)
        try:
            port.open()
        except PortBusy:
            note.outcome, note.detail = "in use", "another program has it open; not touched"
            return None
        except PortDenied as e:
            note.outcome, note.detail = "denied", str(e)
            return None
        except SerialError as e:
            note.outcome, note.detail = "error", str(e)
            return None
        try:
            found = _ask_icom(port, baud, ask_s, end)
            tried.append(f"CI-V at {baud}")
        finally:
            port.close()
        if found is not None:
            found.port = device
            note.outcome, note.detail = "radio", found.evidence
            return found
    for m in yaesu_models.MODELS.values():
        if time.monotonic() > end:
            break
        port = opener(device, m.cat_baud, stop_bits=m.cat_stop_bits) if m.cat_stop_bits != 1 \
            else opener(device, m.cat_baud)
        try:
            port.open()
        except SerialError:
            continue
        try:
            got = port.exchange(b"ID;", ask_s)
            tried.append(f"Yaesu ID at {m.cat_baud}")
        finally:
            port.close()
        found = _read_yaesu(got, m.cat_baud)
        if found is not None:
            found.port = device
            note.outcome, note.detail = "radio", found.evidence
            return found
    for baud in _kenwood_bauds():
        if time.monotonic() > end or (cancel is not None and cancel.is_set()):
            break
        port = opener(device, baud)
        try:
            port.open()
        except SerialError:
            break
        try:
            found = _ask_kenwood(port, baud, ask_s)
            tried.append(f"Kenwood TY at {baud}")
        finally:
            port.close()
        if found is not None:
            found.port = device
            note.outcome, note.detail = "radio", found.evidence
            return found
    note.outcome, note.detail = "silent", "no answer to " + ", ".join(tried)
    return None


def _kenwood_bauds() -> list[int]:
    """Each Kenwood model's factory PC speed first, then its menu's others."""
    out = []
    for m in kenwood_models.MODELS.values():
        for b in (m.pc_baud, *m.pc_bauds):
            if b not in out:
                out.append(b)
    return out


#: Ctrl-X then CR: a TNC-2's CANLINE (its factory $18) clears the line the
#: earlier probes may have left; the CR then ends an empty one.
KENWOOD_CLEAR = b"\x18\r"


def _ask_kenwood(port, baud: int, ask_s: float) -> Found | None:
    """The module docstring's order: clear, TY or FV 0, and only then ID."""
    port.exchange(KENWOOD_CLEAR, ask_s / 2)
    kenwood = None
    for probe, name in ((b"TY\r", "TY"), (b"FV 0\r", "FV")):
        got = port.exchange(probe, ask_s).decode("ascii", "replace")
        if re.search(rf"(^|\r){name} \S", got):
            kenwood = name
            break
    if kenwood is None:
        return None
    got = port.exchange(b"ID\r", ask_s).decode("ascii", "replace")
    m = re.search(r"(?:^|\r)ID (\S+)\r", got)
    if not m:
        return Found(kind="kenwood", name="Kenwood", baud=baud,
                     evidence=f"answered Kenwood {kenwood} at {baud} baud but not ID")
    rid = m.group(1)
    model = next((k for k in kenwood_models.MODELS.values() if k.pc_id == rid), None)
    kind, name = (model.key, model.name) if model else ("kenwood", f"Kenwood {rid}")
    hz = None
    fo = port.exchange(kenwood_cat.read_vfo(0).encode(), ask_s).decode("ascii", "replace")
    for line in kenwood_cat.split_answers(fo)[0]:
        try:
            hz = kenwood_cat.parse_vfo(line).freq
        except kenwood_cat.CatError:
            pass
    evidence = f"answered Kenwood ID with {rid} at {baud} baud"
    if hz:
        evidence += f"; band A on {hz / 1e6:.4f} MHz"
    return Found(kind=kind, name=name, baud=baud, frequency_hz=hz, evidence=evidence)


def _addresses() -> list[int]:
    out = []
    for m in icom_models.MODELS.values():
        if m.civ_address not in out:
            out.append(m.civ_address)
    return out


def _ask_icom(port, baud: int, ask_s: float, end: float) -> Found | None:
    for addr in _addresses():
        if time.monotonic() > end:
            return None
        got = port.exchange(civ.frame(bytes([0x19, 0x00]), to=addr, frm=civ.DEFAULT_CTRL_ADDR), ask_s)
        hit = IcomCiv._read(got, addr)
        if hit is None:
            continue
        frm = hit[0]
        model = next((m for m in icom_models.MODELS.values() if m.civ_address == frm), None)
        kind, name = (model.key, model.name) if model else ("icom", f"Icom (CI-V 0x{frm:02X})")
        hz = None
        again = port.exchange(civ.frame(civ.read_freq(), to=frm, frm=civ.DEFAULT_CTRL_ADDR), ask_s)
        frames, _ = civ.split_frames(again)
        for f in frames:
            try:
                to, src, payload = civ.parse(f)
            except civ.CivError:
                continue
            if src == frm and to == civ.DEFAULT_CTRL_ADDR and payload[:1] in (b"\x03", b"\x00") \
                    and len(payload) >= 6:
                try:
                    hz = civ.decode_freq(payload[1:6])
                except civ.CivError:
                    pass
        evidence = f"answered CI-V \"who are you\" at address 0x{frm:02X}, {baud} baud"
        if hz:
            evidence += f"; on {hz / 1e6:.4f} MHz"
        return Found(kind=kind, name=name, baud=baud, civ_address=frm, frequency_hz=hz, evidence=evidence)
    return None


_ID = re.compile(rb"ID(\d{4});")


def _read_yaesu(data: bytes, baud: int) -> Found | None:
    m = _ID.search(data or b"")
    if not m:
        return None
    code = m.group(1).decode()
    model = next((y for y in yaesu_models.MODELS.values() if y.cat_id == code), None)
    kind, name = (model.key, model.name) if model else ("yaesu", f"Yaesu (ID {code})")
    return Found(kind=kind, name=name, baud=baud, evidence=f"answered Yaesu CAT ID; with {code} at {baud} baud")


# -- sound devices ---------------------------------------------------------------------

#: A radio with no sound card of its own (a TM-V71: audio is its DATA jack)
#: is paired with the interface on its serial port's USB hub, by the names
#: interfaces show: a SignaLink's "USB AUDIO CODEC", a Digirig's or any
#: CM108 board's "USB Audio Device" / "USB PnP Sound Device" (C-Media's own
#: product strings), or the maker's name where the system uses it.
INTERFACE_WORDS = ("CODEC", "USB AUDIO DEVICE", "USB PNP SOUND", "DIGIRIG", "SIGNALINK", "AIOC")


def _squash(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def match_audio(found: Found, inputs: list[str], outputs: list[str], *, others: list[Found] = (),
                codec_of=usbplace.radio_codec) -> Found:
    """Fill `found`'s audio_in/out (module docstring): by the model in the
    name, else by USB placement, else leave them empty to ask. A name match
    that also names another radio found is ignored."""
    model = _squash(found.name)
    rivals = {_squash(o.name) for o in others if o is not found} - {model}

    def by_name(names):
        hits = [n for n in names if model and model in _squash(n) and not any(r and r in _squash(n) for r in rivals)]
        return hits[0] if len(hits) == 1 else ""
    a_in, a_out = by_name(inputs), by_name(outputs)
    if a_in and a_out:
        found.audio_in, found.audio_out, found.audio_how = a_in, a_out, "name"
        return found
    if found.port:
        words = INTERFACE_WORDS if found.kind in kenwood_models.MODELS or found.kind == "kenwood" else ("CODEC",)
        codecs_in = [n for n in inputs if any(w in n.upper() for w in words)]
        codecs_out = [n for n in outputs if any(w in n.upper() for w in words)]
        u_in = a_in or (codec_of(found.port, codecs_in) if codecs_in else None) or ""
        u_out = a_out or (codec_of(found.port, codecs_out) if codecs_out else None) or ""
        if u_in and u_out:
            found.audio_in, found.audio_out = u_in, u_out
            found.audio_how = "name" if (a_in and a_out) else "usb"
            return found
    found.audio_in, found.audio_out, found.audio_how = a_in, a_out, ""
    return found
