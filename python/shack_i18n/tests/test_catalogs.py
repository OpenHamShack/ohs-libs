"""Catalog checks (I18N.md §3.2): missing keys, empty English, plurals,
placeholders, unused-key warnings, across the five catalog formats."""

import json

from conftest import lines_of

EN_TS = "app/i18n/app_en.ts"
DE_TS = "app/i18n/app_de.ts"
EN_JSON = "web/static/i18n/en.json"
DE_JSON = "web/static/i18n/de.json"
EN_XML = "android/app/src/main/res/values/strings.xml"
DE_XML = "android/app/src/main/res/values-de/strings.xml"
XCSTRINGS = "apple/Resources/Localizable.xcstrings"


def _msgs(report, path=None):
    return [f.message for f in report.findings if path is None or f.path == path]


# -- Qt ---------------------------------------------------------------------


def test_qt_good_catalogs_with_their_code_are_clean(repo):
    repo.copy("python/window_good.py", "app/window.py")
    repo.catalogs("qt")
    report = repo.run()
    assert report.findings == [] and report.warnings == []


def test_qt_source_text_missing_from_english_is_a_finding_at_the_use(repo):
    repo.catalogs("qt")
    repo.write("app/w.py", 'class W:\n    def f(self):\n        self.l.setText(self.tr("Not extracted yet"))\n')
    report = repo.run()
    assert f'app/w.py:3:32: "Not extracted yet" is not in the English catalog ({EN_TS}); run pylupdate6 to extract it' in report.lines()


def test_qt_english_plural_needs_both_forms(repo):
    repo.write(
        EN_TS,
        '<TS language="en"><context><name>W</name>\n<message numerus="yes"><source>%n stations</source>'
        "<translation><numerusform>%n station</numerusform><numerusform></numerusform></translation></message>"
        "</context></TS>",
    )
    repo.write("app/w.py", 'class W:\n    def f(self, n):\n        self.l.setText(self.tr("%n stations", "", n))\n')
    assert _msgs(repo.run()) == ['the English plural "%n stations" needs both numerusforms filled in']


def test_qt_plural_use_of_a_plain_entry(repo):
    repo.catalogs("qt")
    repo.write("app/w.py", 'class W:\n    def f(self, n):\n        self.l.setText(self.tr("Ready", "", n))\n')
    (msg,) = _msgs(repo.run(), "app/w.py")
    assert msg.startswith('"Ready" is used as a plural but its English entry')


def test_qt_translation_placeholders_must_match_the_source(repo):
    repo.write(EN_TS, '<TS language="en"><context><name>W</name>\n<message><source>Heard %1 at %2 dB</source><translation/></message></context></TS>')
    repo.write(DE_TS, '<TS language="de"><context><name>W</name>\n<message><source>Heard %1 at %2 dB</source><translation>%1 gehört</translation></message></context></TS>')
    report = repo.run(EN_TS, DE_TS)
    assert _msgs(report) == [
        'the de translation of "Heard %1 at %2 dB" has placeholders %1 where the English has %1, %2; a format call would fail or lose a value'
    ]
    assert report.findings[0].path == DE_TS and report.findings[0].line == 2


def test_typescript_files_are_not_mistaken_for_qt_catalogs(repo):
    repo.write("web/src/app.ts", "export const x: string = 'Hello';\n")
    assert repo.run().findings == []


# -- gettext ----------------------------------------------------------------


def test_gettext_good_catalogs_and_multiline_msgid(repo):
    repo.copy("python/server_good.py", "srv/app.py")
    repo.catalogs("gettext")
    report = repo.run()
    assert report.findings == []
    # The template's plural entry is used by no code: a warning, exit 0.
    assert [w.message for w in report.warnings] == ['"{n} station heard" in the English catalog is not used by any checked code']
    assert report.exit_code == 0


def test_gettext_plural_use_needs_msgid_plural(repo):
    repo.catalogs("gettext")
    repo.write("srv/x.py", 'def f(n):\n    return ngettext("Ready", "Ready", n)\n')
    repo.write("srv/locale/extra.pot", 'msgid "Ready"\nmsgstr ""\n')
    (msg,) = _msgs(repo.run(), "srv/x.py")
    assert msg.startswith('"Ready" is used as a plural but its English entry')


