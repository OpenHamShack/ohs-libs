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
catalogue()   # [{"id", "family", "inputs", "rateHz", "bandwidthHz", "minRateHz", "outputs", "panel", "params",
              #   "needs", "needsFor", "engine", "available", "partial", "missing", "why", "version", "fixed"}, ...]
sid = host.start("rtty", {"baud": 45.45, "shift": 170, "reverse": False},
                 input="iq", rate=12_000, center_hz=0.0, pinned=False)   # -> "ds_3f2a"
host.feed(sid, samples, t0, index)    # numpy complex64 (iq) or float32 (audio); copies, never blocks
host.poll(max_items=500)              # [{"session", "rev", "t", "type", ...}], oldest first; never blocks
host.status(sid)                      # {"id", "decoder", "input", "rate", "params", "pinned", "state", "why",
                                      #  "since", "rev", "stats": {"snrDb", "offsetHz", "baud", "sync", "cpu",
                                      #  "fed", "dropped", ...decoder counters},
                                      #  "meta": the decoder's latest meta fields or None,
                                      #  "audio": {"queued", "dropped"}}
host.configure(sid, {"service": "C3A7"})   # live; checked at once (DecoderError("bad_params", why))
host.audio(sid, max_s=1.0, rate=12_000, channels=1)   # [{"t", "rate", "channels", "pcm": float32 ndarray}]
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
  `image`, `meta` (`fields`: numbers, booleans, text, lists, maps and
  nulls), or `state` (`state`, `why`).
- **Sound is not a result.** A decoder's `Output::Audio` goes to the
  session's own audio ring (`ring_s` seconds; the oldest is dropped and
  counted in `status()["audio"]["dropped"]`), which the app pulls with
  `host.audio()` at its own pace: at most `max_s` seconds, oldest first,
  as runs of one format (`pcm` interleaved when stereo, `t` UTC from the
  samples), converted to `rate` and `channels` when asked by a resampler
  whose state carries from one pull to the next, so the sound is
  unbroken. Radio takes 12 kHz mono for its ordinary audio stream. Every
  decoder that makes sound uses this one path: DAB now; WFM, FreeDV and
  the digital voice modes when they come.
- **Partly available.** `needsFor` names the parts of a decoder that need
  a codec (`{"faad2": ["dab+"]}`). With it missing the entry stays
  `available`, with `partial: true` and `missing: ["faad2"]`; a codec in
  `needs` but not in `needsFor` is needed for everything, and its absence
  makes the entry unavailable (`why: "needs_codec"`, the server's 409
  `decoder_unavailable`). `codecs.publish()` (also done by
  `codecs.status()` and by `Host.start` for a decoder with needs) tells the
  native decoders which probed file each codec is. `rev` rises by one
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

### DAB (`dab`)

Old DAB and DAB+ from one Band III block (ETSI EN 300 401 mode I, TS
102 563). All our own Rust: OFDM, FIC, MSC, Viterbi, RS and our own MPEG-1/2
Layer II decoder for old DAB, which therefore plays with nothing installed.
DAB+'s HE-AAC v2 goes through the user's own faad2, opened at run time from
the file the codec loader's probe passed; never linked, bundled or ported.

```python
from shack_sdr import Host, dab
dab.IQ_RATE                                   # 2_048_000
dab.block_centre("11D")                       # 222_064_000; dab.BLOCKS, dab.block_at(freq_hz)
dab.span_check(centre_hz, rx_centre_hz, rx_rate_hz)   # None or "outside_span"
sid = host.start("dab", {"service": "C221", "block": "11D"}, "iq", dab.IQ_RATE)
host.feed(sid, iq, t0, index)                 # complex64 centred on the block
host.status(sid)["meta"]                      # the feed (below)
host.configure(sid, {"service": "C3A7"})      # what plays, live
host.audio(sid, rate=12_000, channels=1)      # its sound
```

- **IQ:** centred on the block at 2.048 MS/s, taken as it is, or any
  higher rate, resampled in Rust (a Pluto's 2.4 MS/s costs about 3 ms of
  CPU per 96 ms frame more). Lower is refused, `bad_input`/`rate`. The
  engine judges whether its span holds the block (`dab.span_check`), and
  answers `outside_span` as for any decoder.
- **Parameters:** `service` (SId in hex, "" for none: the session lists
  the ensemble without playing) and `block` (a Band III name, checked;
  for the record). Both live.
