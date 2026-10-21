"""i18n-allow.txt and the technical-terms list (I18N.md §3.3)."""

from conftest import lines_of

QT = 'class W:\n    def f(self):\n        self.l.setText({lit})\n'


def _qt(literal: str) -> str:
    import json

    return QT.format(lit=json.dumps(literal))


def test_contract_example_entries_cover_their_literals(repo):
    repo.copy("allow/i18n-allow.txt", "i18n-allow.txt")
    repo.write("ohsnb/ui/smeter.py", _qt("S9+40dB"))
    repo.write("ohsradio/ui/bandbar.py", _qt("IBP"))
    repo.write("ohsnb/ui/window.py", _qt("GM4BRI") + _qt("Pipe | inside").replace("class W", "class X"))
    repo.write("android/kind-x/src/main/java/a/b/DebugPanel.kt", 'fun f() { Text("Fakebox") }\n')
    report = repo.run()
    assert report.findings == [], report.lines()
    assert report.exit_code == 0


def test_glob_star_does_not_cross_directories(repo):
    repo.write("i18n-allow.txt", 'ohsnb/ui/*.py | "Transmit blocked" | test\n')
    repo.write("ohsnb/ui/deep/w.py", _qt("Transmit blocked"))
    report = repo.run()
    # The literal is still a finding, and the entry, having matched nothing,
    # is stale.
    assert lines_of(report, "ohsnb/ui/deep/w.py") == {3}
    assert any(f.path == "i18n-allow.txt" and "matched nothing" in f.message for f in report.findings)


def test_double_star_crosses_directories(repo):
    repo.write("i18n-allow.txt", 'ohsnb/**/w.py | "Transmit blocked" | test\n')
    repo.write("ohsnb/w.py", _qt("Transmit blocked"))
    repo.write("ohsnb/ui/deep/w.py", _qt("Transmit blocked"))
    assert repo.run().findings == []


def test_star_alone_matches_every_file(repo):
    repo.write("i18n-allow.txt", '* | "Transmit blocked" | test\n')
    repo.write("a/b/c/w.py", _qt("Transmit blocked"))
    repo.write("w.py", _qt("Transmit blocked"))
    assert repo.run().findings == []


def test_literal_must_match_exactly(repo):
    repo.write("i18n-allow.txt", '* | "transmit blocked" | case differs\n')
    repo.write("w.py", _qt("Transmit blocked"))
    report = repo.run()
    assert {f.path for f in report.findings} == {"w.py", "i18n-allow.txt"}


def test_missing_reason_is_a_finding_but_the_entry_still_applies(repo):
    repo.write("i18n-allow.txt", '\n* | "Transmit blocked"\n* | "Tune" |   \n')
    repo.write("w.py", _qt("Transmit blocked") + _qt("Tune").replace("class W", "class X"))
    lines = repo.run().lines()
    assert lines == [
        'i18n-allow.txt:2:1: the allow-list entry for "Transmit blocked" has no reason; add one after a second "|" saying why it is not user text',
        'i18n-allow.txt:3:1: the allow-list entry for "Tune" has no reason; add one after a second "|" saying why it is not user text',
    ]


def test_stale_entry_is_a_finding_on_a_whole_repo_run(repo):
    repo.write("i18n-allow.txt", 'gone/file.py | "Old label" | the file was deleted\n')
    repo.write("w.py", "x = 1\n")
    report = repo.run()
    assert report.lines() == [
        'i18n-allow.txt:1:1: the allow-list entry for "Old label" in gone/file.py matched nothing in this run; remove it'
    ]
    assert report.exit_code == 1


def test_partial_run_only_judges_entries_for_files_it_saw(repo):
    repo.write("i18n-allow.txt", 'other/w.py | "Transmit blocked" | test\napp/w.py | "Gone" | test\n')
    repo.write("other/w.py", _qt("Transmit blocked"))
    repo.write("app/w.py", "x = 1\n")
    report = repo.run("app")
    # The other/ entry is not judged (other/ was not scanned); the app/ one is.
    assert [f.line for f in report.findings] == [2]


def test_malformed_lines_are_findings(repo):
    repo.write("i18n-allow.txt", "just some words\n* | 'single quotes' | no\n* | \"ok\" trailing | x\n")
    msgs = [f.message for f in repo.run().findings]
    assert msgs[0].startswith("this line is not an allow-list entry")
    assert msgs[1].startswith("the literal in this allow-list entry must be a JSON string")
    assert msgs[2].startswith("this line has text after the literal")


def test_config_can_name_another_allow_list(repo):
    repo.write(".i18n-check.toml", 'allow_list = "tools/i18n/allow.txt"\n')
    repo.write("tools/i18n/allow.txt", '* | "Transmit blocked" | test\n')
    repo.write("w.py", _qt("Transmit blocked"))
    assert repo.run().findings == []


def test_technical_terms_need_no_entry_and_are_never_stale(repo):
    for i, term in enumerate(["FT8", "USB", "DVB-S2", "QO-100", "RTTY", "OLIVIA"]):
        repo.write(f"m{i}.py", _qt(term))
    repo.write("k/A.kt", 'fun f() { Text("JS8") }\n')
    assert repo.run().findings == []


def test_terms_match_exactly(repo):
    repo.write("w.py", _qt("FT8 decoder"))
    assert lines_of(repo.run(), "w.py") == {3}


def test_allow_list_does_not_hide_catalog_or_parse_findings(repo):
    repo.write("i18n-allow.txt", '* | "x" | test\n')
    repo.write("broken.py", "def f(:\n")
    paths = {f.path for f in repo.run().findings}
    assert "broken.py" in paths


def test_literals_that_are_never_findings_make_stale_entries(repo):
    # I18N.md §3.3's own example lists "S9+" and "fakebox". Neither can be a
    # finding (§3.1: "S9+" has no run of two letters; "fakebox" matches the
    # identifier pattern), so as entries both match nothing and are stale.
    repo.write("i18n-allow.txt", 'ohsnb/ui/smeter.py | "S9+" | scale\nandroid/**/DebugPanel.kt | "fakebox" | debug\n')
    repo.write("ohsnb/ui/smeter.py", _qt("S9+"))
    repo.write("android/k/DebugPanel.kt", 'fun f() { Text("fakebox") }\n')
    report = repo.run()
    assert [(f.path, f.line) for f in report.findings] == [("i18n-allow.txt", 1), ("i18n-allow.txt", 2)]
