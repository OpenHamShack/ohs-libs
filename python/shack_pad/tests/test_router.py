"""The input host end to end against FakeHid: events to the app in front,
faces back, transmit through the lease, and a pad that goes away while
keyed. Covers CONTROLLER-PAD.md §9 cases 1, 2, 4, 5 and 8."""

import threading
import time

import pytest
from helpers import Clock, RecordingLease, make_app

from shack_pad import InputHost
from shack_pad.drivers import AKP03E, Keypad
from shack_pad.fake import FakeHid, akp03e_report, evdev_events

K1, K2, B3 = 0x01, 0x02, 0x31
D2_CW, D2_CCW = 0x91, 0x90


def press(code):
    return akp03e_report(code, 1)


def release(code):
    return akp03e_report(code, 0)


@pytest.fixture
def setup(nb, wb):
    clock = Clock()
    radio = make_app("qo100nb", *nb, name="NB")
    tv = make_app("qo100wb", *wb, name="WB")
    host = InputHost([radio, tv], clock=clock, host="test")
    return host, radio, tv, clock


def attached(host, *reports):
    fake = FakeHid(list(reports))
    dev = AKP03E(fake)
    host.attach(dev)
    return dev, fake


def run_all(host, dev, fake):
    while not fake.drained():
        host.step(dev, 0)


def test_presses_and_turns_reach_the_app_in_front(setup):
    host, radio, tv, _ = setup
    dev, fake = attached(host, press(K2), release(K2), akp03e_report(D2_CW), akp03e_report(D2_CCW))
    run_all(host, dev, fake)
    assert [(a.action, a.kind, a.step, a.control) for a in radio.performed] == [
        ("mute", "press", 0, "K2"), ("tune", "turn", 1, "D2"), ("tune", "turn", -1, "D2")]
    assert radio.performed[0].pad == "pad-test-ajazz-akp03e" and tv.performed == []
    assert radio.performed[0].to_json()["model"] == "ajazz-akp03e"


def test_ptt_held_keys_heartbeats_and_unkeys_on_release(setup):
    host, radio, _, clock = setup
    dev, fake = attached(host, press(K1))
    run_all(host, dev, fake)
    assert radio.tx.calls("key") == [("key", "pad-test-ajazz-akp03e")]
    for _ in range(4):
        clock.advance(0.5)
        host.step(dev, 0)
    assert len(radio.tx.calls("beat")) == 2
    fake.push(release(K1))
    host.step(dev, 0)
    assert not radio.tx.on_air and radio.performed == []      # the app sees no press, only the lease


def test_disconnect_while_held_unkeys_first_then_closes_and_never_rekeys(setup):
    """§9 case 1, on a real serve() thread and real time: target under
    100 ms from the read error, and the unkey before the device is closed."""
    host, radio, _, _ = setup
    host.clock = time.monotonic
    order = radio.tx.log
    fake = FakeHid()
    real_close = fake.close
    fake.close = lambda: (order.append(("close",)), real_close())
    stop = threading.Event()
    t = threading.Thread(target=host.serve, args=(AKP03E(fake), stop), daemon=True)
    t.start()
    fake.push(press(K1))
    deadline = time.monotonic() + 2
    while not radio.tx.on_air and time.monotonic() < deadline:
        time.sleep(0.005)
    assert radio.tx.on_air
    pulled = time.monotonic()
    fake.unplug()
    while radio.tx.on_air and time.monotonic() < deadline:
        time.sleep(0.001)
    took = time.monotonic() - pulled
    t.join(1)
    assert not radio.tx.on_air and took < 0.1, took
    names = [c[0] for c in order]
    assert names.index("unkey") < names.index("close")
    # The pad comes back with nothing pressed: nothing keys.
    dev2, fake2 = attached(host)
    host.step(dev2, 0)
    assert len(radio.tx.calls("key")) == 1


def test_disconnect_after_transmit_keyed_from_the_pad_unkeys(setup):
    """§9 case 2: QO-100 WB's arming transmit, keyed from the pad."""
    host, _, tv, _ = setup
    host.set_foreground("qo100wb")
    dev, fake = attached(host, press(K1), release(K1), press(K1), release(K1))
    run_all(host, dev, fake)
    assert tv.tx.on_air
    fake.unplug()
    with pytest.raises(OSError):
        host.step(dev, 0)
    assert host.detach(dev) is True and not tv.tx.on_air


