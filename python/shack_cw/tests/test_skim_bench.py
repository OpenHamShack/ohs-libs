"""The skimmer's measurements (shack_cw.skim.bench): its CPU at Radio's
segment rate, and its spots on the real bands' recordings when this
machine has them (shack:~/build/cw-corpus)."""
import json
import os
from pathlib import Path

import pytest

from shack_cw.skim import bench

CORPUS = Path(os.environ.get("SHACK_CW_CORPUS", Path.home() / "build" / "cw-corpus"))
MANIFEST = Path(__file__).parent / "corpus" / "manifest.json"


def test_cpu_per_channel_and_front_end():
    # 96 kS/s, Radio's segment rate on 20 m. CW-SKIMMER-PLAN §2.1 measured
    # ~2 % for the front end and 0.26 % a channel on ohs-dev; §7.4 passes
    # within 2x of the plan's 64-channel row (~22 %). Generous here: a
    # shared CI machine is slower and busier than the box.
    r = bench.cpu(channels=(16,), seconds=12.0)
    assert r[16]["decoding"] >= 12
    assert r["front"] < 15.0, r
    assert r[16]["per_channel"] < 1.5, r
    assert r["front"] + 64 * r[16]["per_channel"] < 2 * 2 * 22.0, r


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.skipif(not (CORPUS / "iq").is_dir(), reason="no corpus recordings on this machine")
def test_kiwi_corpus_spots_only_where_the_rbn_heard_calls():
    country = bench.bandplan_country()
    if country is None:
        pytest.skip("shack_bandplan (cty.dat) is not installed")
    r = bench.score_kiwi(_manifest(), CORPUS, country)
    # Never a spot on a quiet frequency; the RBN's calls found at least as
    # often as the decoders print them is the aim (13/10: 3 of 18 spotted,
    # 7 of 18 in the text, the single-signal decoder's own 7).
    assert r["noise_targets"] >= 30 and r["noise_false"] == 0, r["rows"]
    assert r["calls_found"] >= 3, r["rows"]


@pytest.mark.skipif(not (CORPUS / "box-20m-20261013").is_dir(), reason="no box recording on this machine")
def test_box_recording_against_the_rbn():
    country = bench.bandplan_country()
    if country is None:
        pytest.skip("shack_bandplan (cty.dat) is not installed")
    rec = next(r for r in _manifest() if r["id"] == "box-20m-20261013")
    r = bench.score_wideband(rec, CORPUS, country, seconds=180.0)
    # 13/10 on ohs-dev, the first 180 s: 2 spots confirmed, 1 of them the RBN's;
    # the whole 600 s: 17 and 3 of 39 (the box hears through an 80 m dipole).
    assert r["spots_valid"] >= 2 and r["found"] >= 1, r
    assert r["cpu_pct"] < 80.0, r
