# shack-cw

The OpenHamShack CW decoder (PLATFORM-PLAN §1.4: shared components, one of
each). Moved from QO-100 NB (`~/openhamshack-nb` main 47be138:
`ohscore/modes/cw.py`'s decoder and tables, `ohscore/dsp/morse.py`,
`tools/cw_bench.py` and the decoder's tests) so that NB and Radio, Icom and
next Flex run one decoder.

| Module | What |
|---|---|
| `shack_cw.decoder` | `Decoder(emit=None, freq=700, wpm_hint=20, squelch=4, afc=True, snr_db=6, polarity=True)`; `feed(samples, rate)` returns the events (and hands each to `emit`); `feed_frames(tone, quiet, loud)` is the same without the front end, for a skimmer's filter bank (5 ms frames of amplitude). `text`, `wpm` (None until learnt), `snr` (dB, as the squelch sees it), `pitch` (where AFC has found the tone), `locked`, `hearing`; `snr_db` (the squelch) may be set while running; `polarity=False` where there are no F1A signals (Radio). |
| `shack_cw.corpus` | The decoder scored on recordings of the real bands: CER, characters printed on noise, speed error, lock time, by band and category. `python3 -m shack_cw.corpus MANIFEST.json`; `decode_timed`, `score` take an app's own `receiver(rate, pitch, width)`. |
| `shack_cw.morse` | The alphabet and prosigns, `text_to_codes`, and keying envelopes (`runs`, `envelope`, `envelope_of`) at any rate. |
| `shack_cw.bench` | Character error rate on the on-air cases (QO-100's beacons, a human fist, QSB, drift, noise, carriers): `python3 -m shack_cw.bench [--rate R]`. Importable: `run_case`, `decode`, `render` take an app's own `receiver=`. |
| `shack_cw.nn` | The neural engine: `Decoder(engine="nn")`, and `Bank` for a skimmer's channels in one call (see below). `python3 -m shack_cw.nn.cpu` times it. |
| `shack_cw.benchrx` | The receiver the bench hears through: NB's 500 Hz CW filter and medium AGC, copied exactly so the bench prints NB's numbers. |

## Using it

```python
import shack_cw
dec = shack_cw.Decoder(freq=cw_pitch_hz)      # the radio's CW pitch (BFO offset)
for ev in dec.feed(block, 12_000):            # mono float audio, any block length
    publish(ev)   # {"mode": "cw", "text": "K", "freq": 600.0, "wpm": 18.2}
```

One event per character; a word space is `{"text": " "}` with no `wpm`.
That is the shape QO-100 NB's `events` feed has always carried
(openhamshack-nb docs/SERVER-API.md), so every client's CW strip reads any app.
Call `reset()` on a retune or a mode change; a new rate resets by itself.

Nothing in the decoder assumes a rate: frames are 5 ms, AFC's spectrum has
bins of 6 Hz or finer, and the speed is learnt in seconds. At 12 kHz it is
NB's arithmetic operation for operation, and the bench prints NB's table
exactly. Tested at 8, 12, 24 (Flex) and 48 kHz.

**Feed it blocks of about 75 ms** (900 samples at 12 kHz, 1800 at 24),
not a radio's 20 ms packets. That is the block NB's engine hands it and the
bench measures; AFC looks at each block's spectrum and skips any block
under 256 samples, and its averaging (about 1.5 s of memory) counts blocks.
Icom gathers its 20 ms LAN packets into 75 ms blocks for this
(`app/cw.py`); a 24 kHz Flex stream wants the same. Decode off the event
loop: a few milliseconds a second of audio on shack, ten times that on a
Pi.

The decoder wants the audio of a CW filter round the pitch (a few hundred
Hz wide): it measures the noise 125-225 Hz either side of the tone, and
learns how a narrower filter's skirts change that. Wider audio works, with
more chance of a neighbour inside AFC's ±100 Hz.

## Bench (12 kHz, 3 seeds; CER %, noise and carriers in characters a minute)

```
case                        0.0    3.0    6.0    9.0   12.0   15.0
beacon                      0.0    0.0    0.0    0.0    0.8    0.0
beacon-qsb                 65.9   20.9    0.0    0.0    1.6    0.0
beacon-drift                0.0    0.0    0.0    0.0    0.0    0.0
beacon-bookmark             3.1    0.0    0.0    0.0    0.0    4.7
cq20-machine                0.0    0.0    0.0    0.0    0.0    0.0
cq20-human                  0.5    0.5    0.0    0.0    0.5    1.1
cq20-human-qsb-drift       46.0   41.8    5.8    0.0    0.5    1.1
cq12-human                  1.6    1.1    0.0    0.0    0.5    1.1
cq30-machine                1.6    0.0    1.1    0.0    1.1    1.1
beacon-beside               2.3    2.3    2.3    2.3    2.3    8.5
noise                       0.0    0.0    0.0    0.0    0.0    0.0
carriers                    0.0    0.0    0.0    0.0    0.0    0.0
cq20-heavy                 14.8   47.6   58.7   22.2   15.9   36.5
cq20-light                  1.6    2.1    1.1    2.1    3.7    3.2
cq25-flutter               71.4   57.1   50.3   11.6   16.4    1.1
cq20-two                    0.5    0.5    2.6    2.6    2.6    8.5
qrn                         0.0    0.0    0.0    0.0    0.0    0.0
neighbour-200               0.0    0.0    2.3    0.0    0.0    1.3
neighbour-wide              0.0    0.0    0.0    0.0    0.0    0.0
decoder CPU: 3.54 ms per second of audio
per channel without the front end (Decoder.feed_frames): 1204 us per second
```

Before 13/10/2026 (the decoder as moved from NB): qrn 3-35 a minute,
neighbour-200 100-160 up to 9 dB, neighbour-wide 19 and 135 at 12 and
15 dB, beacon-beside 65 % at 15 dB; CPU 1.9 ms a second. Open: heavy
keying (`cq20-heavy`, both before and after) - its short gaps fill in and
dashes run together - and fast flutter.

## Real bands (shack_cw.corpus)

`tools/cw_corpus.py` records IQ from public KiwiSDRs (receive only;
kiwirecorder, fixed gain), captures the Reverse Beacon Network's spots
alongside (`tools/rbn.py`), and annotates each recording: NCDXF/IARU
beacon slots (whose turn, heard or not), RBN-spotted stations (call and
speed), quiet places, and Brian's 20 m case - the pitch 600 Hz beside a
strong station in a 1500 Hz filter. The IQ stays out of git
(`~/build/cw-corpus` on shack, 27 recordings, 80-12 m, 13/10/2026);
`tests/corpus/manifest.json` is its ground truth and `tests/corpus/plan.json`
what recorded it. Every recording is 10 kHz wide with several stations in
it, so a skimmer can be scored on the same files.

```
python3 -m shack_cw.corpus tests/corpus/manifest.json [--opt polarity=0] [--decoder OLD.py]
```

Before -> after (as Radio makes it, `polarity=0`):

| | before | after |
|---|---|---|
| characters a minute, 72 min of quiet places | 11.2 | 0.1 |
| ... beside a strong station, 1500 Hz filter (14 min) | 29.1 | 0.0 |
| ... in the gaps between beacons (34 min) | 0.1 | 0.1 |
| beacon CER, 29 slots | 66 % | 76 % |
| speed error against the RBN, median by band | 0.1-3.1 WPM | 0.2-5.4 WPM |

By band, characters a minute on noise before -> after: 80 m 23.8 -> 0,
40 m 17.3 -> 0, 30 m 16.8 -> 0.6, 20 m 0.9 -> 0. The beacon CER is worse
because the squelch now opens on beacons near 0 dB that it used to miss,
and they decode badly; at `snr_db=10` it is 60 % with noise still at 0
(and the bench's 0-3 dB cases a few % worse).

## The CW skimmer (shack_cw.skim)

Every keyed signal in a band's CW segment, decoded at once from one complex
stream: CW Skimmer's way, for OpenHamShack Radio (openhamshack
`docs/CW-SKIMMER-PLAN.md`). Receive only, and it imports nothing that can
key (a test checks `sys.modules`).

```python
from shack_cw.skim import Skimmer
sk = Skimmer(96_000, lo=14_000_000, hi=14_070_000, centre=14_035_000,
             max_channels=64, country=lookup, band="20m")
spots = sk.feed(block, t=utc)      # cwspot items, each with `rev`
sk.snapshot()                      # the cwskim item: channels, carriers, spotsRev
```

- `frontend`: one 5 ms STFT over the segment with the decoder's own window,
  plus a 40 ms one for detection.
- `detect`: candidates from p90/p25 over 4 s against a floor taken in 2 kHz
  blocks, then tracks, merging, carriers, and the birdie list.
- `pool`: both engines on every signal by default (`engine="both"`): the
  neural `Bank` and a classic `Decoder` through `amplitudes()` +
  `feed_frames`, from one gather, the strongest first, up to
  `max_channels`. Each prints its own text (`pool.texts_by`); the
  snapshot shows the network's. `engine="classic"` or `"nn"` runs one.
  Beside the network the classic engine reads only the first
  `CLASSIC_MAX` (64) channels, the strongest: both on 256 busy channels
  would need more than the worker's one core. The network wants an
  optimised BLAS (OpenBLAS): with Debian's reference BLAS a channel costs
  twice as much.
- `calls`: callsigns and spots.
  - Extraction: CQ, DE, TU, twice over, beacons; `<OTHER> DE <ME>` is never
    a spot.
  - Validation, the spot rule (constants at the top of calls.py, with the
    measurements behind them): the callsign check and seen-twice. Two
    decodes, by either engine, for a call in the callsign list
    (`history`: `scp`'s RBN week and MASTER.SCP, the logbook, calls
    confirmed this session); three for any other call in an ITU shape and
    a DXCC entity. Busted calls (Morse-alike) beside a stronger confirmed one.
  - Each engine's words are judged as a stream of their own, so one
    engine's letters never complete the other's call.
- `scp`: the callsign list: the calls the RBN spotted in a recent week
  (shipped in the LM's `nn/cwlm.npz`) plus MASTER.SCP if there is one,
  re-read when it changes; `fetch()` downloads MASTER.SCP the way
  supercheckpartial.com's developer guide asks (at most daily,
  conditional GET). Never bundled: no terms allow it.
- The neural channels read with the language model (`pool.SKIM_LM`); the
  live readers elsewhere stay greedy.
  - Real fists are taken apart and mended: "TIQCQDESM5D", "DE OE 7AJI".
- `runner`: all of it in a child process (spawn), or a thread. `put()`
  never blocks: a full queue drops and counts. A dead worker is restarted
  with backoff.
- `synth`: bands of many stations with ground truth; `selftest_band()` for
  a frozen build.
- `bench`: `python3 -m shack_cw.skim.bench cpu|synth|corpus`.
- `tools/skim_eval.py`: the spot rule's variants on the IQ library's
  RBN-labelled recordings (office), every hit replayed through `Calls`.

`amplitudes(c, m, hist)` is the step from per-frame complex sums to what
`Decoder.feed_frames` takes. `Decoder.feed` calls it too, so the skimmer
runs the decoder's arithmetic and the bench measures it.

Measured 13/10/2026 on ohs-dev (Xeon E5-2687W v4 VM):

| | |
|---|---|
| CPU at 96 kS/s, front end | 5.4 % of a core |
| per decoded channel | 0.31 % |
| 64 channels | 25.5 % (plan: ~22 %) |
| synthetic 20 m, 30 stations | 27 found, none wrong, frequency within 10 Hz, speed median 3 % |
| Kiwi corpus, 18 RBN-spotted calls | 3 spotted, 7 in the channels' text (the single decoder's own 7); no spot on 43 quiet targets |
| ohs-box 20 m, 600 s, 80 m dipole | 17 confirmed spots, 3 of the RBN's 39 calls; most of the rest are callers in a pileup, which the RBN does not spot |

## The neural engine (shack_cw.nn)

`Decoder(engine="nn")` (and `Skimmer(..., engine="nn")`, `SkimRunner(...,
engine="nn")`) keeps the front end above (AFC, the 5 ms frames at the tone
and its noise references) and the squelch, and replaces the levels and
timing with a small causal network: `amplitudes()` at four coherent
windows as log ratios to the noise beside the tone, a strided convolution
to 50 frames a second, seven residual dilated convolutions (about 5 s of
context), CTC over the alphabet and prosigns, and a speed head. NumPy only
at run time; the weights are `nn/cwnn.npz` (73 k parameters, 270 kB), our
own, trained on synthetic bands only (`tools/cw_neural`). Every layer
keeps just enough of its past to stream, so 75 ms blocks give what a whole
recording gives, and a skimmer runs every channel in one call a block.

Choosing it is one setting; the events, properties and `snr_db` are the
classic engine's. `feed_frames` stays classic-only (the network needs
every reference, not three amplitudes): a skimmer's pool runs a
`shack_cw.nn.Bank` over its channels' sums instead.

