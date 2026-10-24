# shack_sdr

OpenHamShack's SDR decoders, the host that runs them, the runtime codec
loader and the receive-only IQ sources: Milestone 1 of Radio's SDR plan
(openhamshack `docs/RADIO-SDR-PLAN.md`). One wheel, `shack-sdr`: a Rust
extension (`shack_sdr._native`) and a Python package around it.
GPL-3.0-or-later. Nothing here transmits.

## The Python API (what an app calls)

```python
from shack_sdr import Host, DecoderError, catalogue, codecs, register

host = Host(threads=1, cpu_max=0.25)          # also: ring_s=2.0, max_sessions=17, files_dir=None, files_keep=16
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
host.file(name)                       # a decoder's file (an SSTV PNG) by name, or None: api/decoders/files?id=
host.files()                          # [{"name", "session", "decoder", "t", "size", "path"}], oldest first
host.sessions(); host.reset(sid); host.set_outside(sid, True); host.stop(sid); host.close()  # close -> bool
codecs.configure(folder=None, app="openhamshack-radio", data_dir=None, bundle_dirs=(),
                 cache_path=None, probe_argv=None)
codecs.status(rescan=False)           # [{"id", "for", "state", "path", "version", "why", "detail",
                                      #   "where", "host", "shippable"}]
codecs.path("codec2")                 # a file that passed the probe, or None (FreeDV's library)
codecs.folder()                       # "Open codecs folder"; None on a controller
```

- **Results.** `type` is `text` (`text`, `offsetHz`, `snrDb`), `frame`,
  `image` (`event` "start" with `width`, `height`, `format`; "lines" with
  `from` and `rgb`, rows of 3 bytes a pixel, which may be sent again;
  "done" with `file` and `path`), `meta` (`fields`: numbers, booleans,
  text, lists, maps and nulls), or `state` (`state`, `why`).
- **Files are the host's.** A decoder hands over a finished file's bytes
  and never touches the filesystem. The host names it after the session
  (`ds_3f2a_sstv_20261023-101500Z_martin1.png`), keeps the last
  `files_keep` in memory and writes each whole (a temporary name, then a
  rename) to `files_dir` if given; `file(name)` serves it from either, after
  a restart too, and refuses any name but letters, digits and `_-.` (not
  starting with a dot), so a client's id cannot leave the folder. A `meta`
  naming the file names it as the host keeps it.
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

### PSK31, PSK63, PSK125 (`psk`)

Differential BPSK with Varicode, ported from openhamshack-nb
`ohscore/modes/psk.py` (RADIO-SDR-PLAN §8.5 M2), several stations per
session. IQ natively; audio through the host's adapter, as RTTY.

```python
sid = host.start("psk", {"baud": 31.25, "channels": "-800,-300,640"},
                 "audio", 12_000, center_hz=1500.0)       # a 3 kHz passband, three stations
host.configure(sid, {"channels": "-800,-300,640,910"})    # a waterfall click: live, the others undisturbed
host.poll()   # [{"type": "text", "text": "C", "t": ..., "offsetHz": -294.1, "snrDb": -9.8, ...}, ...]
host.status(sid)["meta"]   # {"channels": [{"offsetHz": -300.0, "freqHz": -294.1, "sync": True,
                           #                "snrDb": -9.6, "imdDb": -28.4}, ...]}
```