def test_a_keypads_auto_repeat_never_presses(nb):
    """§9 case 4: Enter held on a keypad sends repeats; nothing steps twice,
    and the ptt is keyed once and unkeyed on the release."""
    radio = make_app("qo100nb", *nb)
    host = InputHost([radio], clock=Clock(), host="t")
    raw = evdev_events((1, 78, 1), (1, 78, 2), (1, 78, 2), (1, 78, 0), (1, 96, 1), (1, 96, 2), (1, 96, 0))
    dev = Keypad(FakeHid([raw]))
    host.attach(dev)
    host.step(dev, 0)
    assert [(a.action, a.step) for a in radio.performed] == [("tune", 1)]
    assert len(radio.tx.calls("key")) == 1 and not radio.tx.on_air


def test_a_window_keyboard_repeat_without_release_is_not_a_second_press(nb):
    radio = make_app("qo100nb", *nb)
    host = InputHost([radio], clock=Clock())
    for _ in range(3):
        host.handle("keyboard", ("press", "KPPLUS", 0))
    host.handle("keyboard", ("release", "KPPLUS", 0))
    host.handle("keyboard", ("press", "KPPLUS", 0))
    assert len(radio.performed) == 2


def test_an_app_on_air_keeps_its_transmit_key_when_another_comes_forward(setup):
    """§9 case 5: WB on air from the pad, NB brought forward: K1 still
    stops WB, the other keys are NB's."""
    pytest.importorskip("PIL")
    host, radio, tv, _ = setup
    host.set_foreground("qo100wb")
    dev, fake = attached(host, press(K1), release(K1), press(K1), release(K1))
    run_all(host, dev, fake)
    assert tv.tx.on_air
    host.set_foreground("qo100nb")
    faces = host.faces("ajazz-akp03e")
    assert (faces["K1"].l1, faces["K1"].tone) == ("ON AIR", "onair")
    assert faces["K2"].icon == "mute"                  # NB's mute, not WB's preview
    fake.push(press(K2), release(K2), press(K1), release(K1))
    run_all(host, dev, fake)
    assert [a.action for a in radio.performed] == ["mute"]
    assert not tv.tx.on_air and radio.tx.calls("key") == []
    assert host.faces("ajazz-akp03e")["K1"].l1 == "PTT"   # back to NB's own key


def test_an_app_on_air_from_another_client_shows_on_air_with_its_countdown(setup):
    host, radio, _, _ = setup
    radio.tx.on_air = True              # keyed from Remote, say
    radio.state["faces"] = {"ptt": {"l1": "ON AIR", "l2": "off 42s", "tone": "onair"},
                            "mute": {"l1": "MUTE", "l2": "on", "tone": "active"}}
    faces = host.faces("ajazz-akp03e")
    assert (faces["K1"].l1, faces["K1"].l2, faces["K1"].tone) == ("ON AIR", "off 42s", "onair")
    assert (faces["K2"].l2, faces["K2"].tone) == ("on", "active")


def test_switching_apps_waits_for_a_held_ptt(setup):
    host, radio, _, _ = setup
    dev, fake = attached(host, press(K1))
    run_all(host, dev, fake)
    assert host.set_foreground("qo100wb") is False and host.front == "qo100nb"
    fake.push(release(K1))
    run_all(host, dev, fake)
    assert host.front == "qo100wb" and not radio.tx.on_air


def test_a_refused_key_up_shows_the_reason_on_the_key(nb):
    """§9 case 8: a view-level device's ptt is refused by the app (the
    proxy, on the platform); the pad keys nothing and says why."""
    lease = RecordingLease(refuse="needs transmit level")
    radio = make_app("qo100nb", *nb, lease=lease)
    host = InputHost([radio], clock=Clock())
    host.handle("ajazz-akp03e", ("press", "K1", 0))
    face = host.faces("ajazz-akp03e")["K1"]
    assert (face.l1, face.tone) == ("TX", "warn") and face.l2.startswith("needs") and not lease.on_air
    assert lease.calls("beat") == []


