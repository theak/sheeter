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
        "barlines": [],
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


class TestTheStoredChoice:
    """The pick itself is one field on the document; set_key does not write it."""

    def test_a_new_document_has_nobody_s_choice_on_it(self, doc):
        assert doc["key_override"] is None

    @pytest.mark.parametrize("bad", [-8, 8, "two flats", True, 1.5])
    def test_the_validator_refuses_an_impossible_choice(self, doc, bad):
        doc["key_override"] = bad
        with pytest.raises(schema.SchemaError):
            schema.validate_analysis(doc)

    def test_the_validator_takes_a_real_one(self, doc):
        doc["key_override"] = -2
        assert schema.validate_analysis(doc) is doc

    def test_normalize_leaves_a_choice_that_is_already_there_alone(self):
        assert schema.normalize({"key_override": 3})["key_override"] == 3
        assert schema.normalize({})["key_override"] is None


# --------------------------------------------------------------------------
# editing a reading by hand


def _editable(barlines=(), extra=()):
    """A grand staff whose right hand repeats E4, so a carry has somewhere to go.

    *extra* adds events after the two the shared fixture builds, which is what puts a
    second E4 on the far side of a barline.
    """
    doc = make_doc(systems=1)
    system = doc["systems"][0]
    right, left = system["staves"]
    right["barlines"] = [float(x) for x in barlines]
    left["barlines"] = [float(x) for x in barlines]
    for index, (x, names) in enumerate(extra, start=len(system["events"])):
        system["events"].append(
            _event(index, x, [_part(right, names, x), _part(left, ["E3"], x)]))
    pipeline.name_everything(doc)
    return schema.validate_analysis(doc)


def _right(doc, event, system=0):
    part = next(p for p in doc["systems"][system]["events"][event]["parts"]
                if p["staff"] == 0)
    return [n["name"] for n in part["notes"]]


