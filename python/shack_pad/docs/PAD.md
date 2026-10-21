# Adding the pad to an OpenHamShack app (five minutes)

`shack_pad` drives the Ajazz AKP03E (six picture keys, three buttons, three
knobs) and USB keypads for every OpenHamShack app. Your app does three
things: **declares** what the pad can do, **performs** those actions, and
**hands over its transmit lease**. The library does the rest: the HID
protocol, key pictures, the operator's remaps, which app gets the pad, and
transmit safety. The design is `docs/contract/CONTROLLER-PAD.md` in
`~/openhamshack`.

## 1. Declare your actions: `pad-actions.json`

```json
{
  "schema": 1,
  "app": "qo100nb",
  "actions": [
    {"id": "tune", "kind": "turn", "label": "pad.tune", "group": "receive", "short": ["TUNE", "Hz"]},
    {"id": "step", "kind": "press", "label": "pad.step", "group": "receive", "short": ["STEP", ""]},
    {"id": "mute", "kind": "press", "label": "pad.mute", "group": "audio"},
    {"id": "ptt", "kind": "press", "tx": true, "hold": true, "label": "pad.ptt", "group": "transmit"}
  ]
}
```

- `kind` is `press` or `turn`. Every turn also gives `<id>:up` and
  `<id>:down` presses for keypads; you still only receive the turn.
- `label` is a catalog key (I18N.md), never English.
- Use the shared ids where they fit (`tune`, `step`, `band`, `band:next`,
  `mode:next`, `channel`, `mute`, `volume`, `record`, `scope`,
  `receivers`, `power`, `ptt`, `transmit`), so a remap learned in one app
  carries to the next.
- Transmit: only `ptt` (`hold: true`, keyed while held: voice and CW) or
  `transmit` (`arm: true`, two presses within 4 s, one press stops: long
  transmissions such as DATV). Both are always `single`.
- `needs: ["x"]` hides an action when your app says it lacks capability `x`.

Check it in your own tests, so a mistake fails your build:

```python
import json, shack_pad
shack_pad.validate_actions(json.load(open("pad-actions.json")))   # raises DeclarationError
```

## 2. Give it a default layout: `pad-layout.json`

```json
{
  "schema": 1,
  "ajazz-akp03e": {
    "K1": {"press": "ptt"}, "K2": {"press": "mute"}, "K6": {"press": "step"},
    "B3": {"press": "page"},
    "D2": {"press": "step", "turn": "tune"}
  },
  "keypad": {"KPENTER": {"press": "ptt"}, "KPPLUS": {"press": "tune:up"}, "KPMINUS": {"press": "tune:down"}}
}
```

Transmit goes on **K1** (pad) and **KPENTER** (keypad) in every app. App
switching, where offered, goes on **B3**. Anything the library cannot use
becomes "nothing" with a note rather than an error. Full examples:
`src/shack_pad/examples/`; JSON Schemas: `spec/schemas/pad-actions.schema.json` and
`pad-layout.schema.json` at the repository root.

## 3. Wire it up (desktop app)

```python
import json
from shack_pad import DesktopPad, InProcessApp, InputHost, LocalLease

def on_pad(action):                     # a PadAction: action, kind, step, control, model, pad
    if action.action == "tune":
        radio.tune_by(action.step * radio.step_hz)
    elif action.action == "mute":
        audio.toggle_mute()

def pad_faces():                        # optional: what keys show, already translated
    return {"onAir": tx.on_air,
            "faces": {"tune": {"l1": f"{radio.mhz:.3f}", "l2": "MHz", "tone": "normal"},
                      "mute": {"l1": tr("MUTE"), "l2": "", "tone": "active" if audio.muted else "normal"}}}

# Your transmit lease. If you already have one with key(remote) ->
# {"onAir", "txError"}, heartbeat(remote) -> bool and unkey(remote), pass it.
# Otherwise LocalLease wraps your own go-on-air/stop with the 5 s rule:
tx = LocalLease(on_key=station.try_key, on_unkey=station.unkey)   # try_key returns None or the refusal
tx.start()

app = InProcessApp("qo100nb", tr("NB"), json.load(open("pad-actions.json")),
                   json.load(open("pad-layout.json")), perform=on_pad, faces=pad_faces, lease=tx)
host = InputHost([app], maps=profile.get("controllerMaps"),
                 on_maps=lambda m: profile.set("controllerMaps", m))   # remaps live in the station profile
pad = DesktopPad(host, app="shack-qo100nb")
pad.start()
window.focusInEvent = lambda e: pad.focus_gained()   # whichever app the operator uses gets the pad
app_exit.connect(pad.close)
```

