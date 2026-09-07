"""Tests for re-spelling a stored reading under a different key signature.

No image and no re-detection: a notehead's y is a staff position whatever the key
is, so everything here is arithmetic on a document that is already on disk.
"""

import pytest

from sheeter import naming, pipeline, pitches, schema


def _staff(index, hand, clef, top):
    return {
        "index": index, "hand": hand, "clef": clef, "clef_confidence": 0.8,
        "lines": [float(top + 20 * i) for i in range(5)], "unit": 20.0,
        "x_range": [50.0, 750.0], "key_fifths": 0, "key_confidence": 0.5,
        "cut_off": False,
    }


def _note(staff, name, x, accidental=None):
    step, alter = pitches.parse_name(name)
    return pitches.make_note(
        step, alter, x, pitches.step_to_y(step, staff["lines"], staff["clef"]),
        w=10.0, h=8.0, hollow=False, accidental=accidental,
        ledger=pitches.ledger_count(step, staff["clef"]))


def _part(staff, names, x, accidentals=None):
    accidentals = accidentals or {}
    notes = sorted((_note(staff, n, x, accidentals.get(n)) for n in names),
                   key=lambda n: n["midi"])
    return {
        "staff": staff["index"], "hand": staff["hand"],
        "duration_hint": "quarter-or-shorter", "dotted": False, "notes": notes,
        "chord": naming.name_chord([n["name"] for n in notes], 0),
    }


def _event(index, x, parts):
    pooled = sorted((n["midi"], n["name"]) for p in parts for n in p["notes"])
    return {
        "index": index, "x": float(x), "x_range": [x - 20.0, x + 20.0],
        "confidence": 0.7, "note": None, "parts": parts,
        "combined": naming.name_combined([n for _m, n in pooled], 0),
    }


def _system(index, top, accidentals=None):
    right = _staff(0, "right", "treble", top)
    left = _staff(1, "left", "bass", top + 200)
    events = [
        _event(0, 200, [_part(right, ["E4", "G4", "B4"], 200, accidentals),
                        _part(left, ["E3"], 200)]),
        _event(1, 400, [_part(right, ["A4", "C5"], 400),
                        _part(left, ["F2"], 400)]),
    ]
    return {
        "index": index, "y_range": [float(top - 40), float(top + 320)],
        "x_range": [50.0, 750.0], "grand": True, "cut_off": False,
        "staves": [right, left], "events": events,
    }


def make_doc(systems=2, accidentals=None):
    doc = schema.new_analysis(
        "abc123def456", "2026-09-05T20:31:00Z", "test.jpg",
        {"filename": "test.jpg", "content_type": "image/jpeg", "bytes": 1000,
         "width": 800, "height": 1000},
        {"width": 800, "height": 1000, "scale": 1.0, "deskew_deg": 0.0})
    doc["systems"] = [_system(i, 100 + 400 * i, accidentals) for i in range(systems)]
    return schema.validate_analysis(doc)


def names(doc, system=0, event=0, staff=0):
    part = [p for p in doc["systems"][system]["events"][event]["parts"]
            if p["staff"] == staff][0]
    return [note["name"] for note in part["notes"]]


@pytest.fixture
def doc():
    return make_doc()


class TestSetKey:
    def test_flats_reach_every_note_that_takes_one(self, doc):
        pipeline.set_key(doc, -2)
        # B and E take the two flats; G, A and C are untouched by them.
        assert names(doc) == ["Eb4", "G4", "Bb4"]
        assert names(doc, event=1) == ["A4", "C5"]

    def test_it_reaches_every_staff_of_every_system(self, doc):
        pipeline.set_key(doc, -2)
        assert names(doc, system=1) == ["Eb4", "G4", "Bb4"], "the second system too"
        assert names(doc, staff=1) == ["Eb3"], "and the left hand, not only the right"
        for system in doc["systems"]:
            for staff in system["staves"]:
                assert staff["key_fifths"] == -2

    def test_a_written_accidental_still_wins(self):
        # A natural in front of the note is the engraver saying "not the key's flat".
        doc = make_doc(accidentals={"E4": "natural"})
        pipeline.set_key(doc, -2)
        assert names(doc) == ["E4", "G4", "Bb4"]

    def test_the_key_says_where_the_alteration_came_from(self, doc):
        pipeline.set_key(doc, -2)
        part = doc["systems"][0]["events"][0]["parts"][0]
        by_name = dict((n["name"], n) for n in part["notes"])
        assert by_name["Bb4"]["from_key"] is True
        assert by_name["G4"]["from_key"] is False

    def test_midi_follows_the_spelling_so_playback_does_too(self, doc):
        before = doc["systems"][0]["events"][0]["parts"][0]["notes"][0]["midi"]
        pipeline.set_key(doc, -2)
        after = doc["systems"][0]["events"][0]["parts"][0]["notes"][0]["midi"]
        assert after == before - 1, "E4 became Eb4, a semitone down"

    def test_chords_are_named_again(self, doc):
        was = doc["systems"][0]["events"][0]["parts"][0]["chord"]
        pipeline.set_key(doc, -2)
        now = doc["systems"][0]["events"][0]["parts"][0]["chord"]
        assert was["symbol"] != now["symbol"], "E minor is not Eb major"
        assert now["symbol"].startswith("Eb")

    def test_going_back_to_no_key_restores_the_reading(self, doc):
        before = [names(doc, s, e, t) for s in (0, 1) for e in (0, 1) for t in (0, 1)]
        pipeline.set_key(doc, 4)
        pipeline.set_key(doc, 0)
        after = [names(doc, s, e, t) for s in (0, 1) for e in (0, 1) for t in (0, 1)]
        assert after == before

    def test_a_reader_who_says_so_is_believed(self, doc):
        # verify.geometry_is_sure reads this, which is what stops the vision pass
        # putting its own key back over a choice somebody made looking at the page.
        pipeline.set_key(doc, 3)
        assert all(staff["key_confidence"] == 1.0
                   for system in doc["systems"] for staff in system["staves"])

    def test_the_result_is_still_a_valid_document(self, doc):
        assert schema.validate_analysis(pipeline.set_key(doc, -4)) is doc

    @pytest.mark.parametrize("fifths", [-8, 8, 99])
    def test_an_impossible_key_is_refused(self, doc, fifths):
        with pytest.raises(ValueError):
            pipeline.set_key(doc, fifths)
