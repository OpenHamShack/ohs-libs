"""What the information field means, once AX.25 has been taken off it.

From the Icom app's APRS receiver (`icom/app/aprs/aprs.py`, 17/10/2026),
checked there against direwolf's `decode_aprs` and here again
(tests/test_direwolf.py). One change: what it says to a person - why a
packet did not parse, a symbol's name, mic-E's message - is now a key the
app translates (PROBLEMS, SYMBOLS, MIC_E_*), not an English sentence, because
a shared library carries no user-visible text (I18N.md).

APRS is a set of formats sharing one field, told apart by the first character.
Most of them are a position; the rest are a status line, a message to another
station, an object somebody has placed on the map, or telemetry. They were
designed in the early nineties to be typed by hand into a TNC, which is why
they are printable ASCII and why so much of the specification is about how many
characters something takes.

**Three things make this harder than a list of formats.**

*Position ambiguity is deliberate.* A station that only knows roughly where it
is sends spaces instead of the last digits of the minutes, and that is not a
fault to be repaired - it is the operator saying "within a minute of arc". So
the uncertainty is carried out of here as a number of metres rather than being
silently rounded away, because the map has to draw the difference between a
GPS fix and a square ten miles across.

*Compressed positions are base 91.* Four printable characters for the latitude
and four for the longitude, which gets a fix into thirteen bytes instead of
nineteen. Nothing about it is hard; it is simply a second format for the same
thing, and forgetting it means missing every modern tracker.

*Mic-E is the awkward one*, and it is worth saying why rather than just
implementing it. It hides half the position in the AX.25 **destination
address** - the field whose job everywhere else is to say what kind of station
sent this - and encodes it in an alphabet where the same digit has three
spellings depending on which of the north/south, longitude offset and east/west
bits it also has to carry. So a mic-E packet cannot be decoded from the
information field alone; it needs the frame around it, which is why `parse`
takes a frame rather than a string. It is also the format most trackers
actually use, so declining to do it would mean an empty map.

**Nothing here raises on bad input.** Every one of these arrived over the air
from somebody this module has never heard of, and a tracker with a firmware bug
transmits a malformed packet every thirty seconds for ever. A field that does
not parse is left out; a packet that makes no sense at all comes back as its
own text with `kind` of "unknown", which is honest and is still worth showing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import PacketError

#: The destination ("tocall") that says which software sent a packet, and
#: the one place it is spelled: everything that sends or synthesises an
#: OpenHamShack frame takes it from here, so the registered one is a
#: one-line change. APZ is the experimental block; a registration
#: (APOHS? or APOHSP) is pending with the APRS tocall registry as of
#: 18/10/2026, and until it is granted every release sends APZOHS.
TOCALL = "APZOHS"

#: The first character of the information field, and what it means.
TYPES = {
    "!": "position", "=": "position", "/": "position", "@": "position",
    ">": "status", ":": "message", ";": "object", ")": "item",
    "`": "mic-e", "'": "mic-e", "\x1c": "mic-e", "\x1d": "mic-e",
    "_": "weather", "T": "telemetry", "$": "nmea", "<": "capabilities",
    "?": "query", "}": "third-party", "{": "user-defined", ",": "test",
}

#: Which types carry messaging capability, which is the only difference
#: between "!" and "=" and between "/" and "@".
MESSAGING = {"=", "@"}

#: A few of the symbols, as keys the app translates into a word next to the
#: marker (the Icom copy had the English words; they are keys here so the
#: library carries no user-visible text).
#: Deliberately partial: the full table is several hundred entries including
#: the alternate overlay set, and an incomplete table that says so is more
#: useful than a complete one nobody maintains. The symbol itself is always
#: carried out untouched, so the page can do better later without the parser
#: changing.
SYMBOLS = {
    "/>": "car", "/k": "truck", "/j": "jeep", "/u": "lorry", "/v": "van",
    "/b": "bicycle", "/<": "motorcycle", "/[": "walker", "/-": "house",
    "/_": "weather-station", "/#": "digipeater", "/&": "gateway",
    "/O": "balloon", "/'": "aircraft", "/^": "aircraft", "/s": "boat",
    "/Y": "yacht", "/r": "repeater", "/I": "tcpip", "/$": "phone",
    "/`": "dish-antenna", "/a": "ambulance", "/f": "fire-engine",
    "/p": "dog", "/=": "train", "/(": "mobile-satellite-station",
    "\\#": "digipeater", "\\&": "gateway", "\\_": "weather-station",
}

#: 1852 metres to a nautical mile: APRS speeds are in knots and courses in
#: degrees true, both straight out of NMEA.
KNOT_KM_H = 1.852

#: Bytes 33 to 123 stand for 0 to 90 in the compressed formats.
_B91_BASE = 33


def _base91(text: str) -> int | None:
    value = 0
    for ch in text:
        digit = ord(ch) - _B91_BASE
        if not 0 <= digit <= 90:
            return None
        value = value * 91 + digit
    return value


@dataclass
class Position:
    lat: float
    lon: float
    #: Half the side of the square the station said it is in, in metres. Zero
    #: for a full six-digit fix; 18520 for a position given to the nearest
    #: degree. The map draws it rather than pretending to a precision the
    #: sender explicitly declined to claim.
    uncertainty_m: float = 0.0
    symbol: str = ""            # table character and code, two characters
    compressed: bool = False


@dataclass
class Packet:
    """One APRS payload, understood as far as it can be."""
    kind: str = "unknown"
    #: The raw information field, always. Everything else here is an opinion
    #: about it; this is what arrived.
    text: str = ""
    messaging: bool = False
    position: Position | None = None
    #: What the sender said the time was. APRS timestamps carry no year and
    #: often no date, so this is the sender's claim rather than a fact - the
    #: moment the packet was heard is kept separately, by the receiver.
    timestamp: str = ""
    time_utc: float | None = None
    comment: str = ""
    status: str = ""
    course_deg: float | None = None
    speed_kmh: float | None = None
    altitude_m: float | None = None
    #: Messaging.
    addressee: str = ""
    message: str = ""
    message_id: str = ""
    ack: str = ""
    #: Objects and items.
    name: str = ""
    live: bool = True
    #: Weather, in the units a person reads: Celsius, millimetres, km/h.
    weather: dict = field(default_factory=dict)
    telemetry: dict = field(default_factory=dict)
    #: Mic-E's three-bit message, a key from MIC_E_STANDARD or MIC_E_CUSTOM.
    mic_e_message: str = ""
    #: Why a packet did not parse further, as a code from PROBLEMS, for the
    #: app to show in its own (translated) words instead of an empty row.
    #: The Icom copy carried English sentences here; a library has no
    #: user-visible text (I18N.md), so the words moved to the apps.
    problem: str = ""
    #: For "exception" only: what was raised, for a bug report, not a person.
    error: str = ""

    @property
    def symbol_name(self) -> str:
        if not self.position or len(self.position.symbol) != 2:
            return ""
        table, code = self.position.symbol
        if self.position.symbol in SYMBOLS:
            return SYMBOLS[self.position.symbol]
        # An overlay: any character other than / or \\ in the table position
        # means the alternate table with that character drawn on top, so the
        # symbol is still the alternate one. This is how every digipeater in
        # the country identifies itself, so it is worth the three lines.
        if table not in "/\\":
            return SYMBOLS.get("\\" + code, "")
        return ""


#: Every code `Packet.problem` can hold, and what it means (for the app's
#: translators, not for a person).
PROBLEMS = {
    "empty": "an empty information field",
    "position": "the position did not parse",
    "timestamp": "a timestamped position with no timestamp",
    "mic-e-short": "a mic-E packet too short to hold a position",
    "mic-e-destination": "the destination is not a mic-E one, so there is no latitude",
    "mic-e-longitude": "a mic-E longitude outside the world",
    "message-addressee": "a message with no addressee",
    "object-short": "an object too short to name anything",
    "object-state": "an object that is neither live nor killed",
    "item-name": "an item with no name",
    "telemetry": "telemetry that is not five channels and eight bits",
    "weather-empty": "a weather report with no readings in it",
    "exception": "the parser raised (see Packet.error): a bug",
}


# -- positions ---------------------------------------------------------------


def _uncompressed(text: str) -> tuple[Position | None, str]:
    """`4903.50N/07201.75W>` and the rest of the line after it.

    Nineteen characters: eight of latitude, the symbol table, nine of
    longitude, the symbol code. Spaces in place of the last digits of the
    minutes are the sender declining to be more precise; each one multiplies
    the uncertainty by ten.
    """
    if len(text) < 19:
        return None, text
    lat_s, table, lon_s, code = text[:8], text[8], text[9:18], text[18]
    rest = text[19:]

    ambiguity = 0
    for i in (6, 5, 3, 2):          # hundredths, tenths, units, tens of minutes
        if lat_s[i] == " ":
            ambiguity += 1
    lat = _degrees_minutes(lat_s, 2, ambiguity, 90.0)
    lon = _degrees_minutes(lon_s, 3, ambiguity, 180.0)
    if lat is None or lon is None:
        return None, text
    if table not in "/\\" and not table.isalnum():
        return None, text
    # A minute of latitude is a nautical mile; each missing digit is a factor
    # of ten on top of that, and the figure quoted is the half-width.
    uncertainty = 0.0 if ambiguity == 0 else 1852.0 * (10.0 ** (ambiguity - 2)) / 2
    return Position(lat=lat, lon=lon, uncertainty_m=uncertainty,
                    symbol=table + code), rest


def _degrees_minutes(text: str, deg_digits: int, ambiguity: int,
                     limit: float) -> float | None:
    """`4903.50N` or `07201.75W` to signed degrees, spaces meaning "about".

    `limit` is checked rather than assumed. Degrees are three characters wide
    for a longitude, so a corrupted byte makes 099 into 999 without changing
    the length or the shape - and a station at 99 degrees north is a marker
    off the top of the map with nothing to say it is wrong.
    """
    want = deg_digits + 5
    if len(text) != want + 1:
        return None
    hemisphere = text[-1].upper()
    if hemisphere not in "NSEW":
        return None
    digits = text[:deg_digits]
    minutes = text[deg_digits:deg_digits + 2]
    if text[deg_digits + 2] != ".":
        return None
    fraction = text[deg_digits + 3:deg_digits + 5]
    # A blanked digit is the middle of its range, not zero: "49  .  N" means
    # somewhere in 49 degrees, and the middle of that is 49.5.
    blanked = (minutes + fraction).count(" ")
    minutes = minutes.replace(" ", "0")
    fraction = fraction.replace(" ", "0")
    if not (digits.isdigit() and minutes.isdigit() and fraction.isdigit()):
        return None
    value = int(digits) + (int(minutes) + int(fraction) / 100.0) / 60.0
    if value > limit:
        return None
    if blanked:
        value += (10.0 ** (blanked - 2)) / 60.0 / 2.0
    if hemisphere in "SW":
        value = -value
    return value


def _compressed(text: str) -> tuple[Position | None, str]:
    """Thirteen characters of base 91, and what follows them."""
    if len(text) < 13:
        return None, text
    table, lat_s, lon_s, code, cs, comp = (
        text[0], text[1:5], text[5:9], text[9], text[10:12], text[12])
    rest = text[13:]
    lat_v = _base91(lat_s)
    lon_v = _base91(lon_s)
    if lat_v is None or lon_v is None:
        return None, text
    lat = 90.0 - lat_v / 380926.0
    lon = -180.0 + lon_v / 190463.0
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None, text
    position = Position(lat=lat, lon=lon, symbol=table + code, compressed=True)
    return position, _compressed_extra(cs, comp, rest)


def _compressed_extra(cs: str, comp: str, rest: str) -> str:
    """The two course/speed characters are handled by the caller through
    `rest`; they are returned prefixed so the comment parser can pick them up
    without a second format."""
    # Kept as part of the comment stream: course, speed and altitude are read
    # out of the comment for the uncompressed form too, so there is one place
    # that knows about them rather than two.
    return _COMPRESSED_CS + cs + comp + rest


#: A marker that cannot occur in a comment (it is a control character), used to
#: hand the compressed course/speed bytes to the comment parser without
#: inventing a second return value that every caller would have to thread.
_COMPRESSED_CS = "\x00"


# -- timestamps --------------------------------------------------------------

_DHM = re.compile(r"^(\d{2})(\d{2})(\d{2})([zh/])")
_MDHM = re.compile(r"^(\d{2})(\d{2})(\d{2})(\d{2})$")


def _timestamp(text: str, now: float | None = None):
    """The seven or eight character timestamp, and the rest.

    Returns the text as sent, a UTC epoch if one can be worked out, and what
    follows. APRS timestamps carry no year and the day-hour-minute form carries
    no month, so the missing parts are taken from now - and where that would
    put the packet in the future, it is taken as last month or yesterday
    instead. A tracker whose clock is a minute fast must not produce a position
    dated next month.
    """
    when = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(
        now, timezone.utc)
    match = _DHM.match(text)
    if match:
        a, b, c, kind = match.groups()
        raw = text[:7]
        rest = text[7:]
        if kind == "h":
            stamp = _at(when, when.year, when.month, when.day,
                        int(a), int(b), int(c))
            if stamp and stamp.timestamp() > when.timestamp() + 3600:
                stamp = _at(when, when.year, when.month, when.day - 1,
                            int(a), int(b), int(c))
        else:
            # Local time is accepted and then treated as UTC, which is what
            # every other APRS client does: the offset is not in the packet, so
            # there is nothing better available and guessing would be worse.
            stamp = _at(when, when.year, when.month, int(a), int(b), int(c), 0)
            if stamp and stamp.timestamp() > when.timestamp() + 86400:
                month = when.month - 1 or 12
                year = when.year if when.month > 1 else when.year - 1
                stamp = _at(when, year, month, int(a), int(b), int(c), 0)
        return raw, (stamp.timestamp() if stamp else None), rest
    match = _MDHM.match(text[:8])
    if match:
        month, day, hour, minute = (int(g) for g in match.groups())
        stamp = _at(when, when.year, month, day, hour, minute, 0)
        if stamp and stamp.timestamp() > when.timestamp() + 86400:
            stamp = _at(when, when.year - 1, month, day, hour, minute, 0)
        return text[:8], (stamp.timestamp() if stamp else None), text[8:]
    return "", None, text


def _at(reference, year, month, day, hour, minute, second):
    try:
        return datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone.utc)
    except ValueError:
        return None


# -- what follows a position -------------------------------------------------

_COURSE_SPEED = re.compile(r"^(\d{3})/(\d{3})")
_ALTITUDE = re.compile(r"/A=(-?\d{6})")
_RANGE = re.compile(r"^RNG(\d{4})")
#: Power, antenna height, gain and directivity - how a digipeater says what it
#: can reach. Worth reading rather than leaving in the comment because on
#: 144.800 the fixed stations are mostly digipeaters and this is the only thing
#: that distinguishes one on a hill from one in a loft.
_PHG = re.compile(r"^PHG(\d)(\d)(\d)([0-8])")


def _extras(comment: str, packet: Packet) -> str:
    """Course, speed and altitude out of the front of a comment."""
    if comment.startswith(_COMPRESSED_CS):
        # The compressed form's two bytes. `comp` says how they should be read;
        # bits 3 and 4 of it select course/speed, radio range or altitude.
        cs, kind, comment = comment[1:3], comment[3:4], comment[4:]
        if cs and cs[0] != " " and len(cs) == 2 and kind:
            mode = (ord(kind) - 33) & 0x18
            c = ord(cs[0]) - 33
            s = ord(cs[1]) - 33
            if mode == 0x10:                      # altitude, as 1.002^n feet
                packet.altitude_m = round(
                    (1.002 ** (c * 91 + s)) * 0.3048, 1)
            elif 0 <= c <= 89:                    # course and speed
                packet.course_deg = float(c * 4)
                packet.speed_kmh = round((1.08 ** s - 1) * KNOT_KM_H, 1)
        return comment
    match = _COURSE_SPEED.match(comment)
    if match:
        course = int(match.group(1))
        speed = int(match.group(2))
        # 000 in the course field means "not known", not "due north".
        packet.course_deg = float(course) if course else None
        packet.speed_kmh = round(speed * KNOT_KM_H, 1)
        comment = comment[7:]
    else:
        match = _RANGE.match(comment)
        if match:
            packet.telemetry["range_km"] = round(int(match.group(1)) * 1.609, 1)
            comment = comment[7:]
        else:
            match = _PHG.match(comment)
            if match:
                power, height, gain, direction = (int(g) for g in match.groups())
                packet.telemetry.update({
                    "power_w": power * power,
                    # The height digit is 10 feet doubled for each step, which
                    # is above average terrain rather than above the ground.
                    "height_m": round(10 * (2 ** height) * 0.3048, 1),
                    "gain_db": gain,
                    "beam_deg": None if direction == 0 else direction * 45,
                })
                comment = comment[7:]
    match = _ALTITUDE.search(comment)
    if match:
        packet.altitude_m = round(int(match.group(1)) * 0.3048, 1)
        comment = comment[:match.start()] + comment[match.end():]
    return comment


def _position_body(text: str, packet: Packet) -> None:
    """A position, compressed or not, and whatever the sender added after it."""
    if text[:1].isdigit() or text[:1] == " ":
        position, rest = _uncompressed(text)
    else:
        position, rest = _compressed(text)
    if position is None:
        packet.problem = "position"
        packet.comment = text
        return
    packet.position = position
    rest = _extras(rest, packet)
    if position.symbol.endswith("_"):
        # A weather station's symbol changes what the three-digit pair after
        # the position means: it is the wind rather than the way the station
        # is travelling. Same field, same units - course in degrees and speed
        # in knots - but a house does not have a heading, and leaving it
        # labelled "course" is how a wind direction ends up drawn as a track.
        if packet.course_deg is not None or packet.speed_kmh is not None:
            if packet.course_deg is not None:
                packet.weather["wind_dir_deg"] = packet.course_deg
            if packet.speed_kmh is not None:
                packet.weather["wind_kmh"] = packet.speed_kmh
            packet.course_deg = packet.speed_kmh = None
        readings = packet.weather
        rest = _weather(rest, packet)
        packet.weather = {**readings, **packet.weather}
        packet.kind = "weather"
    packet.comment = rest


# -- weather -----------------------------------------------------------------

#: The weather fields, as the specification's letters, with what each is in and
#: how to get to the unit a person here reads. The awkward ones are the
#: temperature, which is Fahrenheit, and the rain, which is hundredths of an
#: inch.
_WX = {
    "c": ("wind_dir_deg", 3, lambda v: float(v)),
    "s": ("wind_kmh", 3, lambda v: round(v * 1.609, 1)),
    "g": ("gust_kmh", 3, lambda v: round(v * 1.609, 1)),
    "t": ("temp_c", 3, lambda v: round((v - 32) * 5 / 9, 1)),
    "r": ("rain_hour_mm", 3, lambda v: round(v * 0.254, 1)),
    "p": ("rain_24h_mm", 3, lambda v: round(v * 0.254, 1)),
    "P": ("rain_midnight_mm", 3, lambda v: round(v * 0.254, 1)),
    "h": ("humidity_pct", 2, lambda v: float(v if v else 100)),
    "b": ("pressure_hpa", 5, lambda v: round(v / 10.0, 1)),
}


def _weather(text: str, packet: Packet) -> str:
    """The weather fields off the front of `text`, and what is left of it.

    The leftover matters: a weather station's comment - which is usually the
    software it runs - comes after the readings, and leaving the readings in
    the comment as well means the window shows `g015t045r000p010P010h50b10132`
    to somebody who asked what the weather is.
    """
    at = 0
    found = {}
    while at < len(text):
        entry = _WX.get(text[at])
        if entry is None:
            break
        name, width, convert = entry
        digits = text[at + 1: at + 1 + width]
        if len(digits) != width:
            break
        at += 1 + width
        if not digits.lstrip("-").isdigit():
            # A field the station has no sensor for is sent as dots or spaces,
            # which is a statement and not a fault: skip it and keep reading.
            continue
        found[name] = convert(int(digits))
    if found:
        packet.weather = found
        packet.kind = "weather"
        return text[at:]
    return text


# -- mic-E -------------------------------------------------------------------

#: The three-bit message, standard set and custom set, in the order the bits
#: make when read as A B C. Keys, for the app to put in words.
MIC_E_STANDARD = ["emergency", "priority", "special", "committed",
                  "returning", "in-service", "en-route", "off-duty"]
MIC_E_CUSTOM = ["emergency", "custom-6", "custom-5", "custom-4",
                "custom-3", "custom-2", "custom-1", "custom-0"]


def _mic_e_destination(call: str):
    """The latitude and three flags hidden in the destination address.

    Six characters, each carrying a digit of `ddmmhh` latitude and, in the
    first three, one bit of the message. Characters four, five and six also
    carry north/south, the hundred-degree longitude offset, and east/west. A
    character may be a digit, a letter A to K (custom message), L (a blanked
    digit with the message bit clear), or P to Z (standard message). Anything
    else means this is not a mic-E destination.
    """
    if len(call) < 6:
        return None
    digits = []
    bits = []
    north = offset = west = False
    blanked = 0
    for i, ch in enumerate(call[:6]):
        if "0" <= ch <= "9":
            digit, bit, high = ord(ch) - 48, 0, False
        elif "A" <= ch <= "J":
            digit, bit, high = ord(ch) - 65, 1, None      # custom, no N/S bit
        elif ch == "K":
            digit, bit, high = None, 1, None
        elif ch == "L":
            digit, bit, high = None, 0, False
        elif "P" <= ch <= "Y":
            digit, bit, high = ord(ch) - 80, 1, True
        elif ch == "Z":
            digit, bit, high = None, 1, True
        else:
            return None
        if digit is None:
            blanked += 1
            digit = 0
        digits.append(digit)
        if i < 3:
            bits.append(bit)
        elif high is None:
            # A custom-set character in one of the three flag positions says
            # nothing about the flag, and the specification gives no default.
            # Treated as the low value, which is what other decoders do.
            high = False
        if i == 3:
            north = bool(high)
        elif i == 4:
            offset = bool(high)
        elif i == 5:
            west = bool(high)

    degrees = digits[0] * 10 + digits[1]
    minutes = digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100.0
    if degrees > 90 or minutes >= 60:
        return None
    lat = degrees + minutes / 60.0
    if not north:
        lat = -lat
    custom = any(1 for ch in call[:3] if "A" <= ch <= "K")
    message = (MIC_E_CUSTOM if custom else MIC_E_STANDARD)[
        (bits[0] << 2) | (bits[1] << 1) | bits[2]]
    uncertainty = 0.0 if not blanked else 1852.0 * (10.0 ** (blanked - 2)) / 2
    return lat, offset, west, message, uncertainty


def _mic_e(frame, packet: Packet) -> None:
    info = frame.info
    if len(info) < 9:
        packet.problem = "mic-e-short"
        return
    got = _mic_e_destination(frame.destination.call)
    if got is None:
        packet.problem = "mic-e-destination"
        return
    lat, offset, west, message, uncertainty = got

    degrees = info[1] - 28
    if offset:
        degrees += 100
    if 180 <= degrees <= 189:
        degrees -= 80
    elif 190 <= degrees <= 199:
        degrees -= 190
    minutes = info[2] - 28
    if minutes >= 60:
        minutes -= 60
    hundredths = info[3] - 28
    if not (0 <= degrees <= 179 and 0 <= minutes < 60 and 0 <= hundredths < 100):
        packet.problem = "mic-e-longitude"
        return
    lon = degrees + (minutes + hundredths / 100.0) / 60.0
    if west:
        lon = -lon

    speed = (info[4] - 28) * 10
    dc = info[5] - 28
    speed += dc // 10
    course = (dc % 10) * 100 + (info[6] - 28)
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400

    packet.kind = "mic-e"
    packet.mic_e_message = message
    packet.position = Position(lat=lat, lon=lon, uncertainty_m=uncertainty,
                               symbol=chr(info[8]) + chr(info[7]))
    packet.speed_kmh = round(speed * KNOT_KM_H, 1)
    packet.course_deg = float(course) if course else None

    rest = info[9:].decode("latin-1")
    # The status text can carry an altitude as three base-91 characters and a
    # closing brace, in metres above a datum 10000 m down. It is the one mic-E
    # field outside the fixed part, and it is either the first thing in the
    # status text or the second: many radios put a single character in front
    # of it saying what make they are (`]` is a Kenwood TM-D700), and a
    # decoder that only looks at offset zero silently loses the altitude of
    # every one of them.
    for start in (0, 1):
        if rest[start + 3: start + 4] != "}":
            continue
        value = _base91(rest[start: start + 3])
        if value is None:
            continue
        packet.altitude_m = float(value - 10000)
        rest = rest[start + 4:]
        break
    match = _ALTITUDE.search(rest)
    if match:
        packet.altitude_m = round(int(match.group(1)) * 0.3048, 1)
        rest = rest[:match.start()] + rest[match.end():]
    packet.comment = rest


# -- messages, objects, items ------------------------------------------------

_MSG = re.compile(r"^:([A-Za-z0-9\- ]{9}):(.*)$", re.S)
_MSG_ID = re.compile(r"\{([A-Za-z0-9]{1,5})$")
_ACK = re.compile(r"^(ack|rej)([A-Za-z0-9]{1,5})$")


def _message(text: str, packet: Packet) -> None:
    match = _MSG.match(text)
    if not match:
        packet.problem = "message-addressee"
        return
    packet.kind = "message"
    packet.addressee = match.group(1).strip()
    body = match.group(2)
    ack = _ACK.match(body)
    if ack:
        packet.kind = "ack" if ack.group(1) == "ack" else "reject"
        packet.ack = ack.group(2)
        packet.message_id = ack.group(2)
        return
    ident = _MSG_ID.search(body)
    if ident:
        packet.message_id = ident.group(1)
        body = body[: ident.start()]
    packet.message = body


def _object(text: str, packet: Packet) -> None:
    if len(text) < 18:
        packet.problem = "object-short"
        return
    packet.kind = "object"
    packet.name = text[1:10].strip()
    state = text[10]
    if state not in "*_":
        packet.problem = "object-state"
        return
    packet.live = state == "*"
    stamp, when, rest = _timestamp(text[11:])
    packet.timestamp, packet.time_utc = stamp, when
    _position_body(rest, packet)
    packet.kind = "object"


def _item(text: str, packet: Packet) -> None:
    body = text[1:]
    at = -1
    for i, ch in enumerate(body[:10]):
        if ch in "!_":
            at = i
            break
    if at < 3:
        packet.problem = "item-name"
        return
    packet.kind = "item"
    packet.name = body[:at]
    packet.live = body[at] == "!"
    _position_body(body[at + 1:], packet)
    packet.kind = "item"


_TELEMETRY = re.compile(r"^T#(\d{1,3}|MIC),?"
                        r"(\d{1,3}),(\d{1,3}),(\d{1,3}),(\d{1,3}),(\d{1,3}),"
                        r"([01]{8})")


def _telemetry(text: str, packet: Packet) -> None:
    match = _TELEMETRY.match(text)
    if not match:
        packet.problem = "telemetry"
        return
    packet.kind = "telemetry"
    packet.telemetry = {
        "sequence": match.group(1),
        "analogue": [int(match.group(i)) for i in range(2, 7)],
        "digital": match.group(7),
    }


# -- the way in --------------------------------------------------------------


def parse(frame, now: float | None = None) -> Packet:
    """One AX.25 frame to one APRS packet, as far as it can be understood.

    Takes the frame rather than the information field because mic-E needs the
    destination address. Never raises and never returns None: a packet nobody
    can read is still a packet somebody transmitted, and the window shows it
    with its text and says it did not parse.
    """
    text = frame.text()
    packet = Packet(text=text)
    if not text:
        packet.problem = "empty"
        return packet
    first = text[0]
    packet.kind = TYPES.get(first, "unknown")
    packet.messaging = first in MESSAGING
    try:
        if packet.kind == "mic-e":
            _mic_e(frame, packet)
        elif first in "!=":
            _position_body(text[1:], packet)
        elif first in "/@":
            stamp, when, rest = _timestamp(text[1:], now)
            if not stamp:
                packet.problem = "timestamp"
                packet.comment = text[1:]
                return packet
            packet.timestamp, packet.time_utc = stamp, when
            _position_body(rest, packet)
        elif first == ">":
            packet.status = text[1:]
            packet.comment = text[1:]
        elif first == ":":
            _message(text, packet)
        elif first == ";":
            _object(text, packet)
        elif first == ")":
            _item(text, packet)
        elif first == "_":
            stamp, when, rest = _timestamp(text[1:], now)
            packet.timestamp, packet.time_utc = stamp, when
            packet.comment = _weather(rest, packet)
            if not packet.weather:
                packet.problem = "weather-empty"
        elif first == "T":
            _telemetry(text, packet)
        else:
            packet.comment = text[1:]
    except Exception as e:                       # noqa: BLE001
        # A packet from a stranger must not be able to stop the receiver. The
        # exception is kept in words rather than swallowed, because "this one
        # broke the parser" is a thing worth being able to see.
        packet.problem = "exception"
        packet.error = f"{type(e).__name__}: {e}"
    return packet


def distance_bearing(lat1, lon1, lat2, lon2) -> tuple[float, float]:
    """Great-circle kilometres and the bearing in degrees true.

    Here rather than borrowed from app/grid.py because that module is the
    logbook's and takes Maidenhead locators; this takes the degrees APRS
    actually sends, and one short function is cheaper than a conversion to
    a grid square and back that would throw away the precision.
    """
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    dp = p2 - p1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * radius * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km, (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# -- writing: what this station sends -------------------------------------------
#
# The other direction from everything above, for the engine's APRS station
# (aprs_station.py). Unlike parse, these raise PacketError (a ValueError,
# for the developer) on bad input: the caller is this station, and a beacon with the station 200 miles out to sea is a bug
# to fix, not a packet to show.

#: Characters APRS 1.1 forbids in message text (§14): | and ~ are reserved,
#: { starts the message number.
MESSAGE_FORBIDDEN = "|~{"
MESSAGE_MAX = 67


def _ddmm(value: float, deg_digits: int, hemispheres: str, ambiguity: int) -> str:
    hemisphere = hemispheres[0] if value >= 0 else hemispheres[1]
    value = abs(value)
    degrees = int(value)
    minutes = round((value - degrees) * 60.0, 2)
    if minutes >= 60.0:
        degrees, minutes = degrees + 1, 0.0
    text = f"{degrees:0{deg_digits}d}{minutes:05.2f}"
    # Ambiguity blanks the last digits of the minutes, from the right,
    # skipping the decimal point.
    chars = list(text)
    positions = [len(chars) - 1, len(chars) - 2, len(chars) - 4, len(chars) - 5]
    for at in positions[:ambiguity]:
        chars[at] = " "
    return "".join(chars) + hemisphere


def format_position(lat: float, lon: float, symbol: str = "/-", comment: str = "", *, messaging: bool = True,
                    course_deg: float | None = None, speed_kmh: float | None = None,
                    altitude_m: float | None = None, ambiguity: int = 0) -> str:
    """An uncompressed position report without a timestamp: `=` when the
    station can receive messages, `!` when not (APRS 1.1 §8). Course and
    speed as CSE/SPD (degrees, knots), altitude as /A= (feet)."""
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise PacketError(f"{lat}, {lon} is not a position")
    if len(symbol) != 2:
        raise PacketError(f"symbol {symbol!r}: a table character and a code")
    if not 0 <= ambiguity <= 4:
        raise PacketError("ambiguity is 0 to 4 digits")
    text = ("=" if messaging else "!") + _ddmm(lat, 2, "NS", ambiguity) + symbol[0] \
        + _ddmm(lon, 3, "EW", ambiguity) + symbol[1]
    if course_deg is not None and speed_kmh is not None:
        course = int(round(course_deg)) % 360 or 360
        text += f"{course:03d}/{min(999, int(round(speed_kmh / KNOT_KM_H))):03d}"
    if altitude_m is not None:
        text += f"/A={int(round(altitude_m / 0.3048)):06d}"
    return text + comment


def format_status(text: str) -> str:
    return ">" + text


def _addressee(call: str) -> str:
    call = call.strip().upper()
    if not call or len(call) > 9:
        raise PacketError(f"{call!r}: an addressee is 1 to 9 characters")
    return call.ljust(9)


def format_message(addressee: str, text: str, message_id: str | None = None) -> str:
    """`:ADDRESSEE:text{id`. The text is the caller's to keep within
    MESSAGE_MAX and free of MESSAGE_FORBIDDEN (clean_message does both)."""
    if len(text) > MESSAGE_MAX or any(ch in MESSAGE_FORBIDDEN for ch in text):
        raise PacketError("message text over 67 characters, or with | ~ {")
    out = f":{_addressee(addressee)}:{text}"
    if message_id:
        if not message_id.isalnum() or len(message_id) > 5:
            raise PacketError(f"message id {message_id!r}: 1-5 letters and digits")
        out += "{" + message_id
    return out


def format_ack(addressee: str, message_id: str, reject: bool = False) -> str:
    return f":{_addressee(addressee)}:{'rej' if reject else 'ack'}{message_id}"


def clean_message(text: str) -> list[str]:
    """Text as APRS messages: forbidden characters replaced, cut into parts
    of at most MESSAGE_MAX at word boundaries where there are any."""
    text = "".join("-" if ch in MESSAGE_FORBIDDEN else ch for ch in text.replace("\r", " ").replace("\n", " "))
    parts = []
    while len(text) > MESSAGE_MAX:
        cut = text.rfind(" ", 0, MESSAGE_MAX + 1)
        if cut <= 0:
            cut = MESSAGE_MAX
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text.strip() or not parts:
        parts.append(text.rstrip())
    return parts


# -- objects, items, weather and telemetry (0.3.0) ----------------------------


def _symbol(symbol: str) -> str:
    if len(symbol) != 2:
        raise PacketError(f"symbol {symbol!r}: a table character and a code")
    return symbol


def _object_name(name: str, width: int) -> str:
    name = name.rstrip()
    if not name or len(name) > width or any(not (" " <= ch <= "~") for ch in name):
        raise PacketError(f"{name!r}: a name is 1 to {width} printable characters")
    return name


def format_object(name: str, lat: float, lon: float, symbol: str = "/.", comment: str = "", *,
                  live: bool = True, when: float | None = None) -> str:
    """An object (APRS 1.1 ch. 11): `;NAME_____*DDHHMMzposition`. A killed
    object (`live=False`) is the same with `_`, which is how a map is told
    to take it away. `when` is the UTC time it stands for (now, normally)."""
    name = _object_name(name, 9).ljust(9)
    t = datetime.fromtimestamp(when, timezone.utc) if when is not None else datetime.now(timezone.utc)
    stamp = t.strftime("%d%H%Mz")
    pos = format_position(lat, lon, _symbol(symbol), comment, messaging=False)[1:]
    return f";{name}{'*' if live else '_'}{stamp}{pos}"


def format_item(name: str, lat: float, lon: float, symbol: str = "/.", comment: str = "", *,
                live: bool = True) -> str:
    """An item (APRS 1.1 ch. 11): `)NAME!position`, a 3-9 character name
    with no timestamp; `_` instead of `!` kills it."""
    name = _object_name(name, 9)
    if len(name) < 3 or "!" in name or "_" in name:
        raise PacketError(f"{name!r}: an item name is 3 to 9 characters, no ! or _")
    pos = format_position(lat, lon, _symbol(symbol), comment, messaging=False)[1:]
    return f"){name}{'!' if live else '_'}{pos}"


def _wx(value, width: int, scale: float = 1.0) -> str:
    """One weather field: digits, or dots for no sensor (APRS 1.1 ch. 12)."""
    if value is None:
        return "." * width
    n = int(round(float(value) * scale))
    if n < 0:
        return "-" + str(min(-n, 10 ** (width - 1) - 1)).zfill(width - 1)
    return str(min(n, 10 ** width - 1)).zfill(width)


def format_weather(lat: float, lon: float, wx: dict, comment: str = "", *, symbol: str = "/_") -> str:
    """A complete weather report with position (APRS 1.1 ch. 12): wind
    direction and speed in the course/speed place, then g (gust, mph),
    t (temperature, °F), r/p/P (rain last hour / 24 h / since midnight,
    hundredths of an inch), h (humidity, %; 100 is 00), b (pressure,
    tenths of hPa). `wx` keys: wind_dir_deg, wind_kmh, gust_kmh, temp_c,
    rain_hour_mm, rain_24h_mm, rain_midnight_mm, humidity_pct, pressure_hpa;
    a missing one is sent as dots. The names are the parser's (_WX), so
    a report parsed here formats back to the same readings."""
    def kmh_to_mph(v):
        return None if v is None else v / 1.609344

    def mm_to_in100(v):
        return None if v is None else v / 0.254
    temp_c = wx.get("temp_c")
    temp_f = None if temp_c is None else temp_c * 9 / 5 + 32
    humidity = wx.get("humidity_pct")
    if humidity is not None:
        humidity = 0 if round(humidity) >= 100 else max(1, round(humidity))
    direction = wx.get("wind_dir_deg")
    pos = format_position(lat, lon, _symbol(symbol), "", messaging=False)
    text = pos + _wx(direction % 360 if direction is not None else None, 3) + "/" \
        + _wx(kmh_to_mph(wx.get("wind_kmh")), 3)
    text += "g" + _wx(kmh_to_mph(wx.get("gust_kmh")), 3) + "t" + _wx(temp_f, 3)
    for key, letter in (("rain_hour_mm", "r"), ("rain_24h_mm", "p"), ("rain_midnight_mm", "P")):
        if wx.get(key) is not None:
            text += letter + _wx(mm_to_in100(wx[key]), 3)
    if humidity is not None:
        text += "h" + _wx(humidity, 2)
    if wx.get("pressure_hpa") is not None:
        text += "b" + _wx(wx["pressure_hpa"], 5, 10.0)
    return text + comment


def format_telemetry(sequence: int, analogue: list, digital: str = "00000000") -> str:
    """`T#nnn,a1,a2,a3,a4,a5,bbbbbbbb` (APRS 1.0 telemetry): five values
    0-255 (anything else is clipped), eight bits. The sequence wraps at
    1000."""
    values = [max(0, min(255, int(round(float(v))))) for v in (list(analogue) + [0] * 5)[:5]]
    if len(digital) != 8 or set(digital) - {"0", "1"}:
        raise PacketError("telemetry bits are eight 0s and 1s")
    return f"T#{sequence % 1000:03d}," + ",".join(f"{v:03d}" for v in values) + "," + digital


def format_telemetry_meta(call: str, kind: str, values: list) -> str:
    """The messages that say what telemetry means, addressed to the
    station itself: PARM (names), UNIT (units), EQNS (a,b,c for each
    channel: a*x^2 + b*x + c), BITS (sense bits and project title)."""
    if kind not in ("PARM", "UNIT", "EQNS", "BITS"):
        raise PacketError(kind)
    body = f"{kind}." + ",".join(str(v) for v in values)
    return f":{_addressee(call)}:{body}"
