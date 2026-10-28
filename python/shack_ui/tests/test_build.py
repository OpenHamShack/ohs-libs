"""The builder refuses a transmit action at the line that writes it."""
import pytest

from shack_ui import Action, Bind, Document, TransmitActionError, UIError, native, node


def screen(*children):
    return Document("test/home", node("column", *children), title="Test")


def test_a_built_screen_is_a_valid_document():
    doc = screen(node("value", value=Bind("state.vfo", format="freq")),
                 native("waterfall", stream="main", fallback=node("text", text="Needs a newer app.")),
                 node("button", label="Stop", action=Action.confirm("Stop?", Action.post("api/stop", "control"))),
                 native("ptt", tv=False)).to_json()
    assert doc["ui"] == "1.0"
    assert doc["root"]["children"][1] == {"c": "native", "name": "waterfall", "v": 1, "props": {"stream": "main"},
                                          "fallback": {"c": "text", "text": "Needs a newer app."}}
    assert doc["root"]["children"][2]["action"]["action"] == {"do": "post", "path": "api/stop", "level": "control"}


def test_post_at_transmit_level_is_refused_where_it_is_written():
    with pytest.raises(TransmitActionError) as err:
        Action.post("api/tx", "transmit")
    assert err.value.code == "transmit_action"


def test_a_hand_made_transmit_action_is_refused_too():
    with pytest.raises(TransmitActionError):
        Action("post", {"path": "api/tx", "level": "transmit"})


@pytest.mark.parametrize("name", ["ptt", "enableTx"])
def test_ptt_and_enable_tx_take_no_generic_action(name):
    with pytest.raises(TransmitActionError) as err:
        native(name, action=Action.post("api/ptt", "control"))
    assert err.value.code == "transmit_component_action"


def test_only_the_v1_vocabulary_builds():
    with pytest.raises(UIError) as err:
        node("marquee")
    assert err.value.code == "unknown_component"
    with pytest.raises(UIError) as err:
        native("hologram")
    assert err.value.code == "unknown_native"


def test_document_to_json_validates_the_whole_tree():
    # A node built with a raw dict action slips past the builder's own
    # checks; to_json still refuses it.
    bad = node("button")
    bad.props["action"] = {"do": "post", "path": "/abs", "level": "control"}
    with pytest.raises(UIError) as err:
        screen(bad).to_json()
    assert err.value.code == "schema"
