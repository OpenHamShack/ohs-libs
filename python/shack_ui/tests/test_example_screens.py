"""The other examples: Weather Sats and its viewer build the contract's
documents exactly, and the sketches of Radio's remaining screens build,
validate and keep to the client's icon set on every form."""
import importlib.util
import json
from pathlib import Path

import pytest

from shack_ui import Req, icon_names, iter_parts

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "spec" / "vectors" / "ui2" / "fixtures"


def example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "python" / "shack_ui" / "examples" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


wx = example("radio_wxsat")
radio = example("radio_screens")


@pytest.mark.parametrize("build,fixture", [(wx.wxsat, "wxsat"), (wx.viewer, "wxsat-viewer")])
def test_weather_sats_builds_the_contract_s_document(build, fixture):
    got = build(Req(form="tv", level="control")).json()
    want = json.loads((FIX / "screens" / "radio" / f"{fixture}.json").read_text())
    assert got.pop("rev") and want.pop("rev")
    assert got == want


@pytest.mark.parametrize("form", ["tv", "phone", "tablet"])
@pytest.mark.parametrize("screen", sorted(radio.SCREENS))
def test_each_radio_screen_builds_and_validates(screen, form):
    doc = radio.SCREENS[screen](Req(form=form, level="control")).json()     # raises on any problem
    assert doc["screen"] == screen and doc["form"] == form
    icons = {p["icon"] for p, _, _ in iter_parts(doc["root"]) if isinstance(p.get("icon"), str)}
    assert icons <= icon_names(), icons - icon_names()


def parts(screen):
    doc = radio.SCREENS[screen](Req(level="control")).json()
    return doc, {p.get("type") for p, _, _ in iter_parts(doc["root"])}


def test_what_each_screen_needed_is_there():
    doc, types = parts("radio/home")
    assert {"spectrum", "frequency", "meter", "audio", "banner"} <= types
    doc, types = parts("radio/dab")
    assert {"grid", "card", "audio", "meter"} <= types
    for screen, hold in (("radio/ships", "ais"), ("radio/aprs", "aprs"), ("radio/sonde", "sonde")):
        doc, types = parts(screen)
        assert [h["name"] for h in doc["holds"]] == [hold] and "map" in types and doc["live"]
    doc, types = parts("radio/sonde")
    assert "chart" in types
    doc, types = parts("radio/dmr")
    assert doc["holds"][0]["renewS"] == 10 and {"list", "meter", "badge"} <= types
