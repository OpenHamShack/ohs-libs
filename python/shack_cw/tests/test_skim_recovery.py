"""The per-call recovery metric (shack_cw.skim.recovery): each way a call
is lost is named for the stage that lost it, from what the channels
printed - the decoders' text as the box recording showed it."""
from shack_cw.skim.recovery import TrackLog, stage, summary


def log(text, decoded=True, snr=12.0):
    lg = TrackLog(1, f=[0.0], snr=snr, decoded=decoded)
    lg.text = list(text)
    return lg


def test_each_stage_is_named():
    spot = {"call": "SM5D", "status": "valid"}
    assert stage("SM5D", [log("CQ DE SM5D K ")], [spot])[0] == "spotted"
    assert stage("SM5D", [log("x")], [dict(spot, status="unconfirmed")])[0] == "unconfirmed"
    assert stage("SM5D", [log("CQ DE SM5D K ")], [])[0] == "matcher"
    # The box recording's two spacing faults, as printed.
    assert stage("SM5D", [log("TIQCQDESM5D K ")], []) == ("glued", "TIQCQDESM5D K ")
    assert stage("IZ5FDE", [log("CQCQ I Z 5FDE CQ ")], [])[0] == "split"
    assert stage("GM4BRI", [log("CQ DE GM4BRS K ")], [])[0] == "near"
    assert stage("GM4BRI", [log("TNX FER CALL ")], [])[0] == "letters"
    assert stage("GM4BRI", [log("E ")], [])[0] == "squelch"
    assert stage("GM4BRI", [log("", decoded=False)], [])[0] == "channel"
    assert stage("GM4BRI", [], [])[0] == "absent"


def test_summary_counts_the_text_and_the_tracks():
    rows = [{"band": "20m", "stage": s} for s in ("spotted", "matcher", "split", "absent")]
    s = summary(rows)["20m"]
    assert (s["calls"], s["spotted"], s["in_text"], s["heard"]) == (4, 1, 2, 3)
