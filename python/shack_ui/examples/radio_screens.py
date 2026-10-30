"""Sketches of Radio's other screens (home, DAB, FM, Ships, APRS, Sondes,
DMR), built with shack_ui to show the builder covers what each needs.

These are not the screens Radio will ship: they have no fixture yet and
their layouts are Radio's to settle in SCREENS.md §16 step 5. What they
pin is that every element of §15's table for these screens has a builder
and validates: the spectrum with tap-to-tune, the big dial, timeshifted
audio with level bars from the audio itself, a station grid with box art,
maps with the shared symbol sets, a sonde profile chart on two axes, a DMR
call log that follows its newest row with ticking call timers, and the
receiver holds of each. The routes are Radio's (openhamshack-nb
docs/SERVER-API.md).
"""

from __future__ import annotations

from shack_ui import A, Req, Screen, Symbols, T, item, local, p

GROUND = {"tiles": "api/map/tiles/{z}/{x}/{y}.png", "land": "api/map/land", "zoom": [3, 14],
          "style": local("map.style", "real")}
OSM = "© OpenStreetMap contributors"
SPECTRUM = {"path": "api/spectrum/v1", "name": "main"}
TUNE = A.post("api/set", {"changes": {"vfo": A.HZ}}, level="control")


def audio(**kw) -> dict:
    return p.audio("audio", "api/audio/rx", params={"channels": 2}, autoplay=True, **kw)


def session_hold(name: str, route: str) -> tuple:
    """A decoder session a map keeps alive (AIS, APRS, sondes), renewed
    through presence: Radio treats a presence renewal as renewing it."""
    return (name, A.post(f"api/{route}/session", {"client": A.CLIENT}, level="control", timeout_s=30),
            A.delete(f"api/{route}/session?client={{$client}}", level="control"))


def home(req: Req) -> Screen:
    s = Screen("radio/home", title="Radio", req=req)
    s.live("ui/v2/live/radio/home")
    s.key("chUp", A.post("api/set", {"changes": {"step": "up"}}, level="control", hint="Step"))
    s.root = p.column(
        p.row(p.frequency("dial", T.freq(145_500_000, "MHz", 3), A.post("api/set", {"vfo": A.VALUE}, level="control"),
                          steps=[100, 1000, 12_500, 25_000], step=12_500, weight=1),
              p.choice([p.option(m, m.upper()) for m in ("fm", "am", "usb", "lsb", "cw")], "fm",
                       A.post("api/set", {"mode": A.VALUE}, level="control"), id="mode", style="segmented"),
              gap="m"),
        p.spectrum("scope", SPECTRUM, overlay=True, layers=["band", "dial", "passband", "channels", "stations"],
                   tap=TUNE | {"offsetHz": 0}, drag={"snapHz": 12_500}, weight=1),
        p.row(p.meter(None, id="smeter", min=-140, max=0, style="levels", label="Signal", weight=1),
              p.slider(local("audio.volume", 0.8), 0, 1, A.local("audio.volume", set=A.VALUE), step=0.05, w=200),
              p.button("Mute", A.part("audio", "mute"), icon="mute"),
              gap="m"),
        p.banner(None, id="rxOwner", tone="warn",
                 actions=[A.delete("api/decoders/sessions/dab", level="control", text="Stop DAB")]),
        audio(controls="mute"),
        gap="s", pad="m")
    return s


