"""The worked example builds the contract's Aircraft document exactly, and
its live half drives a client to the state the contract's stream does."""
import importlib.util
import json
from pathlib import Path

from shack_ui import LiveHub, Req, apply_frame, drawn_keys, state_from_document

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "spec" / "vectors" / "ui2" / "fixtures"
spec = importlib.util.spec_from_file_location("radio_aircraft", ROOT / "python" / "shack_ui" / "examples" /
                                              "radio_aircraft.py")
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def test_the_example_builds_the_contract_s_aircraft_document():
    got = example.aircraft(Req(form="tv", level="control")).json()
    want = json.loads((FIX / "screens" / "radio" / "aircraft.json").read_text())
    # rev is a hash of the document; the fixture's was written by hand.
    assert got.pop("rev") and want.pop("rev")
    assert got == want


def test_its_live_half_keeps_a_client_current():
    hub = LiveHub(clock=lambda: 0.0)
    feed, heard, gone = example.live_feed(hub)
    doc = example.aircraft(Req()).json()
    client = feed.connect("tv-1")
    start = state_from_document(doc)
    a = {"lat": 55.5, "lon": -4.4, "track": 90, "altText": "FL370", "at": "2026-10-30T09:20:00Z",
         "distText": "6.7 nm · 92°", "line2": "FL370 · 450 kn", "tone": "band.top"}
    heard("F00202", a)
    heard("F00202", a | {"lat": 55.51})
    state = apply_frame(start, client.frame(0.0), start)
    item = state["collections"]["aircraft"]["items"]["F00202"]
    assert item["lat"] == 55.51 and item["trail"] == [[55.5, -4.4, "band.top"], [55.51, -4.4, "band.top"]]
    gone("F00202")
    state = apply_frame(state, client.frame(10.0), start)
    assert drawn_keys(state, "aircraft") == []
