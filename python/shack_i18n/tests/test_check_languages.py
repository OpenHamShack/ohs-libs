"""Kotlin, Swift, JavaScript, HTML and Android layouts, through the check."""

import pytest

from conftest import lines_of

CASES = [
    ("kotlin/IcomScreen_bad.kt", "android/kind-icom/src/main/java/IcomScreen.kt"),
    ("swift/ContentView_bad.swift", "apple/Sources/StationView.swift"),
    ("javascript/console_bad.js", "web/static/console.js"),
    ("html/console_bad.html", "web/static/index.html"),
    ("android/layout_bad.xml", "android/app/src/main/res/layout/main.xml"),
]
GOOD = [
    ("kotlin/IcomScreen_good.kt", "android/kind-icom/src/main/java/IcomScreen.kt", "android"),
    ("swift/ContentView_good.swift", "apple/Sources/StationView.swift", "apple"),
    ("javascript/console_good.js", "web/static/console.js", "web"),
    ("html/console_good.html", "web/static/index.html", "web"),
]


@pytest.mark.parametrize("fixture,rel", CASES, ids=[c[0] for c in CASES])
def test_findings_are_exactly_the_marked_lines(repo, fixture, rel):
    repo.write(".i18n-check.toml", '[javascript]\nsinks = ["toast("]\n')
    repo.copy(fixture, rel)
    if fixture.startswith("android/"):
        repo.catalogs("android")  # the layout also uses @string/ready
    report = repo.run()
    expected = repo.expected_lines(rel)
    if fixture.endswith(".swift"):
        expected |= {23}  # the multi-line literal; see the fixture's comment
    assert lines_of(report, rel) == expected
    assert report.exit_code == 1


@pytest.mark.parametrize("fixture,rel,system", GOOD, ids=[c[0] for c in GOOD])
def test_catalog_use_and_data_are_clean(repo, fixture, rel, system):
    repo.copy(fixture, rel)
    repo.catalogs(system)
    report = repo.run()
    assert report.findings == [], report.lines()
    assert report.exit_code == 0


def test_kotlin_line_matches_the_contract_example(repo):
    rel = "android/kind-icom/src/main/java/IcomScreen.kt"
    repo.copy("kotlin/IcomScreen_bad.kt", rel)
    assert f'{rel}:8:10: user-visible literal "Tune" passed to Text(); use stringResource()' in repo.run().lines()


def test_kotlin_templates_show_placeholders_and_both_branches_count(repo):
    rel = "k/S.kt"
    repo.copy("kotlin/IcomScreen_bad.kt", rel)
    msgs = {f.message for f in repo.run().findings}
    assert 'user-visible literal "PTT on ${vm.freq}" passed to Text(); use stringResource()' in msgs
    assert 'user-visible literal "$n stations heard" passed to Text(); use stringResource()' in msgs
    assert 'user-visible literal "On air" assigned to title; use stringResource()' in msgs
    assert 'user-visible literal "Idle" assigned to title; use stringResource()' in msgs


def test_swift_verbatim_and_uikit_messages(repo):
    rel = "a/V.swift"
    repo.copy("swift/ContentView_bad.swift", rel)
    msgs = {f.message for f in repo.run().findings}
    assert 'user-visible literal "Transmit blocked" passed to Text(verbatim:); use a String Catalog key: Text("…")' in msgs
    assert 'user-visible literal "\\\\(callsign) was heard" passed to Text(verbatim:); use a String Catalog key: Text("…")' in msgs
    assert 'user-visible literal "Ready" assigned to .text; use String(localized:)' in msgs
    assert 'user-visible literal "Raw \\"title\\"" assigned to .title; use String(localized:)' in msgs


def test_javascript_innerhtml_tests_only_the_text_between_tags(repo):
    rel = "w/c.js"
    repo.copy("javascript/console_bad.js", rel)
    msgs = {f.line: f.message for f in repo.run().findings}
    assert msgs[7] == 'user-visible literal "Ready" assigned to .innerHTML; use t()'
    assert msgs[8] == 'user-visible literal "is on air" assigned to .innerHTML; use t()'


def test_javascript_regex_with_quotes_does_not_derail_the_tokenizer(repo):
    repo.write("w/r.js", 'const a = /["\']/g; const b = x / 2 / y;\nel.textContent = "After the regex";\n')
    assert lines_of(repo.run(), "w/r.js") == {2}


def test_html_messages_name_the_fix(repo):
    rel = "web/index.html"
    repo.copy("html/console_bad.html", rel)
    lines = repo.run().lines()
    assert f'{rel}:10:3: user-visible text "Push to talk" in the title attribute of <button>; add data-i18n-attr="title:key"' in lines
    assert f'{rel}:10:32: user-visible text "PTT" in <button>; add data-i18n="key"' in lines
    # data-i18n-attr covering one attribute does not cover another.
    assert f'{rel}:13:3: user-visible text "Callsign" in the placeholder attribute of <input>; add data-i18n-attr="placeholder:key"' in lines


def test_android_layout_message(repo):
    rel = "android/app/src/main/res/layout/main.xml"
    repo.copy("android/layout_bad.xml", rel)
    assert f'{rel}:5:5: user-visible literal "Callsign" in android:hint on <EditText>; use @string/…' in repo.run().lines()


def test_android_xml_outside_layouts_is_not_scanned_as_a_layout(repo):
    repo.copy("android/layout_bad.xml", "android/app/src/main/res/xml/prefs.xml")
    repo.copy("android/layout_bad.xml", "docs/example.xml")
    assert repo.run().findings == []


def test_configured_sinks_and_attributes(repo):
    repo.write(
        ".i18n-check.toml",
        '[kotlin]\nsinks = ["Banner(", "subtitle ="]\n[swift]\nsinks = [".subtitle ="]\n'
        '[html]\nattributes = ["data-tip"]\n[android_xml]\nattributes = ["android:title"]\n',
    )
    repo.write("k/A.kt", 'fun f() {\n    Banner("Hello there")\n    bar.subtitle = "Second line"\n}\n')
    repo.write("s/A.swift", 'item.subtitle = "Second line"\n')
    repo.write("w/a.html", '<span data-tip="Helpful tip"></span>\n')
    repo.write("a/res/menu/m.xml", '<menu xmlns:android="x"><item android:title="Settings"/></menu>\n')
    report = repo.run()
    assert {(f.path, f.line) for f in report.findings} == {
        ("k/A.kt", 2), ("k/A.kt", 3), ("s/A.swift", 1), ("w/a.html", 1), ("a/res/menu/m.xml", 1)
    }


def test_malformed_sink_in_config_is_exit_2(repo, capsys):
    from shack_i18n import check

    repo.write(".i18n-check.toml", '[kotlin]\nsinks = ["Banner"]\n')
    import os

    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        assert check.main([]) == 2
    finally:
        os.chdir(cwd)
    assert 'must end in "(", "=" or ":"' in capsys.readouterr().err


def test_unterminated_string_is_a_finding_not_a_pass(repo):
    repo.write("k/B.kt", 'fun f() {\n    Text("never closed)\n}\n')
    (f,) = repo.run().findings
    assert (f.line, f.col) == (2, 10) and "could not read this file" in f.message
