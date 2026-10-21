"""The client against the reference controller over real HTTP: pairing
(password and TV code), sync, a dropped answer, well-known resolution, and
the background thread."""
import threading
import time

import pytest

from shack_client import PairingError, Station, discovery
from shack_client import sync as S
from shack_client.pairing import tv_poll, tv_start
from shack_client.testing import ReferenceController
from shack_client.transport import HttpTransport, Offline

T = "2026-10-09T10:12:30Z"


@pytest.fixture
def ctl(tmp_path):
    c = ReferenceController(tmp_path / "services.db", users={
        "brian": {"password": "pw", "transmit": True, "totp": "123456"}})
    url = c.serve()
    yield c, url
    c.close()


def station(tmp_path, name="a", background=False):
    return Station.open("shack-qo100nb", "1.0.0", tmp_path / name / "station.db", background=background)


def test_password_pairing_asks_for_totp_then_syncs(ctl, tmp_path):
    c, url = ctl
    st = station(tmp_path)
    try:
        st.logbook.add({"call": "DL1ABC", "start": T})
        with pytest.raises(PairingError) as e:
            st.pair(url, "brian", "pw", device_name="Shack laptop")
        assert e.value.code == "totp_required"
        with pytest.raises(PairingError) as e:
            st.pair(url, "brian", "wrong", totp="123456")
        assert e.value.code == "bad_credentials"
        p = st.pair(url, "brian", "pw", totp="123456", device_name="Shack laptop")
        assert p.controller_id == c.controller_id and p.name == "GM4BRI"
        assert len(c.records()) == 1 and st.sync.status()["state"] == "idle"
        assert st.pairing_info()["token"].startswith("shackdev_")
    finally:
        st.close()


def test_pairing_again_revokes_the_previous_token(ctl, tmp_path):
    c, url = ctl
    st = station(tmp_path)
    try:
        st.pair(url, "brian", "pw", totp="123456")
        first = st.pairing_info()["token"]
        st.pair(url, "brian", "pw", totp="123456")
        status, ans = HttpTransport(url, first).request("GET", "api/shack/v1/")
        assert (status, ans["code"]) == (401, "token_revoked")
        assert st.sync.run_once()["state"] == "idle"
    finally:
        st.close()


def test_a_dropped_answer_over_http_is_retried_harmlessly(ctl, tmp_path):
    c, url = ctl
    st = station(tmp_path)
    try:
        st.pair(url, "brian", "pw", totp="123456")
        q = st.logbook.add({"call": "DL1ABC", "start": T})
        c.drop_next_push_answer = True
        assert st.sync.run_once()["state"] == "offline"
        assert st.sync.run_once()["state"] == "idle"
        assert len(c.records()) == 1 and len(c.sync.history("logbook", q.uuid)["edits"]) == 1
    finally:
        st.close()


def test_tv_pairing_by_code(ctl, tmp_path):
    c, url = ctl
    phone = station(tmp_path, "phone")
    try:
        phone.pair(url, "brian", "pw", totp="123456", device_kind="phone")
        tv = HttpTransport(url)
        p = tv_start(tv, device_name="Living room TV", client_id="tv-1", app="remote")
        assert len(p.code) == 6 and tv_poll(tv, p) == ("pending", None)
        status, _ = phone.transport().request("POST", "api/shack/v1/pair/approve",
                                              {"code": p.code, "maxLevel": "control"})
        assert status == 200
        state, tok = tv_poll(tv, p)
        assert state == "approved" and tok.max_level == "control" and tok.token.startswith("shackdev_")
        assert tv_poll(tv, p)[0] == "expired"            # spent
    finally:
        phone.close()


def test_well_known_resolves_against_the_base(ctl):
    c, url = ctl
    wk = discovery.well_known(url + "")
    assert wk["controllerId"] == c.controller_id
    assert wk["auth"]["token"] == url + "api/shack/token"
    assert wk["services"]["base"] == url + "api/shack/v1/"


def test_well_known_refuses_something_else(tmp_path):
    with pytest.raises(Offline):
        discovery.well_known("http://127.0.0.1:9/")


def test_background_thread_syncs_after_a_local_write(ctl, tmp_path, monkeypatch):
    monkeypatch.setattr(S, "DEBOUNCE_S", 0.05)
    c, url = ctl
    st = station(tmp_path, background=True)
    try:
        st.pair(url, "brian", "pw", totp="123456")
        st.logbook.add({"call": "DL1ABC", "start": T})
        deadline = time.time() + 5
        while not c.records() and time.time() < deadline:
            time.sleep(0.05)
        assert len(c.records()) == 1
        # Unpair stops it and makes no request.
        before = len(c.requests)
        st.unpair()
        st.logbook.add({"call": "F4XYZ", "start": T})
        time.sleep(0.3)
        assert len(c.requests) == before and st.logbook.count() == 2
    finally:
        st.close()


def test_controller_side_change_reaches_the_app(ctl, tmp_path):
    """A controller's own edit (its web UI) is stamped with its HLC and
    pulled like any other."""
    c, url = ctl
    st = station(tmp_path)
    seen = []
    st.on_change(lambda coll, ids: seen.append((coll, tuple(ids))))
    try:
        st.pair(url, "brian", "pw", totp="123456")
        q = st.logbook.add({"call": "DL1ABC", "start": T})
        st.sync.run_once()
        c.sync.edit("logbook", q.uuid, "update", {"qsl_rcvd": "Y"}, {"app": "controller-ui"})
        st.sync.run_once()
        assert st.logbook.get(q.uuid).fields["qsl_rcvd"] == "Y"
        assert ("logbook", (q.uuid,)) in seen
        assert c.sync.history("logbook", q.uuid)["edits"][-1]["hlc"].endswith(c.sync.node)
    finally:
        st.close()