class TestSetAccidental:
    def test_sharpening_a_note_respells_it(self):
        doc = _editable()
        assert _right(doc, 0) == ["E4", "G4", "B4"]
        assert pipeline.set_accidental(doc, 0, 0, 0, 0, "sharp") is True
        assert _right(doc, 0) == ["E#4", "G4", "B4"]
        schema.validate_analysis(doc)

    def test_flat_and_natural_are_offered_too(self):
        doc = _editable()
        assert pipeline.set_accidental(doc, 0, 0, 0, 0, "flat") is True
        assert _right(doc, 0)[0] == "Eb4"
        # Natural is not the same as no accidental: it has to survive a key that
        # would otherwise flatten this note.
        assert pipeline.set_accidental(doc, 0, 0, 0, 0, "natural") is True
        assert _right(doc, 0)[0] == "E4"
        pipeline.set_key(doc, -3)
        pipeline.apply_edits(doc)
        assert _right(doc, 0)[0] == "E4", "the written natural outranks three flats"

    def test_the_accidental_carries_to_the_rest_of_the_measure(self):
        """What the notation means, and the whole point of the feature: an accidental
        holds at that staff position until the next barline."""
        doc = _editable(barlines=[600], extra=[(500, ["E4"])])
        assert _right(doc, 2) == ["E4"]
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert _right(doc, 0)[0] == "Eb4"
        assert _right(doc, 2) == ["Eb4"], "the later E in the same measure follows"

    def test_it_stops_at_the_barline(self):
        doc = _editable(barlines=[450], extra=[(500, ["E4"])])
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert _right(doc, 0)[0] == "Eb4"
        assert _right(doc, 2) == ["E4"], "a new measure starts clean"

    def test_it_leaves_a_different_staff_position_alone(self):
        doc = _editable(barlines=[600], extra=[(500, ["E5"])])
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert _right(doc, 2) == ["E5"], "an octave up is its own position"

    def test_a_note_with_its_own_accidental_is_not_overruled(self):
        doc = _editable(barlines=[600], extra=[(500, ["E4"])])
        system = doc["systems"][0]
        later = next(p for p in system["events"][2]["parts"] if p["staff"] == 0)
        later["notes"][0]["accidental"] = "natural"
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert _right(doc, 2) == ["E4"], "what is written on the note wins"

    def test_the_other_hand_keeps_its_own_accidentals(self):
        doc = _editable(barlines=[600])
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        left = next(p for p in doc["systems"][0]["events"][0]["parts"]
                    if p["staff"] == 1)
        assert [n["name"] for n in left["notes"]] == ["E3"]

    def test_clearing_puts_the_reading_back(self):
        doc = _editable(barlines=[600], extra=[(500, ["E4"])])
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert pipeline.set_accidental(doc, 0, 0, 0, 0, None) is True
        assert _right(doc, 0) == ["E4", "G4", "B4"]
        assert _right(doc, 2) == ["E4"], "the carry goes with it"
        assert doc["edits"] == [], "and the edit is not kept as dead weight"

    def test_one_edit_per_notehead(self):
        doc = _editable()
        pipeline.set_accidental(doc, 0, 0, 0, 0, "sharp")
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert len(doc["edits"]) == 1, "a correction of a correction is one edit"
        assert doc["edits"][0]["value"] == "flat"
        assert _right(doc, 0)[0] == "Eb4"

    def test_the_chord_is_named_again(self):
        doc = _editable()
        before = doc["systems"][0]["events"][0]["combined"]["symbol"]
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert doc["systems"][0]["events"][0]["combined"]["symbol"] != before

    @pytest.mark.parametrize("value", ["double-sharp", "wobbly", "", "sharpen"])
    def test_an_accidental_nobody_could_have_clicked_is_refused(self, value):
        doc = _editable()
        with pytest.raises(ValueError):
            pipeline.set_accidental(doc, 0, 0, 0, 0, value)

    @pytest.mark.parametrize("target", [
        (9, 0, 0, 0), (0, 9, 0, 0), (0, 0, 9, 0), (0, 0, 0, 9), (0, 0, 0, -1),
    ])
    def test_a_target_that_is_not_there_changes_nothing(self, target):
        doc = _editable()
        before = _right(doc, 0)
        assert pipeline.set_accidental(doc, *target, value="sharp") is False
        assert _right(doc, 0) == before
        assert doc["edits"] == []


class TestDeleteEvent:
    def test_a_spurious_chord_can_go(self):
        doc = _editable()
        assert pipeline.delete_event(doc, 0, 0) is True
        assert len(doc["systems"][0]["events"]) == 1
        assert _right(doc, 0) == ["A4", "C5"], "the one that was second is now first"

    def test_the_events_left_are_renumbered(self):
        """Not cosmetic: the schema requires an event's index to be its position, and
        the verify merge matches its answer up by that index."""
        doc = _editable(extra=[(500, ["E4"]), (600, ["F4"])])
        pipeline.delete_event(doc, 0, 1)
        assert [e["index"] for e in doc["systems"][0]["events"]] == [0, 1, 2]
        schema.validate_analysis(doc)

    def test_deleting_moves_the_edits_that_come_after_it(self):
        doc = _editable(extra=[(500, ["E4"])])
        pipeline.set_accidental(doc, 0, 2, 0, 0, "sharp")
        assert _right(doc, 2) == ["E#4"]
        pipeline.delete_event(doc, 0, 1)
        assert doc["edits"][0]["event"] == 1, "the edit followed its chord"
        pipeline.apply_edits(doc)
        assert _right(doc, 1) == ["E#4"], "and still points at the same notehead"

    def test_an_edit_on_the_deleted_chord_goes_with_it(self):
        doc = _editable(extra=[(500, ["E4"])])
        pipeline.set_accidental(doc, 0, 2, 0, 0, "sharp")
        pipeline.delete_event(doc, 0, 2)
        assert doc["edits"] == []

    def test_deleting_the_last_chord_of_a_system_is_allowed(self):
        doc = _editable()
        assert pipeline.delete_event(doc, 0, 0) is True
        assert pipeline.delete_event(doc, 0, 0) is True
        assert doc["systems"][0]["events"] == []
        schema.validate_analysis(doc)

    @pytest.mark.parametrize("target", [(9, 0), (0, 9), (0, -1)])
    def test_a_chord_that_is_not_there_changes_nothing(self, target):
        doc = _editable()
        assert pipeline.delete_event(doc, *target) is False
        assert len(doc["systems"][0]["events"]) == 2


