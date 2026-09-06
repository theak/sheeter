"""Round-trip accuracy: known score -> engraved image -> pipeline -> the same notes back.

This is the test the project is actually graded on.  ``tools/make_fixtures.py`` builds each
case from a music21 score whose pitches are known exactly, renders it through Verovio, and
also writes a synthesized phone-camera version (rotated, keystoned, unevenly lit, blurred,
JPEG compressed).  The rendered files are not committed, so run

    python tools/make_fixtures.py

first; without them these tests skip rather than fail.
"""

import json
import os

import pytest

from sheeter import pipeline

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
MANIFEST = os.path.join(FIXTURES, "manifest.json")


def _cases():
    if not os.path.isfile(MANIFEST):
        return []
    with open(MANIFEST) as handle:
        return json.load(handle)["cases"]


CASES = _cases()


def _expected(case):
    """``[{hand: [pitch names]}]`` per event, in the manifest's own vocabulary."""
    labels = [staff["label"] for system in case["systems"] for staff in system["staves"]]
    events = []
    for system in case["systems"]:
        for event in system["events"]:
            events.append(dict((key, value) for key, value in event.items()
                               if isinstance(value, list)))
    return labels, events


def _read(case, kind):
    name = case["photo"] if kind == "photo" else case["file"]
    if not name:
        pytest.skip("case %s has no %s image" % (case["name"], kind))
    path = os.path.join(FIXTURES, name)
    if not os.path.isfile(path):
        pytest.skip("run tools/make_fixtures.py to generate %s" % name)
    with open(path, "rb") as handle:
        doc, _png = pipeline.analyze_bytes(handle.read(), name, "image/png")
    return doc


def _actual(doc, labels):
    events = []
    for system in doc["systems"]:
        for event in system["events"]:
            by_hand = {}
            for part in event["parts"]:
                key = part["hand"] or (labels[part["staff"]]
                                       if part["staff"] < len(labels) else "treble")
                by_hand[key] = [note["name"] for note in part["notes"]]
            events.append(by_hand)
    return events


@pytest.mark.skipif(not CASES, reason="no fixture manifest; run tools/make_fixtures.py")
@pytest.mark.parametrize("kind", ["clean", "photo"])
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_reads_every_note(case, kind):
    labels, expected = _expected(case)
    actual = _actual(_read(case, kind), labels)

    assert len(actual) == len(expected), (
        "%s (%s): read %d events, expected %d" % (case["name"], kind,
                                                  len(actual), len(expected)))
    for index, (want, got) in enumerate(zip(expected, actual)):
        for hand, names in want.items():
            assert got.get(hand) == names, (
                "%s (%s) event %d, %s hand: read %s, expected %s"
                % (case["name"], kind, index, hand, got.get(hand), names))
        assert set(got) == set(want), (
            "%s (%s) event %d: read hands %s, expected %s"
            % (case["name"], kind, index, sorted(got), sorted(want)))


@pytest.mark.skipif(not CASES, reason="no fixture manifest; run tools/make_fixtures.py")
def test_key_signatures_are_read(case=None):
    for case in CASES:
        doc = _read(case, "clean")
        expected = [staff["key_fifths"] for system in case["systems"]
                    for staff in system["staves"]]
        actual = [staff["key_fifths"] for system in doc["systems"]
                  for staff in system["staves"]]
        assert actual == expected, "%s: key signatures %s, expected %s" % (
            case["name"], actual, expected)


@pytest.mark.skipif(not CASES, reason="no fixture manifest; run tools/make_fixtures.py")
def test_clefs_and_hands_are_read():
    for case in CASES:
        doc = _read(case, "clean")
        expected = [(staff["clef"], staff["hand"]) for system in case["systems"]
                    for staff in system["staves"]]
        actual = [(staff["clef"], staff["hand"]) for system in doc["systems"]
                  for staff in system["staves"]]
        assert actual == expected, "%s: clefs and hands %s, expected %s" % (
            case["name"], actual, expected)