def test_an_app_with_no_lease_never_keys(nb):
    radio = make_app("qo100nb", *nb)
    radio.lease = None
    host = InputHost([radio], clock=Clock())
    host.handle("ajazz-akp03e", ("press", "K1", 0))
    assert radio.tx.calls("key") == [] and host.faces("ajazz-akp03e")["K1"].tone == "warn"


def test_the_page_key_turns_pages_and_each_page_has_its_own_keys(setup):
    host, radio, _, _ = setup
    host.set_pages("qo100nb", "ajazz-akp03e", 2)
    host.set_binding("qo100nb", "ajazz-akp03e", 1, "K2", "press", "scope")
    host.handle("ajazz-akp03e", ("press", "B3", 0))
    host.handle("ajazz-akp03e", ("press", "K2", 0))
    host.handle("ajazz-akp03e", ("release", "B3", 0))
    host.handle("ajazz-akp03e", ("press", "B3", 0))
    host.handle("ajazz-akp03e", ("release", "K2", 0))
    host.handle("ajazz-akp03e", ("press", "K2", 0))
    assert [a.action for a in radio.performed] == ["scope", "mute"]


def test_app_keys_switch_in_process_and_ask_the_kiosk_on_the_platform(nb, wb):
    asked = []
    radio, tv = make_app("qo100nb", *nb), make_app("qo100wb", *wb)
    host = InputHost([radio, tv], platform=True, on_switch=asked.append, clock=Clock())
    host.set_binding("qo100nb", "ajazz-akp03e", 0, "K6", "press", "app:next")
    host.handle("ajazz-akp03e", ("press", "K6", 0))
    assert asked == ["qo100wb"] and host.front == "qo100nb"   # the kiosk decides, then calls set_foreground
    local = InputHost([make_app("qo100nb", *nb), make_app("qo100wb", *wb)], platform=True, clock=Clock())
    local.set_binding("qo100nb", "ajazz-akp03e", 0, "K6", "press", "app:qo100wb")
    local.handle("ajazz-akp03e", ("press", "K6", 0))
    assert local.front == "qo100wb"


def test_remaps_go_to_the_profile_and_the_stop_key_stays_put_on_air(setup):
    host, radio, _, _ = setup
    stored = []
    host.on_maps = stored.append
    maps = host.set_binding("qo100nb", "ajazz-akp03e", 0, "K6", "press", "scope")
    assert stored == [maps] and maps["qo100nb"]["ajazz-akp03e"]["pages"][0]["K6"] == {"press": "scope"}
    radio.tx.on_air = True
    with pytest.raises(ValueError, match="on air"):
        host.set_binding("qo100nb", "ajazz-akp03e", 0, "K1", "press", "mute")
    with pytest.raises(ValueError, match="on air"):
        host.set_binding("qo100nb", "ajazz-akp03e", 0, "K5", "press", "ptt")
    assert len(stored) == 1             # refused changes never reach the profile


def test_an_action_that_fails_does_not_drop_the_pad(nb):
    radio = make_app("qo100nb", *nb)

    def boom(action):
        raise RuntimeError("app busy")
    radio._perform = boom
    host = InputHost([radio], clock=Clock())
    dev, fake = attached(host, press(K2), release(K2), press(K1))
    run_all(host, dev, fake)
    assert "app busy" in host.message and radio.tx.on_air


def test_keys_are_drawn_and_redrawn_only_when_they_change(setup):
    pytest.importorskip("PIL")
    host, radio, _, clock = setup
    dev, fake = attached(host)
    before = len(fake.writes)
    host.step(dev, 0)
    drawn = len(fake.writes) - before
    assert drawn > 6 * 3                # six keys, each header + data + commit
    clock.advance(1)
    host.step(dev, 0)
    assert len(fake.writes) - before == drawn       # nothing changed, nothing written
    radio.tx.on_air = True
    clock.advance(1)
    host.step(dev, 0)
    k1 = [w for w in fake.writes[before + drawn:] if w[1:9] == b"CRT\0\0BAT"]
    assert [w[13] for w in k1] == [1]   # only K1 (now ON AIR) was sent again


