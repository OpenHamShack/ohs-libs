# shack-dsp

Signal processing every OpenHamShack app with receive audio shares
(PLATFORM-PLAN §1.4: shared components, one of each). Today: **NR2**, the
spectral noise reduction the rig apps (Icom, Flex) run on the server, on the
audio people hear, so every client - web page, Remote on a TV, desktop -
gets it with no DSP of its own (Brian, 10/10/2026).

| Module | What |
|---|---|
| `shack_dsp.nr2` | `Nr2(rate, channels=1, level=50, key=None)`, the float core: `process(x)` with x of shape (n,) or (n, channels), returns what is ready, `delay` samples behind, whatever the blocking. `Nr2Stream(rate, channels=1, level=50, on=False)`: s16le PCM in and out, `on` and `level` settable while it runs, `latency_ms`. |
| `shack_dsp.bench` | `python3 -m shack_dsp.bench [--only snr,cw,edges,cpu] [--rate R]`: SNR gained on speech and CW in noise (shadow-filtered: the noisy audio's gains applied to the clean signal and the noise apart), CW decoding raw vs NR2 (with shack_cw installed), switching edges, CPU per second of audio. |

## Using it

```python
from shack_dsp import Nr2Stream
nr = Nr2Stream(rate=12_000, channels=1, level=50, on=persisted_on)
def on_packet(pcm: bytes):          # each radio packet, any length
    out = nr.feed(pcm)              # may be b"" for a packet while switching on
    if out:
        send_to_listeners(out)
nr.on = False                        # fades out over 50 ms; no click, no lost sample
nr.level = 70                        # 0 gentlest .. 100 strongest
```

- **The algorithm** is NR2's: 21 ms half-overlapped frames under a
  square-root Hann window, a minimum-statistics noise estimate (1.5 s),
  decision-directed a-priori SNR with a second (TSNR) step, the
  log-spectral-amplitude MMSE gain and a gain floor. With every gain 1 the
  output is the input exactly. `nr2.py`'s docstring says why each choice.
- **Strength** (`level` 0-100) moves the gain floor from -8 dB to -26 dB
  and the noise over-estimate from 1x to 2x together.
- **Off is free**: a pass-through with no delay and no work. On, the stream
  runs half a frame (11 ms) behind, and a sample waits up to one more
  half-frame for its hop: `latency_ms` is 21 at 12 and 24 kHz.
- **Stereo**: one gain per bin from the channels' mean power, applied to
  every channel, so the image is kept (Flex's 24 kHz stereo).
- **Decoders must not hear it.** Feed FT8/JS8/WSPR, SSTV, APRS and the CW
  decoder the raw audio. The bench's CW table is the evidence for CW: the
  shack_cw decoder reads the raw audio perfectly from 0 dB SNR (in 500 Hz)
  and NR2's at 0.53/0.13/0.03 character error rate at 0/3/6 dB, because
  the gain's attack and release reshape each element's edges, which is
  what the decoder times.
- **CPU**: about 18-23 ms per second of audio on ohs-dev at any of 8-48
  kHz (the frame scales with the rate), so cheap enough to run on the
  audio callback of an asyncio app: about 0.3 ms per 20 ms packet.

## Measured (ohs-dev, 10/10/2026, `python3 -m shack_dsp.bench`, 12 kHz)

| Signal | Level | SNR in | SNR out | Gain | Signal loss |
|---|---|---|---|---|---|
| speech, 300-2700 Hz noise | 50 | 0 | 11.3 | 11.4 | -2.4 |
| speech | 50 | 6 | 16.5 | 10.6 | -0.9 |
| speech | 100 | 6 | 18.0 | 12.1 | -1.1 |
| CW 20 WPM, 500 Hz filter noise | 50 | 0 | 7.7 | 7.7 | -6.8 |
| CW | 50 | 6 | 15.3 | 9.4 | -2.1 |
| CW | 100 | 6 | 16.4 | 10.4 | -3.0 |

Edges: 9 switches in random 40-600 sample packets, every sample out once,
worst step equal to the input's own. Tests: `tools/test.sh` (or the repo's).

Licence: GPL-3.0-or-later.
