# shack-ui

Server-driven screens, **ui/2** (openhamshack `docs/contract/SCREENS.md`,
decided by Brian 30/10/2026: "launcher and apps were just backend
changes"). The box describes every screen as a document of generic parts
with data; the OpenHamShack client draws it natively and keeps it current
from a live socket of patch ops. A new screen, map layer or symbol ships
with the box app and needs no client release.

This replaces ui/1 (CLIENT-UI-PLAN.md §5), which no client ever drew; its
wheel is 0.3.0.dev1 so it never shares a number with ui/1's 0.2.0. Pure
Python, standard library only at run time (jsonschema for the tests and
`schema_errors`).

| Module | What |
|---|---|
| `build` | `Screen`; the part builders `p.*` (every part of §3); actions `A.*`; typed values `T.*`; `item()`, `local()`; `descriptor()`, `entry()`, `status()` for the launcher; `downgrade()` for older clients |
| `serve` | `Req.from_headers()`: what the client is (form, window, ui minor, language, client id, device, level); `serve_document()`: 406 / 304 / 200 with `ETag`, for any web framework |
| `live` | `LiveHub`, its channels and sessions: a document per rev, coalesced frames at the client's `maxHz`, snaps, per-client extras (`clients`, `refresh`), snap poll answers, pings; `serve_blocking` (http.server sockets) and async `serve` (aiohttp) |
| `holds` | `Holds`: a hold whose stop needs the id its start returned, kept by the box on `ui/v2/hold/<name>` |
| `schema` | `schema()`, the packaged `ui2.schema.json`; `schema_errors(value, ref)` |
| `check` | the validator: `check(doc)` returns every `Problem` (code, where, message) in document order; the rules every renderer enforces too |
| `ops` | the reference op applier: `state_from_document`, `apply_ops`, `apply_frame`, `drawn_keys`, `public_state`; for an app's tests to assert what a client would see |
| `symbols` | `Symbols.load("aircraft" \| "ships" \| "aprs" \| "sondes" \| "station")`, `.only()`, `.for_aprs("/>")`; `icon_names()`, the client's icon set |

The schema is `spec/schemas/ui2.schema.json` and the vectors every
implementation runs are `spec/vectors/ui2/` (its README is the format, for
the Kotlin and Swift renderers). `tools/gen-ui2-vectors.py` writes them.

## A screen, end to end

The worked example is `examples/radio_aircraft.py`: Radio's Aircraft
screen on a TV, which builds exactly the contract's
`screens-fixtures/screens/radio/aircraft.json`
(`tests/test_example_aircraft.py` holds it to it, field for field). In
short:

```python
from shack_ui import A, LiveHub, Req, Screen, Symbols, T, item, local, p, serve_document

def aircraft(req: Req) -> Screen:
    s = Screen("radio/aircraft", title=_("Aircraft"), req=req)
    s.colors(BAND_COLOURS)
    s.symbols(Symbols.load("aircraft") + Symbols.load("station"))
    s.collection("aircraft", filter={"hide": local("adsb.hide", []), "maxAgeS": local("adsb.forgetS", None)})
    s.selection("aircraft", from_="aircraft")
    s.hold("adsb", start=A.post("api/adsb/session", {"client": A.CLIENT}, level="control", timeout_s=30),
                   stop=A.delete("api/adsb/session?client={$client}", level="control"))
    s.live("ui/v2/live/radio/aircraft")
    s.key("menu", A.show("menu", hint=_("Menu")))
    s.root = p.stack(
        p.map("map", align="fill", ground=GROUND, layers=[
            p.layer("aircraft", "markers", from_="aircraft", selection="aircraft",
                    labels=local("adsb.labels", "all"), trails={"fade": True, "width": 2.5})]),
        p.panel("nearest", p.list("list", from_="aircraft", selection="aircraft", keep_focus=True,
                                  on_activate=A.show("details"),
                                  template=p.row(p.badge(style="pill", tone=item("tone")),
                                                 p.text(item("title"), style="title"),
                                                 p.text(item("distText"), style="numeric"))),
                slot="end", w=300, open=True),
        p.panel("details", p.column(p.field(_("Heard"), [T.ago(item("at")), item("rssiText")])),
                slot="end", w=375, for_="aircraft", step=True),
        p.hints(id="hints", align="bottom-start"))
    return s

# The app's route for GET ui/v2/screen/radio/aircraft, in any framework:
resp = serve_document(request.headers, aircraft)     # 406, 304 or 200 + ETag
```

Then the moving data, from the app's own callbacks (any thread):

```python
hub = LiveHub()                                            # one per app process
feed = hub.channel("radio/aircraft", document=aircraft(Req()).json())   # made once; later calls change nothing
feed.up("aircraft", icao, lat=lat, lon=lon, rot=track, sub=alt_text, at=heard_iso)
feed.push("aircraft", icao, "trail", [[lat, lon, tone]], max=30)
feed.order("aircraft", nearest_first)                      # sent only when it changes
feed.rm("aircraft", icao)
feed.set("status", text=_("Receiving"), tone="good")
feed.on_select(lambda client, key: client.up("aircraft", key, photo=photo_for(key)), sel="aircraft")
feed.refresh("aircraft", icao)       # its photo arrived: run on_select again for whoever has it selected

# GET ui/v2/live/radio/aircraft, after the upgrade. `document` is the one
# this client fetched (the same build for its Req), so the hello carries
# its rev: a TV and a phone have different documents and different revs.
req = Req.from_headers(headers)
hub.serve_blocking(sock, "radio/aircraft", req, document=aircraft(req).json())   # Radio's ohscore.server.ws
await hub.serve(ws, "radio/aircraft", req, document=aircraft(req).json())        # aiohttp
```

A channel lives as long as the process unless `hub.remove(name)` drops it
(its clients' sockets close); `Channel.reset(document)` starts one again,
dropping its live data. A polled screen answers its poll with
`poll_answer(collections, parts, every_s=)`, or `feed.poll_answer(rev)`
from a channel: both are snaps (`"snap": true`), the whole state, applied
to the document's state, so an item no longer listed is gone with nothing
remembered per client.

And in the app's tests, what a client would see:

```python
from shack_ui import apply_frame, drawn_keys, state_from_document
start = state_from_document(doc)
s = feed.connect("test")
state = apply_frame(start, s.frame(0.0), start)      # the snap
feed.up("aircraft", "F00202", lat=55.5, lon=-4.4)
state = apply_frame(state, s.frame(1.0), start)
assert drawn_keys(state, "aircraft") == ["F00202"]
```

`examples/radio_wxsat.py` builds the Weather Sats screen and its picture
viewer the same way (both fixtures exactly: tabs, sky plots, a polled
screen); `examples/radio_screens.py` sketches Radio's other screens (home,
DAB, FM, Ships, APRS, Sondes, DMR) to show every element they need has a
builder.

## What the builder takes care of

- **Names.** Keywords are the contract's names in snake case
  (`on_activate=` is `onActivate`, `timeout_s=` is `timeoutS`, `from_=` is
  `from`); camelCase passes through. Nested dicts (a map's `view`) and
  item data keep their names.
- **Checks where the line is written.** Every action is checked as it is
  made: `A.post("api/cq", level="transmit")` raises `TransmitActionError`
  at that line, as does a `..` path or a `$token` placeholder. A call has
  no default level. `Screen.json()` then runs every rule over the whole
  document and raises `UIError` naming each problem.
- **Transmit.** Only `p.ptt` and `p.tx_arm` key, and their timings are
  client code (only `hold_ms`, never below 1000). An action that changes
  what a keyed or armed transmitter sends is `tx=True, lease="<ptt id>"`;
  an item's own action never transmits; no call may reach a key route.
- **Older clients.** `Screen.json()` sends each part newer than the
  client's minor as its `fallback`, or leaves it out (§2.3). Give a new
  part `since=` and a fallback, or add its type to `build.PART_SINCE`.
- **Silence is the restricted case.** A request with no form is a TV, no
  level is view, no transmit header falls back to the level alone.
- **The level header is the proxy's.** Only read it on a request already
  checked to carry the app's proxy secret (CONTROLLER-CONTRACT §5.3).

## Holds

`s.hold(name, start, stop, renew_s=None)` declares what the screen keeps
on the box while it is open (§6): the client runs `start` when the screen
is pushed, comes back or reconnects, and `stop` when it is popped or goes
to the background; it renews every held name through presence, or
repeats `start` every `renew_s` for an app without presence. A refused
start's error `actions` become the remedy buttons of the hold banner.

When the stop needs what the start returned (a decoder session's id),
let `Holds` keep it rather than the client:

```python
holds = Holds()
holds.define("dmr", start=lambda client, req: start_dmr(client)["id"], stop=stop_session)
s.hold("dmr", *holds.actions("dmr"))                  # POST / DELETE ui/v2/hold/dmr
resp = holds.answer(method, "dmr", client, req)       # the app's route
holds.gone(client)                                    # presence says it went
```

## Transmit-safety summary

Nothing here keys a radio, and no test needs one. The rules above are
enforced three times: by the builder at the line, by `Screen.json()` on
the document, and by each renderer on what it receives.