- **Parameters:** `baud` 31.25, 62.5, 125 (QPSK is not in psk.py, so not
  here); `channels`, comma-separated carrier frequencies in Hz from the
  session's centre, up to 16 (default "0"); `afc` (on) and `afcRangeHz`
  0-100 (15) each side of each channel; `squelch` 0-20 (3; 0 = off), the
  strength (dB over the noise in the symbol's own bandwidth) of the signal
  the squelch listens for. All live; a channel kept in a new list keeps
  its state. A channel the band cannot hold (with its AFC range and two
  baud) is refused once the rate is known: the session's `error`,
  `channel_outside`.
- **Results:** one `text` result a character, `t` the time of its first
  symbol (from the samples), `offsetHz` its channel's tracked frequency
  from the session's centre (route it to the nearest of your
  `channels`), `snrDb` in 2.5 kHz (add 19 dB at 31.25 Bd for the symbol
  bandwidth). Characters arrive about a second late at PSK31 (the
  trellis's 32 bits and the phase smoother). `meta` once a second while
  any channel hears a signal: per channel `offsetHz`, `freqHz`, `sync`,
  `snrDb`, `imdDb` (fldigi's IMD, measured on idle, null until the tones
  stand 20 dB over the noise and the products can be told from it).
  Counters: `chars`, `channels`, `acquisitions`, `replays` (the
  look-back decoded again, for the coarse AFC or a step), `gaps`.
- **How it decodes,** and why each part (`src/psk/mod.rs`): matched filter
  with a zero-forcing correction; coarse AFC by squaring and an FFT, with a
  replay of the last three seconds when it moves; symbol timing from the
  envelope, four symbols ahead; a Costas loop for frequency and a centred,
  leave-one-out phase reference, falling back to differential reading
  where the reference's symbols disagree (fast fading); soft bits into a
  Varicode trellis; and a squelch that weighs, for every character, every
  bit before it and the trellis's second after it in a two-state (signal
  or noise) model, so a weak over prints and its edges do not. When the
  squelch closes, what the trellis holds is released at once, and a last
  character whose own bits ("00" and all) are plainly the over's prints
  though the carrier stopped on it (a drain with no idle tail): 0.4 to
  0.7 s after the carrier, where it took 0.13 s of idle and a second more.
  A step in the carrier mid-over (a TX lock correcting the sender: NB's
  own echo) is seen by a frequency detector beside the Costas loop
  (psk.py's fine AFC), measured and placed to the sample on the look-back,
  and the look-back decoded again with the AFC stepped there; on an over
  of 12 dB Es/N0 or more (below, the loop alone follows, as before).
- **Measured** (`tests/psk.rs`, on the test transmitter; golden numbers
  from psk.py, `testdata/psk/`, `tools/psk_golden.py`; 3-4 fixtures a
  point, about 150-210 characters). Character error rate, SNR in 2.5 kHz:

  | 31.25 Bd | -16 | -14 | -13 | -12 | -11 | -10 | -8 | -5 |
  |---|---|---|---|---|---|---|---|---|
  | Rust | 39 % | 7.1 % | 4.8 % | 4.3 % | 0 | 0 | 0 | 0 |
  | psk.py | 96 % | 49 % | 15 % | 17 % | 3.6 % | 5.7 % | 6.0 % | 2.8 % |

  | 62.5 Bd | -13 | -11 | -10 | -9 | -8 | -7 | -5 | -2 |
  |---|---|---|---|---|---|---|---|---|
  | Rust | 27 % | 5.7 % | 5.4 % | 2.1 % | 1.2 % | 0 | 0 | 0 |
  | psk.py | 93 % | 38 % | 19 % | 9.9 % | 6.0 % | 3.5 % | 5.4 % | 5.7 % |

  | 125 Bd | -10 | -8 | -7 | -6 | -5 | -4 | -2 | +1 |
  |---|---|---|---|---|---|---|---|---|
  | Rust | 33 % | 7.8 % | 1.8 % | 1.4 % | 0 | 0.7 % | 0 | 0 |
  | psk.py | 98 % | 54 % | 20 % | 8.5 % | 3.6 % | 5.7 % | 3.6 % | 6.4 % |

  | 31.25 Bd, other channels | Rust | psk.py |
  |---|---|---|
  | Watterson moderate, -4 / 0 / +5 dB | 1.9 / 2.4 / 4.0 % | 9.5 / 6.8 / 6.6 % |
  | Watterson poor, -4 / 0 / +5 dB | 10 / 9.1 / 6.6 % | 17 / 19 / 8.1 % |
  | 7 and 13 Hz off, -11 dB | 2.4 %, 0.6 % | 11 %, 9.0 % |
  | drifting 0.5 Hz/s from -8 Hz, -11 dB | 0.6 % | 14 % |
  | sound card 500 ppm fast, -10 dB | 0 | 3.0 % |
  | a station 50 Hz away as strong / 10 dB stronger, -8 dB | 0 / 68 % | 4.6 / 96 % |
  | a station 70 Hz away, 10 dB stronger, -8 dB | 0 | 16 % |
  | noise alone, 15 minutes | 0 characters | 0 |

  Never worse than psk.py at any of the sweep's 62 points (PSK63 likewise: its
  table is in the test's output), and psk.py's 3-6 % on strong signals,
  noise printed while its squelch decayed after each over, is gone: forty
  strong overs each followed by 8 s of noise print nothing. An over that
  stops dead on its last "00" copies whole at every speed down to -3 dB,
  and one cut mid-character prints nothing false. Steps of 1.6 to 14 Hz
  mid-over, at every speed, 24 and 16 dB in the symbol's bandwidth: 45 of
  45 overs whole (the Costas loop alone: 9).
- **CPU** (one Ryzen 9 6900HX core, eight channels sharing one audio
  adapter): 0.03 % a channel at PSK31, 0.045 % at PSK63, 0.075 % at
  PSK125. `tests/psk.rs` prints it and fails above 3x.
- **Tests without a transmitter:** `psk::tx` (`_native.testsig_psk`):
  G3PLX's waveform (half-cosine transitions, idle as two tones at
  +-baud/2) as a function of time, with drift, clock error, Watterson
  fading, a second station and an overdriven transmitter's IMD.
- **Read, with credit:** our psk.py (its structure, its zero-forcing idea,
  its coarse AFC); fldigi's Varicode table as psk.py transcribed it
  (`src/psk/pskvaricode.cxx`, Copyright (C) 2006 Dave Freese W1HKJ,
  GPL-3.0-or-later, header checked); fldigi's IMD definition.
