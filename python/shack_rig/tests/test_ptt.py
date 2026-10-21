"""Transmit is lease-only: what keys a rig, what refuses, and what unkeys it.

These are the library's transmit-safety promises (lease.py), each driven
through the real IcomRig: against a list standing in for the link where the
question is "what frame went out", and against FakeIcom on loopback where it
is "what did the radio end up doing".
"""

import asyncio

import pytest

from helpers import OnFake, payloads, say, until, wired
from shack_rig import Lease, PttRefused
from shack_rig.icom import models

KEY = b"\x1c\x00\x01"
UNKEY = b"\x1c\x00\x00"
IC9700 = models.model("ic-9700")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def keyable(**kw):
    """A rig that could be keyed if asked properly: on 2 m, transmit allowed."""
    r = wired(IC9700, allow_ptt=True, **kw)
    r.state["freq"] = 144_174_000
    return r


def keyed_frames(r):
    return [p for p in payloads(r) if p == KEY]


# -- refusals ----------------------------------------------------------------

def test_keying_without_a_lease_raises_and_sends_nothing():
    r = keyable()
    with pytest.raises(PttRefused, match="needs a TX lease"):
        r.set_ptt(True)
    with pytest.raises(PttRefused):
        r.set_ptt(True, None)
    assert keyed_frames(r) == [] and r.state["ptt"] is False


def test_something_that_is_not_a_lease_is_not_accepted_as_one():
    r = keyable()
    for fake in (True, "lease", object(), {"holder": "x"}):
        with pytest.raises(PttRefused, match="not a TX lease"):
            r.set_ptt(True, fake)
    assert keyed_frames(r) == []


def test_a_lease_that_refuses_is_a_refusal_with_its_reason():
    r = keyable()
    lease = Lease("digi FT8")
    lease.revoke("the transmit switch is off")
    with pytest.raises(PttRefused) as e:
        r.set_ptt(True, lease)
    assert e.value.reason == "the transmit switch is off"
    assert keyed_frames(r) == []


def test_allow_ptt_off_refuses_even_with_a_valid_lease():
    r = keyable()
    r.allow_ptt = False
    with pytest.raises(PttRefused, match="switched off"):
        r.set_ptt(True, Lease("page"))
    assert keyed_frames(r) == []


def test_the_lease_is_checked_before_anything_else():
    # With transmit switched off *and* no lease, the answer is still "no lease":
    # the rule "no lease, no transmit" does not depend on configuration.
    r = wired(IC9700, allow_ptt=False)
    with pytest.raises(PttRefused, match="needs a TX lease"):
        r.set_ptt(True)


def test_an_unknown_frequency_or_one_outside_the_model_is_not_keyed():
    r = keyable()
    r.state["freq"] = None
    with pytest.raises(PttRefused, match="not known"):
        r.set_ptt(True, Lease("page"))
    r.state["freq"] = 14_074_000                # an IC-9700 has no HF
    with pytest.raises(PttRefused, match="outside the IC-9700's transmit bands"):
        r.set_ptt(True, Lease("page"))
    assert keyed_frames(r) == []


def test_widening_what_the_9700_tunes_did_not_widen_where_it_keys():
    # 146.5 MHz is tunable on an IC-9700 (its US version covers 144-148), and
    # a set_freq there goes out. Keying there is still refused by the model's
    # floor even under a valid lease: whether a UK NoV makes 146-147 MHz
    # transmittable is the licence dataset's answer, and changing the receive
    # table must not have answered it on the dataset's behalf.
    r = keyable()
    assert r.set_freq(146_500_000) and r.state["freq"] == 146_500_000
    with pytest.raises(PttRefused, match="outside the IC-9700's transmit bands"):
        r.set_ptt(True, Lease("page"))
    assert keyed_frames(r) == []


# -- keying and unkeying -----------------------------------------------------

def test_a_valid_lease_keys_and_names_the_holder():
    r = keyable()
    assert r.set_ptt(True, Lease("digi FT8")) is True
    assert payloads(r)[-1] == KEY
    assert r.state["ptt"] is True and r.lease_holder == "digi FT8"


def test_no_link_is_false_not_an_exception():
    r = keyable()
    r._link.connected = False
    r.state["connected"] = False
    assert r.set_ptt(True, Lease("page")) is False
    assert r.lease_holder is None


def test_unkeying_needs_no_lease_and_is_never_refused():
    r = keyable()
    r.set_ptt(True, Lease("page"))
    r.allow_ptt = False                          # as after a restart with TX off
    assert r.set_ptt(False) is True
    assert payloads(r)[-1] == UNKEY
    assert r.state["ptt"] is False and r.lease_holder is None