class TestApplyEdits:
    def test_edits_outlive_a_key_pick(self):
        """What restate_staff loses on a key change is the carry, not the accidental.

        It reads each notehead's own accidental and honours it, so the edited note
        keeps its flat; it has no barlines and no notion of a measure, so the later E
        that was following that flat goes back to the key's spelling.  That is the gap
        the replay closes, and it is why apply_edits runs after set_key.
        """
        doc = _editable(barlines=[600], extra=[(500, ["E4"])])
        pipeline.set_accidental(doc, 0, 0, 0, 0, "flat")
        assert (_right(doc, 0)[0], _right(doc, 2)) == ("Eb4", ["Eb4"])
        pipeline.set_key(doc, 2)
        assert _right(doc, 0)[0] == "Eb4", "the note's own accidental survives"
        assert _right(doc, 2) == ["E4"], "the carry does not"
        pipeline.apply_edits(doc)
        assert (_right(doc, 0)[0], _right(doc, 2)) == ("Eb4", ["Eb4"])

    def test_an_edit_whose_notehead_moved_is_dropped(self):
        """A re-analysis renumbers everything.  An edit that has come to point at some
        other notehead is worse than an edit that is dropped, so the y is checked."""
        doc = _editable()
        pipeline.set_accidental(doc, 0, 0, 0, 0, "sharp")
        note = next(p for p in doc["systems"][0]["events"][0]["parts"]
                    if p["staff"] == 0)["notes"][0]
        note["y"] += 5.0
        pipeline.apply_edits(doc)
        assert doc["edits"] == []

    def test_a_notehead_that_barely_moved_keeps_its_edit(self):
        doc = _editable()
        pipeline.set_accidental(doc, 0, 0, 0, 0, "sharp")
        note = next(p for p in doc["systems"][0]["events"][0]["parts"]
                    if p["staff"] == 0)["notes"][0]
        note["y"] += 0.2
        pipeline.apply_edits(doc)
        assert len(doc["edits"]) == 1
        assert _right(doc, 0)[0] == "E#4"

    def test_a_reading_with_no_edits_is_left_exactly_alone(self):
        doc = _editable()
        before = [_right(doc, i) for i in range(2)]
        pipeline.apply_edits(doc)
        assert [_right(doc, i) for i in range(2)] == before


def _step(name):
    return pitches.parse_name(name)[0]


