"""Radio's Weather Sats screen and its picture viewer, built with shack_ui.

They build exactly the contract's worked examples
(spec/vectors/ui2/fixtures/screens/radio/wxsat.json and wxsat-viewer.json),
which tests/test_example_screens.py checks: tabs that switch on focus, a
list whose OK runs each pass's own action, sky plots, a meter, a gallery
grid from a collection, a map of image frames, and a polled screen
rather than a live one. The poll answer is the app's own (§4.5).
"""

from __future__ import annotations

from shack_ui import A, Req, Screen, T, item, p

COLORS = {"sat.n23": ("#5FD3FF", "#0B6E99"), "sat.n24": ("#B48CFF", "#6A3FC0"),
          "plan.record": ("#57E39B", "#1E8A55"), "plan.busy": ("#FFC857", "#9A6A00"),
          "plan.skip": ("#7F8C99", "#5A6672"), "plan.conflict": ("#FF6B6B", "#B3261E"),
          "plan.recording": ("#C08BFF", "#7B3FC4"), "plan.decoding": ("#4FE3E3", "#0B7F7F")}
SAT = {"paths": [{"d": "M10 10 H14 V14 H10 Z", "fill": "ink"},
                 {"d": "M2 10.5 H9 V13.5 H2 Z M15 10.5 H22 V13.5 H15 Z", "fill": "tone", "stroke": "ink", "w": 0.5}]}
GROUND = {"tiles": "api/map/tiles/{z}/{x}/{y}.png", "land": "api/map/land",
          "landBounds": [-11.0, 49.0, 2.5, 61.0], "zoom": [3, 9], "style": "night"}


def passes_tab() -> dict:
    row = p.row(
        p.badge(style="pill", tone=item("tone"), w=6, h=48),
        p.column(p.text([item("label"), item("freqText")], style="title", size="s"),
                 p.text(item("when"), tone="muted"),
                 p.text(item("dirText"), tone="muted", size="s"), weight=1),
        p.column(p.badge(item("planText"), style="outline", tone=item("planTone")),
                 p.text(item("countdown"), style="numeric", size="s"), align="end"),
        gap="s", pad="s")
    return p.split(
        # OK on a pass runs the pass's own action: record or skip by plan.
        p.list("passList", from_="passes", selection="pass", w=480, focus={"initial": True}, keep_focus=True,
               on_activate=item("action"), empty=p.empty("No passes in the next day", icon="satellite"),
               template=row),
        p.column(p.text("Next pass", id="nextName", style="caption", tone="muted"),
                 p.text("", id="nextCountdown", style="display", size="xxl"),
                 p.skyplot("sky", from_="passes", selection="pass", show="selected", w=250, h=250, min_el=15,
                           rings=[30, 60]),
                 p.column(p.text(item("summary")), p.text(item("whyText"), tone=item("planTone")),
                          p.text(item("hint"), tone="muted", size="s"), id="passInfo", for_="pass", gap="xs"),
                 gap="s", weight=1),
        gap="l")


def live_tab() -> dict:
    return p.row(
        p.skyplot("liveSky", from_="passes", show={"key": None}, w=390, h=390, min_el=15, rings=[30, 60], now=None),
        p.column(
            p.badge("Nothing up", id="liveState", style="pill", tone="muted"),
            p.text(None, id="liveSat", style="title", size="xl"),
            p.text(None, id="liveLine", tone="muted"),
            p.meter(None, id="snr", min=0, max=25, style="bar",
                    ticks=[{"at": 0, "label": "0"}, {"at": 10, "label": "10"}, {"at": 20, "label": "20 dB"}],
                    zones=[{"from": 0, "to": 3, "tone": "bad"}, {"from": 3, "to": 8, "tone": "warn"},
                           {"from": 8, "to": 25, "tone": "good"}]),
            p.row(p.field("Lines", None, id="lines"), p.field("Elevation now", None, id="elNow"), gap="m"),
            p.text(None, id="liveCountdown", style="numeric", size="xl"),
            p.image(None, id="livePicture", w=190, h=250, refresh_s=4, fit="contain", alt="The picture so far"),
            id="liveInfo", gap="s", weight=1),
        gap="l")


