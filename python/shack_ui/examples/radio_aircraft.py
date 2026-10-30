"""The worked example: Radio's Aircraft screen on a TV, built with shack_ui.

It builds exactly the contract's Aircraft document
(spec/vectors/ui2/fixtures/screens/radio/aircraft.json, from openhamshack
docs/contract/screens-fixtures), which tests/test_example_aircraft.py
checks, and shows the LiveHub half that keeps it current. It is the
pattern for every Radio screen: the static shape here, the moving data in
a channel, the receiver held while the screen is open.

    python3 python/shack_ui/examples/radio_aircraft.py     # prints the document

Words are English literals here; Radio passes each through its catalog
(`_()`), because the box sends text already translated (SCREENS.md §8.5).
"""

from __future__ import annotations

import json

from shack_ui import A, LiveHub, Req, Screen, Symbols, T, item, local, p

STATION = (55.479, -4.542)

BANDS = [  # tag, colour name, dark, light, chip, menu row
    ("band:low", "band.low", "#FF9F43", "#B8610F", "Low", "Below 5,000 ft"),
    ("band:lower", "band.lower", "#FFD54F", "#9A7A00", "Lower", "5,000 to 15,000 ft"),
    ("band:middle", "band.middle", "#57E39B", "#1E8A55", "Middle", "15,000 to 25,000 ft"),
    ("band:upper", "band.upper", "#4FC3FF", "#0A6FA8", "Upper", "25,000 to 35,000 ft"),
    ("band:top", "band.top", "#C08BFF", "#7B3FC4", "Top", "35,000 ft and above"),
]
KINDS = [("kind:light", "Light"), ("kind:airliner", "Airliner"), ("kind:heavy", "Heavy"),
         ("kind:rotor", "Rotor"), ("kind:other", "Other")]


def nm(n: int) -> dict:
    return {"km": round(n * 1.852, 1), "label": f"{n} nm"}


RINGS = [nm(25), nm(50), nm(100)]
AIRPORTS = {"EGPK": (55.5094, -4.5867, "PIK", 5), "EGPF": (55.8719, -4.4331, "GLA", 5),
            "EGPH": (55.95, -3.3725, "EDI", 5), "EGPN": (56.4525, -3.0258, "DND", 7)}


def colors() -> dict:
    c = {name: {"dark": dark, "light": light} for _, name, dark, light, _, _ in BANDS}
    c["band.ground"] = {"dark": "#B8C4CF", "light": "#5E6B77"}
    c["band.none"] = {"dark": "#8FA3B3", "light": "#5A6E7E"}
    return c


def hidden(tag: str, shown: bool = True) -> dict:
    """A menu toggle over the hide set: lit while the tag is *not* hidden."""
    return local("adsb.hide", [], has=tag, not_=shown)


def header() -> dict:
    chips = [p.option(tag, chip, tone=name) for tag, name, _, _, chip, _ in BANDS]
    chips.append(p.option("ground", "Ground", tone="band.ground"))
    return p.row(
        p.choice(chips, local("adsb.hide", []), A.local("adsb.hide", set=A.VALUE),
                 id="chips", style="chips", multi=True, inverted=True),
        p.spacer(weight=1),
        p.badge("", id="emergency", style="pill", tone="emergency", hidden=True),
        p.badge("Starting", id="status", style="dot", tone="warn"),
        p.text(T.clock("local", "hm"), style="numeric", size="l"),
        id="header", align="top-fill", h=72, pad="m", gap="m", surface="scrim")


def row_template() -> dict:
    return p.row(
        p.badge(style="pill", tone=item("tone"), w=9, h=27),
        p.column(
            p.row(p.text(item("title"), style="title", size="m", lines=1),
                  p.text(item("routeText"), tone="accent", lines=1), gap="xs"),
            p.text(item("line2"), tone="muted", lines=1),
            p.badge(item("emergencyText"), style="pill", tone="emergency"),
            weight=1),
        p.text(item("distText"), style="numeric", tone="muted"),
        gap="s", pad="s")


def nearest() -> dict:
    return p.panel("nearest", p.column(
        p.text("Nearest", style="caption", tone="muted"),
        p.text("", id="count", style="title", size="m"),
        p.text("", id="session", tone="muted", lines=1),
        p.text(None, id="ignored", tone="muted", size="s"),
        p.list("list", from_="aircraft", selection="aircraft", weight=1, keep_focus=True, focus={"initial": True},
               on_activate=A.show("details", hint="Details"),
               empty=p.empty("Listening for aircraft on 1090 MHz", icon="aircraft"),
               template=row_template()),
        gap="s"),
        slot="end", w=300, open=True, surface="glass", back=A.part("map", "fit"),
        keys={"left": A.focus("map", hint="Map")})


