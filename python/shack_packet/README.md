# shack-packet

The packet radio modem every OpenHamShack app shares (PLATFORM-PLAN §1.4:
shared components, one of each): the Packet Radio app (decided 17/10/2026),
the Icom app's APRS window, and the SDR receive monitor. Bell 202 AFSK at
1200 baud both ways, HDLC, the AX.25 frame codec, FX.25 and APRS payloads
(step 1); and on top of them the packet engine (step 2): channel access,
AX.25 2.2 connected mode, a digipeater, an APRS station, a personal
mailbox, IP over AX.25 with an encryption guard. No GUI and no radio: the
modem fills an array and the engine drives an abstract radio link, which
the app (and later the OpenHamShack controller, for a BBS or node)
supplies. Nothing here keys a radio unless the app has set a callsign and
enabled transmit, checked at the moment of keying.

| Module | What |
|---|---|
| `shack_packet.demod` | `Receiver(rate, diversity=True)`: audio (s16le bytes or float) in at any rate from 8 kHz, `Received(frame, sample, fx25, corrected)` out, each frame once. Two demodulators (direwolf 1.7's profile A front end and the Icom one) x seven slicers, each with a second-order bit clock, an HDLC framer and an FX.25 receiver. `flush()` at the end of audio. |
| `shack_packet.afsk` | `Modulator(rate, amplitude)`: line levels or data bits to phase-continuous audio, state carried between calls. `modulate(frames, rate, fx25_mode=None, txdelay_ms=300, tail_ms=30)` for a whole transmission; `pcm16()`. |
| `shack_packet.fsk` | G3RUH FSK at 9600, 19 200 and 38 400 baud (direwolf `-B` compatible), and the same up to 153 600 and as 4-FSK (`bits=2`): `Modulator(rate, baud)` (NRZI, the x^17+x^12+1 scrambler, a Gaussian BT 0.5 pulse), `modulate(frames, rate, baud=...)` to shaped baseband, `Receiver(rate, baud)` with demod.Receiver's interface. Below. |
| `shack_packet.modem` | The one switch: `ChannelConfig.mode`/`.baud` (or the link's) picks AFSK 1200, G3RUH or a wide mode; `Mode`, `MODES`, `mode("4fsk76800")`, `modulate`, `receiver`, `by_name("g3ruh9600")`. |
| `shack_packet.fsk_bench` | `python3 -m shack_packet.fsk_bench [--only occupied,loopback,cpu,throughput]`: the fast modes through the Pluto's own IQ path, and the engine's throughput on the simulated channel with a Pluto's timing. |
| `shack_packet.ax25` | `Frame` (build, `ui`, `from_monitor`, `encode`, `monitor`), `parse(bytes, modulo=8)`, `Address`, `Control` for I, RR/RNR/REJ/SREJ, SABM/SABME/UA/DM/DISC/FRMR/UI/XID/TEST with P/F at modulo 8 and 128, the PIDs (`PID_NONE`, `PID_IP`, `PID_ARP`, `PID_NETROM`...). The codec only; the connected-mode state machine comes next. |
| `shack_packet.fx25` | Reed-Solomon over GF(2^8) (RS(255,239/223/191) and the shortened codes), the eleven correlation tags, `frame_bits` (None when a frame is too long, so it goes as plain AX.25), `Fx25Receiver`. |
| `shack_packet.hdlc` | FCS, stuffing, flags, NRZI both ways; `Framer`. Frames above this layer never carry their FCS. |
| `shack_packet.aprs` | APRS payloads: positions (plain, compressed, ambiguous), mic-E, messages, objects, items, weather, telemetry. Problems, symbol names and mic-E messages are keys the app translates (`PROBLEMS`, `SYMBOLS`, `MIC_E_*`). |
| `shack_packet.bench` | `python3 -m shack_packet.bench [--only ramp,fx25,synth,cpu,slips]`: the tables below. |
| `shack_packet.slips` | `python3 -m shack_packet.slips sent.wav received.wav`: where a transmit audio path repeated or skipped samples, from a keyed over's saved audio and what came back off the air (`find_slips`, `apply_slips`). Written for the IC-9700's USB codec. |
| `shack_packet.engine` | `Engine(station, radio)`: one channel's station. `connect(call, via=...)` and `listen(call, accept)` give a `Connection` (a byte stream: `send`, `on_data`/`read`, `close`, events); `poll()` does what is due and says when to call again; `Runner(engine)` does that on the wall clock. |
| `shack_packet.channel` | `ChannelAccess`: p-persistent CSMA (TXDELAY, TXTAIL, SLOTTIME, PERSIST), busy from our DCD or the radio's squelch (waited on for 15 s at most, `MAX_BUSY_WAIT_S`), TXDELAY never below the link's measured turnaround. `RadioLink`: what a radio link implements (key/unkey/send_pcm, or send_frames for KISS; audio, frames and busy back). |
| `shack_packet.datalink` | `DataLink`: the AX.25 2.2 SDL - SABM/SABME, UA, DM, DISC, I, RR/RNR/REJ, T1/T3/N2, k, modulo 8 and 128, XID, FRMR received, RNR flow control, digipeater paths; the 2.2 segmenter (in `engine.Connection`). |
| `shack_packet.digi` | `Digipeater`, off by default: own call, aliases, WIDEn-N, UI duplicate suppression, preemptive drop/mark. |
| `shack_packet.aprs_station` | `AprsStation`: beacons with floors and SmartBeaconing, messages with ack/rej and retry back-off, dedupe, the heard table. |
| `shack_packet.mailbox` | `Mailbox`, off by default: a TNC-style PMS (L/R/S/K/H/B) behind `Store` (`MemoryStore`, `FileStore`). Its words go through gettext (`i18n`, domain shack-packet). |
| `shack_packet.ip`, `.guard` | `IpInterface`: IP over AX.25 (RFC 1226), datagram and virtual circuit, ARP over AX.25, routes; IP packet in/out (the TUN device is the app's). `EncryptionGuard`: refuses evidently encrypted traffic before it can be keyed (UK licence clause 11(2)); its docstring says what it misses. |
| `shack_packet.identity` | `Station`: the callsign (the ITU shape, not an alias) and the transmit switch, both required. |
| `shack_packet.sim` | `Medium`, `Simulation`: N stations sharing a frequency in simulated time, as real audio through the modem with noise and collisions, or as frames with airtime, loss and collisions. |
| `shack_packet.radios` | Real radio links (step 3, the `radios` extra: shack-rig): `IcomLink`, `YaesuLink`, `SoundcardLink`, `PlutoLink` on one safety core (`base.AudioRadioLink`), and `turnaround.measure`. Below. |
| `shack_packet.sdr` | The SDR monitor (step 4), receive only: IQ sources (`RtlSdrSource`, `RtlTcpSource`, `PlutoSource`, `FileSource`, `SyntheticSource`), the channel plan as data, an FFT channeliser, `Pipeline`, `Monitor` (the tables the app draws) and `Runner`. Below. |
| `shack_packet.returnpath`, `.linktest` | A link test with only one RF direction (LINK-TEST.md): `ReturnPath`, attached per remote station (`Engine.attach_return_path`), carries the RF receiver's answers by internet, off by default and fail-safe; `RelayClient`, `RelayReturnPath` and `Telemetry` reach the relay (`tools/packet_relay.py`, on the controller box) outwards and stream both ends' link data. `Engine.taps` is the telemetry hook. |
| `shack_packet.netrom` | NET/ROM NODES broadcasts read (`parse_nodes`): the node, its alias, its routes. |
| `shack_packet.ohssession`, `.filexfer` | Files between OpenHamShack stations over a connected session (FILES.md): `FileService(engine, inbox)` attaches to a `Connection`, `send_file`, `accept`/`decline`/`cancel`/`resume`; frames in the text stream, DEFLATE, resume by SHA-256, the whole file checked. `OhsSession` also compresses long text once both ends say HELLO with "z". |
| `shack_packet.yapp` | YAPP and YAPP-C for other packet software (`YappHandler(service)`), sharing the file service's transfers and inbox. |
| `shack_packet.sevenplus` | 7PLUS: `encode(data, name)` to BBS-postable parts, `Collector`/`decode` from any text holding them. |

## Using it

```python
import shack_packet as sp

rx = sp.Receiver(48_000)
def on_audio(pcm: bytes):                 # any packet size
    for heard in rx.feed(pcm):
        frame = sp.parse(heard.frame)     # None if it is not AX.25
        if frame is not None:
            show(frame.monitor(), fx25=heard.fx25, fixed=heard.corrected)

frame = sp.Frame.from_monitor("GM4BRI-9>APRS,WIDE1-1:>on the air")
audio = sp.modulate([frame.encode()], 48_000, fx25_mode="auto", txdelay_ms=250)
radio.send_audio(sp.pcm16(audio))         # under the app's TX lease
```

`fx25_mode`: None for plain AX.25; "auto" picks the code by size as
direwolf does; 16, 32 or 64 asks for that many check bytes. Any frame too
long for FX.25 (more than about 230 bytes after stuffing) goes as plain
AX.25, so ordinary stations always decode it - FX.25 stations hear plain
frames and plain stations hear FX.25 ones.

## Measured (shack, Ryzen 9 6900HX, 17/10/2026, direwolf 1.7)

| | Icom demodulator | here | direwolf atest | atest -P E+ |
|---|---|---|---|---|
| `gen_packets -n 30` ramps, five amplitudes, 12 kHz (of 150) | 36 | **61** | 54 | 57 |
| the same at 44.1 kHz | - | 106 | 104-105 | 104 |
| our noise and twist set (of 600, below) | 13 | **559** | - | - |
| WA8LMF TNC Test CD track 1 (flat, 26 min of 144.39) | - | **1008** | 1005 | 1015 |
| WA8LMF track 2 (the same, de-emphasised) | - | **988** | 983 | 1002 |
| WA8LMF track 4 | - | **107** | 102 | 107 |

Ahead of direwolf's default everywhere; level with or a little behind its
nine-slicer `-P E+` on real recordings. The WA8LMF CD is a dev-only
download (`python -m shack_packet.bench --wav track2.wav`), never committed.

**Our noise and twist** (40 frames a cell, white noise, SNR in a 6 kHz
band; twist is what a radio's pre- or de-emphasis does to the tones):

| twist dB/oct | 3 dB | 4 dB | 5 dB |
|---|---|---|---|
| -9 | 37 | 40 | 40 |
| -6 | 33 | 40 | 40 |
| 0 | 28 | 40 | 40 |
| +6 | 33 | 39 | 40 |
| +9 | 31 | 38 | 40 |

559 of 600 with both demodulators, 450 with one (`diversity=False`: the
direwolf-style front end, best on real recordings, weaker under steep
twist). With the sender's baud rate out by -2/-1/+1/+2 %: 559/557/533/537
(the bit clock tracks it; the Icom design's lost nearly everything at 2 %).
demod.py's docstring has the measurement behind every stage, and what did
not help.

**FX.25** (40 frames a cell, no twist; in brackets, decoded through FX.25):

| check bytes | 0 dB | 1 dB | 2 dB | 3 dB | 4 dB |
|---|---|---|---|---|---|
| plain AX.25 | 0 | 0 | 8 | 30 | 40 |
| 16 | 3 (3) | 4 (4) | 36 (35) | 40 (40) | 40 |
| 32 | 5 (5) | 24 (24) | 40 | 40 | 40 |
| 64 | 15 (15) | 40 (40) | 40 | 40 | 40 |

On direwolf's FX.25 ramps (`-X 16/32/64`, amplitude 50 %): 13/14/15
against atest's 13/13/14.

**Interop with direwolf**, both ways, in `tests/test_direwolf.py` (skipped
without direwolf): `gen_packets` plain and `-X 16/32/64` at 12, 44.1 and
48 kHz decode packet for packet here, reported as FX.25 with the right
code; our modulator's audio at 8, 12, 44.1 and 48 kHz, plain and FX.25,
decodes in `atest`, every FX.25 block through its FEC; `decode_aprs` agrees
with our APRS positions to 1e-5 degrees.

**CPU** per channel, per second of audio, noise only (the worst case),
pure Python and numpy on one core:

| | 12 kHz | 48 kHz |
|---|---|---|
| two demodulators (default) | 23 ms (2.3 %) | 19 ms (1.9 %) |
| one demodulator | 9 ms (0.9 %) | 10 ms (1.0 %) |

So the SDR monitor's ~16 channels cost about a third of one core with both
demodulators, a sixth with one - no Rust or C path is needed for the
modem. (48 kHz is cheaper than 12 because it is decimated to 9.6 kHz; an
SDR monitor that produces its channels at 9.6 kHz gets the 48 kHz figure.)
What costs, in order: the bit clocks (a Python loop per transition, 14 per
channel), the HDLC framers (a Python call per bit), the FX.25 tag searches
and the AGCs; the filters are numpy and cheap. If the monitor ever needs
more, those four loops are the ones to move to compiled code. Turning the
SDR's IQ into 16 FM audio channels is the monitor's own cost and is not
measured here.

## The fast modes: G3RUH FSK (18/10/2026)

`ChannelConfig(baud=9600)` (or 19 200, 38 400) and a link that carries
baseband - so far `PlutoLink(..., baud=9600)` - make a station speak
G3RUH-compatible FSK; 1200 AFSK stays the default and nothing else changes.
Deviation is +-3 kHz at 9600 and in proportion above it (h = 0.625):

| baud | deviation | occupied (99 %) | beyond +-12.5 kHz | fits |
|---|---|---|---|---|
| 9600 | +-3 kHz | 10.4 kHz | -52 dB | a 25 kHz channel |
| 19 200 | +-6 kHz | 20.7 kHz | -26 dB | 50 kHz |
| 38 400 | +-12 kHz | 41.5 kHz | -6 dB | 100 kHz (23 cm data) |

**Direwolf 1.7, both ways** (`tests/test_fsk_direwolf.py`): its
`gen_packets -B` at all three speeds, plain and FX.25, decodes packet for
packet here, and ours decodes in `atest -B`. On its `gen_packets -n 20`
noise ramp: 16/16/13 here against atest's 16/15/12 at 9600/19 200/38 400.

**Through the Pluto's IQ path** (`fsk_bench`, shack under load, 18/10/2026):
the link's own FskModulator at 2.4576 MS/s, white noise, its
FskChannelReceiver and fsk.Receiver; 30 frames a cell, each its own over
with a 30 ms preamble after a second of noise. CNR is in the occupied
bandwidth (Eb/N0 is 0.35 dB more).

| baud | offset | 8 dB | 10 dB | 11 dB | 12 dB | 14 dB |
|---|---|---|---|---|---|---|
| 9600 | 0 | 0 | 6 | 20 | 25 | 30 |
| 9600 | +3 kHz | 0 | 1 | 17 | 27 | 29 |
| 9600 | -3 kHz | 0 | 1 | 13 | 24 | 29 |
| 19 200 | 0 | 1 | 7 | 22 | 25 | 30 |
| 19 200 | +3 kHz | 0 | 10 | 25 | 30 | 30 |
| 19 200 | -3 kHz | 0 | 9 | 21 | 28 | 30 |
| 38 400 | 0 | 0 | 15 | 26 | 28 | 30 |
| 38 400 | +3 kHz | 0 | 16 | 24 | 28 | 29 |
| 38 400 | -3 kHz | 0 | 13 | 24 | 30 | 29 |

| CPU, ms a second of IQ | 9600 | 19 200 | 38 400 |
|---|---|---|---|
| receive, shack (Ryzen 9 6900HX, load ~20) | 105 | 83 | 116 |
| transmit, shack | 99 | 102 | 105 |
| receive, workbench (i5-13420H, idle) | 54 | 54 | 76 |
| transmit, workbench | 52 | 56 | 58 |

The CPU rows are with `OPENBLAS_NUM_THREADS=1`. With OpenBLAS's default
threads, its idle threads spin after every FX.25 tag search (a small
matrix product per slicer per block) and the process time at 38 400 baud
was 329 ms on shack: other cores' time, not the receiver's thread, but
real CPU. It is the same at 1200 baud, only rarer (100 ms blocks).

A frame decodes reliably from about 12 dB CNR at every speed; +-3 kHz off
frequency (what is left of a crystal error after its correction) costs
little. CPU is per second of IQ, one core, all of the receive path (the
channeliser's FFT, the discriminator, the receiver) or the transmitter:
all three speeds keep up in real time with room to spare.

## The wide modes: GFSK to 153 600 baud, 4-FSK (18/10/2026, OHS-to-OHS only)

A Pluto's limit is the IQ samples crossing USB, not the baud, and
PlutoLink pauses receive during its own overs, so one 2.4576 MS/s stream
carries far more than 38 400 baud: `PlutoLink(..., mode="4fsk76800")` and
`ChannelConfig(mode=...)` (modem.py). The bit stream is G3RUH's (NRZI,
scrambler, HDLC, FX.25 if asked); only what carries it changes. Eight
samples a symbol at the modem (decimation 8/4/2 from the IQ rate: 32 and
16 IQ samples a symbol at 76 800 and 153 600). Nobody else speaks these.

| mode | baud | kbit/s | deviation | pulse | occupied (99 %) | fits |
|---|---|---|---|---|---|---|
| gfsk76800 | 76 800 | 76.8 | +-24 kHz (h 0.625) | Gaussian BT 0.5 | 83.0 kHz | a 150 kHz 23 cm DD channel |
| gfsk153600 | 153 600 | 153.6 | +-48 kHz (h 0.625) | Gaussian BT 0.5 | 166.2 kHz | 13 cm 2311-2315 (200 kHz); **not** 23 cm |
| 4fsk38400 | 38 400 | 76.8 | +-24 kHz outer | raised cosine | 70.5 kHz | a 150 kHz 23 cm DD channel |
| 4fsk76800 | 76 800 | 153.6 | +-48 kHz outer | raised cosine | 141.0 kHz | a 150 kHz 23 cm DD channel |

GFSK keeps G3RUH's index and shape: at 153 600 baud h 0.5 was 158 kHz and
2 dB worse, BT 0.35 at h 0.5 fitted 146 kHz but needed 4 dB more. 4-FSK
is M17's Gray-coded levels with the outer pair at 0.625 x the symbol
rate, no sync word of its own (HDLC's flags and the self-syncing
descrambler find frames wherever they fall in a pair), and is not
polarity-free (an SDR knows its own). Modes over 100 kHz sit 200 kHz from
the LO (not 100), and the TX analogue filter asked for grows with the
signal (`tx_bandwidth`, 350-750 kHz).

**IQ loopback** (fsk_bench, shack, 18/10/2026; 30 frames a cell, each its
own over acquired from cold; CNR in the occupied bandwidth):

| mode | offset | 8 dB | 10 dB | 11 dB | 12 dB | 14 dB |
|---|---|---|---|---|---|---|
| gfsk76800 | 0 / +3 / -3 kHz | 1 / 0 / 0 | 13 / 18 / 15 | 23 / 24 / 23 | 28 / 29 / 29 | 30 / 30 / 30 |
| gfsk153600 | 0 / +3 / -3 kHz | 0 / 0 / 0 | 15 / 17 / 14 | 25 / 25 / 24 | 29 / 28 / 30 | 30 / 30 / 30 |

| mode | offset | 12 dB | 14 dB | 16 dB | 18 dB | 20 dB |
|---|---|---|---|---|---|---|
| 4fsk38400 | 0 / +3 / -3 kHz | 0 / 0 / 0 | 4 / 2 / 5 | 23 / 16 / 18 | 29 / 26 / 28 | 30 / 30 / 30 |
| 4fsk76800 | 0 / +3 / -3 kHz | 0 / 1 / 0 | 2 / 2 / 5 | 24 / 22 / 15 | 28 / 28 / 27 | 30 / 30 / 30 |

The GFSK modes decode like G3RUH's (reliable from about 12 dB); 4-FSK
needs about 18 dB, 5-6 dB more, the usual price of four levels to a
discriminator - so per hertz 4fsk76800 carries twice gfsk76800's bits,
but per watt gfsk is the better buy. Eb/N0 is CNR + 10 log10(occupied /
bit rate): +0.3 dB for GFSK, -0.4 dB for 4-FSK.

**CPU**, ms a second of IQ, one core, `OPENBLAS_NUM_THREADS=1` (receive is
the channeliser, discriminator and receiver; RX and TX never run at once):

| | g3ruh38400 | gfsk76800 | gfsk153600 | 4fsk38400 | 4fsk76800 |
|---|---|---|---|---|---|
| receive, shack (Ryzen 9 6900HX, load 0.7) | 58 | 95 | 169 | 79 | 134 |
| transmit, shack | 57 | 62 | 70 | 59 | 63 |
| receive, workbench (i5-13420H, idle) | 75 | 123 | 221 | 106 | 183 |
| transmit, workbench | 55 | 58 | 66 | 56 | 59 |

Every mode keeps up in real time with at least 4x to spare on either host.
Receive-only on both Plutos (shack's Rev.B, workbench's nano on RNDIS),
1299.525 MHz, all four wide modes: TX read back quiet at open and close,
receive kept 99.1-99.2 % of real time, no gaps over 71 ms.

## Fast-mode throughput, and why it was 39 % (18/10/2026)

The keyed 38 400 baud transfer's log (10 KB in 5.5 s, k 16, PACLEN 256,
TXDELAY 200 ms) spent each window cycle like this: 0.93 s of our over
(0.2 of it TXDELAY), 0.6 s from its end to the RR being heard, 0.2 s more
before the next over. The fixed part, from the link's own timing: TXDELAY
200 ms each way (and the channel would not go below the Pluto's assumed
100 ms turnaround plus 80 ms however it was set); busy, the channel power
averaged over 100 ms and seen a 50 ms block late, holding each station
~150 ms after the other's carrier dropped, twice a cycle; 50 ms drain,
85 ms receive resume, 75 ms receive pipeline. A 16 x 256-byte window is
0.85 s at 38 400 and 0.21 s at 153 600, so at the new rates the
turnaround is most of the time. And at 38 400 and up a lost frame was
expensive: REJ resends everything after it.

What changed: TXDELAY's floor is the turnaround plus the mode's lock bits
(2-FSK locks in 32-64 bits, 4-FSK in 256; 96 and 384 kept), not 80 ms;
busy on a fast channel averages 20-30 ms (as many samples as the 12.5 kHz
channel's 100 ms); selective reject between two OHS stations
(`LinkConfig.srej`, `--srej`); and windows of 63 (modulo 128, at most half
the modulus for SREJ) and PACLEN 512 (up to 2048 allowed). On the
frames-mode simulated channel with the Pluto's timing (`sim.PLUTO`,
taken from that run's log; 10 KB at 38 400 as keyed: 5.0 s simulated
against 5.5 s on the air), 100 KB, % of the bit rate, three seeds
(`fsk_bench --only throughput`):

| mode | settings | clean | BER 1e-5 | BER 1e-4 |
|---|---|---|---|---|
| g3ruh9600 | as keyed / tuned | 74 / 91 | 59 / 84 | 17 / 37 |
| g3ruh38400 | as keyed / tuned | 47 / 87 | 37 / 79 | 9 / 44 |
| gfsk76800, 4fsk38400 | as keyed / tuned | 32 / 83 | 25 / 73 | 5 / 39 |
| gfsk153600, 4fsk76800 | as keyed / tuned | 19 / 74 | 15 / 62 | 3 / 29 |

"As keyed" is k 16, PACLEN 256, TXDELAY 200 ms, REJ, 100 ms busy;
"tuned" is k 63, PACLEN 512, SREJ, TXDELAY at its floor with a **30 ms
Pluto turnaround that is assumed, not measured** (the keyed test's
`--turnaround-ms` says), busy 20-30 ms. So 153.6 kbit/s should move about
110 kbit/s of data on a clean channel. The frames-mode channel does not
know which modulation it is, so a 4-FSK row is its GFSK twin at the same
bit rate; its BER at a given CNR is the loopback table's business. SREJ
alone, BER 1e-5, k 63 PACLEN 512: 33 -> 79 % at 38 400, 28 -> 61 % at
153 600. A shorter T1 floor (0.5 or 0.3 s against 1 s) changed nothing:
T1 rarely fires; the window's go-back-N did the damage.

## Where the wide modes may go (UK)

RSGB band plan, 2026 edition (effective 1 January 2024; the PDF is in
~/build/packet-02-map/research/rsgb_band_plan_2026.pdf, 23 cm p.23, 13 cm
p.25):

- **23 cm 1299.000-1299.750 MHz**: "150 kHz, DD High Speed Digital Data -
  5 x 150kHz channels: 1299.075, 1299.225, 1299.375, 1299.525, 1299.675 MHz
  (+/- 75 kHz)". The same at **1241.000-1241.750** (1241.075 ... 1241.675).
  Licence notes: Amateur Service secondary; specific conditions within
  1240-1325 MHz near Bude and Harrogate and in Northern Ireland; note 8,
  1240-1300 is "subject to major replanning". Nothing on 23 cm is wider
  than 150 kHz.
- **1298.000-1299.000 MHz** is "20 kHz, All Modes ... General mixed analogue
  or digital use in channels 1298.025-1298.975 MHz (RS1-RS39)": 1298.500 is
  a 20 kHz channel. The 38 400 baud run there on 18/10 (41.5 kHz) was out
  of plan, and every wide mode would be.
- **13 cm 2310.000-2320.000 MHz**, 200 kHz: "2,311.000-2,315.000 MHz High
  speed data". Also 2322-2350 "Wideband Modes, including data, ATV"
  (unchannelled, shared with ATV) and, with a Full-licence NoV,
  2300.400-2301.800 (500 kHz, "Wideband Data/DATV - 2301.100 MHz"). Licence
  notes: secondary, "Users must accept interference from ISM users";
  "Operation in 2310-2350 and 2390-2400 MHz are subject to specific
  conditions and guidance"; Bude/Harrogate. Avoid 2320-2322 (narrowband,
  beacons, FM/DV) and 2400-2450 (satellites, ATV, ISM).

A Pluto's LO leakage and IQ image sit `ifo` and `2 x ifo` below the
channel, so the plan has to hold those too. Proposed, for Brian to approve:

- **1299.525 MHz** (23 cm DD channel 4) for gfsk76800, 4fsk38400 and
  4fsk76800 (and g3ruh38400, instead of 1298.5): with the 200 kHz offset
  the LO is at 1299.325 and the image at 1299.125, with 100 kHz at 1299.425
  and 1299.325 - all inside 1299.000-1299.750. (1299.225 would put a wide
  mode's image at 1298.825, outside.)
- **2312.000 MHz** (13 cm high speed data) for gfsk153600, the one mode
  too wide for 23 cm: LO 2311.800, image 2311.600, inside 2311-2315.

`tools/fastfsk_test.py` refuses to key any mode wider than 25 kHz unless
its whole occupied bandwidth is inside one of those segments
(`BAND_SEGMENTS`; `--allow-out-of-plan` overrides, Brian's say-so only),
and logs the LO and image if they fall outside.

## Link adaptation (18/10/2026, OHS-to-OHS)

The first 4fsk76800 transfer on the air (Brian present) came through
intact at 16.9 kbit/s: the nano's weak direction decoded 7 UI frames in
50 at 4fsk76800 (47/50 at gfsk76800), so its RRs were lost and the sender
sat in T1 (nine expiries). `adapt.py` makes each direction run at the
fastest mode its receiver can decode; its docstring has the design and
what was rejected. In short:

- **The receiver of each direction decides** and asks in an XID command
  (private PI 0xF0: version, top mode, requested mode; others skip it), at
  connect and on every change, resent until answered. The sender sends I
  frames at the last mode asked for. One authority per direction.
- **The control path never adapts**: every S and U frame (RR, REJ, SREJ,
  XID, SABME, UA, DISC, UI) goes at the ladder's floor, g3ruh9600. An over
  carries its floor part first, then its data part with its own preamble.
- **Evidence**: the level (C/N0 from each frame's channel power, falling at
  once), a quarter of a burst newly missing, a poll with nothing new since
  our last ack; up after a clean burst when the level clears the next mode
  by 3 dB (4 dB hysteresis), the wait doubling when an up-step fails for
  loss.
- **Ladder** by the C/N0 each mode needs: 4fsk76800 (69.5 dB-Hz) >
  gfsk76800 (61.2) > g3ruh38400 (58.2) > g3ruh19200 (55.2) > g3ruh9600
  (52.2). 4fsk38400 (66.5) is left out: gfsk76800 carries the same bit rate
  on 5 dB less. A gfsk153600 top (13 cm) goes gfsk153600 > gfsk76800 > ...
- **Receive**: one channel filter and demodulator per listened mode (the
  floor, the request, the one below, the last one for 10 s), the engine
  sorting their frames into air order. CPU, ms a second of IQ, 3/4/5
  modes: shack 329/369/478, workbench 386/483/546: real time on both.
- An over carries at most 2 s of I frames, so at slow modes the evidence
  comes every couple of seconds; slowing down resends what is outstanding
  at once.

Simulated (`fsk_bench --only adapt`): 100 KB, kbit/s of data, k 63,
PACLEN 512, SREJ, the Pluto's timing, three seeds; each frame's chance
from a fit to the IQ loopback (`sim.frame_success`), C/N0 per direction:

| channel | adaptive | fixed 4fsk76800 | fixed g3ruh9600 |
|---|---|---|---|
| A->B 74, B->A 62 dB-Hz (18/10's case) | **100.7** | 36.3 | 6.9 |
| A->B 74, B->A 68 | 100.7 | 104.4 | 6.9 |
| 74/68, 8 dB fades 4 s in 10 | **49.7** | 45.1 | 6.9 |
| 74/68, 15 dB fades 4 s in 10 | 41.6 | 56.0 | 6.9 |
| 66/60, 6 dB fades 3 s in 15 | **31.6** | failed | 6.9 |

Adaptive wins wherever the top mode cannot carry one direction or the
link is weak; it loses to a fixed top on deep, regular fades, where riding
them out in T1 and then sending at full speed beats climbing back up for a
couple of seconds after each one.

Keyed (fastfsk_test, both ends): `--adapt [--top-mode 4fsk76800]
[--floor-mode g3ruh9600] --window 63 --paclen 512 --srej`. The stats line
shows each direction's mode, the steps and frames heard by mode.

## Path test, PTT output, amplifier, a Pluto on the controller (18/10/2026)

- **pathtest.py**: one direction at a time (the cable moved by hand):
  numbered UI frames per mode, a receiver hearing every mode at once and
  counting decoded/missing/C/N0/offset/bad FCS, a text report, and
  `best_mode()` to seed `LinkConfig.adapt_start`; `drive_steps()` for
  setting an amplifier's drive by hand.
- **PTT output** (`PlutoLink(ptt_output=...)`, off by default): gpo0-3
  driven here with relay lead and tail, or "firmware" for PlutoDVB2's and
  Tezuka's own GPO0; the guard drops the GPOs. radios/pluto.py's docstring
  has the sequencing and what each firmware does with its GPOs. Which
  pins reach a connector on each board is not verified.
- **Amplifier** (`max_tx_gain_db`): a drive cap refused at open and at
  every write, and kept by the guard ("quiet overdrive").
- **A Pluto shared by the controller box** (`pluto://host?iiod=30431&ssh=30422`,
  shack_pluto.endpoint): iiod and SSH on their own ports; the guard waits
  25 s for PlutoDVB2's slow SSH.
- **fastsettings.py**: every setting above as one dataclass for the app,
  with reason keys and the UK wide-data band-plan check.

## The engine, measured (shack, 18/10/2026)

On the simulated channel (`sim`, 1200 baud, TXDELAY 300 ms, PERSIST 63,
SLOTTIME 100 ms, k 4 / 8, N1 256), 100 kB from one station to another:

| | clean | + a third station beaconing every 20 s | + 5 % frame loss | + 15 % loss |
|---|---|---|---|---|
| modulo 8 | 938 bit/s | 792 | 655 | 528 |
| modulo 128 | 925 | 791 | 571 | 358 |

(One seed each; over eight seeds of 50 kB the same cells vary by about
10 %.) In audio mode, through the real modulator, white noise and the real
receiver and DCD, 10 kB at 6-12 dB SNR with the beaconing third station:
about 790 bit/s. Modulo 128 gains nothing at 1200 baud: one transmission
is capped at 10 s, which a modulo-8 window of four 256-byte frames
already nearly fills. A frames-mode 100 kB transfer takes under a second of
CPU; audio mode runs about seven times faster than real time with three
stations.

Assumptions in those numbers: a radio up to power instantly and a
turnaround the link reports truthfully (the channel stretches TXDELAY to
the turnaround plus 80 ms; a radio whose link reports nothing, with a
450 ms key-up, loses every frame - tested), DCD 50 ms after a carrier
starts in frames mode (the receiver's own DCD in audio mode: a median
100 ms, 300 ms at worst at 3 dB SNR), 2 ms propagation and processing.

**Design choices** (datalink.py has the measurements): REJ, not SREJ;
FRMR received, never sent (2.2); T1 runs from when our frames left the
air, learns the round trip by Karn's rule (T1 = 2 SRT, 1-60 s), doubles
per retry and forgets the back-off once the link recovers; T1 is not
paused while the channel is busy (direwolf's way cost 10-20 %); SABME
falls back to SABM after three tries or an FRMR; XID answered always, sent
once after SABME; direwolf's defaults otherwise (FRACK 3 s, RETRY 10,
PACLEN 256, MAXFRAME 4, EMAXFRAME 8).

**Against direwolf 1.7** (`tests/test_direwolf_link.py`, skipped without
it): every frame of a lossy session at both moduli, and APRS, IP and ARP UI
frames, decode in `atest -h` byte for byte with direwolf's frame type;
and live, a direwolf station on audio pipes (UDP in, a FIFO out through
ALSA's user-space file plugin - no sound device, no snd-aloop) connects to
the engine with SABME and XID and sends segmented data, and the engine
connects to it at modulo 8 and 128, transfers, and disconnects. About a
minute of real time.

## Radio links (step 3, 18/10/2026)

`channel.RadioLink` for real radios, through shack_rig (`pip install
shack-packet[radios]`; a desktop also wants `shack-rig[desktop]`: PortAudio,
and pyserial on Windows):

```python
from shack_packet.radios import IcomLink, YaesuLink, SoundcardLink, PlutoLink
radio = IcomLink({"model": "ic-9700", "host": "198.51.100.150", "username": "op"},
                 password=secret, frequency=144_800_000)
radio = YaesuLink("/dev/ttyUSB0", audio="USB Audio CODEC", frequency=144_800_000)
radio = SoundcardLink("SignaLink USB", ptt="rts", ptt_device="COM4")
radio = PlutoLink("ip:192.168.2.1", 144_800_000, rx_gain=25.0,  # numpy and libiio only
                  guard=shack_pluto.guard.Guard(SshRunner.for_uri("ip:192.168.2.1"), 15))
radio = HackRFLink(144_800_000, tx_vga_db=20)                    # libhackrf, half-duplex
radio = SdrRxLink(shack_packet.sdr.RtlSdrSource(), 144_800_000)  # receive only: RTL-SDR, rtl_tcp, SDRplay
engine = shack_packet.Engine(station, radio)
with radio:                      # open: claim, connect, set up for packet; close: unkey, put back
    shack_packet.Runner(engine).start()
```

| Link | Radio | PTT | Audio | Busy |
|---|---|---|---|---|
| `IcomLink` | IC-9700 LAN/USB, IC-705 USB/Wi-Fi (IcomRig, a make_rig profile) | CI-V `1C 00` | 12 kHz both ways (LAN stream; USB codec divided/raised exactly) | squelch, CI-V `15 01` |
| `YaesuLink` | FTX-1 USB (YaesuRig) | CAT `TX1;`, or RTS/DTR on the Standard port | the codec's own rate, no resampling | squelch, `RI0;` P8 |
| `SoundcardLink` | anything on a sound card | RTS/DTR on a serial port, VOX, or none | the card's own rate | our DCD only |
| `PlutoLink` | a Pluto (AD936x, libiio): the link is the FM modem, the channel 100 kHz above both LOs | TX LO powerdown 0 / gain; quiet (powerdown 1, -89.75 dB, read back) otherwise | 12 kHz both ways (FM at 2.4 MS/s, phase-continuous, carrier rather than a hole); with `baud` 9600-38 400, G3RUH baseband at 8 samples a bit (IQ at 2.4576 MS/s); with `mode`, the wide modes at 8 samples a symbol | channel power over its floor |
| `HackRFLink` | a HackRF One (libhackrf, ctypes): half-duplex, PlutoLink's modulator and receiver | `hackrf_start_tx`; `hackrf_stop_tx` (transceiver OFF) to unkey, confirmed from the M0 state | 12 kHz both ways (8-bit IQ at 2.4 MS/s) | channel power over its floor |
| `SdrRxLink` | any `sdr.IqSource`: RTL-SDR, rtl_tcp, SDRplay (the user's own API) | none: `key()` is refused, "receive-only" | 12 kHz in | channel power over its floor |

**A Pluto transmits only under a guard on the Pluto itself**
(`shack_pluto.guard`, started over SSH at open): it quiets the transmitter
when this process dies, freezes or holds a key too long. Without a
confirmed guard PlutoLink is receive-only ("tx-unguarded"). A HackRF has
nothing to run a guard on: `radios/hackrf.py` says what a crash mid-over
leaves (the modulation stops, an LO-leakage carrier stays until the next
open) and what is done about it.

**Safety, all three** (`radios/base.py`): the station (callsign, transmit
switch) and the claim are asked at `key()` and every 10 ms while keyed; a
15 s key-down watchdog; any error, a radio that goes away, and `close()`
unkey, and an unkey that cannot be sent is owed and retried; audio is paced
into the radio 20 ms at a time and the unkey waits until it has played
(at most 1 s); RTS/DTR are low from the moment a port opens and only the
key raises the configured line. A rig link keys under a lease that is the
same test, so the rig drops the key by itself if the link were stuck.
Events (`watchdog`, `disarmed`, `disconnected`, `busy-stuck`, `vox`,
`setup`...) are `radios.REASONS` keys, as engine Events. A Pluto has more,
because nothing outside the link stands between it and the antenna
(`radios/pluto.py`): its transmitter is quieted and read back before
anything else at open, after every over and at close, every transmit
attribute write passes a gate of named values, and an independent
watchdog with its own libiio context quiets it if it has been up longer
than the key-down limit plus a second, even with the supervisor stuck.

**Opening sets the radio up** and closing puts it back: frequency, FM with
DATA (Icom FM-D in one 0x26 frame; Yaesu DATA-FM `MD0A;`), simplex and every
tone off, the data source (Icom DATA MOD to LAN or USB - written only where
Icom's guide gives the item, the IC-9700's; Yaesu MOD SOURCE to USB in both
the FM and DATA blocks), each read first and restored. What could not be
done is in `link.setup` and an event, never a silent default.

**Turnaround**: `turnaround_s` is assumed (0.15 s for the rigs, 0.1 s for a
sound card, 0.15 s on VOX) until `radios.turnaround.measure(link, probe,
confirm=CONFIRM)` times the real radio - by its own output meter
(`RigPowerProbe`) or a monitor receiver (`LevelProbe`) - with the operator
present and a dummy load, and returns `turnaround_s` and `txdelay_s`.

**Broker seam**: `Claim` (acquire / held / release). The desktop app talks
to the radio directly (`LocalClaim`); a broker-backed claim is for later.

Tested against shack_rig's fakes only (FakeIcom over the LAN protocol, a
CI-V radio and FakeFtx1 on ptys, FakeBackend sound devices, stand-in aplay
and serial lines). Nothing has met a real radio yet.

## The SDR monitor (step 4, 18/10/2026)

Receive only: many packet channels at once from one SDR, for the app's
monitor window (frames per channel, heard, nodes, who talks to whom,
activity, the map, a waterfall). GUI-free; the Qt app draws `Monitor`.

```python
from shack_packet import sdr
plan = sdr.default_plan()                    # channels.json: 144.800, .925, .9375, .950, 145.825
source = sdr.RtlSdrSource(plan.centre_for(2.4e6), 2.4e6)      # or RtlTcpSource / PlutoSource / FileSource
pipeline = sdr.Pipeline(source.rate, source.centre, plan, ppm=1.5)
runner = sdr.Runner(source, pipeline, on_error=show_reason)    # SdrError.reason: sdr.REASONS keys
runner.start()
pipeline.monitor.snapshot()                  # frames, heard, nodes, links, channels+activity, positions, ppm
pipeline.monitor.feed_heard(aprs_station)    # optional: into the engine's heard table (AprsStation.hear)
runner.stop()
```

**How**: one overlap-save FFT filter bank over the whole capture (N =
512 x D, an eighth overlap, an 8001-tap channel filter at 2.4 MS/s),
channel IQ at ~19.2 kS/s (an FM packet signal is ~10.4 kHz wide), a
discriminator, and demod.Receiver per channel at that rate (its own
decimator takes it to 9.6 kHz). The crystal error is corrected in
software for every source (fractions of a ppm); `Monitor.ppm_estimate()`
reads it from the stations heard. Channel filter, measured: flat (0.2 dB)
to +-6 kHz, 46 dB down at 7.1 kHz, 81 at 8 kHz, 116 at the next channel's
centre. The channels are data: `from_spots()` takes the band-plan
dataset's entries, `from_dict()` the operator's list.

**CPU**, `python3 -m shack_packet.sdr.bench` on shack (Ryzen 9 6900HX, one
core, numpy 2.5, 2.4 MS/s, the least of three 10 s runs):

| | 5 channels (default plan) | 16 channels (144.800-144.9875) |
|---|---|---|
| channeliser alone | 24 ms/s (2.4 %) | 33 ms/s (3.3 %) |
| both demodulators, noise only | 135 ms/s (13.5 %) | 374 ms/s (37.4 %) |
| both, a station once a second on every channel | 138 ms/s | 381 ms/s |
| one demodulator, noise only | 83 ms/s (8.3 %) | 208 ms/s (20.8 %) |
| one, a station on every channel | 87 ms/s | 217 ms/s (21.7 %) |

So about 21 ms a channel-second with both demodulators and 11 with one;
`diversity="auto"` (the default) runs both up to 8 channels and one
beyond: 14 % of a core for the default plan, 22 % for all sixteen.
RTL-SDR/rtl_tcp sample conversion adds 1 ms/s. Those figures need
`sdr.memory.keep_heap()` (glibc's top pad, set by the first Pipeline):
without it numpy's FFT scratch made the channeliser alone 86 ms/s in some
processes, all of it page faults. Not measured on macOS or Windows.

**Sources, and what a desktop installer bundles:**

| Source | Binding | Windows | macOS | Linux |
|---|---|---|---|---|
| `RtlSdrSource` | our own ctypes over librtlsdr (no pyrtlsdr) | `rtlsdr.dll`, `libusb-1.0.dll`; the dongle needs WinUSB via Zadig (List All Devices, "Bulk-In, Interface (Interface 0)", 0BDA 2838, Replace Driver) | `librtlsdr.dylib`, `libusb-1.0.dylib` (install names to @loader_path) | librtlsdr0 as a dependency, its udev rule, dvb_usb_rtl28xxu blacklisted |
| `RtlTcpSource` | pure Python | nothing | nothing | nothing |
| `PlutoSource` | libiio 0.2x Python bindings (`iio`) | libiio.dll (+ ADI's Pluto USB drivers for USB) | libiio.dylib | libiio0, python3-libiio |
| `FileSource` | numpy | nothing | nothing | nothing |

**The Pluto's transmitter is held quiet** (a Tezuka Pluto boots with its
TX LO up): TX LO powerdown 1 and TX gain -89.75 dB are the first writes,
read back, and a Pluto that does not read quiet is not used; one gate
allows receive settings and those two values only, refusing anything
else before it reaches the radio; the TX DMA device is never opened;
rates below 2.084 MS/s (which need the FIR shared with TX) are refused;
close restores the receive settings found and quiets TX again. A stock
Pluto (AD9363) is refused on 2 m with the AD9364 setting named.

**Tested** on synthetic IQ end to end (`tests/test_sdr_*.py`): five
channels 12 dB apart with their own transmitter errors through a
receiver 30 ppm out; a carrier 50 dB up in the next channel; FX.25 and
pre-emphasised stations; lost samples and a decoder that stalls (frames
still dated within 50 ms); recordings round-tripped through SigMF;
rtl_tcp against a server that speaks the protocol; librtlsdr through a
stand-in compiled from C at test time; the Pluto through a stand-in
libiio that boots like Tezuka. **Never against real hardware**: no SDR is
attached to shack or ohs-dev, and the boxes' Plutos belong to QO-100 NB.

Tests: `tools/test.sh` (or the repo's). About 2 minutes with direwolf
(`apt install direwolf`), of which the SDR monitor's synthetic-IQ tests
are about 45 s; 436 tests on shack, 18/10/2026.

Licence: GPL-3.0-or-later.

## On air, 18/10/2026 (Brian present): shack Pluto (A) <-> Tezuka nano on workbench (B)
Nano switched to `usb_ethernet_mode=rndis` first (NCM stalled RX and watchdog-rebooted it); RNDIS soak
10 min, 100 % kept. Results:
- 9600, 432.650: UI A->B 47/50 (shack under load: tx_late 25, no gaps), B->A 50/50; 10 KB A->B 33.5 s,
  2.4 kbit/s data, CRC match (4 REJ, 28 retransmits).
- 19200, 1298.5: 10 KB in 11.0 s, 7.4 kbit/s, CRC match, tx_late 0.
- 38400, 1298.5, window 16: 10 KB in 5.5 s, 14.9 kbit/s, CRC match, tx_late 0.
Nano offset with xo 39999236: -17 to -170 Hz.