def dab(req: Req) -> Screen:
    s = Screen("radio/dab", title="DAB Radio", req=req)
    s.collection("stations")
    s.selection("station", from_="stations")
    s.live("ui/v2/live/radio/dab")
    tile = p.card(p.column(p.image(item("art"), aspect=1, fit="cover", hero=item("sid"), alt=item("name")),
                           p.text(item("name"), style="title", size="s", lines=1, fit=True)),
                  action=A.post("api/dab/play", {"sid": item("sid"), "client": A.CLIENT}, level="control",
                                timeout_s=30),
                  on_hold=A.post("api/dab/favourites", {"sid": item("sid")}, level="control", text="Favourite"),
                  selected=item("playing"))
    s.root = p.split(
        p.grid(id="stations", from_="stations", columns={"tv": 4, "tablet": 4, "phone": 3}, aspect=1, gap="m",
               template=tile, empty=p.empty("No stations yet: scan the band", icon="dab",
                                            action=A.post("api/scan", {"band": "dab"}, level="control", text="Scan"))),
        p.column(p.image(None, id="slide", aspect=4 / 3, fit="contain", hero="playing", refresh_s=10),
                 p.text("", id="service", style="display", size="xl", fit=True),
                 p.text("", id="dls", tone="muted", lines=2),
                 p.meter(id="levels", style="levels", source={"audio": "audio"}),
                 audio(controls="timeshift"),
                 p.button("Sleep in 30 min", A.post("api/dab/sleep", {"min": 30, "client": A.CLIENT},
                                                    level="control"), style="quiet"),
                 gap="s"),
        ratio=0.55, compact="stack")
    return s


def fm(req: Req) -> Screen:
    s = Screen("radio/fm", title="FM", req=req)
    s.collection("stations")
    s.live("ui/v2/live/radio/fm")
    s.root = p.column(
        p.frequency("dial", T.freq(97_700_000, "MHz", 1), A.post("api/set", {"vfo": A.VALUE}, level="control"),
                    steps=[100_000], step=100_000, min=87_500_000, max=108_000_000),
        p.text("", id="rds", style="title", fit=True),
        p.list("stations", from_="stations", keep_focus=True,
               empty=p.empty("No stations yet: scan the band", icon="fm",
                             action=A.post("api/scan", {"band": "fm"}, level="control", text="Scan")),
               template=p.row(p.text(item("freqText"), style="numeric", w=90), p.text(item("name"), weight=1),
                              p.badge(item("rdsText"), style="outline")),
               on_activate=A.post("api/set", {"changes": {"vfo": item("hz"), "mode": "wfm"}}, level="control")),
        p.spectrum("scope", SPECTRUM, layers=["band", "dial", "stations"], tap=TUNE, h=160),
        audio(controls="timeshift"),
        gap="s", pad="m")
    return s


def traffic(req: Req, screen: str, title: str, coll: str, route: str, symbols: Symbols, details: list[dict]) -> Screen:
    """Ships, APRS and Sondes share Aircraft's shape: a map with a list and
    a details panel over it, one selection, one receiver session."""
    s = Screen(screen, title=title, req=req)
    s.symbols(symbols + Symbols.load("station"))
    s.collection(coll, filter={"hide": local(f"{route}.hide", []), "maxAgeS": local(f"{route}.forgetS", None)})
    s.selection(coll, from_=coll)
    s.hold(*session_hold(route, route))
    s.live(f"ui/v2/live/{screen}")
    s.key("menu", A.show("menu", hint="Menu"))
    s.key("play", A.part("map", "fit", hint="Show all"))
    s.root = p.stack(
        p.map("map", align="fill", ground=GROUND, attribution=OSM, view={"fit": "layers", "free": "start"},
              layers=[p.layer("station", "markers", items=[{"lat": 55.479, "lon": -4.542, "sym": "station",
                                                           "label": "N0CALL", "pin": True}]),
                      p.layer(coll, "markers", from_=coll, selection=coll, labels="all",
                              trails={"fade": True, "width": 2})]),
        p.panel("nearest", p.list("list", from_=coll, selection=coll, keep_focus=True,
                                  on_activate=A.show("details"), focus={"initial": True},
                                  template=p.row(p.icon(sym=item("sym"), tone=item("tone")),
                                                 p.column(p.text(item("label"), style="title", size="m"),
                                                          p.text(item("line2"), tone="muted"), weight=1),
                                                 p.text(item("distText"), style="numeric"), gap="s")),
                slot="end", w=300, open=True, surface="glass"),
        p.panel("details", p.scroll(p.column(*details, gap="s")), slot="end", w=375, for_=coll, step=True,
                surface="glass"),
        p.panel("menu", p.choice([p.option("real", "Real"), p.option("night", "Night")], local("map.style", "real"),
                                 A.local("map.style", set=A.VALUE), style="stepper", text="Map"),
                slot="end", w=315),
        p.hints(id="hints", align="bottom-start", style="pills"))
    return s


