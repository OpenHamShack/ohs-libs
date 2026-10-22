# shack_sdr

OpenHamShack's SDR decoders, the host that runs them, the runtime codec
loader and the receive-only IQ sources: Milestone 1 of Radio's SDR plan
(openhamshack `docs/RADIO-SDR-PLAN.md`). One wheel, `shack-sdr`: a Rust
extension (`shack_sdr._native`) and a Python package around it.
GPL-3.0-or-later. Nothing here transmits.

## The Python API (what an app calls)

```python
from shack_sdr import Host, DecoderError, catalogue, codecs, register

host = Host(threads=1, cpu_max=0.25)          # also: ring_s=2.0, max_sessions=17
catalogue()   # [{"id", "family", "inputs", "rateHz", "bandwidthHz", "outputs", "panel", "params",
              #   "needs", "engine", "available", "why", "version", "fixed"}, ...]
sid = host.start("rtty", {"baud": 45.45, "shift": 170, "reverse": False},
                 input="iq", rate=12_000, center_hz=0.0, pinned=False)   # -> "ds_3f2a"
host.feed(sid, samples, t0, index)    # numpy complex64 (iq) or float32 (audio); copies, never blocks
host.poll(max_items=500)              # [{"session", "rev", "t", "type", ...}], oldest first; never blocks
host.status(sid)                      # {"id", "decoder", "input", "rate", "params", "pinned", "state", "why",
                                      #  "since", "rev", "stats": {"snrDb", "offsetHz", "baud", "sync", "cpu",
                                      #  "fed", "dropped", ...decoder counters}}
host.stats()                          # {"sessions", "threads", "cpu", "cpuMax", "queued", "lost"}
host.sessions(); host.reset(sid); host.set_outside(sid, True); host.stop(sid); host.close()  # close -> bool
codecs.configure(folder=None, app="openhamshack-radio", data_dir=None, bundle_dirs=(),
                 cache_path=None, probe_argv=None)
codecs.status(rescan=False)           # [{"id", "for", "state", "path", "version", "why", "detail",
                                      #   "where", "host", "shippable"}]
codecs.path("codec2")                 # a file that passed the probe, or None (FreeDV's library)
codecs.folder()                       # "Open codecs folder"; None on a controller
```

- **Results.** `type` is `text` (`text`, `offsetHz`, `snrDb`), `frame`,
  `image`, `audio`, `meta`, or `state` (`state`, `why`). `rev` rises by one
  per result per session, so a gap means something was missed. `t` is UTC
  seconds from the samples' `t0` and `index`, never the clock.
- **States:** `starting`, `running`, `nosignal`, `synced`, `outside`,
  `budget` (why `cpu_budget`), `error` (why a code, `panic` for a caught
  panic), `stopped`.
- **Refusals** raise `DecoderError(code, detail)`: `unknown_decoder`,
  `unknown_session`, `bad_params` (`unknown_param`, `bad_param`),
  `bad_input` (`input_kind`, `rate`, `centre`), `input_unsupported`,
  `too_many_sessions`, `decoder_unavailable` (`not_installed`, `mode_only`),
  `closed`. Codes only; the app words them.