`try_key` is where your app makes the same checks it makes for any client
(transmit switch, `shack_bandplan.check`, broker claim). The pad is only
ever a client of that lease.

On Windows and macOS there is no keypad grab: feed your window's keys to
the same map with `host.handle("keyboard", ("press", "KPENTER", 0))` and
`("release", …)`; key names are Linux's (`KPENTER`, `KPPLUS`, `F13`…).

## 4. On the controller (platform)

You write no pad code beyond the two JSON files and the actions: the
platform's `shack-input` service owns the pad and runs an `InputHost` with
every app wrapped in a `PadApp` that talks to your app's HTTP API (the
action and faces endpoints of CONTROLLER-PAD.md §6.1, and your ordinary
`api/ptt` lease). Name the two files in your manifest
(`openhamshack.actions`, `openhamshack.controller_layout`).

## What the library guarantees

- A transmit key only ever calls your lease: key, heartbeat every 1 s,
  unkey. A refused key-up is shown on the key.
- **Pad pulled while keyed: it unkeys first, then closes** (under 100 ms in
  the tests). A pad that comes back never re-keys.
- If the library itself dies, its heartbeats stop and your lease unkeys
  at 5 s.
- The transmit key shows ON AIR whenever your app is on air, keyed from
  anywhere, and cannot be remapped while you are on air.
- An app on air keeps its transmit key on its control even when another
  app is in front; a held PTT delays an app switch until released.
- Key auto-repeat never counts as a press.

## Telling a remote operator what is wrong

A pad that does nothing is the worst outcome for someone nobody can visit.
Give the operator a page with three things (QO-100 NB and Radio have it in
Settings > Control pad):

- `pad.status()`: `serving`, `waiting` (another OpenHamShack app has it),
  `missing`, `error` or `idle`, with `error` already a sentence that says
  what to do (`shack_pad.diagnose.explain`: "close the Ajazz or Mirabox
  Stream Dock software", "the udev rule is missing", "unplugged").
- `host.start_test()` / `stop_test()`: every key lit in turn, then each
  press shown on its own key; events reach `host.listeners` for a live
  list, and nothing reaches your app or the lease. Refused while on air.
- `diagnose.report(diagnose.survey(), [your lines])`: one text to copy:
  every Ajazz/Mirabox device and HID interface with its usage page, which
  one the driver opens, the report sizes it declares, hidapi/Pillow/font,
  the makers' software if running, and the problems as sentences. No
  serial numbers. Put `diagnose.describe_libraries()` in your start-up
  facts too: a frozen build that lost hidapi or Pillow then says so.

On Windows the pad is found by its vendor collection (usage page 0xFFA0,
usage 1, as mirajazz/opendeck-akp03 query), not by interface number, and
Windows lets the makers' software open it at the same time; `survey()`
looks for that software because nothing else would.

## Testing your app with no pad

```python
from shack_pad.drivers import AKP03E
from shack_pad.fake import FakeHid, akp03e_report

fake = FakeHid([akp03e_report(0x02, 1), akp03e_report(0x91)])   # K2 down, D2 one detent clockwise
dev = AKP03E(fake)
host.attach(dev)
host.step(dev, 0); host.step(dev, 0)                            # your on_pad got mute, then tune +1
fake.unplug()                                                   # next step raises OSError; then
host.detach(dev)                                                # unkeys (if keyed), blanks, closes
```

## Not done or not verified yet

- Windows and macOS: the hidapi transport, font paths and `msvcrt` lock are
  written but have never run with a pad on those systems. The interface
  choice is tested against enumerations shaped as hidapi builds them on
  each (tests/test_diagnose.py), not against a real Windows enumeration.
- No real pad was used: everything is tested against `FakeHid` with the
  reports captured from workshop's pad on 3 Oct 2026.
- Action glyphs are drawn in code (the brand kit has none yet); a PNG named
  `icons/<name>.png` takes precedence if the brand build ever makes them.
- The library's own words (ON AIR, ARM, PAGE) go through gettext domain
  `shack-pad`; no second-language catalog exists yet.
