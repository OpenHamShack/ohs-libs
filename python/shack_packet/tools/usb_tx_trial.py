#!/usr/bin/env python3
"""One packet over through an Icom's USB codec, with the codec opened the
way the environment says, and the audio saved for `shack_packet.slips`.

THIS KEYS THE RADIO when TX=1. Only with Brian present, on a licensed
frequency, at the power the link's test mode sets (minimum_power_for_test).
Without TX=1 it is a dry run: the same audio goes to the codec with the
radio unkeyed, after VOX has been read back as off (16 46 00) - which is
how this script itself was tested.

Written 18/10/2026 for the IC-9700's USB transmit slips (slips.py). The
question each run answers is "does this way of feeding the codec slip
less?", so everything about the feeding is a variable:

    MODE=link|own   link: the production path (IcomLink.send_pcm, 12 kHz
                    up-sampled by txaudio.AplayOut to 48 kHz mono, 50 ms
                    periods x 4). own: aplay run here, with:
    RATE=48000      32000, 44100 or 48000 (the PCM2901 plays all three)
    CH=1            1 or 2 (altset 2 mono, altset 1 stereo)
    PERIOD_MS=50    aplay's period; BUFFER_PERIODS=4 periods in its buffer
    PREROLL_S=0     own only: seconds of silence the codec plays before the
                    key (aplay opened first, one process for the whole run)
    FX25=           "", auto, 16, 32 or 64
    N=3 AMP=0.5 FREQ=432650000 TXDELAY_MS=300
    DEV=hw:CARD=CODEC,DEV=0  CIV=/dev/serial/by-id/...IC-9700..._A-if00-port0
    OUT=~/build/usb-tx-trial

Each run writes OUT/<stamp>-sent.wav (the over at 48 kHz, the reference
for `python3 -m shack_packet.slips`) and OUT/<stamp>.json (the settings,
the frames' text and the timing). The received side is whatever the
operator captures (the Pluto or the RSPdx, FM-demodulated to 48 kHz).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from shack_packet import afsk, aprs, ax25, bench
from shack_packet.identity import Station
from shack_packet.radios.icom import IcomLink

CIV_9700 = "/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_IC-9700_13003524_A-if00-port0"


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def main() -> int:
    tx = env("TX", "0") == "1"
    path = env("MODE", "link")
    if path not in ("link", "own"):
        print("MODE is link or own", file=sys.stderr)
        return 2
    rate, ch = int(env("RATE", "48000")), int(env("CH", "1"))
    period_ms, periods = float(env("PERIOD_MS", "50")), int(env("BUFFER_PERIODS", "4"))
    preroll = float(env("PREROLL_S", "0"))
    fx = env("FX25", "") or None
    fx = fx if fx in (None, "auto") else int(fx)
    n, amp, freq = int(env("N", "3")), float(env("AMP", "0.5")), int(env("FREQ", "432650000"))
    txdelay = float(env("TXDELAY_MS", "300"))
    dev, civ = env("DEV", "hw:CARD=CODEC,DEV=0"), env("CIV", CIV_9700)
    out = Path(os.path.expanduser(env("OUT", "~/build/usb-tx-trial")))
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if path == "link" and (rate, ch, preroll) != (48000, 1, 0.0):
        print("MODE=link is the production path: RATE, CH and PREROLL_S apply to MODE=own only", file=sys.stderr)
        return 2

    station = Station.of("GM4BRI", tx_enabled=True)
    texts = [f"USB trial {i + 1}/{n} {time.strftime('%H:%M:%S')}" for i in range(n)]
    frames = [ax25.Frame.ui(station.address(), ax25.Address(aprs.TOCALL, 0), t).encode() for t in texts]
    over_s = len(afsk.modulate(frames, rate=48_000, fx25_mode=fx, txdelay_ms=txdelay)) / 48_000
    reference = afsk.modulate(frames, rate=48_000, amplitude=amp, fx25_mode=fx, txdelay_ms=txdelay)
    bench.write_wav(out / f"{stamp}-sent.wav", reference, 48_000)

    profile = {"model": "ic-9700", "transport": "usb", "civ_device": civ, "audio_device": dev,
               "audio_backend": "alsa", "tx_audio": "alsa", "tx_audio_device": dev}
    link = IcomLink(profile, password="", frequency=freq if tx else None, transmit=tx,
                    station=station if tx else None, minimum_power_for_test=tx, max_key_s=over_s + preroll + 3)
    record = {"stamp": stamp, "tx": tx, "path": path, "rate": rate, "channels": ch, "period_ms": period_ms,
              "buffer_periods": periods, "preroll_s": preroll, "fx25": fx, "frames": texts, "amp": amp,
              "freq": freq, "over_s": round(over_s, 3)}
    aplay = None
    link.open()
    try:
        ask = lambda cmd: (lambda g: g.hex(" ") if g else None)(link._call(link.rig.ask, cmd, cmd, 0.5, timeout=2))
        vox, ptt = ask(b"\x16\x46"), ask(b"\x1c\x00")
        record.update(vox=vox, ptt_before=ptt, nr=ask(b"\x16\x40"), radio_freq=link.rig.state.get("freq"))
        print(f"VOX {vox}  PTT {ptt}  NR {record['nr']}  freq {link.rig.state.get('freq')}", flush=True)
        if vox != "16 46 00" or ptt != "1c 00 00":
            raise SystemExit("VOX is not confirmed off, or the radio is already keyed: nothing played")
        if tx:
            link._ensure_minimum_power_or_raise()
            print("power confirmed at the test minimum", flush=True)
        if path == "link":
            pcm = afsk.pcm16(afsk.modulate(frames, rate=12_000, amplitude=amp, fx25_mode=fx, txdelay_ms=txdelay))
            if tx:
                link.key()
                link.send_pcm(pcm)
                t0 = time.time()
                time.sleep(over_s + 0.2)
                link.unkey()
                time.sleep(1.2)
            else:
                # Unkeyed, the link refuses audio (base.send_pcm), so the
                # dry run feeds the same sink the link would, at its pace.
                from shack_rig.txaudio import AplayOut
                sink = AplayOut(dev)
                t0 = time.time()
                for at in range(0, len(pcm), 480):
                    sink.send_audio(pcm[at:at + 480])
                    time.sleep(max(0.0, t0 + (at + 480) / 24000 - 0.06 - time.time()))
                sink.tx_end()
                time.sleep(sink.tx_drain_s + 0.2)
                sink.close()
        else:
            audio = afsk.modulate(frames, rate=rate, amplitude=amp, fx25_mode=fx, txdelay_ms=txdelay)
            body = np.concatenate([np.zeros(int(preroll * rate)), audio, np.zeros(int(0.1 * rate))])
            pcm = np.repeat(np.round(body * 32767).astype("<i2")[:, None], ch, axis=1).tobytes()
            period = int(rate * period_ms / 1000)
            argv = ["aplay", "-D", dev, "-t", "raw", "-f", "S16_LE", "-c", str(ch), "-r", str(rate),
                    f"--period-size={period}", f"--buffer-size={period * periods}", "-"]
            if tx and not preroll:
                link.key()
            aplay = subprocess.Popen(argv, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            t0 = time.time()
            writer = __import__("threading").Thread(target=lambda: (aplay.stdin.write(pcm), aplay.stdin.close()),
                                                    daemon=True)
            writer.start()
            if tx and preroll:
                time.sleep(max(0.0, t0 + preroll - 0.15 - time.time()))
                link.key()
            time.sleep(max(0.0, t0 + preroll + over_s + period * periods / rate + 0.1 - time.time()))
            if tx:
                link.unkey()
            aplay.wait(timeout=5)
            record["aplay_stderr"] = aplay.stderr.read().decode(errors="replace")[-2000:]
            time.sleep(1.2 if tx else 0.1)
        record["ptt_after"] = ask(b"\x1c\x00")
        record["events"] = [list(map(str, e)) for e in list(getattr(link, "events", []))[-5:]]
        record["wall_s"] = round(time.time() - t0, 3)
        print(f"{'keyed' if tx else 'dry run, unkeyed'}: {n} frames, {over_s:.2f} s over; PTT after {record['ptt_after']}")
    finally:
        if aplay is not None and aplay.poll() is None:
            aplay.terminate()
        link.close()
        (out / f"{stamp}.json").write_text(json.dumps(record, indent=1))
        print("wrote", out / f"{stamp}-sent.wav", "and", out / f"{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
