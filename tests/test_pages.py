"""Real pages against what a person said is on them.

The corpus in tests/fixtures is engraved to order and says what the geometry can do.
These are photocopied jazz voicings, the thing the app is actually pointed at, and they
are where it has actually gone wrong: sharps fused into a cascade, a stem read as an
accidental and then as a key signature, a chord's lowest note lost at the edge of its
staff's search band.  Each of those was fixed against one of these pages and is held
here so it stays fixed.

tests/pages/manifest.json carries the ground truth.  Entries in a page's known_wrong
list are failures the reader has today; they are expected to fail, and a known_wrong
entry that passes fails this suite, because that is the moment to promote it.
"""

import json
import os

import pytest

from sheeter import pipeline

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")
MANIFEST = json.load(open(os.path.join(HERE, "manifest.json")))
PAGES = {page["file"]: page for page in MANIFEST["pages"]}

_readings = {}


def reading(name):
    if name not in _readings:
        raw = open(os.path.join(HERE, name), "rb").read()
        doc, _png = pipeline.analyze_bytes(raw, name, "image/png")
        _readings[name] = doc
    return _readings[name]


def events_of(doc):
    return [event for system in doc["systems"] for event in system["events"]]


def hand_names(event, hand):
    for part in event["parts"]:
        if part["hand"] == hand:
            return sorted(note["name"] for note in part["notes"])
    return None


def checks():
    """Every (page, what) pair the manifest can check, with its expected status."""
    out = []
    for page in MANIFEST["pages"]:
        wrong = set(page.get("known_wrong", []))
        out.append((page["file"], "key", "key" in wrong))
        if page.get("events") is not None:
            out.append((page["file"], "events", "events" in wrong))
        if page.get("clefs") is not None:
            out.append((page["file"], "clefs", "clefs" in wrong))
        for index, step in enumerate(page["steps"], start=1):
            for hand in ("right", "left"):
                if step.get(hand) is not None:
                    what = "%d.%s" % (index, hand)
                    out.append((page["file"], what, what in wrong))
    return out


def run_check(page, what):
    """Returns None when the reading agrees with the manifest, else a message."""
    doc = reading(page["file"])
    events = events_of(doc)
    if what == "key":
        keys = [staff["key_fifths"] for system in doc["systems"]
                for staff in system["staves"]]
        if any(key != page["key"] for key in keys):
            return "key signatures read %s, page has %d" % (keys, page["key"])
        return None
    if what == "events":
        if len(events) != page["events"]:
            return "read %d events, page has %d" % (len(events), page["events"])
        return None
    if what == "clefs":
        clefs = [[staff["clef"] for staff in system["staves"]] for system in doc["systems"]]
        if clefs != page["clefs"]:
            return "clefs read %s, page has %s" % (clefs, page["clefs"])
        return None
    index, hand = what.split(".")
    index = int(index)
    want = sorted(page["steps"][index - 1][hand])
    if index > len(events):
        return "step %d: only %d events were read" % (index, len(events))
    got = hand_names(events[index - 1], hand)
    if got != want:
        return "step %d %s hand: read %s, page has %s" % (index, hand, got, want)
    return None


@pytest.mark.parametrize("name,what,known_wrong", checks(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_page(name, what, known_wrong):
    problem = run_check(PAGES[name], what)
    if known_wrong:
        if problem is None:
            pytest.fail("%s %s now reads correctly: move it out of known_wrong in "
                        "tests/pages/manifest.json" % (name, what))
        pytest.xfail("known: " + problem)
    assert problem is None, "%s: %s" % (name, problem)


def test_every_page_in_the_manifest_exists():
    for page in MANIFEST["pages"]:
        assert os.path.exists(os.path.join(HERE, page["file"])), page["file"]


def test_known_wrong_entries_refer_to_real_checks():
    for page in MANIFEST["pages"]:
        valid = {"key", "events", "clefs"} | {
            "%d.%s" % (i, hand) for i in range(1, len(page["steps"]) + 1)
            for hand in ("right", "left")}
        for entry in page.get("known_wrong", []):
            assert entry in valid, "%s: %r is not a check" % (page["file"], entry)
