"""The operator's remaps over an app's layout: the box's resolve rules, now
per app kind and stored as a dict in the station profile."""

import pytest

from shack_pad import actions, mapping
from shack_pad.drivers import AKP03E, Keypad


@pytest.fixture
def nbmap(nb):
    acts, _ = actions.load(nb[0])
    return acts, nb[1]


def resolve(nbmap, entry=None, drv=AKP03E):
    acts, layout = nbmap
    return mapping.resolve(drv, layout, entry, acts)


def test_with_no_changes_it_is_the_apps_layout(nbmap):
    pages, notes = resolve(nbmap)
    assert notes == [] and len(pages) == 1
    assert pages[0]["K1"] == {"press": "ptt"} and pages[0]["D2"] == {"press": "step", "turn": "tune"}
    kp, _ = resolve(nbmap, drv=Keypad)
    assert kp[0]["KPENTER"] == {"press": "ptt"} and kp[0]["KPPLUS"] == {"press": "tune:up"}


def test_a_model_the_layout_does_not_cover_still_gets_transmit_on_its_home(nb):
    acts, _ = actions.load(nb[0])
    pages, _ = mapping.resolve(AKP03E, {"schema": 1}, None, acts)
    assert pages[0]["K1"] == {"press": "ptt"} and pages[0]["K2"] == {"press": "none"}


def test_a_bad_layout_entry_becomes_nothing_with_a_note(nb):
    acts, _ = actions.load(nb[0])
    layout = {"schema": 1, "ajazz-akp03e": {"K9": {"press": "mute"}, "K2": {"press": "launch"},
                                             "K3": {"turn": "tune"}, "D1": {"turn": "mute"}}}
    pages, notes = mapping.resolve(AKP03E, layout, None, acts)
    assert pages[0]["K2"]["press"] == "none" and pages[0]["D1"]["turn"] == "none-turn"
    assert len(notes) == 4


def test_anything_unusable_in_the_store_falls_back_with_a_note(nbmap):
    """CONTROLLER-PAD.md §9 case 7: an unknown action, a control the model
    lacks, a turn on a key, transmit on page 2."""
    entry = {"pages": [{"K2": {"press": "warp"}, "X9": {"press": "mute"}, "K3": {"turn": "tune"}},
                       {"K4": {"press": "ptt"}}]}
    pages, notes = resolve(nbmap, entry)
    assert pages[0]["K2"]["press"] == "mute" and pages[0]["K3"] == {"press": "record"}
    assert pages[1]["K4"]["press"] == "mode:next" and pages[1]["K1"]["press"] == "ptt"
    text = " | ".join(notes)
    for bit in ("'warp'", "X9", "cannot turn", "only be set on page 1"):
        assert bit in text


def test_junk_in_the_store_is_the_default(nbmap):
    for junk in (None, "x", {"pages": "x"}, {"pages": []}, {"pages": [3]}):
        assert resolve(nbmap, junk)[0] == resolve(nbmap)[0][:1]


def test_moving_ptt_clears_its_old_key_and_it_stays_put_on_every_page(nbmap):
    acts, layout = nbmap
    entry = mapping.set_binding(AKP03E, layout, None, acts, 0, "B2", "press", "ptt")
    entry = mapping.set_pages(entry, 2)
    entry = mapping.set_binding(AKP03E, layout, entry, acts, 1, "K1", "press", "scope")
    pages, _ = mapping.resolve(AKP03E, layout, entry, acts)
    assert pages[0]["B2"]["press"] == "ptt" and pages[0]["K1"]["press"] == "none"
    assert pages[1]["B2"]["press"] == "ptt" and pages[1]["K1"]["press"] == "scope"
    with pytest.raises(ValueError, match="every page"):
        mapping.set_binding(AKP03E, layout, entry, acts, 1, "B2", "press", "mute")
    with pytest.raises(ValueError, match="page 1"):
        mapping.set_binding(AKP03E, layout, entry, acts, 1, "K2", "press", "ptt")


def test_set_binding_refuses_before_changing_anything(nbmap):
    acts, layout = nbmap
    for args in ((0, "K9", "press", "mute"), (0, "K2", "turn", "tune"), (0, "K2", "press", "tune"),
                 (0, "K2", "press", "warp"), (3, "K2", "press", "mute")):
        with pytest.raises(ValueError):
            mapping.set_binding(AKP03E, layout, None, acts, *args)


def test_on_air_the_transmit_key_cannot_be_moved_or_taken_away(nbmap):
    acts, layout = nbmap
    with pytest.raises(ValueError, match="on air"):
        mapping.set_binding(AKP03E, layout, None, acts, 0, "K1", "press", "mute", on_air=True)
    with pytest.raises(ValueError, match="on air"):
        mapping.set_binding(AKP03E, layout, None, acts, 0, "K4", "press", "ptt", on_air=True)
    moved = mapping.set_binding(AKP03E, layout, None, acts, 0, "K4", "press", "ptt")
    with pytest.raises(ValueError, match="on air"):
        mapping.reset(AKP03E, layout, moved, acts, on_air=True)
    # Anything else may still change on air.
    mapping.set_binding(AKP03E, layout, None, acts, 0, "K2", "press", "scope", on_air=True)
    assert mapping.reset(AKP03E, layout, moved, acts) is None


def test_pages_are_at_most_four_and_unreachable_pages_are_noted(nbmap):
    acts, layout = nbmap
    with pytest.raises(ValueError):
        mapping.set_pages(None, 5)
    no_page_key = {"schema": 1, "ajazz-akp03e": {**layout["ajazz-akp03e"], "B3": {"press": "none"}}}
    _, notes = mapping.resolve(AKP03E, no_page_key, {"pages": [{}, {}]}, acts)
    assert any("cannot be reached" in n for n in notes)


def test_the_profile_shape_is_per_kind_then_model():
    maps = {"radio": {"keypad": {"pages": [{}]}}}
    maps2 = mapping.with_entry(maps, "qo100nb", "ajazz-akp03e", {"pages": [{"K2": {"press": "scope"}}]})
    assert maps == {"radio": {"keypad": {"pages": [{}]}}}          # not changed in place
    assert mapping.entry_of(maps2, "qo100nb", "ajazz-akp03e")["pages"][0]["K2"] == {"press": "scope"}
    assert mapping.with_entry(maps2, "qo100nb", "ajazz-akp03e", None) == maps
    assert mapping.entry_of({"qo100nb": "junk"}, "qo100nb", "ajazz-akp03e") == {}