- **The feed** (the session's `meta`, also in `status()["meta"]`):

  ```json
  {"sync": true, "ensemble": {"id": "C1CE", "label": "Shack Test Mux", "shortLabel": "ShackTes"},
   "services": [{"sid": "C221", "label": "Shack MP2 Tone", "shortLabel": "ShackMP2", "bitrate": 128,
                 "kind": "dab", "subchannel": 1, "protection": "3", "why": null, "needs": []},
                {"sid": "C3A7", "label": "Shack DAB+ Data", "shortLabel": "ShackDAB", "bitrate": 64,
                 "kind": "dab+", "subchannel": 2, "protection": "3-A",
                 "why": "needs_codec:faad2", "needs": ["faad2"]}],
   "playing": "C221", "why": null, "audio": {"codec": "mp2"}, "block": "11D"}
  ```

  `ensemble` is null and `services` empty until heard; `sync` is true
  while FIBs pass their CRC. `audio.codec` is `mp2`, `he-aac`, `he-aac-v2`
  or `aac-lc`.
- **Codes** (`why`, the feed's and a service's; the app words them, and
  these are the sentences suggested for Radio's catalogue):

  | Code | Suggested sentence |
  |---|---|
  | `no_signal` | No DAB signal on this block. |
  | `no_sync` | A signal, but not DAB, or too weak to lock to. |
  | `fic_errors` | DAB heard, but too weak to read the station list. |
  | `service_not_found` | This station is not on this block. |
  | `needs_codec:faad2` | Needs faad2: install it to hear DAB+ stations. (On a controller: ask its administrator to install faad2.) |
  | `codec_config`, `codec_open` | faad2 could not play this station's sound. |
  | `unsupported_protection` | This station uses a coding Radio cannot read. |

- **faad2 missing:** the session still finds the ensemble and lists every
  service, and old DAB plays. A DAB+ service chosen says
  `needs_codec:faad2` (the session's state `running` with that `why`, the
  feed's `why`, and the service's own `why`/`needs`); its super frames are
  still found and counted (`stats.superframes`), so it is healthy and silent
  only for want of faad2. Installed later, it is picked up by the playing
  session at its next super frame once `codecs.status(rescan=True)` has
  found it ("Look again").
- **Counters** (`stats`): `frames`, `misses`, `resyncs`, `fibsOk`,
  `fibsBad`, `audioFrames`, `audioErrors`, `superframes`, `rsCorrected`,
  `rsFailed`, `aus`, `auErrors`, `codecErrors`.
- **Measured** on the transmitter model (`tests/dab.rs`, 24 frames a case;
  an MP2 frame counts only when its sound is the transmitted tones, a DAB+
  AU only when it arrives whole):

  | Channel | FIC | MP2 (UEP 3, 128 kbit/s) | DAB+ (EEP 3-A, 64 kbit/s) |
  |---|---|---|---|
  | AWGN 20, 12, 9, 7 dB | 100 % | 100 % | 100 % |
  | AWGN 5 dB | 100 % | 10 % | 93 % |
  | AWGN 4 dB | 98 % | 0 | 0 |
  | AWGN 3 dB | 55 % | 0 | 0 |
  | 12 dB, offset +1.2 / -17.3 / +35 kHz | 100 % | 100 % | 100 % |
  | 12 dB, echo 60 us at -3 dB; first path 6 dB weaker than its echo | 100 % | 100 % | 100 % |
  | 15 dB, 3 paths fading at 10 Hz | 98.6 % | 96 % | 100 % |
  | 18 dB, 2 paths fading at 40 Hz | 98.9 % | 100 % | 100 % |
  | 12 dB, clock +40 ppm at +9 kHz; -60 ppm; 2.4 MS/s input | 100 % | 100 % | 100 % |

  SNR is in the block's 1.536 MHz. CPU per 96 ms frame on a Ryzen 9
  6900HX core: 0.9 ms listing only, 3.3 ms playing MP2, 2.2 ms DAB+ before
  faad2 (only the FIC's and the chosen sub-channel's symbols are
  demodulated); 2.4 MS/s input adds about 3 ms.
- **Tests without a transmitter:** `dab::tx` (and `_native.testsig_dab`)
  is a DAB transmitter model: FIC, an MP2 service from our own encoder
  and a DAB+ service of marked test AUs, through the whole chain to OFDM,
  and a channel model. Our MP2 decoder is also held against FFmpeg's on
  streams from FFmpeg's and twolame's encoders (`tests/mp2.rs`, skipped
  where there is no ffmpeg). `SHACK_TEST_FAAD2=<a real libfaad.so.2>` runs
  the one test against a real faad2.
- **Read, with credit** (licence headers checked): EN 300 401 and TS 102
  563 are the sources; welle.io's tables (GPL-2.0-or-later) cross-checked
  ours (its UEP table has one wrong entry); dablin's AudioSpecificConfig
  layout (GPL-3.0-or-later); FFmpeg's Layer II tables (LGPL-2.1-or-later).
  faad2's and fdk-aac's own sources were not read.
- **Not done yet:** DLS and slideshow from PAD, FIG 2 (extended labels),
  data services, a multiplex reconfiguration's "next" configuration
  (applied only once it is current), MP2's scale-factor CRC concealment.

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