- **Not done yet:** QPSK and the FEC modes; a signal browser that finds
  the stations itself (psk.py's `find_carriers`); a station 10 dB stronger
  50 Hz away still costs two characters in three; a step in a weak over's
  carrier (under 12 dB Es/N0) still costs what the loop alone loses.

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
   "playing": "C221", "why": null, "audio": {"codec": "mp2"}, "block": "11D",
   "dls": "Shack MP2 Tone - now: 1 kHz left, 1.5 kHz right", "complete": true,
   "tii": [{"main": 62, "sub": 7, "levelDb": -1.0, "confidence": 1.0},
           {"main": 5, "sub": 12, "levelDb": -7.0, "confidence": 1.0}]}
  ```

  Each service map also carries the FIC's service information (null or
  empty until heard): `pty` (`{"code": 10, "id": "pop_music", "dynamic":
  false}`, the European table's codes; `id` null under another table),
  `language` (ISO 639: `"en"`), `userApps` (`["slideshow", "spi"]`, or
  `"ua_44a"` for one we do not name), `hasSlideshow`, `announcements` (the
  types that may interrupt it: `traffic`, `news`, ...) and `clusters`,
  `components` (`[{"scids": 2, "label": ...}]`, FIG 1/4 and 2/4),
  `alsoOn` (service following, listed only: `{"ensembles": [{"eid",
  "khz": [...], "block"}], "fm": [{"pi", "mhz": [...]}], "services": [linked
  SIds], "hardLink"}`), and `now`/`next` from SPI's programme guide
  (`{"name", "start" (ISO UTC), "durationS", "description"}`). Labels are
  FIG 2's (UTF-8) when one is heard, else FIG 1's. Beside `services`:
  `ensemble` adds `ecc` ("E1"), `country` (ISO 3166, from the ECC and the
  EId), `ltoMinutes` and `utc` (the ensemble's clock, to the minute);
  `announcements` lists those on now (`[{"cluster", "types", "subchannel",
  "new", "services"}]`; nothing switches); `dlPlus` is the playing label's
  DL Plus (`{"itemTitle", "itemArtist", "itemAlbum", "programmeNow",
  "programmeHost", "stationNameLong", "itemRunning", "itemToggle",
  "tags": [{"type", "text"}]}`, null without); `slide` is the latest
  slide's details (as its frame, below); `spi` is the ensemble's SPI
  service when it has one (`{"sid", "subchannel", "state": "listening" |
  "resting" | "idle" | "too_wide", "logos", "schedules"}`).

  `ensemble` is null and `services` empty until heard; `sync` is true
  while FIBs pass their CRC. `dls` is the playing service's programme text
  (dynamic label), the latest complete one, null until one is and cleared
  when the service changes; it needs no faad2, even for DAB+. `complete`
  says the ensemble's label and every service's label and bit rate are
  known. `audio.codec` is `mp2`, `he-aac`, `he-aac-v2`
  or `aac-lc`.
- **Transmitters heard (`tii`)**, strongest first: each transmitter's TII
  (EN 300 401 §14.8, mode I) from the null symbols, `main` 0-69 and `sub`
  0-23 as numbers (Ofcom's list writes both in hex: "3E"/"07" is 62/7;
  with the ensemble's `id` they name the site), `levelDb` its share of the
  received power (0 for a lone transmitter, about -3 each for two equal
  ones; to 0.5 dB) and `confidence` 0-1 (to 0.1). Confidence is at most
  0.7 for two transmitters fitted into one comb, and at most 0.3 for one
  within two combs of a transmitter 25 dB or more louder: a transmitter's
  own amplifier puts products about 30 dB under it there, which nothing in
  the null tells from a weak real one. An app should weigh a low
  confidence against the transmitters it knows for the ensemble. Empty
  until about 1.5 s after the lock, then looked at every 3 s, from about
  3 s of averaging; a gap in the samples keeps the last list, a retune or
  no DAB clears it. Up to 12 listed. On nine real Band III blocks
  (tests/dab_tii_real.rs, ignored: the recordings are 600 MB each)
  everything at 0.5 or more was a transmitter that is there, and every
  false one at 0.3 or less; on the transmitter model one 20 dB under the
  strongest, or 300 us late, is still found, and nothing is listed from
  noise. 35 us of CPU per 96 ms frame (one FFT more, the averaging, and a
  look every 3 s). The SNR is read from the empty nulls, so TII does not
  lower it. `_native.testsig_dab(..., sites=[(main, sub, delay_us, gain_db)],
  eid=0xCE15)` makes such a network for an app's tests.
- **Pictures and the guide are `frame` results** with the bytes in `raw`
  (never in the feed's text), so the app keeps them as it likes:
  - `slide` (SlideShow, TS 101 499; from the playing service's X-PAD in
    old DAB or DAB+, or its packet-mode component): `{"sid",
    "contentType": "image/jpeg" | "image/png", "name", "categoryTitle",
    "categoryId", "slideId", "clickThroughUrl", "width", "height",
    "size"}`. Each slide once, however often the carousel repeats it.
    TriggerTime is not honoured: a slide is shown when it is whole.
  - `logo` (from the ensemble's SPI carousel, TS 102 818/102 371):
    `{"sid", "variant": "square" | "wide", "contentType", "name", "width",
    "height", "size"}`; square prefers 128x128, wide 320x240. Which
    service a picture belongs to is read from the SPI service information
    file, or, with none in the carousel, from a ContentName holding the SId
    in hex.
  - `schedule`: `{"sid", "programmes": [{"name", "start", "durationS",
    "description"}]}`, a service's whole known schedule each time it
    changes.

  Nothing is fetched: URLs are passed on as text. Memory is bounded: an
  object over 450 KB (SlideShow's own limit) is refused when its header
  says so, a few objects are assembled and a few MB held at once, and
  anything unheard for 10-15 minutes of sample time is dropped. **SPI
  decoding costs a second sub-channel**, so it runs only while a service
  plays (never in `survey` or a listing-only session), only for an SPI
  sub-channel of 64 kbit/s or less, and for 10 minutes of signal before
  resting 30 (sooner once the whole carousel has come round once); measured on
  the transmitter model, 6 ms of thread time per 4.8 s for a 16 kbit/s
  carousel (`spiCpuMs`; the extra OFDM symbols are not in that count).
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
- **A band scan** asks each block "is there an ensemble here, and what is
  in it?" without playing: a session with no `service` (only the FIC's
  symbols are demodulated) until its meta says `complete`, or
  `dab.survey(iq, rate, give_up_s=1.0, wait_s=0.0)` on captured IQ, which
  returns `{"meta", "syncS", "completeS", "detailsS", "listenedS"}` and
  stops as soon as it knows; with `wait_s` it listens on after `complete`
  until every service has its programme type (`detailsS`) or `wait_s` has
  passed. On the local blocks' captures the types come with the labels
  (0.35-1.4 s); only the programme type is waited for, since language and
  user applications are optional and most ensembles never send them. Measured with labels repeated once a second (EN 300 401's rate):
  FIC read 0.15-0.2 s after the first frame begins (at most 0.21 s from 24
  random starting points at 9 dB), list complete after 0.7-0.8 s, and
  noise never syncs. Budget about 1.5 s per block that holds an ensemble
  and `give_up_s` (1 s) for an empty one.
- **Counters** (`stats`): `frames`, `misses`, `resyncs`, `fibsOk`,
  `fibsBad`, `audioFrames`, `audioErrors`, `superframes`, `rsCorrected`,
  `rsFailed`, `aus`, `auErrors`, `codecErrors`, `slides`, `logos`,
  `motGroups`, `motObjects`, `motErrors`, `motRefused`, `packetErrors`, `spiCpuMs`.
- **Measured** on the transmitter model (`tests/dab.rs`, 24 frames a case;
  an MP2 frame counts only when its sound is the transmitted tones, a DAB+
  AU only when it arrives whole):

  | Channel | FIC | MP2 (UEP 3, 128 kbit/s) | DAB+ (EEP 3-A, 64 kbit/s) |
  |---|---|---|---|
  | AWGN 20, 12, 9 dB | 100 % | 100 % | 100 % |
  | AWGN 7 dB | 100 % | 97 % | 100 % |
  | AWGN 5 dB | 100 % | 12 % | 93-100 % |
  | AWGN 4 dB | 99 % | 0 | 0 |
  | AWGN 3 dB | 55-69 % | 0 | 0 |
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
  and a channel model; and the data side: X-PAD carousels (SlideShow),
  DL Plus, a packet-mode data service carrying an MOT directory, binary
  SPI files, and any FIG a test adds (`tests/dab_data.rs`). Real signal:
  `tests/dab_data.rs`'s ignored tests read a SigMF recording
  (`SHACK_DAB_IQ`, `SHACK_DAB_RATE`; block 11B, Ayr, 23/10/2026, on shack
  in ~/build/dab11b, 600 MB, never committed): its PTy, country, clock,
  linkage sets, the other ensemble on 11C, and STV Radio's DL Plus. Our MP2 decoder is also held against FFmpeg's on
  streams from FFmpeg's and twolame's encoders (`tests/mp2.rs`, skipped
  where there is no ffmpeg). `SHACK_TEST_FAAD2=<a real libfaad.so.2>` runs
  the one test against a real faad2.
- **Read, with credit** (licence headers checked): EN 300 401, TS 102
  563, EN 301 234 (MOT), TS 101 499 (SlideShow), TS 102 371 (SPI's
  binary encoding) and TS 102 980 (DL Plus) are the sources; welle.io's tables (GPL-2.0-or-later) cross-checked
  ours (its UEP table has one wrong entry); dablin's AudioSpecificConfig
  layout (GPL-3.0-or-later); FFmpeg's Layer II tables (LGPL-2.1-or-later).
  faad2's and fdk-aac's own sources were not read.
- **Not done yet:** SlideShow's TriggerTime and categories as a browsable
  set, SPI's genres, links and compressed "advanced" files, the packet
  FEC, OE announcements (FIG 0/25, 0/26), a multiplex reconfiguration's
  "next" configuration (applied only once it is current), MP2's
  scale-factor CRC concealment.

### SSTV (`sstv`)

Pictures in fourteen modes - Martin M1/M2, Scottie S1/S2/DX, Robot 36/72,
PD50/90/120/160/180/240/290 - from a VIS header, or from a train of line
syncs when the header was missed (a picture joined part way). It replaces
openhamshack-nb `ohscore/modes/sstv.py` (Robot 36, Martin M1, Scottie S1,
PD120) and is the one SSTV decoder (GUIDE §1): OpenHamShack Icom has a
second, pure-Python one in `app/sstv`, and Flex vendors sstv.py; both
should move to this.

```python
from shack_sdr import Host, sstv
host = Host(files_dir=pictures)
sid = host.start("sstv", {}, "audio", 12_000)   # any rate from 6 kHz; or "iq" from 4 kHz,
host.feed(sid, audio, t0, index)                #   centred on sstv.IQ_CENTRE_HZ (dial + 1900)
host.poll()            # image start / lines / done, and meta (python/shack_sdr/sstv.py)
host.file(name)        # the PNG
sstv.modes(); sstv.testsig("martin1", snr_db=10.0); sstv.score(sent, got); sstv.png_rgb(png)
```

- **Parameters:** `mode` ("" for every mode, or one mode's id), `syncStart`
  (on: start from line syncs without a header), `afcRangeHz` (0-150, 150:
  how far off tune a header is looked for).
- **The feed** (`meta`, also `status()["meta"]`): `state` (`receiving`
  every sixteen lines, `done`, `lost`; `unsupported` or `bad_vis` for a
  header not decoded), `mode`, `vis`, `partial`, `width`, `height`,
  `rows`, `started` (UTC from the samples), `slantPpm` (the sender's sound
  card, ppm fast), `snrDb` (in 2.5 kHz), `offsetHz`, `syncs`, `missed`,
  `corrected` (the final straightening moved a row), `file`. The codes and
  suggested sentences are in python/shack_sdr/sstv.py.
- **How** (src/sstv/mod.rs says why for each): baseband from a 1750 Hz
  mix; VIS by sstv.py's template against detectors at 25 Hz steps (found to
  +-150 Hz off tune, the offset measured to a hertz and followed), placed to
  a fraction of a sample by matching the decoded header, parity mending a
  faded bit; line syncs by a filter matched to the pulse and the tones
  around it; the line clock a weighted straight-line fit through the syncs,
  its span chosen by cross-validation, and the whole picture read again
  from the fit when it ends (rows that move are re-sent); pixels the mean
  frequency over their exact span through a filter as narrow as the SNR
  asks (550 Hz either side at 8 dB, 2 kHz clean), less the filtered noise's
  known pull; Robot 36's colour from both lines of its pair. A header heard
  inside a picture ends it only if the picture's syncs stopped under it.
- **Measured** against sstv.py on the same 116 fixtures (`tests/sstv.rs`;
  the test card, 12 kHz audio in 0.1 s blocks, SNR in 2.5 kHz; fading the
  mean of eight pictures). PSNR, Python / Rust, dB (rows within a pixel of
  their place, Python / Rust):

  | Channel | Robot 36 | Martin M1 | Scottie S1 | PD120 |
  |---|---|---|---|---|
  | clean | 24.3 / 27.1 (100 / 100 %) | 32.5 / 36.2 (100 / 100 %) | 31.5 / 36.3 (100 / 100 %) | 30.6 / 31.2 (100 / 100 %) |
  | 30 dB | 23.3 / 25.0 (100 / 100 %) | 31.6 / 34.6 (100 / 100 %) | 31.0 / 33.8 (100 / 100 %) | 27.3 / 27.7 (100 / 100 %) |
  | 20 dB | 18.9 / 21.5 (100 / 100 %) | 27.0 / 28.6 (100 / 100 %) | 26.9 / 28.1 (100 / 100 %) | 19.9 / 23.5 (99 / 99 %) |
  | 15 dB | 15.1 / 19.9 (95 / 100 %) | 23.3 / 25.7 (100 / 100 %) | 23.0 / 25.4 (100 / 100 %) | 15.4 / 21.4 (86 / 96 %) |
  | 10 dB | 11.1 / 17.9 (63 / 93 %) | 18.8 / 22.9 (99 / 100 %) | 18.4 / 22.8 (98 / 100 %) | 11.2 / 19.2 (50 / 70 %) |
  | 6 dB | 8.3 / 15.4 (33 / 81 %) | 14.7 / 20.2 (83 / 100 %) | 14.3 / 20.2 (82 / 100 %) | 8.4 / 16.0 (25 / 59 %) |
  | 3 dB | none / 12.5 (- / 57 %) | none / 17.1 (- / 95 %) | none / 17.0 (- / 93 %) | none / 12.8 (- / 43 %) |
  | 40 Hz high, 20 dB | 16.3 / 21.6 (100 / 100 %) | 23.9 / 28.6 (100 / 100 %) | 23.9 / 28.1 (100 / 100 %) | 16.3 / 23.8 (69 / 100 %) |
  | 100 Hz low, 20 dB | none / 21.6 (- / 100 %) | none / 27.9 (- / 100 %) | none / 27.8 (- / 100 %) | none / 23.3 (- / 100 %) |
  | clock +200 ppm, 20 dB | 18.8 / 21.5 (100 / 100 %) | 27.2 / 28.2 (100 / 100 %) | 26.9 / 27.9 (100 / 100 %) | 19.6 / 23.6 (91 / 100 %) |
  | clock -200 ppm, 20 dB | 19.0 / 21.4 (100 / 100 %) | 26.6 / 27.8 (100 / 100 %) | 26.6 / 28.0 (100 / 100 %) | 19.9 / 23.6 (96 / 100 %) |
  | joined 40 % in, 20 dB | 18.5 / 21.4 (99 / 100 %) | 26.5 / 28.1 (100 / 100 %) | 26.4 / 27.3 (100 / 100 %) | 19.9 / 23.5 (98 / 100 %) |
  | no header, 20 dB | 18.9 / 21.3 (100 / 100 %) | 27.4 / 28.1 (100 / 100 %) | 16.6 / 28.0 (98 / 100 %) | 19.9 / 23.6 (100 / 100 %) |
  | Watterson good, 25 dB | 18.1 / 20.0 (90 / 90 %) | 22.6 / 24.0 (97 / 97 %) | 21.8 / 22.8 (92 / 95 %) | 17.6 / 20.1 (54 / 67 %) |
  | Watterson moderate, 25 dB | 15.6 / 17.9 (46 / 46 %) | 20.1 / 21.9 (62 / 73 %) | 20.6 / 21.9 (66 / 69 %) | 16.8 / 19.9 (23 / 25 %) |

  The ten modes sstv.py never had: clean 27.8-44.6 dB, every row in
  place; 10 dB 18.8-27.7 dB; +-200 ppm and 100 Hz off tune as at 20 dB;
  joined part way, every row in place (`sweep` prints them). Fading is
  random: through ten fades Robot 36's rows in place ranged 55-95 % for
  one decoder, so it is judged on means. Under multipath the channel's
  delay differs between 1200 Hz (the syncs) and the picture's tones, so
  no decoder keeps every row in place there.
- **CPU**, thread time on one Ryzen 9 6900HX core: 0.3-0.5 % of a core at
  12 kHz (PD120, the most pixels a second: 0.33 %), 0.46 % at 48 kHz.
  Memory: a picture's baseband is kept until it ends (8 bytes a sample at
  about 12 kHz: 11 MB for Martin M1, 28 MB for PD290).
- **Tests without a transmitter:** `sstv::tx` (`_native.testsig_sstv`,
  `sstv.testsig`) sends any picture in any mode through AWGN, a tuning
  offset, the sender's clock error (modelled in the sender's time, not
  resampled) and Watterson fading; `tx::test_card` and `tx::score` are the
  picture and the measure. The fixtures (`testdata/sstv/`) are recipes for
  it pinned by sha256, `golden.json` what sstv.py did on them
  (`tools/sstv_golden.py`).
- **Read, with credit:** JL Barber (N7CXI), "Proposal for SSTV Mode
  Specifications" (Dayton, 2000), through sstv.py's transcription; OpenHamShack
  Icom's `app/sstv/modes.py` table and its line-clock fit (both ours); QSSTV's
  picture times as that table quotes them, as a check (facts, no code read).
  slowrx (ISC) and QSSTV (GPL) were not read.
- **Not done yet:** Wraase SC2, Pasokon, AVT, the MMSSTV MP/MR/ML family
  and the black-and-white Robots (named when heard, not decoded); FSK ID
  after a picture; a real-air corpus (iqlib has no SSTV recording yet).

### Broadcast FM (`wfm`): stereo, RDS and the band scan

One station's channel IQ to 48 kHz stereo with RDS, all in Rust off the
GIL (`src/wfm/`), replacing openhamshack-nb `ohscore/dsp/wfm.py`'s mono
path; and the band scanner's many-carrier RDS on a wide capture.

```python
from shack_sdr import wfm
rx = wfm.Receiver(rate=240_000, deemphasis_us=50)   # stereo=True, blend=True, rds=True,
                                                    # audio_rate=48_000 (24_000, 12_000), channels=2 (1)