def test_an_unkey_that_could_not_be_sent_is_sent_when_the_link_returns():
    r = keyable()
    r.set_ptt(True, Lease("page"))
    r._link.connected = False
    r.state["connected"] = False
    assert r.set_ptt(False) is False
    r._link.connected = True
    r.state["connected"] = True
    r._watch_lease()                             # what the poll loop does first
    assert payloads(r)[-1] == UNKEY and r.state["ptt"] is False


def test_a_lease_that_lapses_while_keyed_unkeys_the_rig():
    clock = Clock()
    lease = Lease("remote-1", seconds=5.0, clock=clock)
    r = keyable()
    r.set_ptt(True, lease)
    clock.t += 3
    assert lease.heartbeat()
    r._watch_lease()
    assert payloads(r)[-1] == KEY, "a heartbeated lease keeps the key"
    clock.t += 6                                 # no heartbeat for 6 s
    r._watch_lease()
    assert payloads(r)[-1] == UNKEY
    assert r.state["ptt"] is False and r.lease_holder is None


def test_a_lease_that_loses_the_broker_claim_unkeys_the_rig():
    claimed = [True]
    r = keyable()
    r.set_ptt(True, Lease("pad", claim=lambda: claimed[0]))
    r._watch_lease()
    assert payloads(r)[-1] == KEY
    claimed[0] = False                           # the broker gave the radio away
    r._watch_lease()
    assert payloads(r)[-1] == UNKEY


def test_a_lease_that_breaks_counts_as_a_refusal():
    class Broken:
        holder = "broken"

        def __init__(self):
            self.ok = True

        def refusal(self):
            if self.ok:
                return None
            raise RuntimeError("the app's lease store went away")

    lease = Broken()
    r = keyable()
    r.set_ptt(True, lease)
    lease.ok = False
    r._watch_lease()
    assert payloads(r)[-1] == UNKEY


def test_the_operator_keying_at_the_radio_is_reported_not_overridden():
    r = keyable()
    say(r, b"\x1c\x00\x01")                      # the radio says it is transmitting
    assert r.state["ptt"] is True
    r._watch_lease()
    assert UNKEY not in payloads(r), "no lease of ours, so nothing of ours to end"


# -- transmit audio ----------------------------------------------------------

def test_transmit_audio_needs_the_rig_keyed_under_a_live_lease():
    clock = Clock()
    lease = Lease("digi FT8", clock=clock)
    r = keyable()
    assert r.send_audio(b"\0\0" * 240) is False, "not keyed: VOX must not be a way round the lease"
    r.set_ptt(True, lease)
    assert r.send_audio(b"\0\0" * 240) is True
    clock.t += 10
    assert r.send_audio(b"\0\0" * 240) is False, "the lease lapsed"
    r.set_ptt(False)
    assert r.send_audio(b"\0\0" * 240) is False
    assert len(r._link.audio) == 1


# -- the reference lease -----------------------------------------------------

def test_the_reference_lease():
    clock = Clock()
    lease = Lease("x", seconds=5.0, clock=clock)
    assert lease.refusal() is None
    clock.t += 4.9
    assert lease.heartbeat() and lease.refusal() is None
    clock.t += 5.1
    assert "expired" in lease.refusal()
    assert lease.heartbeat() is False, "a lapsed lease is not revived by a late heartbeat"
    fresh = Lease("y", clock=clock)
    fresh.release()
    assert fresh.refusal() == "the transmission ended" and not fresh.heartbeat()


# -- end to end, on the fake radio -------------------------------------------

def test_on_the_wire_the_radio_is_keyed_and_unkeyed_when_the_lease_ends():
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            with pytest.raises(PttRefused):
                rig.set_ptt(True)
            await asyncio.sleep(0.3)
            assert radio.ptt_commands == [], "the radio was never asked to key"

            lease = Lease("remote-1")
            assert rig.set_ptt(True, lease)
            assert await until(lambda: radio.ptt, 2)
            lease.revoke("the client stopped heartbeating")
            # The poll loop runs every 0.1 s here; the unkey must follow
            # within a few of them, without anybody calling set_ptt(False).
            assert await until(lambda: not radio.ptt, 2)
            assert radio.ptt_commands == [True, False]
            assert await until(lambda: rig.state["ptt"] is False, 2)

    asyncio.run(run())


def test_on_the_wire_no_transmit_stream_is_asked_for_with_tx_off():
    async def run():
        async with OnFake(tx_enabled=False) as (rig, radio):
            req = radio.stream_requests[-1]
            assert req["tx_enable"] == 0 and req["tx_rate"] == 0
            with pytest.raises(PttRefused, match="switched off"):
                rig.set_ptt(True, Lease("page"))
            await asyncio.sleep(0.2)
            assert radio.ptt_commands == [] and radio.tx_audio_packets == 0

    asyncio.run(run())