def test_run_finds_a_pad_serves_it_and_looks_again_after_it_goes(nb):
    radio = make_app("qo100nb", *nb)
    host = InputHost([radio], host="t")
    fakes = []

    class Found(AKP03E):
        @classmethod
        def find(cls, cfg=None):
            return ["fake:1"] if len(fakes) < 2 else []

        @classmethod
        def open(cls, path):
            fake = FakeHid([press(K2), release(K2)], path)
            fakes.append(fake)
            return cls(fake)

    stop = threading.Event()
    t = threading.Thread(target=host.run, args=(stop, [Found]), kwargs={"every": 0.05}, daemon=True)
    t.start()
    assert _wait(lambda: len(radio.performed) == 1)
    fakes[0].unplug()                   # gone: run() opens it again when find() sees it
    assert _wait(lambda: len(fakes) == 2 and len(radio.performed) == 2)
    stop.set()
    t.join(2)
    assert all(f.closed for f in fakes)


def test_without_pillow_keys_still_work_and_nothing_is_drawn(nb, monkeypatch):
    from shack_pad import faces as facemod
    monkeypatch.setattr(facemod, "have_pillow", lambda: False)
    monkeypatch.setattr(facemod, "_jpeg_cache", {})
    radio = make_app("qo100nb", *nb)
    host = InputHost([radio], clock=Clock())
    dev, fake = attached(host, press(K2))
    wake = len(fake.writes)
    run_all(host, dev, fake)
    assert len(fake.writes) == wake and [a.action for a in radio.performed] == ["mute"]


def _wait(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.005)
    return cond()


def _verbs(writes):
    """What the reports written were: {CRT verb: count}, a key image's
    header (BAT) counted against its key (K1..K6), so one K2 is K2 drawn
    once. Image data reports carry no verb and are not counted."""
    out = {}
    for w in writes:
        if w[1:6] != b"CRT\0\0":
            continue
        verb = w[6:13].rstrip(b"\0").decode(errors="replace")
        if verb.startswith("BAT"):
            verb = f"K{w[13]}"
        elif verb[:3] in ("LIG", "CLE", "DIS", "STP"):
            verb = verb[:3]
        out[verb] = out.get(verb, 0) + 1
    return out


def test_an_idle_pad_hears_only_a_keep_alive_every_ten_seconds(setup):
    """Ray's AKP03E dropped its connection every 39 s with no keep-alive,
    and each drop re-woke every key. A minute of a quiet pad, stepped as
    serve() steps it: six keep-alives, and no picture, brightness or
    clear sent again."""
    pytest.importorskip("PIL")
    host, _, _, clock = setup
    dev, fake = attached(host)
    host.step(dev, 0)                   # the first drawing
    before = len(fake.writes)
    for _ in range(240):
        clock.advance(0.25)
        host.step(dev, 0)
    assert _verbs(fake.writes[before:]) == {"CONNECT": 6}


def test_a_live_readout_redraws_only_its_own_key_at_most_twice_a_second(setup):
    """A face that changes on every look (a meter, a frequency): only that
    key is sent again, and no faster than the half-second redraw."""
    pytest.importorskip("PIL")
    host, radio, _, clock = setup
    dev, fake = attached(host)
    host.step(dev, 0)
    before = len(fake.writes)
    for n in range(40):                 # ten seconds of 250 ms steps
        radio.state["faces"] = {"mute": {"l1": "S", "l2": str(n)}}    # K2, new on every step
        clock.advance(0.25)
        host.step(dev, 0)
    sent = _verbs(fake.writes[before:])
    assert sent == {"K2": 20, "STP": 20, "CONNECT": 1}


def test_a_face_that_will_not_draw_does_not_drop_the_pad(nb, monkeypatch):
    """Raised out of step() before, serve() then blanked the pad and the
    desktop woke it again 250 ms later: every key flickering for ever."""
    from shack_pad import faces as facemod

    def broken(*a, **k):
        raise ValueError("no FreeType")
    monkeypatch.setattr(facemod, "render_jpeg", broken)
    radio = make_app("qo100nb", *nb)
    host = InputHost([radio], clock=Clock())
    dev, fake = attached(host, press(K2))
    wake = len(fake.writes)
    run_all(host, dev, fake)
    assert "no FreeType" in host.message and [a.action for a in radio.performed] == ["mute"]
    assert len(fake.writes) == wake
