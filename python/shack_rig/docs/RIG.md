# Driving a radio from an OpenHamShack app with `shack_rig`

`shack_rig` is the rig-control library the per-maker rig apps share
(PLATFORM-PLAN §1.4): transports, the CI-V codec, a maker-neutral `Rig`
surface with an Icom backend, and a hamlib rigctld server. It was extracted
from OpenHamShack Icom (`~/dock360-apps-icom-9700`, branch `icom`, 24a343a),
so most of this page is that app's adoption; the first section is for any app.

## 1. Any app: build a rig, read its state, key it under your lease

```python
from shack_rig import Lease, PttRefused, RigctlServer
from shack_rig.icom import make_rig

rig = make_rig({"model": "ic-9700", "host": "198.51.100.150", "username": "op",
                "audio_rate": 12000}, password=secret, tx_enabled=tx_switch)
await rig.start()                      # the supervisor connects, and reconnects for ever
rig.listen(lambda state: push(state))  # every change; keys are shack_rig.STATE_KEYS
rig.set_freq(144_174_000)              # False = did not reach the radio
rig.set_mode("USB-D")                  # or set_mode("USB", data=True)
stop = rig.listen_audio(decoder.feed)  # 16-bit mono PCM at rig.state["audio_rate"]
stop = rig.listen_scope(waterfall.draw)
```

**Transmit takes a lease, every time.** Give the rig an object with a
`holder` string and a `refusal()` method that answers `None` while that
holder may transmit on this rig, and a sentence otherwise:

```python
class AppLease:                      # yours: your TX lease + the broker claim
    def __init__(self, holder): self.holder = holder
    def refusal(self):
        if not app.tx_switch:                    return "transmitting is switched off"
        if not app.lease_held(self.holder):      return "the lease expired"
        if not app.broker_claim_held("radio-1"): return "another app holds this radio"
        return None                              # cheap: called four times a second

try:
    rig.set_ptt(True, AppLease("digi FT8"))
except PttRefused as e:
    show(e.reason)
...
rig.set_ptt(False)                   # never needs a lease, never refused
```

What the library guarantees, and the tests that prove it (`tests/test_ptt.py`):

- No lease, a non-lease, or a refusing lease raises `PttRefused` and sends
  nothing. So do `allow_ptt` off (from `tx_enabled`), an unknown frequency,
  and a frequency outside the model's amateur bands.
- While keyed, the rig re-asks `refusal()` at the top of every poll and
  unkeys on the first refusal. A `refusal()` that raises counts as one.
- `set_ptt(False)` is always sent; if the link is down it is owed and sent
  the moment the link is back.
- `send_audio()` is refused unless the rig is keyed under a live lease (VOX
  with a LAN/USB modulation source would otherwise key on audio alone). Key
  first, then send audio, then `tx_end()`, then unkey: the order the Icom
  app's transmitter already uses.
- A radio keyed at its own front panel is reported in `state["ptt"]` and left
  alone: that is the operator, not a lease.

`shack_rig.Lease(holder, seconds=5, claim=callable)` is a reference
implementation with REMOTE-API §7 timings, for a desktop app with no lease of
its own yet and for tests.

**rigctl for third-party software** (WSJT-X "Hamlib NET rigctl"):

```python
server = RigctlServer(rig, "0.0.0.0", 4532, lease=lambda holder: app.grant(holder))
await server.start()
```

`lease(holder)` is asked on each client's `T 1` (`holder` is
`"rigctl <ip>:<port>"`); return a TxLease or `None`. Leave it out and the
server is receive-only (`T 1` answers `RPRT -11`). `T 0` unkeys and calls the
lease's `release()` if it has one; a client whose socket closes while keyed
is unkeyed at once.