pcm = rx.process(channel_iq, t0)      # complex64 in; float32 (n, channels) out; process_composite(mpx, t0)
rx.rds()      # {"pi": "C201", "ps": "TEST FM ", "rt", "pty": 10, "ptyName": "Pop music", "ta", "tp",
              #  "ct": "2026-10-23T15:05:00+01:00", "af": [89100000, ...], "stereo", "ts", "rev"}; None = not heard
rx.status()   # {"stereo", "blend", "pilot", "snrDb", "levelDb", "offsetHz", "rdsSync", "rdsSnrDb",
              #  "blocks", "corrected", "blockErrors", "groups"}
rx.reset()    # every retune: RDS cleared, loops restarted
rx.set_deemphasis(75); rx.set_stereo(False); rx.set_blend(False)   # live
wfm.scan_band(iq, rate=8_000_000, center_hz=97_000_000, budget_s=3.0, threads=0, min_snr_db=10.0, rt=False)
              # [{"hz", "levelDb", "stereo", "pi", "ps", "pty", "ptyName", "rt", "ta", "tp", "ct", "af",
              #   "rdsSync", "complete"}]; report=True: {"stations", "usedS", "timedOut", "cpuS", "wallS", "noiseDb"}
wfm.pty_name(10)                      # "Pop music": the European table, through the shack-sdr catalog
sid = host.start("wfm", {"deemphasis": 50}, "iq", 240_000)    # the same receiver as a Host session:
host.audio(sid, rate=12_000, channels=1)   # its sound from the ring; RDS as `meta` results (pty a number)
```

- **Input:** the channel IQ at a whole multiple of 48 kHz of at least
  144 kS/s (Radio's bank gives 240 kS/s; anything else is
  `DecoderError("bad_input", "rate")`), or the composite (75 kHz = 1.0).
  IQ because Radio has it, the discriminator is one `atan2` a sample, and
  the same pass gives the level and the tuning offset. Full deviation
  comes out at 0.5 and a 20 Hz DC high-pass is kept, as wfm.py.
- **Stereo:** a pilot PLL; the stereo flag on above 4 % of deviation for
  150 ms, off below 3 % for 300 ms; L-R from the pilot's second harmonic;
  50/75 µs de-emphasis; one decimator for sum and difference, flat to
  15 kHz and 70 dB down from 19 kHz. **Blend** to mono on the stereo
  channel's SNR, estimated from the composite's empty 64-84 kHz (within
  1 dB of the measured from 8 to 40 dB CNR): mono at 26 dB, full stereo
  at 40 (mono is 20 dB quieter than stereo on the same signal).
- **RDS** (EN 62106): matched filter for the shaped biphase symbol,
  Costas loop (works in mono and with the subcarrier in quadrature), bit
  timing at the matched filter's energy peak, syndrome sync, 2-bit burst
  correction only in sync, at a low recent error rate, and on the
  demodulator's weakest bits. Groups 0A/0B (PS, TA, TP, AF), 2A/2B
  (RadioText with its A/B flag), 4A (clock), PI and PTY from every group.
  A field is published only when stable (each PS/RadioText segment twice
  alike, never old and new halves together; PI/PTY/TP/TA twice; AFs
  twice; the clock from a group with all four blocks clean), and cleared
  on `reset` or a newly confirmed PI.
- **Band scan:** one overlap-save channeliser for every carrier (each
  segment transformed once; each carrier's bins inverse-transformed to
  200-400 kS/s), memory about 0.1 s of spectra whatever the capture's
  length; carriers on the 100 kHz grid in 87.5-108 MHz `min_snr_db` over
  the 30th-percentile floor and no weaker than their neighbours; each
  decoded (pilot and RDS, no audio) until it has PI, PS and PTY (and
  RadioText with `rt`), the budget is spent or the capture ends.
- **Measured** (tests/wfm.rs, tests/wfm_scan.rs, on the test transmitter;
  CNR is over the sampled 240 kHz, about 1.2 dB worse than in 180 kHz):
  separation 83-109 dB from 100 Hz to 15 kHz at 50 dB CNR; THD+N
  -111 dB at full deviation; response flat to 0.2 dB. RDS block error
  rate 0.6 % at 20 dB CNR, 1.3 % at 15, 5 % at 12, 52 % at 10 (the FM
  threshold); PS within 1.2 s down to 10 dB, and in 7 s at 9; through a
  25 kHz tuning error, ±100 ppm station clock, quadrature RDS, no pilot
  and two echoes; no published field ever wrong, down to 6 dB. The scan
  names twelve stations from -25 to -58 dBFS in 8 and 10 MHz captures.
- **CPU** (thread time, one Ryzen 9 6900HX core): a station at 240 kS/s
  is 2.7 % with stereo and RDS, 3.0 % at 12 kHz mono, 2.1 % RDS and pilot
  only. The scan is about 35 ms of CPU per MHz per second of capture with
  every carrier decoded to the end (the worst case); a typical scan stops
  each station after about 1.1 s, and threads divide the wall time.
  `tests/wfm_scan.rs` prints the figure on every run and fails only above
  3x it (`SHACK_SDR_CPU_BUDGET_SCALE` sets the multiple): a regression
  catcher, since a shared VM measures half as much again with nothing changed.
- **Tests without a transmitter:** `wfm::tx` (`_native.testsig_wfm`,
  `testsig_fm_band`) builds the BS.450 multiplex with pre-emphasis, the
  pilot, L-R, RDS from real groups (`rds::Station`), FM at ±75 kHz, and a
  channel with AWGN, offset, clock error and echoes; and a wide band of
  such stations.
- **Read, with credit:** EN 62106 is the source; redsea (MIT, Oona
  Räisänen) for the G0 character table as it lays it out, and its overall
  receiver shape and 2-bit correction limit. gr-rds was not needed.
- **Not done yet:** RadioText+ and other ODA groups, EON (14A),
  Enhanced RadioText, AF method B's pairs (each frequency is listed, the
  pairing ignored), RBDS's PTY names (the number is there for an app that
  wants them). The scan's levels and floor come from the capture's first
  0.1 s, which is right for FM's constant envelope but would miss a
  station that starts later in the capture.

## Sources

`shack_sdr.sources`: `IqSource`, `IqBlock`, `SdrError`, `REASONS`,
`RtlSdrSource`, `SdrplaySource`, `cu8_to_complex`, `complex_to_cu8`, and
`rtlsdr.availability()`, `sdrplay.availability()` (`why`
`sdrplay_api_missing`, `host` on a controller), `RtlSdrSource.rates()`,
`SdrplaySource.rates()`. Moved from `shack_packet.sdr`; since 23/10/2026
`SdrplaySource` opens an RSP exactly as SoapySDRPlay3 does (below), which
changes what Packet's RSP hears too (the tuner's DC tracking), never its gain.

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

### SDRplay's gain models and switches

```python
from shack_sdr.sources import SdrplaySource, sdrplay
src = SdrplaySource(145_650_000, 2_400_000, 40.0, serial="240309F070", lna_state=4, antenna="Antenna A",
                    gain_model="soapysdrplay3",   # or "reduction" (the default, Packet's)
                    agc_setpoint=-30,             # dBfs, set at open whatever the gain
                    bias_t=None, rf_notch=None, dab_notch=None, hdr=None)   # None: SDRplay's default (off)
