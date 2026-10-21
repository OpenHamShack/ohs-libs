"""The neural CW engine (shack_cw.nn): its streaming arithmetic with any
weights, and what the shipped weights do with real-shaped signals.

The first group builds random weights in the runtime's own layout, so it
tests the machinery (blocks of any length give one answer, a channel is
the same alone or in a busy bank) whatever the training produced. The
second group decodes signals through the receiver every app has, with
the weights the package ships."""
from __future__ import annotations

import json

import numpy as np
import pytest

from shack_cw import Decoder
from shack_cw.benchrx import CwReceiver
from shack_cw.morse import envelope
from shack_cw.nn import FRONTS, N_FEAT, SYMBOLS, WINDOWS, Bank, Features, Net, NnDecoder, n_feat, offsets, tokens

FS = 8000
PITCH = 700.0


def random_weights(path, width: int = 8, dilations=(1, 2, 4), seed: int = 0, front: str = "v1", ahead=()) -> str:
    rng = np.random.default_rng(seed)
    r = lambda *s: (rng.standard_normal(s) * 0.4).astype(np.float32)   # noqa: E731
    nf = n_feat(front)
    w = {"down_w": r(8 * nf, width), "down_b": r(width), "out_w": r(width, len(SYMBOLS)),
         "out_b": r(len(SYMBOLS)), "wpm_w": r(width, 1), "wpm_b": r(1)}
    for i in range(len(dilations)):
        w[f"conv{i}_w"], w[f"conv{i}_b"] = r(3 * width, width), r(width)
        w[f"mix{i}_w"], w[f"mix{i}_b"] = r(width, width), r(width)
    meta = {"width": width, "dilations": list(dilations), "stride": 4, "k_down": 8, "symbols": SYMBOLS,
            "n_feat": nf, "windows": list(FRONTS[front]["windows"]), "front": front,
            "ahead": [d in ahead for d in dilations]}
    p = str(path / f"w-{front}-{seed}.npz")
    np.savez(p, meta=np.array(json.dumps(meta)), **w)
    return p


def blocks(n, rng, most=40):
    i = 0
    while i < n:
        k = int(rng.integers(1, most))
        yield slice(i, min(n, i + k))
        i += k


def test_features_are_the_same_in_any_blocks():
    rng = np.random.default_rng(1)
    c = rng.standard_normal((700, 3, 7)) + 1j * rng.standard_normal((700, 3, 7))
    whole = Features(3)(c)
    f = Features(3)
    parts = np.concatenate([f(c[s]) for s in blocks(len(c), rng)])
    np.testing.assert_allclose(parts, whole, rtol=1e-5, atol=1e-5)
    assert whole.shape == (700, 3, N_FEAT)


def test_features_are_amplitudes_at_four_windows():
    # The network was trained on amplitudes() at each window; the fused
    # arithmetic must be the same numbers, or the weights no longer fit.
    from shack_cw.decoder import amplitudes
    rng = np.random.default_rng(6)
    c = rng.standard_normal((300, 2, 7)) + 1j * rng.standard_normal((300, 2, 7))
    f = Features(2)(c, scale=3.0)
    zero = (np.zeros((8, 2, 7), np.complex128), np.zeros((8, 2, 7)))
    cols = []
    for m in WINDOWS:
        tone, quiet, loud, _ = amplitudes(c, m, zero, scale=3.0)
        cols += [tone, quiet, loud]
    a = np.log(np.stack(cols, axis=-1))
    q4 = a[:, :, 3 * WINDOWS.index(4) + 1]
    floor = np.array([q4[max(0, i - 199):i + 1].mean(axis=0) for i in range(len(q4))])
    np.testing.assert_allclose(f, a - floor[:, :, None], rtol=1e-5, atol=1e-5)