def gallery_tab() -> dict:
    card = p.card(p.stack(
        p.image(item("thumb"), fit="cover", align="fill", hero=item("id"), rotate=item("rotate"), alt=item("label")),
        p.column(p.text(item("label"), style="title", size="s"),
                 p.text([item("when"), item("maxElText")], size="s", tone="muted"),
                 align="bottom-fill", surface="scrim", pad="s")),
        surface="card", action=A.open("radio/wxsat/viewer", {"image": item("id")}))
    return p.grid(id="galleryGrid", from_="images", columns={"tv": 4, "tablet": 3, "phone": 2}, aspect=1.25, gap="m",
                  empty=p.empty("No pictures yet: they appear after a pass is decoded", icon="picture"),
                  template=card)


def map_tab() -> dict:
    return p.map(
        "wxmap", ground=GROUND, attribution="© OpenStreetMap contributors",
        view={"center": [52.0, 2.0], "zoom": 5},
        views=[{"center": [52.0, 2.0], "zoom": 5, "label": "Europe"}, {"center": [55.0, -3.5], "zoom": 7, "label": "UK"}],
        keys={"down": A.part("wxmap", "view", hint="Europe or UK"), "ok": A.part("wxmap", "play", hint="Play"),
              "play": A.part("wxmap", "play"), "left": A.part("wxmap", "prev", hint="Earlier"),
              "right": A.part("wxmap", "next", hint="Later")},
        layers=[p.layer("frames", "images", from_="frames", frame=-1, alpha=0.92, fade_ms=600, play_ms=1400,
                        scrubber=True, empty="Pictures from the last day appear here once passes are projected")])


def wxsat(req: Req) -> Screen:
    s = Screen("radio/wxsat", title="Weather Sats", req=req)
    s.backdrop("aurora")
    s.words({"s": "{n}s", "min": "{n}m", "h": "{n}h", "d": "{n}d"})
    s.colors({k: {"dark": d, "light": li} for k, (d, li) in COLORS.items()})
    s.symbols({"sat": SAT})
    for c in ("passes", "images", "frames"):
        s.collection(c)
    s.selection("pass", from_="passes")
    # Polled, not live: 5 s now, and the answer's everyS moves it to 2 s
    # while recording and 10 s when idle (§4.5).
    s.poll("ui/v2/poll/radio/wxsat", 5)
    s.root = p.column(
        p.row(p.icon(sym="sat", tone="sat.n23", size="l"),
              p.column(p.text("Weather Sats", style="title", size="xl"),
                       p.text("from IO75rl", id="elements", tone="muted"), weight=1),
              p.text(T.clock("local+utc", "hm"), style="numeric", size="l"),
              gap="m"),
        p.tabs("tabs", [p.tab("passes", "Passes", passes_tab()), p.tab("live", "Live", live_tab()),
                        p.tab("gallery", "Gallery", gallery_tab()), p.tab("map", "Map", map_tab())],
               selected="passes", focus_selects=True, weight=1, badges={"live": None}),
        p.hints(style="pills"),
        pad="l", gap="m")
    return s


PRODUCTS = [("rgb", "RGB", "AVHRR 221 False Color"), ("ir", "IR", "10.8um Thermal IR (Uncalibrated)"),
            ("msa", "MSA", "MSA"), ("ch1", "Channel 1", "Channel 1")]


def viewer(req: Req, image: str = "meteor-m2-3-20261030T0919Z") -> Screen:
    """One decoded pass: a product strip along the bottom, the picture
    turned for a northbound pass, a glass caption. The box fills in the
    pass's own words; these are the worked example's."""
    s = Screen("radio/wxsat/viewer", title="Meteor-M N2-3, 09:19", req=req)
    s.backdrop("plain")

    def page(pid, label, alt):
        img = p.image(f"api/wxsat/images/{image}/{pid}.jpg?w=1920", fit="contain", rotate=180,
                      **({"hero": image} if pid == "rgb" else {}), alt=alt)
        return p.tab(pid, label, img)

    s.root = p.stack(
        p.tabs("products", [page(*x) for x in PRODUCTS], align="fill", selected="rgb", focus_selects=True,
               place="bottom", style="pills", focus={"initial": True}),
        p.column(p.text("Meteor-M N2-3", style="title", size="l"),
                 p.text(T.time("2026-10-30T09:19:21Z", "local+utc", "dayhm"), tone="muted"),
                 p.text("Max elevation 41.9° · 1400 × 1820", tone="muted", size="s"),
                 align="top-start", pad="m", surface="glass"))
    return s
