"""probe.py [WEIGHTS]: a 38 dB keyer at 24 WPM through the bench receiver,
clean, with a backwave, and with a weaker station on the same frequency
keying at the same time (25 and 15 dB down) - decoded by the nn engine with
WEIGHTS (default the shipped ones) and by the classic engine. Round 2 found
the co-channel case this way: the classic engine reads every letter."""
import sys

import numpy as np

from shack_cw import Decoder
from shack_cw.benchrx import CwReceiver
from shack_cw.morse import envelope

FS = 8000
TEXT = "MYRIG IS ABT 2TT WATTS ANT 4EL YAGI BK DE DF2IC K"


def make(snr, backwave_db=None, cochan_db=None, seed=0):
    rng = np.random.default_rng(seed)
    env = envelope(TEXT, 24, FS, tail=1.0)
    env = np.concatenate([np.zeros(FS), env, np.zeros(FS)])
    n = len(env)
    if backwave_db is not None:
        env = np.maximum(env, 10 ** (-backwave_db / 20))
    a = np.sqrt(10 ** (snr / 10) * 2 / FS * 2500)
    z = a * env * np.exp(2j * np.pi * 3.0 * np.arange(n) / FS)
    if cochan_db is not None:
        e2 = envelope("CQ CQ DE EA8QP EA8QP K " * 4, 18, FS)[:n]
        e2 = np.concatenate([e2, np.zeros(n - len(e2))])
        z += a * 10 ** (-cochan_db / 20) * e2 * np.exp(2j * np.pi * 8.0 * np.arange(n) / FS)
    z += rng.standard_normal(n) + 1j * rng.standard_normal(n)
    x = z * np.exp(2j * np.pi * 700 * np.arange(n) / FS)
    rx = CwReceiver(FS, 700.0, 500.0)
    return np.concatenate([rx(x[i:i + 600]) for i in range(0, n, 600)])


w = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
for label, kw in [("clean 38", {}), ("clean 25", {"snr": 25}), ("backwave -30", {"backwave_db": 30}),
                  ("backwave -25", {"backwave_db": 25}), ("cochan -25", {"cochan_db": 25}), ("cochan -15", {"cochan_db": 15})]:
    snr = kw.pop("snr", 38)
    a = make(snr, **kw)
    d = Decoder(None, freq=700.0, polarity=False, engine="nn", **({"weights": w} if w else {}))
    t = "".join(e["text"] for i in range(0, len(a), 600) for e in d.feed(a[i:i + 600], FS))
    c = Decoder(None, freq=700.0, polarity=False)
    tc = "".join(e["text"] for i in range(0, len(a), 600) for e in c.feed(a[i:i + 600], FS))
    print(f"{label:14s} nn {t!r}\n{'':14s} cl {tc!r}")