@pytest.mark.parametrize("ahead", [(), (1, 2)])
def test_the_network_streams_as_one_pass(tmp_path, ahead):
    net = Net(random_weights(tmp_path, ahead=ahead))
    rng = np.random.default_rng(2)
    x = rng.standard_normal((2, 803, N_FEAT)).astype(np.float32)
    lp1, w1 = net.step(x, net.state(2))
    st = net.state(2)
    parts = [net.step(x[:, s], st) for s in blocks(x.shape[1], rng)]
    lp2 = np.concatenate([p[0] for p in parts], axis=1)
    np.testing.assert_allclose(lp2, lp1, rtol=1e-4, atol=1e-4)
    assert lp1.shape == (2, 803 // 4, len(SYMBOLS))     # 200 frames a second in, 50 out
    np.testing.assert_allclose(np.exp(lp1).sum(-1), 1.0, rtol=1e-5)
    assert net.delay == sum(ahead)


def test_a_look_ahead_block_sees_the_frames_after_its_output():
    # Output j of a network whose one block looks ahead by d describes
    # frame j - d: changing input after that frame's own outputs must
    # still change it (a causal network's would not move), and only up
    # to d outputs on.
    import tempfile
    from pathlib import Path
    d = 4
    with tempfile.TemporaryDirectory() as t:
        net = Net(random_weights(Path(t), dilations=(d,), ahead=(d,)))
        causal = Net(random_weights(Path(t), dilations=(d,), seed=0))
    rng = np.random.default_rng(9)
    x = rng.standard_normal((1, 400, N_FEAT)).astype(np.float32)
    y = x.copy()
    y[:, 200:204] += 3.0                       # input frames of output 50 (stride 4)
    for n, expect in ((net, True), (causal, False)):
        a, _ = n.step(x, n.state(1))
        b, _ = n.step(y, n.state(1))
        moved = np.abs(a - b).max(axis=-1)[0] > 1e-6
        # Nothing before output 50 (the first whose frames changed) moves
        # in either; the look-ahead network's output for it comes d later.
        assert not moved[:50].any()
        assert moved[50 + d] if expect else moved[50]


def test_v2_features_stream_and_carry_v1():
    # v2 reads the same seven columns as v1 first: its first twelve
    # features (windows 1-8 of tone, quiet, loud) are v1's, in v1's order.
    rng = np.random.default_rng(11)
    cols = len(offsets("v2"))
    c = rng.standard_normal((700, 3, cols)) + 1j * rng.standard_normal((700, 3, cols))
    whole = Features(3, "v2")(c)
    f = Features(3, "v2")
    parts = np.concatenate([f(c[s]) for s in blocks(len(c), rng)])
    np.testing.assert_allclose(parts, whole, rtol=1e-5, atol=1e-5)
    assert whole.shape == (700, 3, n_feat("v2"))
    v1 = Features(3)(c)
    # The floor is the same (quiet at m = 4), and so are windows 1, 2, 4, 8.
    np.testing.assert_allclose(whole[..., :N_FEAT], v1, rtol=1e-5, atol=1e-5)


def test_a_v2_network_decodes_through_the_decoder_and_the_skimmer(tmp_path, monkeypatch):
    # Weights for another front make the decoder's mixer and the skimmer's
    # gather sum their frequencies too; random weights print rubbish, but
    # they must run and stream the same.
    from shack_cw.skim import Skimmer
    from shack_cw.skim.synth import selftest_band
    w = random_weights(tmp_path, front="v2", ahead=(1,))
    d = Decoder(None, freq=PITCH, polarity=False, engine="nn", weights=w)
    d.feed(np.zeros(int(0.075 * FS), np.float32), FS)
    assert len(d._ref_off) == len(offsets("v2"))
    rng = np.random.default_rng(3)
    a = rng.standard_normal(4 * FS).astype(np.float32)
    whole = "".join(e["text"] for e in Decoder(None, freq=PITCH, polarity=False, engine="nn", weights=w).feed(a, FS))
    assert isinstance(whole, str)
    import shack_cw.nn as nnmod
    from pathlib import Path
    monkeypatch.setattr(nnmod, "WEIGHTS", Path(w))          # what a skimmer's Bank loads
    z, _ = selftest_band()
    sk = Skimmer(24_000.0, -12_000.0 * 0.84, 12_000.0 * 0.84, 0.0, engine="nn")
    blk = int(0.075 * 24_000)
    for i in range(0, min(len(z), 24_000 * 5), blk):
        sk.feed(z[i:i + blk])
    assert sk.pool.gather.off.shape == (len(offsets("v2")),)
    assert sk.pool.bank.net.front == "v2"


def keyed_sums(n, rng, key):
    """Complex sums with a strong keyed tone (index 0) over unit noise."""
    c = (rng.standard_normal((n, 7)) + 1j * rng.standard_normal((n, 7))) / np.sqrt(2)
    c[:, 0] += 20.0 * key
    return c


def test_a_channel_is_the_same_alone_or_in_a_busy_bank(tmp_path):
    w = random_weights(tmp_path, seed=3)
    rng = np.random.default_rng(4)
    n = 3000
    key = (np.sin(np.arange(n) / 9.0) > 0).astype(float)
    a, b, c = keyed_sums(n, rng, key), keyed_sums(n, rng, 1 - key), keyed_sums(n, rng, key[::-1])
    alone, busy = Bank(6.0, w), Bank(6.0, w)
    got_alone, got_busy = [], []
    for i, s in enumerate(blocks(n, np.random.default_rng(5), 30)):
        got_alone += alone.run(a[s][:, None], ["a"])["a"]
        # Others come and go round it, in front of it and behind it.
        keys = [["a"], ["b", "a"], ["b", "a", "c"], ["c", "a"], ["a", "b"]][(i // 7) % 5]
        src = {"a": a, "b": b, "c": c}
        got_busy += busy.run(np.stack([src[k][s] for k in keys], axis=1), keys)["a"]
    assert got_alone, "the squelch should have opened on a tone 20 dB up"
    assert got_busy == got_alone


def test_an_unknown_engine_is_refused():
    with pytest.raises(ValueError):
        Decoder(engine="neural-ish")
    assert type(Decoder(engine="classic")) is Decoder
    assert type(Decoder(engine="nn")) is NnDecoder


def test_tokens_read_prosigns_and_spaces():
    assert [SYMBOLS[i] for i in tokens("cq  de g4abc <SK> + =")] == \
        list("CQ DE G4ABC ") + ["<SK>", " ", "<AR>", " ", "<BT>"]


# ------------------------------------------------------------ the shipped weights
def receive(z):
    """8 kHz complex with the signal at 0 Hz -> the bench receiver's audio."""
    x = z * np.exp(2j * np.pi * PITCH * np.arange(len(z)) / FS)
    rx = CwReceiver(FS, PITCH, 500.0)
    blk = int(0.075 * FS)
    return np.concatenate([rx(x[i:i + blk]) for i in range(0, len(x), blk)])


def signal(text, wpm, snr_2500, seconds, seed=0):
    rng = np.random.default_rng(seed)
    env = envelope(text, wpm, FS, tail=1.0)
    env = np.concatenate([np.zeros(FS), env])
    n = int(seconds * FS)
    env = np.concatenate([env, np.zeros(max(0, n - len(env)))])[:n]
    a = np.sqrt(10 ** (snr_2500 / 10) * 2 / FS * 2500)
    noise = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    return receive(a * env * np.exp(2j * np.pi * 3.0 * np.arange(n) / FS) + noise)


def decode(audio, **kw):
    d = Decoder(None, freq=PITCH, polarity=False, engine="nn", **kw)
    blk = int(0.075 * FS)
    return "".join(e["text"] for i in range(0, len(audio), blk) for e in d.feed(audio[i:i + blk], FS)), d


def test_shipped_weights_read_a_cq():
    text, d = decode(signal("CQ CQ DE G4ABC G4ABC K", 22, 5.0, 14.0))
    assert "G4ABC" in text.split()
    assert d.wpm is not None and 17 <= d.wpm <= 27


@pytest.mark.parametrize("wpm", [12, 35])
def test_shipped_weights_follow_the_speed(wpm):
    text, _ = decode(signal("CQ TEST DL1ABC DL1ABC", wpm, 10.0, 30.0 if wpm < 20 else 12.0, seed=wpm))
    assert "DL1ABC" in text.replace(" ", "")


def test_shipped_weights_read_a_strong_local():
    # A local station 45 dB over the noise in 2.5 kHz: the first network,
    # trained to 30 dB, printed nothing here (its noise references hold the
    # tone's own main lobe at that level), where the classic engine read it.
    text, _ = decode(signal("CQ CQ DE EA8QP EA8QP K", 24, 45.0, 12.0, seed=3))
    assert "EA8QP" in text.split()


def test_noise_prints_nothing():
    rng = np.random.default_rng(7)
    n = 60 * FS
    audio = receive(rng.standard_normal(n) + 1j * rng.standard_normal(n))
    text, d = decode(audio)
    assert len(text.replace(" ", "")) <= 1
    assert not d.hearing


def test_the_skimmer_runs_the_neural_engine():
    from shack_cw.skim import Skimmer
    from shack_cw.skim.synth import selftest_band
    z, stations = selftest_band()
    sk = Skimmer(24_000.0, -12_000.0 * 0.84 / 2 * 2, 12_000.0 * 0.84, 0.0, engine="nn")
    blk = int(0.075 * 24_000)
    for i in range(0, len(z), blk):
        sk.feed(z[i:i + blk])
    text = " ".join(t.text for t in sk.pool.texts.values()).replace(" ", "")
    assert sum(s.call in text for s in stations) >= 3
    assert sk.pool.bank is not None and sk.decoding >= 3


def test_an_ensemble_averages_its_networks_and_streams(tmp_path):
    # Weights joined by "+" are one network to a bank: a v1 and a v2
    # network, each reading its own share of the v2 features, averaged
    # in probability; a network with itself is just that network.
    from shack_cw.nn import Ensemble, load
    w1, w2 = random_weights(tmp_path, seed=1), random_weights(tmp_path, seed=2, front="v2")
    ens = load(f"{w1}+{w2}")
    assert isinstance(ens, Ensemble) and ens.front == "v2"
    rng = np.random.default_rng(12)
    x = rng.standard_normal((2, 403, n_feat("v2"))).astype(np.float32)
    lp, _ = ens.step(x, ens.state(2))
    a, _ = load(w1).step(x[..., :N_FEAT], load(w1).state(2))
    b, _ = load(w2).step(x, load(w2).state(2))
    np.testing.assert_allclose(np.exp(lp), (np.exp(a) + np.exp(b)) / 2, rtol=1e-4, atol=1e-6)
    st = ens.state(2)
    parts = np.concatenate([ens.step(x[:, s], st)[0] for s in blocks(x.shape[1], rng)], axis=1)
    np.testing.assert_allclose(parts, lp, rtol=1e-4, atol=1e-4)
    same, _ = load(f"{w1}+{w1}").step(x[..., :N_FEAT], load(f"{w1}+{w1}").state(2))
    np.testing.assert_allclose(same, a, rtol=1e-4, atol=1e-5)
    # And a bank runs it, channels coming and going.
    bank = Bank(6.0, f"{w1}+{w2}")
    c = rng.standard_normal((300, 3, len(offsets("v2")))) + 0j
    bank.run(c[:100, :2], ["p", "q"])
    bank.run(c[100:200], ["q", "r", "p"])
    bank.run(c[200:, 1:2], ["r"])


# ------------------------------------------------------------ the space model
@pytest.fixture(scope="module")
def space_table(tmp_path_factory):
    """A small space model built as tools/cw_neural/spacelm.py builds one."""
    import subprocess
    import sys
    from pathlib import Path
    out = tmp_path_factory.mktemp("space") / "spacelm.npz"
    tool = Path(__file__).resolve().parents[1] / "tools" / "cw_neural" / "spacelm.py"
    subprocess.run([sys.executable, str(tool), str(out), "--overs", "20000"], check=True, capture_output=True)
    return out


def test_the_space_model_knows_where_words_end(space_table):
    from shack_cw.nn.space import SpaceModel
    m = SpaceModel(space_table)
    t = tokens
    assert m.logodds(t("CQ"), t("D")[0]) > 2      # CQ DE
    assert m.logodds(t("DE"), t("G")[0]) > 2      # DE G4ABC
    assert m.logodds(t("Q D"), t("E")[0]) < -2    # not D E
    assert m.logodds(t("DK5L"), t("M")[0]) < -1   # not DK5L M
    assert m.logodds(t("G4"), t("A")[0]) < -2     # not G4 ABC


def test_the_space_model_decides_a_gap_with_the_network(tmp_path, space_table):
    # Bank._spaced: the network's best margin for a space in a gap, plus
    # space_lm times the model, decides it. Driven directly with made-up
    # network outputs: "CQ" then "D" with the space losing narrowly gets
    # its space from the model; "G4" then "A" with the space winning
    # narrowly loses it, and keeps it with the model off.
    from shack_cw.nn.space import SpaceModel
    b = Bank(6.0, random_weights(tmp_path))
    b._space_model = SpaceModel(space_table)
    b.run(np.zeros((8, 2, 7), np.complex128), ["a", "b"])
    S = {ch: SYMBOLS.index(ch) for ch in "CQDG4A"}
    ts = np.array([0, 2, 6])

    def spaced(j, chars, gap_margin, lm):
        b.space_lm = lm
        b.held[j], b.hist[j], b.gap[j] = [], [], -np.inf
        best = np.array([S[chars[0]], 0, S[chars[1]], 0, 0, 0, S[chars[2]]])
        margin = np.array([-9, -9, -9, -9, gap_margin, -9, -9.0])
        b._spaced(j, best, margin, ts, 0)
        return "".join(SYMBOLS[s] for _, s in b.held[j])

    assert spaced(0, "CQD", -0.5, 1.0) == "CQ D"
    assert spaced(1, "G4A", 0.5, 1.0) == "G4A"
    assert spaced(1, "G4A", 0.5, 0.0) == "G4 A"


def test_a_fresh_decoder_answers_before_its_first_block():
    # Flex read .wpm and .locked straight after creating the decoder and
    # got IndexError: the bank has no channel until 150 ms have run.
    d = Decoder(None, freq=PITCH, polarity=False, engine="nn")
    assert d.wpm is None and d.locked is False and d.snr is None and d.hearing is False
    d.feed(np.zeros(int(0.075 * FS), np.float32), FS)       # half a gather: still no channel
    assert d.wpm is None and d.locked is False and d.snr is None and d.hearing is False