Settings the network does not learn were chosen on real KiwiSDR
recordings kept apart from the decoder bench (`tools/cw_neural/realval.py`:
128 RBN-spotted calls, 47 NCDXF slots, in the IQ library as
`20261013-kiwisdr-*-cwval*`), never on the bench's clips, which are the
test set: a word-space bonus of 1.0, the squelch at the classic 6 dB.

`python3 -m shack_cw.nn.cpu` measures both engines on one core.

Scored once a candidate was chosen, on the decoder bench's 136 real clips
(ohs-libs research/cw-decoder-bench, `tools/decoder_bench/score.py`; our
squelch in front, 13/10/2026):

| | classic | nn | DeepCW (AGPL, reference only) |
|---|---|---|---|
| RBN calls as a word / anywhere, of 78 | 16 / 24 | 23 / 27 | 24 / 33 |
| ... 20 m (46) | 8 / 13 | 11 / 14 | 16 / 21 |
| ... 30 m (17) | 6 / 8 | 10 / 10 | 7 / 9 |
| ... 17 m (8) | 2 / 2 | 2 / 2 | 1 / 2 |
| ... 40 m (4), 12 m (2), 60 m (1) | 0 / 1 | 0 / 1 | 0 / 1 |
| ... SNR < -5 dB in 2.5 kHz (29) | 2 / 2 | 3 / 3 | 3 / 4 |
| ... -5 to 5 dB (20) | 2 / 7 | 7 / 10 | 6 / 10 |
| ... 5 to 15 dB (14) | 4 / 5 | 6 / 6 | 4 / 7 |
| ... 15 dB and over (15) | 8 / 10 | 7 / 8 | 11 / 12 |
| NCDXF slots: copied / CER, of 29 | 17 / 32 % | 15 / 25.3 % | 13 / 21 % |
| ... -5 to 5 dB (18) | 10 / 37 % | 9 / 24 % | 10 / 15 % |
| ... 5 to 15 dB (7) | 4 / 30 % | 3 / 31 % | 3 / 31 % |
| ... 15 dB and over (4) | 3 / 12 % | 3 / 19 % | 0 / 31 % |
| characters a minute, quiet targets / beacon gaps | 0.2 / 0.1 | 0.0 / 0.1 | 1.5 / 3.2 |

