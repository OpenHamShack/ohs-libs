"""The local logbook and profile: local-first behaviour, ADIF, history."""
import json
import os
import stat

import pytest

from shack_client import Station
from shack_client import adif as A

T = "2026-10-09T10:12:30Z"


@pytest.fixture
def st(tmp_path):
    s = Station.open("shack-qo100nb", "1.0.0", tmp_path / "station.db", background=False)
    yield s
    s.close()


def test_a_contact_needs_a_call_and_a_start(st):
    with pytest.raises(ValueError, match="callsign"):
        st.logbook.add({"start": T})
    with pytest.raises(ValueError, match="start time"):
        st.logbook.add({"call": "DL1ABC"})
    with pytest.raises(ValueError, match="ADIF"):
        st.logbook.add({"call": "DL1ABC", "start": T, "RST": "59"})


def test_add_is_idempotent_by_uuid_and_upper_cases_the_call(st):
    q = st.logbook.add({"call": "dl1abc", "start": T}, "01927C4E-6B1A-7CC2-8A3B-5D2C1E0F9A77")
    again = st.logbook.add({"call": "DL1ABC", "start": T}, "01927c4e-6b1a-7cc2-8a3b-5d2c1e0f9a77")
    assert q.uuid == again.uuid == "01927c4e-6b1a-7cc2-8a3b-5d2c1e0f9a77" and q.fields["call"] == "DL1ABC"
    assert st.logbook.count() == 1


def test_minted_ids_are_uuid_v7(st):
    q = st.logbook.add({"call": "DL1ABC", "start": T})
    assert q.uuid[14] == "7" and q.uuid[19] in "89ab"


def test_duplicates_are_advice_not_refusal(st):
    a = st.logbook.add({"call": "DL1ABC", "start": T, "freq_hz": 2_400_250_000, "mode": "SSB"})
    b = st.logbook.add({"call": "DL1ABC", "start": "2026-10-09T10:15:00Z", "freq_hz": 2_400_260_000, "mode": "SSB"})
    c = st.logbook.add({"call": "DL1ABC", "start": "2026-10-09T11:15:00Z", "freq_hz": 2_400_260_000, "mode": "SSB"})
    assert b.duplicate_of == [a.uuid] and c.duplicate_of == [] and st.logbook.count() == 3


def test_query_band_from_frequency_and_deleted_are_skipped(st):
    a = st.logbook.add({"call": "DL1ABC", "start": T, "freq_hz": 2_400_250_000, "mode": "SSB"})
    st.logbook.add({"call": "DL1ABC", "start": T, "freq_hz": 144_300_000, "mode": "SSB"})
    assert [q.uuid for q in st.logbook.query(call="dl1abc", band="13cm")] == [a.uuid]
    assert st.logbook.worked_before("DL1ABC", band="13cm", mode="ssb")
    st.logbook.delete(a.uuid)
    assert st.logbook.query(band="13cm") == []
    assert st.logbook.query(band="13cm", include_deleted=True)[0].deleted
    assert [h["kind"] for h in st.logbook.history(a.uuid)] == ["create", "delete"]


def test_update_and_delete_of_an_unknown_contact(st):
    with pytest.raises(LookupError):
        st.logbook.update("00000000-0000-7000-8000-000000000000", {"comment": "x"})


def test_adif_round_trip_keeps_every_field(st):
    st.logbook.add({"call": "DL1ABC", "start": T, "end": "2026-10-09T10:13:45Z", "freq_hz": 2_400_250_000,
                    "freq_rx_hz": 10_489_750_000, "mode": "MFSK", "submode": "FT4", "rst_sent": "-05",
                    "gridsquare": "JO62", "app_shack_grid_source": "decode", "comment": "a <tag> inside"})
    text = st.logbook.export_adif()
    assert "<ADIF_VER:5>3.1.4" in text and "<FREQ:11>2400.250000" in text
    other_path = st.store.path.with_name("other.db")
    other = Station.open("shack-radio", "1.0.0", other_path, background=False)
    try:
        report = other.logbook.import_adif(text)
        assert report == {"added": 1, "duplicates": [], "errors": []}
        mine, theirs = st.logbook.query()[0], other.logbook.query()[0]
        assert theirs.uuid == mine.uuid and theirs.fields == mine.fields
        assert other.logbook.import_adif(text)["duplicates"] == [{"line": 3, "of": mine.uuid}]
    finally:
        other.close()