class TestSetPitch:
    def test_a_notehead_read_a_space_off_can_be_moved(self):
        doc = _editable()
        assert _right(doc, 0) == ["E4", "G4", "B4"]
        assert pipeline.set_pitch(doc, 0, 0, 0, 0, _step("F4")) is True
        assert _right(doc, 0) == ["F4", "G4", "B4"]
        schema.validate_analysis(doc)

    def test_the_chord_stays_in_order(self):
        doc = _editable()
        pipeline.set_pitch(doc, 0, 0, 0, 0, _step("C5"))
        assert _right(doc, 0) == ["G4", "B4", "C5"]

    def test_the_move_is_a_staff_position_and_the_key_still_spells_it(self):
        """Why a move needs nothing replayed: it writes the geometry, and every later
        re-derivation reads the geometry.  It also means the key still applies to it."""
        doc = _editable()
        pipeline.set_pitch(doc, 0, 0, 0, 0, _step("F4"))
        assert doc["edits"] == [], "not a correction that has to be replayed"
        pipeline.set_key(doc, 1)
        assert _right(doc, 0) == ["F#4", "G4", "B4"], "one sharp reaches the F"

    def test_a_written_accidental_travels_with_the_notehead(self):
        doc = _editable()
        pipeline.set_accidental(doc, 0, 0, 0, 0, "sharp")
        assert _right(doc, 0) == ["E#4", "G4", "B4"]
        pipeline.set_pitch(doc, 0, 0, 0, 0, _step("F4"))
        assert _right(doc, 0) == ["F#4", "G4", "B4"]
        assert len(doc["edits"]) == 1, "still one correction, not two"
        pipeline.apply_edits(doc)
        assert _right(doc, 0) == ["F#4", "G4", "B4"], "and it still finds its notehead"

    def test_the_carry_follows_the_note_to_its_new_position(self):
        doc = _editable(barlines=[600], extra=[(500, ["F4"])])
        pipeline.set_accidental(doc, 0, 0, 0, 0, "sharp")
        assert _right(doc, 2) == ["F4"], "nothing to carry to yet"
        pipeline.set_pitch(doc, 0, 0, 0, 0, _step("F4"))
        assert _right(doc, 2) == ["F#4"], "now the later F is in its shadow"

    def test_moving_onto_the_position_it_is_already_on_is_not_a_change(self):
        doc = _editable()
        assert pipeline.set_pitch(doc, 0, 0, 0, 0, _step("E4")) is False

    @pytest.mark.parametrize("step", [999, -999, None, "F4", 4.5])
    def test_a_position_off_the_menu_is_refused(self, step):
        doc = _editable()
        assert pipeline.set_pitch(doc, 0, 0, 0, 0, step) is False
        assert _right(doc, 0) == ["E4", "G4", "B4"]


class TestDeleteNote:
    def test_one_note_of_a_chord_can_go(self):
        doc = _editable()
        assert pipeline.delete_note(doc, 0, 0, 0, 1) is True
        assert _right(doc, 0) == ["E4", "B4"]
        schema.validate_analysis(doc)

    def test_the_chord_is_named_again_without_it(self):
        doc = _editable()
        before = doc["systems"][0]["events"][0]["combined"]["symbol"]
        pipeline.delete_note(doc, 0, 0, 0, 1)
        assert doc["systems"][0]["events"][0]["combined"]["symbol"] != before

    def test_emptying_a_hand_takes_the_hand_and_leaves_the_chord(self):
        doc = _editable()
        event = doc["systems"][0]["events"][0]
        assert [part["staff"] for part in event["parts"]] == [0, 1]
        assert pipeline.delete_note(doc, 0, 0, 1, 0) is True     # the lone left hand
        event = doc["systems"][0]["events"][0]
        assert [part["staff"] for part in event["parts"]] == [0]
        schema.validate_analysis(doc)

    def test_emptying_every_hand_takes_the_chord(self):
        """The schema will not hold an event with no parts, so the last note of the
        last hand takes the whole chord with it, renumbering and all."""
        doc = _editable()
        pipeline.delete_note(doc, 0, 0, 1, 0)
        for _ in range(3):
            pipeline.delete_note(doc, 0, 0, 0, 0)
        assert [event["index"] for event in doc["systems"][0]["events"]] == [0]
        assert _right(doc, 0) == ["A4", "C5"], "the chord after it moved up"
        schema.validate_analysis(doc)

    def test_the_correction_on_a_deleted_note_goes_with_it(self):
        doc = _editable()
        pipeline.set_accidental(doc, 0, 0, 0, 0, "sharp")
        pipeline.delete_note(doc, 0, 0, 0, 0)
        assert doc["edits"] == []

    def test_a_note_that_is_not_there_changes_nothing(self):
        doc = _editable()
        assert pipeline.delete_note(doc, 0, 0, 0, 9) is False
        assert _right(doc, 0) == ["E4", "G4", "B4"]