**The scope's settings** (Icom; 10/10/2026): `rig.state["scope_settings"]`
is `{receiver, mode, span_hz, edge, ref_db, speed, vbw, averaging}` as the
radio reports them (None each until answered, None whole on a model without
`model.scope`), read once a second while a scope listener is subscribed.
`rig.set_scope_settings({"span_hz": 50_000, "ref_db": -5.5, ...})` sends
any subset; `rig.scope_setting_refusal(settings)` says why one would be
refused, and then nothing is sent. Spans are full widths
(`model.scope["spans_hz"]`); averaging is the one 1A 05 write the library
makes, through a builder fixed to item 0192.

**D-STAR call signs** (Icom, in DV): `rig.state["dv"]` is `{caller, suffix,
ur, rpt1, rpt2, message, receiving, packet_loss, heard_at}`, None until the
radio reports a call. Read (20 00/01/02 02) only while in DV; the radio's
own automatic-output settings (20 0x 00) are never written, so there is
nothing to put back. `heard_at` moves when a new over starts.

**Tests without a radio:** `shack_rig.icom.fake.FakeIcom` is an IC-9700 on
127.0.0.1 speaking the LAN protocol (login, CI-V, audio, scope, PTT); point a
profile's `host`/`port` at it. It records `civ_commands`, `ptt_commands` and
`stream_requests` so a test can check what reached the "radio".

## 1a. Added for the packet radio links (18/10/2026)

All additive; nothing an existing caller does changes.

- **Icom, for a packet station**: `rig.watch_squelch(True)` reads the
  squelch (`15 01`) into `state["busy"]` on every poll; `rig.set_simplex()`
  (`0F 10`, `16 42/43/4B 00`); `rig.read_data_mod()`, `set_data_mod("usb"|
  "lan")`, `restore_data_mod()` - DATA MOD only, only from the model's
  `data_mod` row (IcomModel.data_mod: the IC-9700's from the CI-V guide p. 7;
  the IC-705 has none, so nothing is written), only after it has been read.
- **USB transmit audio** (TRANSPORTS.md stage three): profile `tx_audio`
  `alsa` (aplay, the appliance) or `portaudio` (`tx_audio_device`, a
  desktop), honoured only with `tx_enabled`. 12 kHz in, the codec's rate out
  (`txaudio.py`). Without it `tx_rate` stays 0, as before. `audio_backend:
  portaudio` captures through PortAudio for a desktop.
- **`shack_rig.sound`**: devices by name, the device's own rate, `""`
  refused; `FakeBackend` for tests.
- **`shack_rig.yaesu`**: `YaesuRig(device, "ftx-1", ptt="cat"|"rts"|"dtr",
  ptt_device=..., audio_in=..., audio_out=...)`, the same Rig surface and
  lease rules as IcomRig, threads instead of asyncio; `fake.FakeFtx1`.
- **`shack_rig.kenwood`**: `KenwoodRig(device, "tm-v71", ptt="rts"|"dtr"|
  "cm108"|"vox"|"none", ptt_device=..., audio_in=..., audio_out=...)`: the
  TM-V71's PC port for control (LA3QMA's command list, hamlib tmd710.c,
  CHIRP), packet through a sound-card interface on its DATA jack, keyed by
  the interface and never by CAT `TX`. `datasetup.settings(rig,
  frequency=, speed=1200|9600)` sets the band, VFO, FM simplex and EXT. DATA
  BAND/SPEED (a whole-menu `MU` write, the radio's own answer with one
  field changed). `fake.FakeTmV71`. Not yet checked against a radio.
- **`shack_rig.cm108`**: `Cm108Ptt(device, gpio=3)`, PTT on a CM108/CM119
  chip's GPIO pin (Dire Wolf cm108.c's HID report), `set_line` like a
  serial port's, low on open.