def test_adif_import_of_a_wsjtx_style_file(st):
    text = ("WSJT-X ADIF Export<eoh>\n<call:6>DL1ABC <gridsquare:4>JO62 <mode:4>MFSK <submode:3>FT4 "
            "<qso_date:8>20261009 <time_on:6>101230 <time_off:6>101345 <freq:9>14.080000 <rst_sent:3>-10 <eor>\n"
            "<call:5>F4XYZ <mode:3>FT8 <qso_date:8>20261009 <time_on:4>1020 <freq:9>14.074000 <eor>\n"
            "<call:5>F4XYZ <mode:3>FT8 <qso_date:8>20261009 <time_on:4>1021 <freq:9>14.074500 <eor>\n"
            "<mode:3>FT8 <qso_date:8>20261009 <eor>\n")
    r = st.logbook.import_adif(text)
    assert r["added"] == 2 and len(r["duplicates"]) == 1 and len(r["errors"]) == 1
    q = st.logbook.query(call="DL1ABC")[0]
    assert q.fields["end"] == "2026-10-09T10:13:45Z" and q.fields["freq_hz"] == 14_080_000
    assert (q.fields["mode"], q.fields["submode"]) == ("MFSK", "FT4")


def test_adif_parser_honours_lengths():
    recs = A.parse("<eoh><comment:7>a<b>\nc! <call:3>G4A<eor>")
    assert recs == [(1, {"COMMENT": "a<b>\nc!", "CALL": "G4A"})]


def test_profile_normalises_and_merges_paths(st):
    p = st.profile.update({"callsign": "gm4bri", "locator": "io75rl", "conditions": {"nearLondon": False}})
    assert (p["callsign"], p["locator"], p["conditions"]) == ("GM4BRI", "IO75RL", {"nearLondon": False})
    p = st.profile.update({"conditions": {"scotland40km": True}})
    assert p["conditions"] == {"nearLondon": False, "scotland40km": True}
    with pytest.raises(ValueError, match="Maidenhead"):
        st.profile.update({"locator": "XX99"})


def test_on_change_reports_local_writes(st):
    seen = []
    st.on_change(lambda coll, ids: seen.append((coll, ids)))
    q = st.logbook.add({"call": "DL1ABC", "start": T})
    assert ("logbook", [q.uuid]) in seen


def test_on_change_notices_another_process(st, tmp_path):
    import threading
    hit = threading.Event()
    st.on_change(lambda coll, ids: hit.set() if coll is None else None)
    other = Station.open("shack-radio", "1.0.0", st.store.path, background=False)
    try:
        other.logbook.add({"call": "F4XYZ", "start": T})
        assert hit.wait(3)
    finally:
        other.close()


def test_one_store_shared_by_two_apps(st):
    radio = Station.open("shack-radio", "1.0.0", st.store.path, background=False)
    try:
        q = radio.logbook.add({"call": "F4XYZ", "start": T})
        assert st.logbook.get(q.uuid).origin["app"] == "shack-radio"
        assert st.store.store_id == radio.store.store_id
    finally:
        radio.close()


def test_default_store_location_follows_xdg(tmp_path, monkeypatch):
    from shack_client.store import default_path
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_path() == tmp_path / "openhamshack" / "station.db"


def test_pairing_file_is_private(st):
    st._write_pairing({"controllerId": "c", "baseUrl": "http://x/", "token": "shackdev_secret"})
    assert stat.S_IMODE(os.stat(st.pairing_path).st_mode) == 0o600
    assert json.loads(st.pairing_path.read_text())["token"] == "shackdev_secret"
