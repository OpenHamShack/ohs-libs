"""MASTER.SCP as the skimmer's callsign list (shack_cw.skim.scp): read,
re-read when it changes, and fetched only the way the site asks."""
import email.message
import io
import os
import urllib.error

import pytest

from shack_cw.skim import scp

CALLS = [f"G{n}ABC" for n in range(10)] + [f"DL{n}X{chr(65 + n % 26)}" for n in range(scp.MIN_CALLS)]


def scp_text(calls=CALLS):
    return "!!Order,1,1\n#\n# Super Check Partial\n#\n" + "\n".join(calls) + "\n"


def test_the_list_answers_for_a_call_and_its_home_call(tmp_path):
    p = tmp_path / "MASTER.SCP"
    p.write_text(scp_text())
    h = scp.Scp([p])
    assert h("G4ABC") is True and h("G4ABC/P") is True and h("EA8/G4ABC") is True
    assert h("GM4BRI") is False
    assert len(h) == len(set(CALLS))
    # Comments and the header are not calls.
    assert h("!!ORDER,1,1") is False and h("#") is False


def test_no_file_means_no_list_not_an_empty_one(tmp_path):
    h = scp.Scp([tmp_path / "MASTER.SCP"])
    assert h("G4ABC") is None
    # A stub (a failed download saved by hand, an HTML error page) is no list.
    (tmp_path / "MASTER.SCP").write_text(scp_text(["G4ABC"]))
    assert scp.Scp([tmp_path / "MASTER.SCP"])("G4ABC") is None


def test_a_file_that_arrives_or_changes_is_read_again(tmp_path):
    p = tmp_path / "MASTER.SCP"
    h = scp.Scp([p], recheck_s=0.0)
    assert h("GM4BRI") is None
    p.write_text(scp_text())
    assert h("GM4BRI") is False
    p.write_text(scp_text(CALLS + ["GM4BRI"]))
    os.utime(p, ns=(1, 1))      # a new stamp, whatever the clock's resolution
    assert h("GM4BRI") is True


def test_the_first_list_that_exists_wins(tmp_path):
    (tmp_path / "b.scp").write_text(scp_text())
    h = scp.history([str(tmp_path / "a.scp"), str(tmp_path / "b.scp")])
    assert h.source == tmp_path / "b.scp" and h("G1ABC") is True


class FakeResponse(io.BytesIO):
    def __init__(self, body, etag=None):
        super().__init__(body)
        self.headers = email.message.Message()
        if etag:
            self.headers["ETag"] = etag

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_asks_at_most_once_a_day_and_conditionally(tmp_path):
    p = tmp_path / "MASTER.SCP"
    asked = []

    def opener(req, timeout):
        asked.append(dict(req.header_items()))
        return FakeResponse(scp_text().encode(), etag='"v1"')
    assert scp.fetch(p, "OpenHamShack-Radio/test", opener=opener) == "fetched"
    assert scp.Scp([p])("G4ABC") is True
    assert asked[0]["User-agent"] == "OpenHamShack-Radio/test" and "If-none-match" not in asked[0]
    assert (tmp_path / "MASTER.SCP.etag").read_text() == '"v1"'
    # A day has not passed: nothing is asked.
    assert scp.fetch(p, "OpenHamShack-Radio/test", opener=opener) == "fresh" and len(asked) == 1
    # A day later: a conditional GET; 304 keeps the file and restarts the day.
    old = p.stat().st_mtime - scp.FETCH_EVERY_S - 10
    os.utime(p, (old, old))

    def not_modified(req, timeout):
        asked.append(dict(req.header_items()))
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
    assert scp.fetch(p, "OpenHamShack-Radio/test", opener=not_modified) == "unchanged"
    assert asked[-1]["If-none-match"] == '"v1"'
    assert scp.fetch(p, "OpenHamShack-Radio/test", opener=not_modified) == "fresh"


def test_a_download_that_is_not_a_list_never_replaces_the_file(tmp_path):
    p = tmp_path / "MASTER.SCP"
    p.write_text(scp_text())
    old = p.stat().st_mtime - scp.FETCH_EVERY_S - 10
    os.utime(p, (old, old))

    def broken(req, timeout):
        return FakeResponse(b"<html>maintenance</html>")
    with pytest.raises(ValueError):
        scp.fetch(p, "OpenHamShack-Radio/test", opener=broken)
    assert scp.Scp([p])("G4ABC") is True


def test_the_rbn_week_ships_with_the_language_model_and_is_always_there(tmp_path):
    base = scp.bundled_calls()
    assert len(base) > 10_000
    call = sorted(base)[len(base) // 2]
    # No MASTER.SCP: the bundled list still answers, so the rule does not
    # fall back to three decodes for every call.
    h = scp.history([str(tmp_path / "MASTER.SCP")])
    assert h(call) is True and h(call + "/P") is True and h("QQ9QQQ") is False
    # MASTER.SCP adds its calls to it.
    (tmp_path / "MASTER.SCP").write_text(scp_text())
    h = scp.history([str(tmp_path / "MASTER.SCP")])
    assert h(call) is True and h("G4ABC") is True
    assert len(h) == len(base | set(CALLS))