def test_gettext_translation_placeholders_and_fuzzy(repo):
    repo.catalogs("gettext")
    repo.write(
        "srv/locale/fr/LC_MESSAGES/shack-srv.po",
        'msgid ""\nmsgstr ""\n\nmsgid "{n} station heard"\nmsgid_plural "{n} stations heard"\n'
        'msgstr[0] "{n} station entendue"\nmsgstr[1] "{count} stations entendues"\n',
    )
    report = repo.run("srv")
    # de.po's mismatch is fuzzy, so ignored; fr's second form adds {count}.
    assert _msgs(report) == [
        'the fr translation of "{n} station heard" (plural form "1") has placeholders {count} where the English has {n}; a format call would fail or lose a value'
    ]


def test_unreadable_po_is_a_finding(repo):
    repo.write("srv/locale/x.pot", "msgid unquoted\n")
    (f,) = repo.run().findings
    assert f.path == "srv/locale/x.pot" and f.message.startswith("this catalog could not be read because line 1")


# -- web --------------------------------------------------------------------


def test_web_good_catalogs_with_html_and_js_are_clean(repo):
    repo.copy("html/console_good.html", "web/static/index.html")
    repo.copy("javascript/console_good.js", "web/static/console.js")
    repo.catalogs("web")
    report = repo.run()
    assert report.findings == []
    assert [w.message for w in report.warnings] == ['"stations" in the English catalog is not used by any checked code']


def test_web_missing_keys_from_js_and_html(repo):
    repo.catalogs("web")
    repo.write("web/static/a.js", 'el.textContent = t("tx.missing");\n')
    repo.write("web/static/a.html", '<p data-i18n="nope">x</p><b data-i18n-attr="title:also.nope" title="y"></b>\n')
    report = repo.run()
    assert f'web/static/a.js:1:20: "tx.missing" is not in the English catalog ({EN_JSON}); add it there' in report.lines()
    assert set(_msgs(report, "web/static/a.html")) == {
        f'"nope" is not in the English catalog ({EN_JSON}); add it there',
        f'"also.nope" is not in the English catalog ({EN_JSON}); add it there',
    }


def test_web_english_empty_and_incomplete_plural(repo):
    repo.write(EN_JSON, '{\n "a": "",\n "b": {"other": "{n} things"},\n "c": 3\n}\n')
    report = repo.run()
    assert {(f.line, f.message) for f in report.findings} == {
        (2, 'the English text for "a" is empty'),
        (3, 'the English plural "b" needs "one" and "other" filled in'),
        (4, 'the entry "c" must be a string or a plural object'),
    }


def test_web_translation_placeholders(repo):
    repo.write(EN_JSON, json.dumps({"heard": "Heard {name} at {freq:.3f}", "n": {"one": "{n} station", "other": "{n} stations"}}))
    repo.write(DE_JSON, json.dumps({"heard": "{nom} gehört bei {freq}", "n": {"one": "eine Station", "other": "{n} Stationen {x}"}}))
    msgs = _msgs(repo.run(EN_JSON, DE_JSON))
    assert msgs == [
        'the de translation of "heard" has placeholders {freq}, {nom} where the English has {freq}, {name}; a format call would fail or lose a value',
        'the de translation of "n" (plural form "other") has placeholders {n}, {x} where the English has {n}; a format call would fail or lose a value',
    ]


def test_literal_percent_and_doubled_braces_are_not_placeholders(repo):
    repo.write(EN_JSON, json.dumps({"p": "100% {{sure}} {v}"}))
    repo.write(DE_JSON, json.dumps({"p": "{v} zu 100 %"}))
    assert repo.run(EN_JSON, DE_JSON).findings == []


def test_bad_json_catalog_is_a_finding(repo):
    repo.write(EN_JSON, "{not json")
    (f,) = repo.run().findings
    assert f.message.startswith("this catalog could not be read because it is not valid JSON")


# -- Android ----------------------------------------------------------------


