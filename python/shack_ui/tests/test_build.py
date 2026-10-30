"""The builder and the document answer: what an app writes and what a
client then receives."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from shack_ui import (A, Req, Screen, T, TransmitActionError, UIError, Unsupported, descriptor, downgrade, entry,
                      item, local, p, part, rev, serve_document, status)
from shack_ui import build


def screen(*children, **kw):
    s = Screen("test/home", title="Test", **kw)
    s.root = p.column(*children)
    return s


# ---------------------------------------------------------------- names and values

def test_keywords_become_the_contract_s_names():
    lst = p.list("rows", from_="c", on_activate=A.back(), keep_focus=True, max_w=300)
    assert lst == {"type": "list", "id": "rows", "from": "c", "onActivate": {"do": "back"}, "keepFocus": True,
                   "maxW": 300}
    assert p.text("x", maxW=3)["maxW"] == 3          # camelCase passes through


def test_nested_dicts_and_item_data_keep_their_names():
    s = Screen("t/x")
    s.collection("c").put("a", {"snake_case": 1}, distText="2 nm")
    s.root = p.map("m", view={"minKm": 5})
    d = s.document()
    assert d["collections"]["c"]["items"]["a"] == {"snake_case": 1, "distText": "2 nm"}
    assert d["root"]["view"] == {"minKm": 5}


def test_typed_values_take_datetimes_and_bindings():
    t0 = datetime(2026, 10, 30, 9, 20, tzinfo=timezone.utc)
    assert T.countdown(t0, ["in {d}", "up {d}", "over"], then=t0 + timedelta(minutes=15)) == {
        "countdown": {"to": "2026-10-30T09:20:00Z", "text": ["in {d}", "up {d}", "over"],
                      "then": "2026-10-30T09:35:00Z"}}
    assert T.ago(item("at")) == {"ago": {"$": "at"}}
    assert T.freq(145_500_000, "MHz", 3, text="145.500 MHz") == {"freq": 145_500_000, "unit": "MHz",
                                                                "decimals": 3, "text": "145.500 MHz"}
    with pytest.raises(TypeError):
        T.ago(datetime(2026, 10, 30, 9, 20))                 # naive: which zone?
    assert local("adsb.hide", [], has="ground", not_=True) == {"local": "adsb.hide", "default": [],
                                                             "has": "ground", "not": True}


# ---------------------------------------------------------------- actions refused where written

def test_a_transmit_call_without_a_lease_is_refused_at_the_line():
    with pytest.raises(TransmitActionError) as e:
        A.post("api/cq", level="transmit")
    assert e.value.code == "transmit_action"
    with pytest.raises(TransmitActionError):
        A.post("api/cq", level="control", tx=True)
    a = A.post("api/cq", {"text": A.VALUE}, level="transmit", tx=True, lease="ptt")
    assert a["tx"] is True and a["lease"] == "ptt"


@pytest.mark.parametrize("path,code", [("/api/x", "bad_path"), ("../x", "bad_path"), ("http://x/y", "absolute_url"),
                                       ("api/x?c={$token}", "bad_placeholder")])
def test_a_bad_path_is_refused_at_the_line(path, code):
    with pytest.raises(UIError) as e:
        A.post(path, level="control")
    assert e.value.code == code


def test_a_call_needs_a_level():
    with pytest.raises(TypeError):
        A.post("api/x")                                     # keyword-only, no default


def test_actions_carry_text_hint_and_confirm():
    a = A.delete("api/rec/1", level="control", text="Delete", hint="Del",
                 confirm=A.ask("Delete?", "It cannot be undone.", tone="bad", no="Keep"))
    assert a["confirm"] == {"title": "Delete?", "text": "It cannot be undone.", "tone": "bad", "no": "Keep"}
    assert a["text"] == "Delete" and a["hint"] == "Del"


def test_every_kind_of_action_builds():
    acts = [A.open("radio/dab", {"band": "III"}), A.back(), A.show("menu", tab="map"), A.hide("menu"),
            A.focus("list"), A.part("map", "fit"), A.select("thing", item("key")),
            A.local("adsb.hide", toggle="ground"), A.local("adsb.style", cycle=["real", "night"]),
            A.local("adsb.forgetS", set=None), A.send("scan", 1), A.link("https://example.org/x", qr=True),
            A.seq(A.part("map", "follow"), A.show("hud"))]
    assert [a["do"] for a in acts] == ["open", "back", "show", "hide", "focus", "part", "select", "local", "local",
                                       "local", "send", "link", "seq"]
    assert acts[9] == {"do": "local", "key": "adsb.forgetS", "set": None}


# ---------------------------------------------------------------- the document

def test_json_validates_the_whole_document():
    s = screen(p.button("Go", A.show("nowhere")))
    with pytest.raises(UIError) as e:
        s.json()
    assert e.value.code == "bad_reference"
    s = screen(p.text(item("x")))
    with pytest.raises(UIError) as e:
        s.json()
    assert e.value.code == "template_binding"


def test_a_ptt_and_a_lease_gated_action_pass():
    s = screen(p.ptt("ptt", "api/ptt", "api/ptt/heartbeat"),
               p.button("CQ", A.post("api/cq", level="transmit", tx=True, lease="ptt")))
    d = s.json()
    assert d["root"]["children"][0] == {"type": "ptt", "id": "ptt", "key": "api/ptt",
                                        "heartbeat": "api/ptt/heartbeat", "holdMs": 1000}


def test_ptt_with_a_short_hold_or_an_action_is_refused():
    with pytest.raises(TransmitActionError):
        screen(p.ptt("ptt", "api/ptt", "api/ptt/heartbeat", hold_ms=200)).json()
    with pytest.raises(TransmitActionError):
        screen(p.tx_arm("arm", "api/autotx", "api/ptt/heartbeat", action=A.back())).json()


def test_a_call_to_a_ptt_route_is_refused():
    s = screen(p.ptt("ptt", "api/ptt", "api/ptt/heartbeat"),
               p.button("Sneaky", A.post("api/ptt/heartbeat", level="control")))
    with pytest.raises(UIError) as e:
        s.json()
    assert e.value.code == "key_route"


def test_the_document_carries_everything_given_and_a_stable_rev():
    def build():
        s = Screen("radio/x", title="X", form="phone", level="control")
        s.backdrop("glass").words({"s": "{n} s"}).colors({"a": {"dark": "#000000", "light": "#FFFFFF"}})
        s.symbols({"dot": {"paths": [{"circle": [12, 12, 4], "fill": "tone"}]}})
        s.collection("c", {"a": {"label": "A"}}, filter={"hide": local("x.hide", [])})
        s.selection("one", from_="c")
        s.hold("rx", A.post("api/s", {"client": A.CLIENT}, level="control"),
               A.delete("api/s?client={$client}", level="control"), renew_s=10)
        s.poll("ui/v2/poll/radio/x", 5)
        s.key("menu", A.show("p"))
        s.root = p.stack(p.list("l", from_="c", selection="one", template=p.text(item("label"))),
                         p.panel("p", p.text("hi")))
        return s.json()
    a, b = build(), build()
    assert a == b and a["rev"] == rev(a) and len(a["rev"]) == 12
    assert a["holds"][0]["renewS"] == 10 and a["poll"] == {"path": "ui/v2/poll/radio/x", "everyS": 5}
    assert a["collections"]["c"]["order"] == ["a"]


def test_without_a_request_a_screen_is_for_a_tv_at_view():
    d = Screen("t/x", root=p.text("x")).json()
    assert (d["form"], d["level"]) == ("tv", "view")
    d = Screen("t/x", req=Req(form="phone", level="control"), root=p.text("x")).json()
    assert (d["form"], d["level"]) == ("phone", "control")


# ---------------------------------------------------------------- older clients (§2.3)

def test_a_part_newer_than_the_client_becomes_its_fallback_or_goes(monkeypatch):
    monkeypatch.setattr(build, "UI_MINOR", 2)
    doc = {"ui": "2.2", "screen": "t", "root": p.column(
        part("hologram", since="2.2", fallback=p.text("flat")),
        part("sparkle", since="2.1"),
        p.panel("side", part("hologram", since="2.2")),
        p.tabs("t", [p.tab("a", "A", part("sparkle", since="2.1", fallback=p.text("old")))]),
        p.text("kept"))}
    for_old = downgrade(doc, 0)
    kids = for_old["root"]["children"]
    assert for_old["ui"] == "2.0"
    assert kids[0] == {"type": "text", "value": "flat"}
    assert [k["type"] for k in kids] == ["text", "panel", "tabs", "text"]
    assert "child" not in kids[1]
    assert kids[2]["tabs"][0]["child"] == {"type": "text", "value": "old"}
    assert downgrade(doc, 1)["root"]["children"][1] == {"type": "sparkle", "since": "2.1"}
    assert downgrade(doc, 2) == doc
    assert downgrade({"ui": "2.2", "root": part("hologram", since="2.2")}, 0)["root"]["type"] == "empty"


def test_a_part_type_s_minor_counts_without_since(monkeypatch):
    monkeypatch.setattr(build, "UI_MINOR", 1)
    monkeypatch.setitem(build.PART_SINCE, "gauge", 1)
    doc = {"ui": "2.1", "screen": "t", "root": p.column(part("gauge", fallback=p.text("12 dB")))}
    assert downgrade(doc, 0)["root"]["children"] == [{"type": "text", "value": "12 dB"}]


# ---------------------------------------------------------------- the request and the answer

def test_req_reads_the_client_s_headers():
    r = Req.from_headers({"X-OpenHamShack-UI": "2.0", "x-openhamshack-form": "phone; w=411; h=891",
                          "Accept-Language": "fr;q=0.5, en-GB;q=0.9, *", "X-OpenHamShack-Client": "remote-7f3a",
                          "X-OpenHamShack-Device": "dev_K4M2", "X-OpenHamShack-Level": "control",
                          "X-OpenHamShack-Transmit": "digital"})
    assert r == Req(form="phone", w=411, h=891, minor=0, lang="en-GB", client="remote-7f3a", device="dev_K4M2",
                    level="control", transmit="digital")


def test_req_errs_on_the_restricted_side_when_headers_say_nothing():
    r = Req.from_headers({"X-OpenHamShack-Level": "admin", "X-OpenHamShack-Form": "fridge"})
    assert (r.level, r.form, r.transmit, r.device) == ("view", "tv", "none", None)
    assert Req.from_headers({"X-OpenHamShack-Level": "transmit"}).transmit == "full"   # older controller


@pytest.mark.parametrize("ui", ["1.4", "3.0", "two"])
def test_another_major_is_406(ui):
    with pytest.raises(Unsupported):
        Req.from_headers({"X-OpenHamShack-UI": ui})
    r = serve_document({"X-OpenHamShack-UI": ui}, lambda req: screen(p.text("x")))
    assert r.status == 406 and json.loads(r.body) == {"error": f"This box serves ui 2.0, not {ui}",
                                                      "code": "ui_unsupported", "ui": ["2.0"]}


def test_serve_document_gives_etag_then_304():
    seen = []

    def build_it(req):
        seen.append(req)
        return Screen("t/x", title="X", req=req, root=p.text("hello"))

    r = serve_document({"X-OpenHamShack-Form": "tablet", "Accept-Language": "de"}, build_it)
    doc = json.loads(r.body)
    assert r.status == 200 and r.headers["ETag"] == f'"{doc["rev"]}"' and doc["form"] == "tablet"
    assert r.headers["Content-Language"] == "de" and r.headers["Cache-Control"] == "no-cache"
    again = serve_document({"X-OpenHamShack-Form": "tablet", "If-None-Match": r.headers["ETag"]}, build_it)
    assert again.status == 304 and again.body == b"" and again.headers["ETag"] == r.headers["ETag"]
    assert serve_document({"If-None-Match": r.headers["ETag"]}, build_it).status == 200   # a TV gets another rev
    assert seen[0].form == "tablet"


def test_descriptor_and_status():
    d = descriptor([entry("aircraft", "Aircraft", "radio/aircraft", art="ui/v2/art/aircraft.png", icon="aircraft",
                          forms=["tv", "tablet"])], app="shack-radio", name="Radio")
    assert d == {"ui": "2.0", "app": "shack-radio", "name": "Radio", "minClient": "2.0", "entries": [
        {"id": "aircraft", "title": "Aircraft", "art": "ui/v2/art/aircraft.png", "icon": "aircraft",
         "screen": "radio/aircraft", "forms": ["tv", "tablet"]}]}
    assert status({"radio": ("145.500 MHz FM", "good")}) == {"entries": {"radio": {"status": "145.500 MHz FM",
                                                                                  "tone": "good"}}}