class TestAddNote:
    def test_a_missed_note_can_be_added(self):
        doc = _editable()
        assert pipeline.add_note(doc, 0, 0, 0, _step("D5")) is True
        assert _right(doc, 0) == ["E4", "G4", "B4", "D5"]
        schema.validate_analysis(doc)

    def test_the_key_spells_the_new_note(self):
        doc = _editable()
        pipeline.set_key(doc, -2)
        pipeline.add_note(doc, 0, 0, 0, _step("B4"))
        assert "Bb4" in _right(doc, 0), "two flats reach a note added under them"

    def test_a_whole_missed_hand_comes_back(self):
        """The case with no part to add to: the reader says this staff plays something
        here and there is nothing to hang it on, so a part is made for it."""
        doc = _editable()
        pipeline.delete_note(doc, 0, 0, 1, 0)
        event = doc["systems"][0]["events"][0]
        assert [part["staff"] for part in event["parts"]] == [0]
        assert pipeline.add_note(doc, 0, 0, 1, _step("C3")) is True
        event = doc["systems"][0]["events"][0]
        left = next(part for part in event["parts"] if part["staff"] == 1)
        assert [note["name"] for note in left["notes"]] == ["C3"]
        assert left["hand"] == "left", "and it is that staff's own hand"
        schema.validate_analysis(doc)

    def test_the_position_that_is_already_sounding_is_refused(self):
        doc = _editable()
        assert pipeline.add_note(doc, 0, 0, 0, _step("E4")) is False
        assert _right(doc, 0) == ["E4", "G4", "B4"]

    def test_an_added_note_takes_the_size_of_the_ones_beside_it(self):
        doc = _editable()
        pipeline.add_note(doc, 0, 0, 0, _step("D5"))
        notes = next(part for part in doc["systems"][0]["events"][0]["parts"]
                     if part["staff"] == 0)["notes"]
        added = next(note for note in notes if note["name"] == "D5")
        assert (added["w"], added["h"]) == (10.0, 8.0), "the fixture's own notehead size"
        assert added["changed"] is True

    @pytest.mark.parametrize("target", [(9, 0, 0), (0, 9, 0), (0, 0, 9)])
    def test_a_target_that_is_not_there_changes_nothing(self, target):
        doc = _editable()
        assert pipeline.add_note(doc, *target, step=_step("D5")) is False


class TestAnchorsSurviveStructuralChange:
    """The reason a correction is anchored by height and not by its index in the chord.

    Adding or removing a notehead renumbers the ones after it.  An anchor that counted
    positions would come to mean a different note; one that remembers where the
    notehead sits does not, and does not need a fix-up on every structural change.
    """

    def test_adding_a_lower_note_does_not_move_a_correction(self):
        doc = _editable()
        pipeline.set_accidental(doc, 0, 0, 0, 1, "sharp")     # the G
        assert _right(doc, 0) == ["E4", "G#4", "B4"]
        pipeline.add_note(doc, 0, 0, 0, _step("D4"))          # slots in below it
        assert _right(doc, 0) == ["D4", "E4", "G#4", "B4"]
        pipeline.apply_edits(doc)
        assert _right(doc, 0) == ["D4", "E4", "G#4", "B4"], "still the G"

    def test_removing_a_lower_note_does_not_move_a_correction(self):
        doc = _editable()
        pipeline.set_accidental(doc, 0, 0, 0, 2, "flat")      # the B
        assert _right(doc, 0) == ["E4", "G4", "Bb4"]
        pipeline.delete_note(doc, 0, 0, 0, 0)                 # take the E out
        pipeline.apply_edits(doc)
        assert _right(doc, 0) == ["G4", "Bb4"], "still the B"
