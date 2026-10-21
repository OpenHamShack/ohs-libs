"""The Python scanner, run through the check on fixture files."""

from conftest import lines_of


def test_qt_window_findings_are_exactly_the_marked_lines(repo):
    repo.copy("python/window_bad.py", "app/window.py")
    report = repo.run()
    assert lines_of(report, "app/window.py") == repo.expected_lines("app/window.py")
    assert report.exit_code == 1


def test_finding_line_has_the_contract_shape(repo):
    repo.copy("python/window_bad.py", "app/window.py")
    lines = repo.run().lines()
    # §3's example, to the character: path:line:col, the literal as JSON, the
    # sink with the widget class when the code shows it, and the fix.
    assert 'app/window.py:11:29: user-visible literal "Transmit blocked" passed to QLabel.setText; use self.tr()' in lines
    assert 'app/window.py:9:29: user-visible literal "Radio" passed to setWindowTitle; use self.tr()' in lines
    assert 'app/window.py:12:31: user-visible literal "Tune" passed to QPushButton(); use self.tr()' in lines


def test_template_literal_is_shown_with_its_placeholders(repo):
    repo.copy("python/window_bad.py", "app/window.py")
    msgs = [f.message for f in repo.run().findings]
    assert 'user-visible literal "Tune {station} now" passed to QPushButton.setToolTip; use self.tr()' in msgs


def test_column_counts_characters_not_bytes(repo):
    repo.write("app/w.py", 'class W:\n    def f(self):\n        x = "ü"; self.l.setText("Grüße")\n')
    (f,) = repo.run().findings
    assert (f.line, f.col) == (3, 33)


def test_outside_a_method_the_fix_is_translate(repo):
    repo.copy("python/window_bad.py", "app/window.py")
    msgs = [f.message for f in repo.run().findings if f.line == 27]
    assert msgs == ['user-visible literal "Quit" passed to addAction; use QCoreApplication.translate()']


def test_catalog_calls_and_non_sinks_are_clean(repo):
    repo.copy("python/window_good.py", "app/window.py")
    repo.catalogs("qt")
    report = repo.run()
    assert report.findings == [], report.lines()
    assert report.exit_code == 0


def test_server_error_values_and_configured_exceptions(repo):
    repo.write(".i18n-check.toml", '[python]\nuser_facing_exceptions = ["ValueError", "LicenceError"]\n')
    repo.copy("python/server_bad.py", "srv/app.py")
    report = repo.run()
    assert lines_of(report, "srv/app.py") == repo.expected_lines("srv/app.py")
    msgs = {f.line: f.message for f in report.findings}
    assert msgs[10] == 'user-visible literal "This device is held by another app." sent as a JSON "error"; use the server\'s catalog (_())'
    assert msgs[20] == 'user-visible literal "Type a licence name." raised in ValueError(); use the package\'s catalog (_())'


def test_exceptions_are_not_sinks_unless_configured(repo):
    repo.copy("python/server_bad.py", "srv/app.py")
    report = repo.run()
    assert lines_of(report, "srv/app.py") == {10, 12}


def test_server_done_properly_is_clean(repo):
    repo.write(".i18n-check.toml", '[python]\nuser_facing_exceptions = ["ValueError"]\n')
    repo.copy("python/server_good.py", "srv/app.py")
    repo.catalogs("gettext")
    assert repo.run().findings == []


def test_configured_python_sinks(repo):
    repo.write(".i18n-check.toml", '[python]\nsinks = ["setSubtitle", "Banner"]\n')
    repo.write("app/x.py", 'def f(w):\n    w.setSubtitle("On air")\n    Banner("Hello there")\n    other("Not a sink")\n')
    assert lines_of(repo.run(), "app/x.py") == {2, 3}


def test_unparseable_python_is_a_finding_not_a_pass(repo):
    repo.write("app/broken.py", 'def f(:\n    w.setText("Hidden")\n')
    (f,) = repo.run().findings
    assert f.path == "app/broken.py" and "could not parse" in f.message