def test_android_missing_key_plural_and_placeholders(repo):
    repo.catalogs("android")
    repo.write(DE_XML, '<resources>\n<string name="heard">%s gehört</string>\n</resources>\n')
    repo.write("android/app/src/main/res/values-night/strings.xml", "<resources><string name='x'></string></resources>")
    repo.write(
        "android/app/src/main/java/A.kt",
        "fun f() {\n    Text(stringResource(R.string.missing))\n    Text(pluralStringResource(R.plurals.stations_heard, 1, 1))\n"
        "    Text(stringResource(R.string.ready)); Text(stringResource(R.string.tune)); Text(stringResource(R.string.transmit))\n"
        "    Text(stringResource(R.string.heard))\n}\n",
    )
    report = repo.run()
    assert report.lines() == [
        f"android/app/src/main/java/A.kt:2:34: @string/missing is not in the English catalog ({EN_XML}); add it there",
        f'{DE_XML}:2:1: the de translation of "string/heard" has placeholders %s where the English has %1$s, %2$d; a format call would fail or lose a value',
    ]


def test_android_english_plural_needs_one_and_other(repo):
    repo.write(EN_XML, '<resources>\n<plurals name="n"><item quantity="other">%d</item></plurals>\n</resources>\n')
    assert _msgs(repo.run()) == ['the English plural "plurals/n" needs "one" and "other" filled in']


def test_android_layout_reference_counts_as_a_use(repo):
    repo.write(EN_XML, '<resources>\n<string name="ready">Ready</string>\n</resources>\n')
    repo.write("android/app/src/main/res/layout/a.xml", '<T xmlns:android="x" android:text="@string/ready"/>\n')
    report = repo.run()
    assert report.findings == [] and report.warnings == []


# -- Apple ------------------------------------------------------------------


def test_apple_good_catalog_with_its_code_is_clean(repo):
    repo.copy("swift/ContentView_good.swift", "apple/Sources/V.swift")
    repo.catalogs("apple")
    report = repo.run()
    assert report.findings == []
    assert {w.message for w in report.warnings} == {
        '"Tune" in the English catalog is not used by any checked code',
        '"%lld stations" in the English catalog is not used by any checked code',
    }


def test_apple_missing_key_empty_english_plural_and_placeholders(repo):
    cat = {
        "sourceLanguage": "en",
        "strings": {
            "Empty": {"localizations": {"en": {"stringUnit": {"state": "translated", "value": ""}}}},
            "%lld left": {"localizations": {
                "en": {"variations": {"plural": {"other": {"stringUnit": {"state": "translated", "value": "%lld left"}}}}},
                "de": {"stringUnit": {"state": "translated", "value": "%@ übrig"}},
            }},
        },
    }
    repo.write(XCSTRINGS, json.dumps(cat, indent=2))
    repo.write("apple/Sources/V.swift", 'let a = String(localized: "Not in the catalog")\nlet b = Text("Empty")\n')
    report = repo.run()
    assert set(_msgs(report)) == {
        f'"Not in the catalog" is not in the English catalog ({XCSTRINGS}); build in Xcode to extract it',
        'the English text for "Empty" is empty',
        'the English plural "%lld left" needs "one" and "other" filled in',
        'the de translation of "%lld left" has placeholders %@ where the English has %lld; a format call would fail or lose a value',
    }


# -- whole-repo rules -------------------------------------------------------


def test_no_english_catalog_is_one_finding_not_one_per_use(repo):
    repo.write("web/static/a.js", 'x.textContent = t("one");\ny.textContent = t("two");\n')
    report = repo.run()
    assert report.lines() == [
        'web/static/a.js:1:19: this code uses catalog keys such as "one", but the repo has no English web catalog (static/i18n/en.json)'
    ]


def test_partial_run_does_not_judge_missing_catalogs_or_unused_keys(repo):
    repo.catalogs("web")
    repo.write("web/static/a.js", 'x.textContent = t("ready");\n')
    report = repo.run("web/static/a.js")
    assert report.findings == [] and report.warnings == []


def test_warnings_print_with_a_prefix_and_keep_exit_0(repo):
    repo.write(EN_JSON, '{\n  "spare": "Spare"\n}\n')
    report = repo.run()
    assert report.lines() == [f'{EN_JSON}:2:1: warning: "spare" in the English catalog is not used by any checked code']
    assert report.exit_code == 0