src.set_gain(45.0); src.set_gain(None)            # live: one Update(Tuner_Gr | Ctrl_Agc)
src.gain_now()        # 45.0: the gain as this model reports it, from the API's structure while running
src.gain_sense()      # {"gainIsReduction": True, "gainModel": "soapysdrplay3", "rfgrRange": [0, 27]}
src.set_options(bias_t=True, rf_notch=False)      # live: SoapySDRPlay3's Update reason for each
src.status()          # ...plus "gainModel", "gainIsReduction", "rfgrRange", "gRdB", "lnaState",
                      # "options", "overloads", "overloaded", "apiGaps", "apiLost"
sdrplay.soapy_gain_split(40.0, sdrplay.HW_RSPDX)  # (59, 1); soapy_gain_of(59, 1, hw) -> 40.0
```

- **`gain_model="reduction"`** (the default, unchanged): a number is gRdB
  (20-59) with AGC off and the LNA at `lna_state`; None is SDRplay's AGC
  at 50 Hz. `gain_now()` is gRdB.
- **`gain_model="soapysdrplay3"`**: SoapySDR's overall gain under
  SoapySDRPlay3, for a station that ran on it (Radio's `device soapy`,
  `driver=sdrplay`). A number is split as SoapySDR's base
  Device::setGain splits it over SoapySDRPlay3's elements, IFGR (gRdB
  20-59) first, then RFGR (the LNA state, 0 to the model's top: 3 RSP1,
  8 RSP2, 9 RSP1A/1B/duo, 27 RSPdx/dx R2), each truncated as its
  `(int)value` is; both are reductions, so more is less gain. None is
  AGC_CTRL_EN at `agc_setpoint` with gRdB and the LNA state left as they
  were (`lna_state`, its `rfgain_sel`, at open; the last split after a
  manual spell). `gain_now()` and `status()["gain"]` are its getGain:
  (gRdB - 20) + LNA state. Held to the table openhamshack-nb's tests hold
  Radio to (`pytests/test_sources.py` SOAPYSDRPLAY3_RSPDX): manual 40 is
  gRdB 59 at LNA state 1, as on ohs-box.
- **`gain_sense()`** gives the three fields Radio's S-meter reads
  (`gainIsReduction`, `gainModel`, `rfgrRange`); they are in `status()`
  too.
- **Switches** `bias_t`, `rf_notch`, `dab_notch`, `hdr` write the field
  and push the Update reason SoapySDRPlay3's `biasT_ctrl`,
  `rfnotch_ctrl`, `dabnotch_ctrl`, `hdr_ctrl` do, per model (an RSPduo's
  notch is the AM notch on Tuner 1's Hi-Z port); a model without the
  switch ignores it, as SoapySDRPlay3 does. `status()["options"]` reads
  back each switch the model has. Bias-T puts DC on the antenna socket:
  leave it None unless the station's preamplifier wants it.

### SDRplay: parity with SoapySDRPlay3

Radio ran ohs-box's RSPdx through SoapySDRPlay3 0.5.2 (MIT,
github.com/pothosware/SoapySDRPlay3) until store 0.1.22-beta.2, and now
through `SdrplaySource`. With the gain mapped exactly (gRdB 59, LNA state
1 at rxGain 40) a repeater ident still measured 28.6 dB over the noise
against 31.8 under SoapySDRPlay3, jittering about ±1 dB within an ident
against ±0.2. So every field SoapySDRPlay3 sets for an RSPdx at open and
stream start was listed from its source (line numbers are 0.5.2's
`Settings.cpp`, `Streaming.cpp`, `CMakeLists.txt`) for the case Radio
used: `serial` and `rfgain_sel=4` its only arguments, 2.4 MS/s, Antenna
A, manual gain 40, CF32. Radio's calls, in order (openhamshack-nb
`ohscore/devices/soapy.py`): construct, setAntenna, setSampleRate,
setGainMode(false), setGain(40), setupStream(CF32), activateStream, then
setFrequency on the running stream.

| Field | SoapySDRPlay3 at 2.4 MS/s | Where | shack_sdr before 23/10/2026 | Now |
|---|---|---|---|---|
| fsHz | 2 400 000 (rate > 2 MS/s: fs = rate) | Settings.cpp 1019-1022, 807-809 | 2 400 000 | same |
| ifType | IF_Zero | 1019-1022, 813-815 | IF_Zero | same |
| decimation | factor 1 = API default, so enable, factor and wideBandSignal untouched (0, 1, 0) | 818-827 | enable written 0 | same values |
| bwType | getBwEnumForRate: BW_1_536 | 830-832, 1091-1101 | BW_1_536 fixed | `bw_for_rate`, same at 2.4 |
| loMode | never written: LO_Auto | - | LO_Auto written | same |
| rfHz | (uint32_t)frequency, after Init | 674-676 | float, before Init | truncated; still set before Init (one tune, not two) |
| ppm | never written (only setFrequencyCorrection, 701-704): 0 | - | 0 | same |
| DCenable, IQenable | API default 1, 1 (only `iqcorr_ctrl` writes them) | 1617-1626 | default | same |
| dcOffsetTuner | **dcCal 4, speedUp 0, trackTime 63** before Init; refreshRateTime default 2048 | Streaming.cpp 359-361 | **API default: dcCal 3, trackTime 1** (9 µs of tracking every 18 ms) | **4, 0, 63** |
| AGC (manual) | setGainMode(false): AGC_DISABLE | 515-527 | AGC_DISABLE | same |
| AGC set point | **-30 dBfs in the constructor, whatever the gain** | 65 | -30 under AGC only, else API default -60 | **-30 always** (`agc_setpoint`) |
| AGC (automatic) | AGC_CTRL_EN | 515-527 | AGC_50HZ | AGC_CTRL_EN under `gain_model="soapysdrplay3"` |
| gRdB, LNA state | rfgain_sel=4 sets LNA 4 (RF_GAIN_IN_MENU is ON by default, CMakeLists 36); setGain(40) then splits IFGR, RFGR: 59, 1 | 68-76, 1590-1614, 537-565 | a private override in Radio | `gain_model="soapysdrplay3"` |
| minGr, attack/decay, adsbMode | never written: NORMAL_MIN_GR, 0, ADSB_DECIMATION | - | default | same |
| antennaSel (RSPdx) | ANTENNA_A, device level | 274-293 | ANTENNA_A | same |
| rfNotchEnable, rfDabNotchEnable (RSPdx) | 0 (only `rfnotch_ctrl`, `dabnotch_ctrl`) | 1707-1763, 1764-1801 | 0, never written | options `rf_notch`, `dab_notch` |
| hdrEnable (RSPdx) | 0 (only `hdr_ctrl`) | 1802-1826 | 0 | option `hdr` |
| biasTEnable | 0 (only `biasT_ctrl`) | 1661-1706 | 0 | option `bias_t` |
| extRefOutputEn (RSP2, duo) | 0 (only `extref_ctrl`) | 1635-1660 | 0 | same (not offered: no station uses it) |
| device.tuner (not a duo) | GetDevices' own (Tuner_A) | 2100-2105 | Tuner_A if 0 | same |
| USB mode | ISOCH (BULK only if built so; off by default) | Streaming.cpp 368-371, CMakeLists 42 | default ISOCH | same |
| Debug | DebugEnable(Disable) | Settings.cpp 2125, Streaming.cpp 356 | not called: API default Disable | same |
| Power overload event | **acknowledged at once, Update(Ctrl_OverloadMsgAck), detected and corrected** | Streaming.cpp 173-185 | **ignored, never acknowledged** | acknowledged; counted (`overloads`, `overloaded`) |
| Sample format | CF32 = int16 / 32768 (native CS16, full scale 32767) | Streaming.cpp 40-44, 145-153 | / 32768 | same, bit for bit |
| Callback buffering | copies each packet in C into 8 buffers of 65 536 samples (27 ms each); a full ring drops the packet | Streaming.cpp 101-143, SoapySDRPlay.hpp 44-46 | a numpy array per packet (about 1800 a second) under the GIL, concatenated every 50 ms | two memmoves per packet into one preallocated 50 ms block; API-side drops counted from firstSampleNum (`apiGaps`, `apiLost`) |

**Differences found:** three that touch the radio - the tuner's DC
offset tracking (SDRplay's default retracks for 9 µs every 18 ms;
SoapySDRPlay3 tracks for 567 µs), overload events never acknowledged, and
the AGC set point outside AGC (inert in manual) - and one in how long the
API's thread is held per packet. All four now match.

**Expected on air** (not yet run on an RSP; ohs-box is the place to check
it): the DC tracking is the likely cause of both symptoms. A 9 µs
correction every 18 ms (55 Hz) steps the I/Q offset 55 times a second;
on an FM ident that is level modulation at 55 Hz and its harmonics, and
spreading it over the channel lifts the noise measured beside it.
Expect the ident's level to settle to SoapySDRPlay3's ±0.2 dB and the
SNR to come back by up to the 3.2 dB missing, with the same noise floor
in a quiet channel. The overload ack matters only on a strong signal: SDRplay's
API asks for each overload change to be acknowledged, and what it does
with one that never is is not documented; if `overloads` stays 0 on 2 m,
it was not involved. If
`apiGaps` rises above 0 the callback was losing packets, which would also
show as clicks and a raised floor; it should stay 0 now.


## Building and testing

`tools/test.sh` (repository root) runs `cargo test --release`, `cargo deny
check bans licenses`, these Python tests against an extension built from
the tree (`tools/dev-build.sh`), and `tools/check-encumbered.py` on it.
Wheels: `tools/build-sdr-wheels.sh` (repository root) into
`~/build/ohs-dist/shack-sdr/` with `SHA256SUMS`; never committed.

The PSK fixtures (`testdata/psk/`, `tools/psk_golden.py`) are made and
held the same way (`tests/psk.rs`).

The RTTY fixtures (`testdata/rtty/`) are recipes for `testsig` pinned by
sha256, and `golden.json` is what openhamshack-nb's `rtty.py` printed on
them (`tools/rtty_golden.py`). `tests/rtty.rs` holds the Rust decoder to
never worse than it at any point of the sweep, and to §7.2's targets.
Two more there have no rtty.py figure: an over through a receiver's AGC
(the noise held down under it and coming back as the carrier drops, the
shape captured from openhamshack-nb's own-echo test) must print nothing
after the over, and 40 strong overs each followed by 8 s of noise may
print at most 5 false characters between them.

The SSTV fixtures (`testdata/sstv/`) are the same idea: recipes for
`sstv::tx` pinned by sha256, `golden.json` what openhamshack-nb's
`sstv.py` did on them (`tools/sstv_golden.py`), and `tests/sstv.rs` holds
the Rust decoder to never worse, case by case and on the mean of each
fading condition's eight pictures.

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