def grid(*fields: dict) -> dict:
    return p.grid(*fields, columns={"tv": 2, "tablet": 2, "phone": 2}, gap="s")


def details() -> dict:
    return p.panel("details", p.scroll(p.column(
        p.row(p.icon(sym=item("sym"), tone=item("tone"), size="xl", surface="card"),
              p.column(p.text(item("kindText"), style="caption", tone="muted"),
                       p.text(item("title"), style="display", size="xl"),
                       p.text(item("icaoLine"), tone="muted", style="mono"), weight=1),
              gap="s"),
        p.text(item("regLine")),
        p.text(item("opLine"), tone="muted"),
        # Planespotters' terms: their picture, from their host, with the
        # photographer's credit and a link (a QR on a TV): SCREENS.md §9.
        p.row(p.image(item("photo.src"), h=140, weight=1, fit="cover", credit=item("photo.credit"),
                      link=item("photo.link"), qr=True, alt=item("photo.alt")), gap="s"),
        p.text(item("routeLong"), tone="accent"),
        p.progress(item("progress"), style="bar", tone="accent"),
        p.row(p.field("To go", item("toGo")), p.field("ETA", item("eta")), gap="m"),
        p.banner(item("emergencyText"), tone="emergency"),
        grid(p.field("Altitude", item("sub")), p.field("Vertical", item("vertText")),
             p.field("Speed", item("speedText")), p.field("Track", item("trackText")),
             p.field("Squawk", item("squawk"), tone=item("squawkTone")), p.field("From", item("distText")),
             p.field("Heard", [T.ago(item("at")), item("rssiText")]), p.field("Messages", item("messagesText"))),
        p.section("Cockpit", grid(*[p.field(label, item(f"ehs.{f}")) for label, f in (
            ("Selected altitude", "selAlt"), ("Selected heading", "selHdg"), ("IAS", "ias"), ("TAS", "tas"),
            ("Mach", "mach"), ("Roll", "roll"), ("Pressure set", "baro"))])),
        gap="s")),
        slot="end", w=375, for_="aircraft", step=True, surface="glass",
        keys={"ok": A.seq(A.part("map", "follow"), A.show("hud"), hint="Follow")})


def hud() -> dict:
    return p.panel("hud", p.row(
        p.column(p.badge("Following", style="outline", tone="accent"),
                 p.text(item("title"), style="title", size="l"),
                 p.text([item("routeText"), item("kindText")], tone="muted")),
        p.field("Altitude", item("sub")), p.field("Speed", item("speedText")),
        p.field("Heading", item("trackText")), p.field("From", item("distText")),
        gap="l", pad="m"),
        slot="bottom", max_w=860, surface="glass", for_="aircraft",
        on_close=A.seq(A.part("map", "unfollow"), A.show("details")))


def menu() -> dict:
    def toggle(text, tag, tone=None, shown=True):
        return p.toggle(text, hidden(tag, shown), A.local("adsb.hide", toggle=tag),
                        **({"tone": tone} if tone else {}))

    def stepper(text, key, default, options):
        return p.choice([p.option(v, t) for v, t in options], local(key, default), A.local(key, set=A.VALUE),
                        style="stepper", text=text)

    return p.panel("menu", p.column(
        p.text("Show", style="caption", tone="muted"),
        *[toggle(row, tag, name) for tag, name, _, _, _, row in BANDS],
        toggle("Altitude unknown", "band:none", "band.none"),
        toggle("Hide aircraft on the ground", "ground", shown=False),
        p.text("Kinds", style="caption", tone="muted"),
        *[toggle(text, tag) for tag, text in KINDS],
        p.text("Map", style="caption", tone="muted"),
        stepper("Style", "adsb.style", "real", [("real", "Real"), ("night", "Night")]),
        stepper("Rings", "adsb.rings", RINGS, [([], "Off"), ([nm(10), nm(25), nm(50)], "10, 25, 50 nm"),
                                               (RINGS, "25, 50, 100 nm"), ([nm(50), nm(100), nm(200)], "50, 100, 200 nm")]),
        stepper("Labels", "adsb.labels", "all", [("all", "All"), ({"tag": "airborne"}, "Airborne"), ("none", "Off")]),
        stepper("Forget after", "adsb.forgetS", None, [(None, "Radio's setting"), (60, "1 min"), (120, "2 min"),
                                                       (180, "3 min"), (300, "5 min"), (600, "10 min")]),
        gap="s"),
        slot="end", w=315, surface="glass")