- **What the operator changes stays theirs** (21/10/2026, Packet Radio
  0.3.2): `DataSetup.restore()` and `recover()` put a setting back only
  while the radio still holds what the set-up wrote (`apply`, `adjust`);
  one the operator changed on the radio meanwhile is left and reported
  `"kept"`. The value written is journalled beside the one found; an older
  journal is put back whole. `Setting(app_moves=True)` for a setting the
  app keeps moving itself (a satellite pass's Doppler-stepped VFOs).
  `FakeIcom.turn_dial(hz, mode)` turns the pretend radio's knobs and sends
  the CI-V transceive frame a real 9700 does. Packet links key only on the
  channel the app confirmed, in FM (shack_packet RigLink: `tuned()`,
  `confirm_channel`, `tune_channel`, refusals `off-channel` and
  `off-mode`).
- **Serial**: `SerialPort(..., stop_bits=2)`, `set_line("rts"|"dtr", on)`
  for a PTT driver, `read_wait()`; pyserial on Windows, opened with both
  lines low.

## 2. OpenHamShack Icom: the adoption steps (milestone M3)

Do these in the app repo on a branch, one commit each, running the app's
selftests after each. Pin `shack-rig` per REPO-LAYOUT §4 (exact version and
hash); develop against `pip install -e ~/ohs-libs/python/shack_rig`.

1. **Imports.** Delete the moved files from `app/` and import instead:

   | App module | Library |
   |---|---|
   | `app/civ.py` | `shack_rig.icom.civ` |
   | `app/radios.py` | `shack_rig.icom.models` (`Model`, `model()`, `MODELS`, `ALL_MODES`, `DEFAULT_MODEL` unchanged; `DATA_CAPABLE` and band ranges re-exported from `shack_rig.model`) |
   | `app/icomlan.py` | `shack_rig.icom.lan` |
   | `app/link.py` | `shack_rig.icom.link` |
   | `app/icomusb.py` | `shack_rig.icom.usb` |
   | `app/usbprobe.py` | `shack_rig.icom.usbprobe` |
   | `app/serialio.py` | `shack_rig.serialio` |
   | `app/usbaudio.py` | `shack_rig.usbaudio` |
   | `app/radio.py` (`Radio`) | `shack_rig.icom.rig` (`IcomRig`; `Radio` is an alias) |
   | `app/rigctl.py` | `shack_rig.rigctl` |
   | `tools/fakeicom.py` | `shack_rig.icom.fake` |

   `settings.py` imports `TRANSPORTS`, `AUDIO_RATES` and `BAUDS`; take
   `TRANSPORTS` from `shack_rig.icom.build` rather than keeping a second copy.

2. **Build rigs with `make_rig`.** In `registry.py`, `Radios._build` and
   `_usb_link` become a call to `shack_rig.icom.make_rig(profile,
   password=..., tx_enabled=self.tx_enabled, poll_interval=POLL_MS/1000,
   scope=SCOPE_ENABLED, client_name=RADIO_CLIENT_NAME default,
   usb_cache=settings.path.with_name("usb-probe.json"),
   busy_ports=self.busy_ports)`. The env fallbacks (`env_radio()`), the
   stored password lookup, profiles, selection and `DigiSession` stay in the
   app. `BAND_SPOTS`/`band_buttons` move to `shack_rig.band_buttons`.

3. **Give every keying path a lease.** Three call sites key today:
   - `main.py` `set_ptt` (`POST /api/ptt`): pass the lease the request holds
     (REMOTE-API §7 / CONTROLLER-CONTRACT §6.1), map `PttRefused` to 403 with
     `e.reason`; the handler's own `allow_ptt` and `can_transmit` checks can
     go, the rig makes both.
   - `digi/transmit.py` `_send` (`self.radio.set_ptt(True)`): the transmitter
     needs a lease for its holder ("digi FT8"), taken when the operator arms
     and checked by the rig for the whole over. `PttRefused` already lands in
     its `except Exception` and fails the item with the reason. Its own
     `allow_ptt` gate at line ~287 can stay as a UI hint.
   - `digi/selftest_digi.py`'s `FakeRadio.set_ptt(on)` grows `lease=None`.
   - The rigctl server: construct with `lease=` (step 4).

4. **rigctl.** `RigctlServer(None, bind, port, model_key=..., lease=app.rigctl_lease)`.
   `_attach` may keep setting `radio` and `model_key`; with `model_key=None`
   the server describes `radio.model` itself. Until the app has a lease to
   hand out, leave `lease` out: WSJT-X can still tune and read, and `T 1`
   answers "function not available" instead of keying unasked.

5. **Selftests.** `tools/selftest.py`'s civ/rigctl/LAN/spectrum/data-mode
   sections, `selftest_usb.py` and most of `selftest_usbaudio.py` now live in
   `ohs-libs/python/shack_rig/tests/`; delete the app's copies rather than
   keeping two. Keep the app-level ones (`selftest_radios.py`,
   `selftest_lanstreams.py`'s HTTP half, `selftest_txlan.py`, digi), pointing
   their imports at the library.

## 3. What changed on the move, and what did not move cleanly

Behaviour changes (all deliberate; each is in a commit message):

- `set_ptt(True)` needs a lease and raises `PttRefused` for every policy
  refusal (it returned False for `allow_ptt` off). It also refuses with the
  frequency unknown, where the app used to send the key anyway.
- `send_audio` is refused unless keyed under a lease.
- rigctl keys only through `lease=`, and unkeys a client that disconnects
  while keyed.
- Loggers are `shack_rig.*` (`radio`, `rigctl`, `icomlan`, `icomusb`,
  `usbprobe`, `usbaudio`, `serialio`, `link` before). Log filters keyed on
  the old names need updating.
- `LanLink`'s default client name is `OpenHamShack` (was `dock360`); the app
  always passes `RADIO_CLIENT_NAME`, so only code relying on the default sees it.
- `Model.public()` gains `"maker": "icom"`. `dump_state` says
  `rigctld_version=shack-rig-<model>` (was `dock360-<model>`).

Not moved, or not cleanly:

- **The registry** (`registry.py`): profiles, selection, switching,
  `Live.public()` and the per-radio `DigiSession` are the app's model of its
  own UI, not rig control. Only the profile-to-rig rules moved (`make_rig`).
- **The transmit audio sink contract** (`TxAudioSink` in `digi/transmit.py`)
  stays with the transmitter; the rig still satisfies it.
- **Transmit over USB** was never written (the app's TRANSPORTS.md stage
  three, `aplay`); `UsbLink.tx_rate` is 0 here as it was there.
- **The keying band check uses the selected VFO.** Under split the radio
  transmits on the other VFO, which the cache does not track; the floor can
  refuse or allow on the receive frequency. The app's lease and band-plan
  check is where the transmit frequency should be judged.
- **Comments cite the app's documents** (`docs/TRANSPORTS.md`,
  `docs/OFF-AIR.md`, `docs/USB-AUDIO.md`, `docs/STANDALONE-PI.md`) and its
  modules; they stay citations to `~/dock360-apps-icom-9700/icom/`.
- **Two source-text tests were not ported as written**, because house rules
  forbid tests that read source. `selftest.py`'s big-endian port reads are
  covered by the FakeIcom tests, which cannot connect unless those reads are
  right. `selftest_usb.py`'s "nothing above the radio speaks CI-V" became a
  behavioural test: a real `RigctlServer` drives an `IcomRig` on a `UsbLink`
  on a pty, unmodified (`test_rigctl_drives_a_rig_on_a_serial_port_without_knowing_it`).
- **The off-air recording stays in the app.** `test_usbaudio.py` uses it when
  `SHACK_RIG_OFF_AIR` names `40m-ft8-260919-161015.wav`, and skips otherwise.
- **The FT8 decodes** (`test_audio_path`, the jt9 check in the USB-audio
  test) need the app's decoders and stay there.