def ships(req: Req) -> Screen:
    return traffic(req, "radio/ships", "Ships", "vessels", "ais", Symbols.load("ships"), [
        p.text(item("name"), style="display", size="xl"),
        p.field("MMSI", item("mmsi")), p.field("Destination", item("dest")), p.field("ETA", item("etaText")),
        p.field("Heard", T.ago(item("at")))])


def aprs(req: Req) -> Screen:
    s = traffic(req, "radio/aprs", "APRS", "stations", "aprs", Symbols.load("aprs"), [
        p.text(item("call"), style="display", size="xl"), p.text(item("comment"), tone="muted"),
        p.field("Path", item("path"), style="mono"), p.field("Heard", T.ago(item("at"))),
        p.table("packets", [p.column_of("Time", T.time(item("at"), "local", "hms")), p.column_of("Packet", item("raw"),
                                                                                                mono=True)],
                from_="packets")])
    # The selected station's recent packets: the box fills this for the
    # client that selected it (LiveHub on_select), nobody else.
    s.collection("packets")
    return s


def sondes(req: Req) -> Screen:
    s = traffic(req, "radio/sonde", "Sondes", "sondes", "sonde", Symbols.load("sondes"), [
        p.text(item("serial"), style="display", size="xl"),
        p.field("Altitude", item("altText")), p.field("Climb", item("climbText")),
        p.field("Burst predicted", item("burstText")),
        p.chart("profile", [{"mark": "line", "from": "profile", "x": "temp", "y": "alt", "tone": "bad"},
                            {"mark": "line", "from": "profile", "x": "rh", "y": "alt", "tone": "info", "axis": "y2"},
                            {"mark": "arrow", "points": []}],
                x={"type": "number", "label": "°C"}, y={"type": "number", "label": "km"},
                h=220)])
    s.collection("profile")
    return s


def dmr(req: Req) -> Screen:
    s = Screen("radio/dmr", title="DMR", req=req)
    s.collection("calls")
    s.live("ui/v2/live/radio/dmr")
    s.hold("dmr", A.post("api/decoders/sessions", {"decoder": "dmr", "client": A.CLIENT}, level="control"),
           A.delete("api/decoders/sessions/dmr", level="control"), renew_s=10)
    slot = lambda n: p.card(p.column(  # noqa: E731
        p.badge(f"Slot {n}", id=f"slot{n}", style="dot", tone="muted", pulse=False),
        p.text(None, id=f"talker{n}", style="title", size="l"),
        p.text(None, id=f"group{n}", tone="muted"),
        p.text(None, id=f"timer{n}", style="numeric")), w=260)
    s.root = p.column(
        p.row(slot(1), slot(2), p.meter(id="level", style="bar", source={"audio": "audio"}, weight=1), gap="m"),
        p.list("log", from_="calls", follow="top", keep_focus=True,
               template=p.row(p.text(item("callsign"), style="title", size="s", w=120),
                              p.text(item("name"), weight=1),
                              p.text(item("tg"), tone="muted"),
                              p.text(T.since(item("start")), style="numeric"), gap="s")),
        audio(controls="mute"),
        gap="m", pad="m")
    return s


SCREENS = {"radio/home": home, "radio/dab": dab, "radio/fm": fm, "radio/ships": ships, "radio/aprs": aprs,
           "radio/sonde": sondes, "radio/dmr": dmr}
