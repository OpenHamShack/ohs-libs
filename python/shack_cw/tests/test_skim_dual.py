"""Both CW engines on every skimmer channel (Skimmer(engine="both")):
each prints its own text from the one gather, the snapshot shows the
network's, and a call either engine prints counts toward seen-twice."""
import numpy as np

from shack_cw.skim import Skimmer
from shack_cw.skim.synth import selftest_band

RATE = 24_000.0


def run(engine):
    z, stations = selftest_band()
    sk = Skimmer(RATE, -RATE * 0.42, RATE * 0.42, 0.0, engine=engine)
    blk = int(0.075 * RATE)
    spots = []
    for i in range(0, len(z), blk):
        spots += sk.feed(z[i:i + blk])
    return sk, stations, spots


def flat(texts):
    return " ".join(t.text for t in texts.values()).replace(" ", "")


def test_both_engines_print_their_own_text_on_every_channel():
    sk, stations, spots = run("both")
    assert sk.pool.engines == ("nn", "classic")
    nn, classic = sk.pool.texts_by["nn"], sk.pool.texts_by["classic"]
    assert sk.pool.texts is nn and set(nn) == set(classic)
    assert sum(s.call in flat(nn) for s in stations) >= 3
    assert sum(s.call in flat(classic) for s in stations) >= 3
    # The snapshot's text is the primary engine's.
    snap = sk.snapshot()
    assert any(ch["text"] for ch in snap["channels"])
    for ch in snap["channels"]:
        assert nn[ch["id"]].text.endswith(ch["text"])
    valid = {s["call"] for s in spots if s["status"] == "valid"}
    assert valid and valid <= {s.call for s in stations}


def test_hits_come_from_both_engines():
    sk, _, _ = run("both")
    seen = set()
    orig = sk.calls._hit

    def logged(*a):
        seen.add(a[9])          # _hit(call, kind, track, f, t, wpm, snr, band, info, engine)
        return orig(*a)
    sk.calls._hit = logged
    z, _ = selftest_band()
    blk = int(0.075 * RATE)
    for i in range(0, len(z), blk):
        sk.feed(z[i:i + blk], t=100.0 + i / RATE)
    assert {"nn", "classic"} <= seen


def test_one_engine_alone_still_runs_alone():
    sk, _, _ = run("classic")
    assert sk.pool.bank is None and list(sk.pool.texts_by) == ["classic"]
    sk, _, _ = run("nn")
    assert sk.pool.bank is not None and list(sk.pool.texts_by) == ["nn"]
    assert all(ch.decoder is None for ch in sk.pool.channels.values())


def test_an_unknown_engine_is_refused():
    import pytest
    with pytest.raises(ValueError):
        Skimmer(RATE, -1000.0, 1000.0, 0.0, engine="nope")


def test_the_language_model_reads_the_skimmers_channels_only():
    # Brian, 14/10/2026: the LM in the skimmer, never the live readers.
    from shack_cw import Decoder, nn
    from shack_cw.skim import pool
    sk, stations, spots = run("both")
    assert sk.pool.bank.lm == pool.SKIM_LM > 0
    # The beam search really ran on the channels, coming and going as the
    # tracks did, and they still read the band.
    assert any(s is not None for s in sk.pool.bank.search)
    assert sum(s.call in flat(sk.pool.texts_by["nn"]) for s in stations) >= 3
    assert {s["call"] for s in spots if s["status"] == "valid"} <= {s.call for s in stations}
    # A live reader (Radio's CW panel, Icom, Flex) stays greedy.
    assert nn.LM == 0
    assert Decoder(None, freq=700.0, polarity=False, engine="nn")._bank.lm == 0


def test_the_classic_engine_reads_only_its_budget_of_channels(monkeypatch):
    # Beside the network the classic engine takes CLASSIC_MAX channels, the
    # first given out (the strongest); the network reads every channel.
    from shack_cw.skim import pool
    monkeypatch.setattr(pool, "CLASSIC_MAX", 2)
    sk, stations, _ = run("both")
    classic = [k for k, ch in sk.pool.channels.items() if ch.decoder is not None]
    assert sk.decoding >= 3 and len(classic) == 2
    assert all(sk.pool.texts_by["classic"][k].text == "" for k in sk.pool.channels if k not in classic)
    assert sum(1 for k in sk.pool.channels if sk.pool.texts_by["nn"][k].text) >= 3
    # A slot freed is given to the next channel made.
    sk.pool.drop(classic[0])
    sk.pool._add(99_999)
    assert sk.pool.channels[99_999].decoder is not None
    # The classic engine alone has no budget.
    sk, _, _ = run("classic")
    assert all(ch.decoder is not None for ch in sk.pool.channels.values())