- **Audio into an IQ decoder.** `input="audio"` with `center_hz` (where the
  signal sits in the audio, rtty.py's `center`) runs the host's analytic
  adapter, so RTTY on Icom's or Flex's audio is the same decoder as on
  Radio's channel IQ.
- **`pinned=True`** for the mode decoder: the CPU budget never pauses it.
- **Python-backed decoders** (CW through `shack_cw`; FT8, FT4, Q65, JS8
  through ohsdigi) are in the same catalogue with `engine` naming the
  library. CW runs as a session (`fixed: true`); the slot modes are mode
  decoders only in M1, and `register(id, factory)` gives one a runner.

### RTTY's parameters

`baud` 45.45, 50, 75; `shift` 170, 425, 450, 850; `reverse`; `afc` (on);
`afcRangeHz` 0-250 (100); `squelch` 0-20 dB (3; 0 = off); `usos` (on). Its
status counters: `frames`, `chars`, `rejected`, `gaps`, `replays`.

## Sources

`shack_sdr.sources`: `IqSource`, `IqBlock`, `SdrError`, `REASONS`,
`RtlSdrSource`, `SdrplaySource`, `cu8_to_complex`, `complex_to_cu8`, and
`rtlsdr.availability()`, `sdrplay.availability()` (`why`
`sdrplay_api_missing`, `host` on a controller), `RtlSdrSource.rates()`,
`SdrplaySource.rates()`. Moved from `shack_packet.sdr`, behaviour unchanged.

**Antenna/input selection** (added for Radio, RADIO-SDR-PLAN §6):
every source has `src.antennas()` (its input names, the first the
default), `src.antenna` (the current one) and `src.set_antenna(name)`
(switches live where the model allows it; refuses a name the receiver
does not have with `SdrError("bad-antenna")`, a `REASONS` key, never a
sentence). `RtlSdrSource` reports one input, `"RX"`, with nothing to
switch. `SdrplaySource.antennas()` depends on the RSP model `open()`
found ([] before that):

| Model | Inputs |
|---|---|
| RSP1, RSP1A, RSP1B | `"RX"` (one input; never switches) |
| RSP2 | `"Antenna A"`, `"Antenna B"`, `"Hi-Z"` |
| RSPdx, RSPdxR2 | `"Antenna A"`, `"Antenna B"`, `"Antenna C"` |
| RSPduo | `"Tuner 1 50 ohm"`, `"Tuner 1 Hi-Z"`, `"Tuner 2 50 ohm"` (Single Tuner mode; this module never asks for Dual Tuner or an API slave role) |

These are SoapySDRPlay3's own names and model mapping
(`Settings.cpp` `listAntennas`/`setAntenna`/`getAntenna`; MIT,
github.com/pothosware/SoapySDRPlay3, read 22/10/2026), so a setting saved
through either path means the same input. Switching never re-opens the
stream: an RSPdx or RSP2 input is one `sdrplay_api_Update` call, and an
RSPduo tuner change is `sdrplay_api_SwapRspDuoActiveTuner` (both of
`SdrplaySource`'s stream callbacks run the same code, because which
tuner the API now drives can change under that call). NOT YET RUN ON AN
RSP of any kind: see `sources/sdrplay.py`'s module docstring.

## Building and testing

`tools/test.sh` (repository root) runs `cargo test --release`, `cargo deny
check bans licenses`, these Python tests against an extension built from
the tree (`tools/dev-build.sh`), and `tools/check-encumbered.py` on it.
Wheels: `tools/build-sdr-wheels.sh` (repository root) into
`~/build/ohs-dist/shack-sdr/` with `SHA256SUMS`; never committed.

The RTTY fixtures (`testdata/rtty/`) are recipes for `testsig` pinned by
sha256, and `golden.json` is what openhamshack-nb's `rtty.py` printed on
them (`tools/rtty_golden.py`). `tests/rtty.rs` holds the Rust decoder to
never worse than it at any point of the sweep, and to §7.2's targets.
Two more there have no rtty.py figure: an over through a receiver's AGC
(the noise held down under it and coming back as the carrier drops, the
shape captured from openhamshack-nb's own-echo test) must print nothing
after the over, and 40 strong overs each followed by 8 s of noise may
print at most 5 false characters between them.

## Packet's switch-over (for the Packet session; nothing here changes shack_packet)

1. Depend on `shack-sdr` (pyproject `dependencies`), and vendor its wheel
   in Packet's installers as NB vendors ohsdigi's.
2. `sdr/source.py` becomes a re-export:
   `from shack_sdr.sources.source import LAG_LIMIT_S, REASONS, IqBlock,
   IqSource, SdrError, SourceStats, complex_to_cu8, cu8_to_complex`.
3. `sdr/rtlsdr.py` and `sdr/sdrplay.py` are removed;
   `sdr/__init__.py` imports `RtlSdrSource` and `SdrplaySource` from
   `shack_sdr.sources` (and anything that used the modules,
   `shack_sdr.sources.rtlsdr` / `.sdrplay`).
4. **`SdrError` is no longer a `PacketError`.** Every `except
   PacketError` that relied on catching a source failure must name
   `SdrError` too (in ohs-libs today, `radios/pluto.py` already catches
   both); check the Qt app in `~/shack-packet` the same way. `read()`
   before `open()` raises `SdrError("read-failed")`, not `PacketError`.
5. Tests: the binding tests in `tests/test_sdr_rtlsdr.py` and
   `test_sdr_sdrplay.py` are duplicated here and can go; the ones that hear
   frames through `SdrRxLink` stay, importing from `shack_sdr.sources`.
6. Unchanged and staying in shack_packet: `radios/sdrrx.py`
   (`FmChannelReceiver`, `FskChannelReceiver`, `SdrRxLink`, with 0.3.x's
   busy cap and hysteresis per mode and C/N0 per frame), `sdr/channeliser`,
   `plan`, `pipeline`, `monitor`, `runner`, `rtltcp`, `pluto`, `files`,
   `synth`, `memory`. They are Packet's modem and links; shack_sdr imports
   no radio link (receive only by construction), and the channeliser moves
   to Rust in M3 (RADIO-SDR-PLAN §3.3).
