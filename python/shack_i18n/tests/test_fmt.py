"""shack_i18n.fmt against the shared vectors, plus what only Python can show.

The vectors are the contract every port runs (I18N.md §5 item 3); these
tests load the file the Kotlin, Swift and JS suites will load, so a change
to a rule is a change to that file, never to a Python-only expectation.
"""

import json
from pathlib import Path

import pytest

from shack_i18n import fmt

SPEC = Path(__file__).resolve().parents[3] / "spec"
VECTORS = json.loads((SPEC / "vectors" / "i18n" / "format.json").read_text(encoding="utf-8"))


def _ids(cases):
    return [f"{i}:{c.get('locale')}:{c.get('text', c.get('x', c.get('hz')))!r}" for i, c in enumerate(cases)]


def _expect(case, call):
    if "error" in case:
        with pytest.raises(fmt.FormatError) as err:
            call()
        assert err.value.code == case["error"]
        # Whatever the code, what a person might see is a whole sentence.
        assert str(err.value)[-1] in ".?!"
        assert str(err.value)[0].isupper() or str(err.value)[0] == '"'
        return None
    return call()


@pytest.mark.parametrize("case", VECTORS["frequency"], ids=_ids(VECTORS["frequency"]))
def test_frequency_vectors(case):
    got = _expect(case, lambda: fmt.frequency(case["hz"], locale=case["locale"], unit=case["unit"], decimals=case["decimals"]))
    if "text" in case:
        assert got == case["text"]


@pytest.mark.parametrize("case", VECTORS["parse_frequency"], ids=_ids(VECTORS["parse_frequency"]))
def test_parse_frequency_vectors(case):
    got = _expect(case, lambda: fmt.parse_frequency(case["text"], locale=case["locale"], unit=case["unit"]))
    if "hz" in case:
        assert got == case["hz"] and isinstance(got, int)


@pytest.mark.parametrize("case", VECTORS["offset"], ids=_ids(VECTORS["offset"]))
def test_offset_vectors(case):
    got = _expect(case, lambda: fmt.offset(case["hz"], unit=case["unit"], locale=case["locale"], decimals=case["decimals"]))
    if "text" in case:
        assert got == case["text"]


@pytest.mark.parametrize("case", VECTORS["number"], ids=_ids(VECTORS["number"]))
def test_number_vectors(case):
    got = _expect(case, lambda: fmt.number(case["x"], locale=case["locale"], decimals=case["decimals"]))
    if "text" in case:
        assert got == case["text"]


@pytest.mark.parametrize("case", VECTORS["swr"], ids=_ids(VECTORS["swr"]))
def test_swr_vectors(case):
    got = _expect(case, lambda: fmt.swr(case["x"], locale=case["locale"]))
    if "text" in case:
        assert got == case["text"]


def test_vector_file_matches_its_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SPEC / "schemas" / "i18n-vectors.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(VECTORS, schema)


def test_every_listed_locale_round_trips_through_parse():
    # Whatever a locale's separator, what frequency() prints, parse reads
    # back: the display and the entry field can never disagree.
    from shack_i18n._cldr import TABLE

    for tag in TABLE:
        for hz, unit in [(10489750000, "MHz"), (14074000, "kHz"), (1500, "Hz")]:
            text = fmt.frequency(hz, locale=tag, unit=unit)
            assert fmt.parse_frequency(text, locale=tag, unit=None) == hz, (tag, text)


def test_format_error_is_a_value_error():
    # Callers that already catch ValueError around user input keep working.
    with pytest.raises(ValueError):
        fmt.parse_frequency("10.489,750", locale="de", unit="MHz")


def test_bool_is_not_a_number():
    with pytest.raises(fmt.FormatError) as err:
        fmt.number(True)
    assert err.value.code == "not-a-number"