On the realval dev set (128 calls, 47 slots) it copied 49 calls as words
against the classic engine's 45, and beacons at 16 % CER against 39 %.

CPU, one core, ms a second of signal (% of a core):

| | ohs-dev (Xeon E5-2687W v4 VM) | shack (Ryzen 9 6900HX) |
|---|---|---|
| classic, one decoder on audio | 7.9 (0.79 %) | 3.4 (0.34 %) |
| nn, one decoder on audio | 10.0 (1.00 %) | 4.7 (0.47 %) |
| classic, a skimmer channel (`feed_frames`) | 2.4 (0.24 %) | 1.2 (0.12 %) |
| nn, a skimmer channel in a bank of 64 | 0.94 (0.094 %) | 0.63 (0.063 %) |
| whole skimmer at 64 channels (`skim.bench cpu`) | nn 15.6 %, classic 27.4 % | |

Not measured on ohs-box's i5-9400T; the plan's classic figures put it
about 1.4 times ohs-dev, so about 0.13 % a skimmer channel.

### Round 2 (14/10/2026): what the weights can now say, and what did not help

The shipped weights are still round 1's. Weights now carry more than
numbers, so a candidate is tried by passing its file (`weights=`, or
`--weights` to the tools):

- `meta["front"]` picks the features (`FRONTS`): `v1`, or `v2` with an
  80 ms window and four bins one and two filter-bank bins either side of
  the tone. The decoder's mixer and the skimmer's gather sum whatever
  the weights read.
