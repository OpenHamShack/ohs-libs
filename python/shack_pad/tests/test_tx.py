"""The pad as a transmit-lease client (tx.py): hold and arm, heartbeats,
refusals, and the reference in-process lease's 5 s rule."""

from helpers import Clock, RecordingLease

from shack_pad.tx import ARM_SECONDS, LocalLease, PadTx


def test_hold_keys_beats_every_second_and_release_unkeys():
    clock, lease = Clock(), RecordingLease()
    tx = PadTx("pad-test-ajazz-akp03e", clock)
    tx.press("radio", "ptt", "K1", lease, "hold", on_air=False)
    assert lease.calls("key") == [("key", "pad-test-ajazz-akp03e")]
    for _ in range(3):
        clock.advance(0.5)
        tx.tick()
    assert len(lease.calls("beat")) == 1
    clock.advance(0.5)
    tx.tick()
    assert len(lease.calls("beat")) == 2
    tx.release("K2")                    # another key's release changes nothing
    assert lease.on_air
    tx.release("K1")
    assert not lease.on_air and tx.keyed_app() is None


def test_arm_needs_two_presses_within_four_seconds_and_one_press_stops():
    clock, lease = Clock(), RecordingLease()
    tx = PadTx("pad", clock)
    tx.press("wb", "transmit", "K1", lease, "arm", on_air=False)
    assert tx.is_armed("wb") and lease.calls("key") == []
    clock.advance(ARM_SECONDS + 0.1)
    tx.tick()
    tx.press("wb", "transmit", "K1", lease, "arm", on_air=False)      # too late: arms again
    assert lease.calls("key") == []
    clock.advance(1)
    tx.press("wb", "transmit", "K1", lease, "arm", on_air=False)
    assert lease.on_air
    tx.press("wb", "transmit", "K1", lease, "arm", on_air=True)
    assert not lease.on_air and not tx.is_armed("wb")


def test_arm_on_air_from_another_client_one_press_stops_it():
    lease = RecordingLease()
    lease.on_air = True
    PadTx("pad", Clock()).press("wb", "transmit", "K1", lease, "arm", on_air=True)
    assert lease.calls("unkey") and lease.calls("key") == []


def test_a_refused_key_up_is_shown_and_nothing_beats():
    clock, lease = Clock(), RecordingLease(refuse="needs transmit level")
    tx = PadTx("pad", clock)
    tx.press("radio", "ptt", "K1", lease, "hold", on_air=False)
    assert tx.refusal("radio") == "needs transmit level" and tx.keyed_app() is None
    clock.advance(2)
    tx.tick()
    assert lease.calls("beat") == []
    clock.advance(1)
    assert tx.refusal("radio") is None


def test_an_app_that_cannot_be_reached_is_a_refusal_not_a_crash():
    class Down(RecordingLease):
        def key(self, remote):
            raise ConnectionError("app not answering")
    tx = PadTx("pad", Clock())
    tx.press("radio", "ptt", "K1", Down(), "hold", on_air=False)
    assert tx.refusal("radio") == "app not answering"


def test_a_lost_lease_or_an_app_unkeying_itself_stops_the_heartbeats():
    clock, lease = Clock(), RecordingLease()
    tx = PadTx("pad", clock)
    tx.press("radio", "ptt", "K1", lease, "hold", on_air=False)
    lease.lost = True                   # 409: the app no longer counts us as holder
    clock.advance(1)
    tx.tick()
    assert tx.keyed_app() is None
    clock.advance(1)
    tx.tick()
    assert len(lease.calls("beat")) == 1
    tx.press("radio", "ptt", "K1", lease, "hold", on_air=False)
    lease.on_air = False                # the app's own time-out
    tx.tick(lambda kind: lease.on_air)
    assert tx.keyed_app() is None


def test_abort_unkeys_and_nothing_is_remembered():
    lease = RecordingLease()
    tx = PadTx("pad", Clock())
    tx.press("radio", "ptt", "K1", lease, "hold", on_air=False)
    assert tx.abort() is True and not lease.on_air
    assert tx.abort() is False and len(lease.calls("unkey")) == 1


def test_the_local_lease_unkeys_after_five_silent_seconds():
    """CONTROLLER-PAD.md §9 case 3: the library dies while held, its
    heartbeats stop, and the app's lease ends the transmission at 5 s."""
    clock, events = Clock(), []
    lease = LocalLease(lambda: events.append("rf on"), lambda: events.append("rf off"), clock)
    tx = PadTx("pad", clock)
    tx.press("radio", "ptt", "K1", lease, "hold", on_air=False)
    assert events == ["rf on"] and lease.on_air
    clock.advance(1)
    tx.tick()                           # one heartbeat, then the process "dies": no more ticks
    clock.advance(4.9)
    assert not lease.check()
    clock.advance(0.1)
    assert lease.check() and events == ["rf on", "rf off"] and not lease.on_air
    assert lease.heartbeat("pad") is False


def test_the_local_lease_refuses_with_the_apps_reason_and_one_holder_at_a_time():
    lease = LocalLease(lambda: "licence: not in band", lambda: None, Clock())
    assert lease.key("pad") == {"onAir": False, "txError": "licence: not in band"}
    ok = LocalLease(lambda: None, lambda: None, Clock())
    assert ok.key("remote-1")["onAir"] and not ok.key("pad")["onAir"]
    ok.unkey("pad")                     # any unkey ends it, whoever sends it
    assert not ok.on_air
