# shack-pad

The OpenHamShack controller library: the Ajazz AKP03E and USB keypads in
every OpenHamShack app, on the controller and on Linux, Windows and macOS
desktops. Design: `~/openhamshack/docs/contract/CONTROLLER-PAD.md`.
Adopting it in an app: [docs/PAD.md](docs/PAD.md).

- `drivers/`, `transport/`: the AKP03E protocol and keypads, over hidraw
  (Linux), hidapi (Windows/macOS, untested there) or `fake.FakeHid` (tests)
- `actions.py`, `mapping.py`: `pad-actions.json`, `pad-layout.json`, the
  operator's remaps (`controllerMaps` in the station profile)
- `router.py`: `InputHost`, the pad to the app in front; `PadApp`, the
  interface an app presents
- `tx.py`: the pad as a transmit-lease client; unplugged while keyed unkeys first
- `focus.py`: desktop hand-over through a lock file
- `faces.py`: key pictures (Pillow), brand colours, system font

Core is stdlib only; `pip install shack-pad[hid]` adds hidapi and Pillow.
Licence GPL-3.0-or-later.

    tools/test.sh          # venv in ~/build/venvs/shack-pad, no hardware needed