- `meta["ahead"]` marks blocks that look ahead (taps t-d, t, t+d): the
  same causal arithmetic with the residual taken d frames back, so
  characters come sum(d) later and nothing sees the future.
- Weights joined by `+` are an `Ensemble` (probabilities averaged).
- `Bank.space_lm` (default 0) adds a space model (`nn.space`, built by
  `tools/cw_neural/spacelm.py` from synthetic overs) to each word-space
  decision. No table ships.

Training data is now the front end's complex sums (any front from one
set), and round 1's lineage can be fine-tuned into the v2 front.

On the realval dev set none of these moved the call count beyond the
noise: round 1's soup 49 of 128 as words, its fine-tune on round 2's
data 50, the v2 front 48, a 0.3 s look-ahead 43-44 trained from scratch
and 44 fine-tuned, ensembles 47-49, the space model 50/46/43 at weight
0.5/1/2. Beacons moved within 15-17 % CER. What did turn up is a failure
the dev set barely holds: a second, weaker station on the same frequency
keying at the same time (a QSO partner, a pileup caller). With one
15 dB under a 38 dB signal 8 Hz away, the classic engine read every
letter; round 1's network printed rubbish already at 25 dB down. Synthetic
examples with such a station (`data.py --profile cochan`) fixed 25 dB
down and halved the errors at 15.

