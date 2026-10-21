"""The language model in the neural engine's search (shack_cw.nn.lm) and
the tool that builds it (tools/cw_lm/build.py).

The search is driven with made-up network outputs, so each test says
exactly what the network heard; the model is built by the real tool from
a few lines of survey-shaped text and a call list."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from shack_cw.nn import BLANK, SPACE, SYMBOLS, Bank, tokens
from shack_cw.nn.lm import CharLM, Search

TOOL = Path(__file__).resolve().parents[1] / "tools" / "cw_lm" / "build.py"


def _window(rec, k, text, group="kiwi-cwpseudo", cls="agree"):
    return {"rec": rec, "group": group, "hz": 14025000.0, "k": k, "cls": cls, "nn": text, "classic": text}


@pytest.fixture(scope="module")
def table(tmp_path_factory):
    d = tmp_path_factory.mktemp("lm")
    texts = ["CQ CQ DE G4ABC G4ABC K", " G4ABC DE DL1XYZ TU 5NN", "CQ TEST OK1CF", "CQCQ DE SM5D SM5D K",
             " TNX FER CALL UR RST 599 NAME BRIAN"] * 3
    wins = [_window(f"rec-{i}", 0, t) for i, t in enumerate(texts)]
    # Held out: the test set's recordings, and windows the engines did not agree on.
    wins.append(_window("box-20m-20261013", 0, "ZZZZZ QQQQ ZZZZ", group="box-20m-skimmer"))
    wins.append(_window("rec-x", 0, "XQXQ XQXQ", cls="readable"))
    (d / "analysis.json").write_text(json.dumps({"windows": wins}))
    (d / "calls.txt").write_text("OK1CF 50\nG4ABC 9\nDL1XYZ 4\nSM5D 30\nBUST1 1\n")
    out = d / "lm.npz"
    r = subprocess.run([sys.executable, str(TOOL), str(out), "--analysis", str(d / "analysis.json"),
                        "--calls", str(d / "calls.txt"), "--copies", "20", "--min-count", "1"],
                       check=True, capture_output=True, text=True)
    return out, json.loads(r.stdout)


def lp(lm: CharLM, text: str, after: str = "") -> float:
    ctx = (SPACE,) * (lm.order - 1)
    for s in tokens(after):
        ctx = ctx[1:] + (s,)
    total = 0.0
    for s in tokens(text):
        total += float(lm.dist(ctx)[s])
        ctx = ctx[1:] + (s,)
    return total


def test_the_model_learns_cw_and_holds_out_what_it_must(table):
    path, meta = table
    lm = CharLM(path)
    # Calls in the text were masked and refilled from the list: the list
    # is what the model knows, a call spotted only once is a bust.
    assert lm.known("OK1CF") and lm.known("OK1CF/P") and not lm.known("BUST1")
    assert meta["masked_calls"] > 0
    # The shape of an over: DE then a call, not DE glued to it.
    assert lp(lm, "DE G4ABC") > lp(lm, "DEG4ABC") + 2
    # The test set's text and unagreed text never reach it.
    assert lp(lm, "ZZZZZ") < lp(lm, "CQ CQ") - 10
    assert lp(lm, "XQXQ") < lp(lm, "TU 5NN") - 10


def path_logp(path: str, strength: float = 8.0, alt: dict | None = None) -> np.ndarray:
    """Network outputs for a reading: each character for two outputs then
    a blank, `strength` nats over everything else. alt maps a character's
    index in `path` to (other character, its log-probability margin):
    the network half hears that other one there instead."""
    seq = []
    for i, ch in enumerate(path):
        sym = SPACE if ch == " " else SYMBOLS.index(ch)
        for _ in range(2):
            row = np.full(len(SYMBOLS), -strength)
            row[sym] = 0.0
            if alt and i in alt:
                other, m = alt[i]
                row[SYMBOLS.index(other)] = -m
            seq.append(row)
        seq.append(np.where(np.arange(len(SYMBOLS)) == BLANK, 0.0, -strength))
    seq += [np.where(np.arange(len(SYMBOLS)) == BLANK, 0.0, -strength)] * 80
    x = np.array(seq, np.float32)
    return x - np.log(np.exp(x).sum(axis=1, keepdims=True))


def read(search: Search, logp: np.ndarray, block: int = 8) -> str:
    out = []
    for i in range(0, len(logp), block):
        out += search.step(logp[i:i + block], i)
    return "".join(SYMBOLS[s] for _, s in out)


def test_with_no_weight_the_search_reads_what_the_network_heard(table):
    lm = CharLM(table[0])
    x = path_logp("CQ DE XQ9ZZ K")
    assert read(Search(lm, 0.0, known_call=0.0), x) == "CQ DE XQ9ZZ K"


def test_the_model_settles_a_letter_the_network_half_heard(table):
    # The last letter of OK1CF heard as T a little more than as F: greedy
    # prints OK1CT; the search, knowing OK1CF and how calls end, OK1CF.
    lm = CharLM(table[0])
    x = path_logp("CQ DE OK1CT K", alt={10: ("F", 0.3)})
    assert read(Search(lm, 0.0, known_call=0.0), x) == "CQ DE OK1CT K"
    assert read(Search(lm, 0.5), x) == "CQ DE OK1CF K"


def test_a_sure_network_is_not_overruled(table):
    # The same letter heard clearly: the model's dislike of OK1CT costs
    # less than the network's certainty.
    lm = CharLM(table[0])
    x = path_logp("CQ DE OK1CT K", alt={10: ("F", 6.0)})
    assert read(Search(lm, 0.5), x) == "CQ DE OK1CT K"


def test_a_character_is_out_within_the_lag_in_any_blocks(table):
    lm = CharLM(table[0])
    x = path_logp("CQ CQ DE SM5D SM5D K")
    for block in (1, 3, 8, 40):
        s = Search(lm, 0.5, lag=25)
        got, late = [], []
        for i in range(0, len(x), block):
            for t, sym in s.step(x[i:i + block], i):
                got.append(SYMBOLS[sym])
                late.append(min(i + block, len(x)) - t)
        assert "".join(got) == "CQ CQ DE SM5D SM5D K"
        assert max(late) <= 25 + block


def test_what_is_printed_does_not_depend_on_the_blocks(table):
    # Commits are made output by output: a forced commit at a block's end
    # would print a different reading in an app's 150 ms blocks than in a
    # replay's. Uncertain outputs (several letters half heard) make the
    # beams disagree long enough for the lag to decide.
    lm = CharLM(table[0])
    rng = np.random.default_rng(3)
    x = path_logp("CQ DE OK1CT OK1CF K", strength=3.0, alt={i: ("E", float(rng.uniform(0, 1))) for i in range(0, 19, 2)})
    x = x + rng.normal(0, 0.8, x.shape).astype(np.float32)
    runs = []
    for block in (1, 7, 8, 40):
        s = Search(lm, 0.5, lag=10)
        runs.append([e for i in range(0, len(x), block) for e in s.step(x[i:i + block], i)])
    assert runs[0] and all(r == runs[0] for r in runs)


def test_the_bank_reads_through_the_search(table, tmp_path):
    # Bank._read with lm > 0 takes each channel's characters from its own
    # search; two channels, each its own reading, through the squelch.
    b = Bank(6.0)
    b.lm, b.lm_table = 0.5, str(table[0])
    b.trace = []
    b._sync(["a", "b"])
    xa, xb = path_logp("CQ DE OK1CT K", alt={10: ("F", 0.3)}), path_logp("TU 5NN")
    n = max(len(xa), len(xb))
    pad = lambda x: np.concatenate([x, np.repeat(x[-1:], n - len(x), 0)])   # noqa: E731
    logp = np.stack([pad(xa), pad(xb)]).astype(np.float32)
    got = {"a": "", "b": ""}
    for i in range(0, n, 8):
        out = {"a": [], "b": []}
        b._read(logp[:, i:i + 8].copy(), np.full((2, min(8, n - i)), 20.0), np.ones((min(8, n - i), 2), bool), out)
        for k in out:
            got[k] += "".join(out[k])
    assert got == {"a": "CQ DE OK1CF K", "b": "TU 5NN"}
    assert {k for k, _, _ in b.trace} == {"a", "b"}


# ------------------------------------------------------- the shipped model
def test_the_shipped_model_knows_real_cw():
    from shack_cw.nn.lm import load
    lm = load()
    assert lm.meta["min_count"] >= 1 and len(lm.calls) > 10000
    assert lp(lm, "CQ DE") > lp(lm, "CQDE") + 3
    assert lp(lm, "TU 5NN") > lp(lm, "TU 5NT") + 2
    # A call's prefix, digit and suffix: no space inside it.
    assert lp(lm, "DE OK1CF") > lp(lm, "DE OK 1CF") + 3


def test_a_decoder_with_the_language_model_reads_a_cq_and_not_noise():
    from test_nn import decode, receive, signal
    text, d = decode(signal("CQ CQ DE G4ABC G4ABC K", 22, 5.0, 14.0), lm=0.5)
    assert "G4ABC" in text.split() and "CQ" in text.split()
    rng = np.random.default_rng(7)
    n = 60 * 8000
    text, d = decode(receive(rng.standard_normal(n) + 1j * rng.standard_normal(n)), lm=0.5)
    assert len(text.replace(" ", "")) <= 1