def the_map() -> dict:
    return p.map(
        "map", align="fill",
        ground={"tiles": "api/map/tiles/{z}/{x}/{y}.png", "land": "api/map/land",
                "landBounds": [-11.0, 49.0, 2.5, 61.0], "zoom": [6, 14], "style": local("adsb.style", "real")},
        attribution="© OpenStreetMap contributors",
        view={"center": [54.5, -4.0], "zoom": 7, "fit": "layers", "minKm": 185.2, "maxKm": 450, "free": "start"},
        follow={"zoom": 10},
        back=A.focus("nearest"),
        keys={"ok": A.part("map", "selectCentre", hint="Choose")},
        layers=[
            p.layer("rings", "rings", center=list(STATION), rings=local("adsb.rings", RINGS)),
            p.layer("airports", "markers", from_="airports", labels="all"),
            p.layer("station", "markers", items=[{"lat": STATION[0], "lon": STATION[1], "sym": "station",
                                                  "tone": "default", "label": "N0CALL", "pin": True}]),
            p.layer("aircraft", "markers", from_="aircraft", selection="aircraft",
                    labels=local("adsb.labels", "all"), trails={"fade": True, "width": 2.5}),
        ])


def aircraft(req: Req) -> Screen:
    s = Screen("radio/aircraft", title="Aircraft", req=req)
    s.backdrop("plain")
    s.words({"s": "{n} s", "min": "{n} min", "h": "{n} h", "ago": "{d} ago"})
    s.colors(colors())
    s.symbols(Symbols.load("aircraft") + Symbols.load("station"))
    s.collection("aircraft", filter={"hide": local("adsb.hide", []), "maxAgeS": local("adsb.forgetS", None)})
    airports = s.collection("airports")
    for icao, (lat, lon, label, min_zoom) in AIRPORTS.items():
        airports.put(icao, lat=lat, lon=lon, sym="airport", tone="muted", label=label, minZoom=min_zoom)
    s.selection("aircraft", from_="aircraft")
    # The receiver at 6 MS/s for ADS-B while this screen is anywhere in the
    # stack; a start waits for the receiver to reopen, hence 30 s.
    s.hold("adsb", start=A.post("api/adsb/session", {"client": A.CLIENT}, level="control", timeout_s=30),
           stop=A.delete("api/adsb/session?client={$client}", level="control"))
    s.live("ui/v2/live/radio/aircraft")
    s.key("menu", A.show("menu", hint="Menu"))
    s.key("play", A.part("map", "fit", hint="Show all"))
    s.key("chUp", A.part("map", "zoomIn", hint="Zoom"))
    s.key("ff", A.part("map", "zoomIn"))
    s.key("chDown", A.part("map", "zoomOut"))
    s.key("rew", A.part("map", "zoomOut"))
    s.root = p.stack(the_map(), header(), nearest(), details(), hud(), menu(),
                     p.hints(id="hints", align="bottom-start", style="pills"))
    return s


# ---------------------------------------------------------------- the live half

def live_feed(hub: LiveHub):
    """What Radio's ADS-B table callbacks do with the screen's channel:
    each change is one call, and the hub coalesces them per client."""
    feed = hub.channel("radio/aircraft", document=aircraft(Req()).json())

    def heard(icao: str, a: dict) -> None:
        feed.up("aircraft", icao, lat=a["lat"], lon=a["lon"], rot=a["track"], sub=a["altText"],
                at=a["at"], distText=a["distText"], line2=a["line2"])
        feed.push("aircraft", icao, "trail", [[a["lat"], a["lon"], a["tone"]]], max=30)

    def gone(icao: str) -> None:
        feed.rm("aircraft", icao)

    # Only the selected aircraft needs its photo and cockpit values: they
    # go to the client that selected it, not to every TV.
    feed.on_select(lambda client, key: client.up("aircraft", key, **{"photo": {"src": None}}), sel="aircraft")
    return feed, heard, gone


if __name__ == "__main__":
    print(json.dumps(aircraft(Req()).json(), indent=1, ensure_ascii=False))
