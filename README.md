# ohs-libs

Shared OpenHamShack libraries and data, one of each, released in lockstep
(design: `~/openhamshack/docs/contract/REPO-LAYOUT.md`). Code identifiers
use `shack`, never the initials OHS (brand kit rule); the repository keeps
this directory name until it is renamed deliberately.

| Path | What |
|---|---|
| `data/bandplan/` | Licence regimes, band plans, QO-100 plans, presets, countries, cty.dat: the JSON is the source of truth (BANDPLAN-DATA.md). `dataVersion` is CalVer, separate from the package version. |
| `data/bandplan-overrides/` | Plan rows ohs-libs transcribed from the PDFs itself (LF/MF, 3 cm, the RSGB 13/9/6 cm corrections), each citing its page; `export_ohscore.py` applies them, so they survive a re-export. Not shipped. |
| `python/shack_bandplan` | Loads the dataset; `check(context(profile), Signal(...))` answers "may I transmit this?" with a verdict. |
| `python/shack_client` | The local logbook and station profile (offline first), sync with a controller, discovery, pairing, and the controller's side of sync (`shack_client.controller`) for the Dock360 edition. |
| `python/shack_rig` | Rig control for the per-maker rig apps: serial, Icom LAN and USB transports, the CI-V codec, a maker-neutral `Rig` with an Icom backend, a rigctld server; transmit only under the app's lease (`python/shack_rig/docs/RIG.md`). |
| `python/shack_cw` | The CW decoder every receiving app shares (from QO-100 NB): `Decoder.feed(samples, rate)` at any sample rate, events in NB's shape, the Morse tables and keying envelopes, and the bench (`python -m shack_cw.bench`). numpy, the one package that is not standard library only. |
| `python/shack_dsp` | Signal processing for receive audio: NR2, the spectral noise reduction the rig apps run on the server (`Nr2Stream`: s16le in and out, switchable without a click), and its bench (`python -m shack_dsp.bench`). numpy. |
| `python/shack_packet` | The packet radio modem (Packet Radio app, Icom's APRS, the SDR monitor): Bell 202 AFSK receive (`Receiver`: 61 frames on direwolf's noise ramps where atest gets 54) and transmit (`modulate`), HDLC, the AX.25 frame codec for every frame type, FX.25 and APRS payloads, and its bench (`python -m shack_packet.bench`). numpy. |
| `python/shack_rec` | Recordings kept on the controller: receive audio as FLAC (our own numpy encoder) with a JSON sidecar, the recordings folder with its disk-use limit and free-space floor, range requests, and the API every app with recordings serves (`python/shack_rec/README.md`). numpy. |
| `python/shack_desktop` | The desktop form of the web-UI apps: the app's own server on loopback behind a per-launch token, in a native window (pywebview) or the browser (`python/shack_desktop/docs/DESKTOP.md`). |
| `python/shack_i18n` | `shack-i18n-check` and locale-aware formatting (I18N.md). |
| `rust/shack_sdr` | Radio's SDR decoders in Rust with a Python face, one wheel `shack-sdr` (RADIO-SDR-PLAN): the decoder host (threads, rings, CPU budget), one catalogue including the Python-backed decoders, RTTY, the runtime codec loader (codec2 shipped; faad2, fdk-aac and mbelib only ever the user's own) and the receive-only RTL-SDR and SDRplay sources moved from `shack_packet.sdr` (`rust/shack_sdr/README.md`). `rust/` is the Cargo workspace. |
| `kotlin/` | Gradle build: `shack-client` (store, merge, sync, pairing, discovery parsing, `ShackStation`; SQLite through `SqlOpener` so Android brings its own) and `shack-i18n` (`ShackFormat`). Kotlin/JVM, no runtime dependency, usable from Android (minSdk 28). |
| `Package.swift`, `swift/` | Swift package: `ShackClient` (the same, on the system SQLite3, pairing in the Keychain) and `ShackFormat`. iOS 16, tvOS 16, macOS 13. |
| `spec/schemas/` | JSON Schemas for the dataset, the vector files and the pad action and layout declarations. |
| `spec/vectors/` | Language-neutral conformance vectors (bandplan, sync T1-T21, i18n formatting, captured AKP03E pad reports) that every implementation runs. |
| `tools/` | `test.sh` (everything, the Rust workspace included), `build.sh` (wheels), `build-sdr-wheels.sh` (the `shack-sdr` wheels, every platform), `check-encumbered.py` (nothing patent-encumbered linked or carried; denylist `spec/encumbered.json`), `extract_pot.py` (gettext templates), `export_ohscore.py` (the one-time conversion of the OpenHamShack modules). |

Not here yet: the `shack-i18n.js` web loader, `release.sh`, and an Android
SQLite adapter (Remote supplies it: `kotlin/shack-client` Sql.kt says how).

```
python3 -m venv ~/build/venvs/ohs-libs
~/build/venvs/ohs-libs/bin/pip install pytest jsonschema build zeroconf \
    pillow aiohttp numpy -e python/shack_bandplan -e python/shack_client -e python/shack_desktop -e python/shack_i18n \
    -e python/shack_pad -e python/shack_rig -e python/shack_desktop -e python/shack_cw -e python/shack_dsp -e python/shack_rec -e python/shack_packet
tools/test.sh
tools/build.sh ~/build/ohs-libs-wheels
```

The Kotlin and Swift suites run the same `spec/vectors` from the checkout.
Kotlin builds on shack (JDK 17 and Gradle 9.8.1 in `~/android-toolchain`;
copy `kotlin/` and `spec/` to a directory under `~/build/` and run
`kotlin/test.sh`). Swift builds on the Mac mini under
`/Volumes/OpenHamShackBuild` with `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`
and `TMPDIR` on that volume: `swift test`. `tools/test.sh` does not run
either, because neither toolchain is on the Python build host.

Licence: GPL-3.0-or-later.