The one round-2 candidate scored on the bench (the test set's fifth
look in all), round 1's r4 fine-tuned on round 2's data and then on the
co-channel profile (`runs-r2/H-D-cochan/avg5.npz` on office):

| | classic | nn (round 1, shipped) | nn r2-H | DeepCW |
|---|---|---|---|---|
| RBN calls as a word / anywhere, of 78 | 16 / 24 | 23 / 27 | 21 / 26 | 24 / 33 |
| ... 15 dB and over (15) | 8 / 10 | 7 / 8 | 7 / 8 | 11 / 12 |
| NCDXF slots: CER, of 29 | 32 % | 25.3 % | 21.9 % | 21 % |
| ... -5 to 5 dB (18) | 37 % | 24 % | 22 % | 15 % |
| ... 15 dB and over (4) | 12 % | 19 % | 6 % | 31 % |
| characters a minute, quiet / beacon gaps | 0.2 / 0.1 | 0.0 / 0.1 | 0.0 / 0.1 | 1.5 / 3.2 |

A skimmer channel in a bank of 64 on shack: 0.068 % of a core for a v1
network, 0.084 % for v2.

### A language model in the search (nn.lm, 14/10/2026)

`Decoder(engine="nn", lm=0.5)` (or `Bank.lm = 0.5`) reads each channel
with a streaming CTC beam search that weighs the network against a
character n-gram of real CW: what calls and CW phrases look like. Off
by default (`LM = 0`): characters come out up to 0.5 s later.

The model (`nn/cwlm.npz`, 1 MB, order 6, Kneser-Ney) is built by
`tools/cw_lm/build.py` from the CW survey's stretches where both engines
agreed (12.5 k characters of real overs, re-spaced by `calls.segment`,
each call masked) refilled from 17.7 k calls the RBN spotted on CW in
the week 01-07/10/2026. It holds no text from the decoder bench's
recordings. `tools/cw_lm/dump.py` keeps the network's outputs on real
recordings and `evaluate.py` replays them through `Bank._read` at any
setting; every survey line is scored with a model built without its
own fold's text, the dev set with one built without the dev set's.

| weight 0.5, bonus 1 | greedy | LM |
|---|---|---|
| survey held-out lines (262, 19.7 h): RBN calls of 341 as a word / after `calls.segment` / anywhere | 106 / 119 / 142 | 127 / 134 / 150 |
| ... the 255 on the model's call list | 82 / 93 / 108 | 95 / 100 / 113 |
| ... the 86 not on it | 24 / 26 / 34 | 32 / 34 / 37 |
| survey: 15 s windows with an RBN call as a word | 294 | 353 |
| survey: letters changed where both engines agreed (CER against classic) | 7.5 % | 8.5 % |
| survey: call-shaped words the RBN spotted that day | 38 % | 42 % |
| dev set: RBN calls of 128 as a word / anywhere | 49 / 62 | 55 / 62 |
| dev set: NCDXF calls copied / CER, of 47 | 27 / 16 % | 31 / 14 % |
| characters a minute in beacon gaps, quiet targets | 0.0 / 0.0 | 0.1 / 0.0 |
| reading CPU a channel (on top of the network) | 0.03 % | 0.05 % |
| printed after it was heard, median / 95th | 0.1 / 0.7 s | 0.6 / 1.0 s |

Weight 0.25 found fewer calls (118 as words), 1.0 fewer (124) and
changed 10.5 % of the agreed letters. The same search with random calls
of the right shape in place of the RBN's list found 115 / 125 / 141 on
the survey and 47 / 59 on the dev set: the list carries the letters,
the text the spacing. Rebuild the model from a recent week of the RBN
archive now and then (`build.py --calls`, a call and its spot count a
line); a call first heard after the list was made gains only its
spacing.

## Tests

```
tools/test.sh          # makes ~/build/venvs/shack-cw on first run
```

Licence: GPL-3.0-or-later.
