"""The rows the fix panel is drawn from, and which of them an edit moves.

Two things rest on this.  The page and a single re-rendered panel are built from the
same rows, so a panel swapped into a page in the browser is the markup that page would
have had; and comparing rows is how an edit decides which panels it has to send back,
which is only right if a row holds everything a panel shows.
"""

import copy

import pytest

from sheeter import naming, panels, pipeline, pitches, schema, store

LINES = [100.0, 120.0, 140.0, 160.0, 180.0]


def note_at(name, x):
    step, alter = pitches.parse_name(name)
    return pitches.make_note(step, alter, x,
                             pitches.step_to_y(step, LINES, "treble"))


def make_doc(chords, barlines=()):
    """A treble-staff reading of *chords*, one event each, spaced 100px apart."""
    events = []
    for index, names in enumerate(chords):
        x = 100.0 + index * 100.0
        notes = sorted((note_at(name, x) for name in names),
                       key=lambda note: note["midi"])
        events.append({
            "index": index, "x": x, "x_range": [x - 20, x + 20], "confidence": 0.9,
            "note": None, "combined": naming.name_combined(list(names)),
            "parts": [{"staff": 0, "hand": None, "duration_hint": "quarter-or-shorter",
                       "dotted": False, "notes": notes,
                       "chord": naming.name_chord(list(names))}],
        })
    doc = schema.new_analysis(
        store.analysis_id(b"panels"), "2026-09-05T20:31:00Z", "Bars 1 to 4",
        {"filename": "p.png", "content_type": "image/png", "bytes": 9,
         "width": 900, "height": 600},
        {"width": 600, "height": 400, "scale": 1.0, "deskew_deg": 0.0})
    doc["systems"] = [{
        "index": 0, "y_range": [80, 200], "x_range": [0, 600], "grand": False,
        "cut_off": False,
        "staves": [{"index": 0, "hand": None, "clef": "treble", "clef_confidence": 0.9,
                    "lines": LINES, "unit": 20.0, "x_range": [0, 600],
                    "key_fifths": 0, "key_confidence": 0.9, "cut_off": False,
                    "barlines": list(barlines)}],
        "events": events}]
    return schema.validate_analysis(doc)


@pytest.fixture
def doc():
    return make_doc([("C4", "E4", "G4"), ("C4", "F4"), ("D4", "G4")])


class TestRows:
    def test_one_row_per_step_numbered_from_one(self, doc):
        rows = panels.fix_steps(doc)
        assert [row["number"] for row in rows] == [1, 2, 3]
        # A two-note event is named as the interval it is, not guessed at as a chord.
        assert [row["symbol"] for row in rows] == ["C", "C+P4", "D+P4"]

    def test_a_chord_reads_high_to_low(self, doc):
        """The order it is read off the page, which is not the order it is stored in."""
        names = [note["pretty"] for note in panels.fix_steps(doc)[0]["hands"][0]["notes"]]
        assert names == ["G 4", "E 4", "C 4"]

    def test_the_octave_is_spaced_off_the_note(self, doc):
        """"G# 4", not "G#4": at label size the two run together."""
        doc["systems"][0]["events"][0]["parts"][0]["notes"][0]["accidental"] = "sharp"
        pipeline.measure_accidentals(doc["systems"][0], doc["systems"][0]["staves"][0])
        assert panels.fix_steps(doc)[0]["hands"][0]["notes"][-1]["pretty"] == "C♯ 4"

    def test_every_note_carries_the_pitch_to_play(self, doc):
        """What the play button in the row is for, and what the overlay compares before
        and after an edit to work out which note the edit was about."""
        for row in panels.fix_steps(doc):
            for note in row["hands"][0]["notes"]:
                assert isinstance(note["midi"], int)

    def test_the_button_in_force_is_what_the_note_sounds(self, doc):
        """Not only what is printed in front of it: a note the key sharpens shows
        sharp, because that is what it is."""
        pipeline.set_key(doc, 2)                       # F# and C# in the key
        row = panels.fix_steps(doc)[0]
        marked = dict((note["pretty"], note["current"]) for note in row["hands"][0]["notes"])
        assert marked["C♯ 4"] == "sharp"
        assert marked["G 4"] == "natural"

    def test_a_corrected_note_is_marked_as_corrected(self, doc):
        """Which is what puts the undo button in its row."""
        pipeline.set_accidental(doc, 0, 0, 0, 0, value="sharp")
        rows = panels.fix_steps(doc)
        by_hand = [note["pretty"] for note in rows[0]["hands"][0]["notes"]
                   if note["by_hand"]]
        assert by_hand == ["C♯ 4"]

    def test_a_staff_playing_nothing_here_is_still_offered(self, doc):
        """It is how a whole hand the reader missed gets put back."""
        doc["systems"][0]["events"][1]["parts"] = []
        row = panels.fix_steps(doc)[1]
        assert row["hands"][0]["notes"] == []
        assert row["hands"][0]["choices"], "the menu to add one with is still there"


class TestWhichPanelsMoved:
    """The comparison the edit routes use to decide what to send back.

    Positional, which is safe rather than lucky: anything that changes how many steps
    there are answers with a reload instead of coming through here.
    """

    def test_sharpening_a_note_moves_its_own_panel(self, doc):
        before = panels.fix_steps(doc)
        assert pipeline.set_accidental(doc, 0, 2, 0, 0, value="sharp")
        assert panels.changed_panels(before, panels.fix_steps(doc)) == [3]

    def test_an_accidental_moves_the_panels_it_carries_to(self, doc):
        """A written accidental holds to the end of its measure at that staff position,
        so sharpening the C in step 1 re-spells the C in step 2 as well.  Its panel is
        as wrong as the edited one until it is redrawn, which is the whole reason the
        answer is a set of panels rather than the one that was clicked in.
        """
        before = panels.fix_steps(doc)
        assert pipeline.set_accidental(doc, 0, 0, 0, 0, value="sharp")
        moved = panels.changed_panels(before, panels.fix_steps(doc))
        assert moved == [1, 2], "the note's own step, and the C it carries to"
        after = panels.fix_steps(doc)
        assert [n["pretty"] for n in after[1]["hands"][0]["notes"]] == ["F 4", "C♯ 4"]

    def test_a_barline_stops_the_carry_and_so_stops_the_redraw(self, doc):
        """The same edit with a barline between the two: one panel, not two."""
        walled = make_doc([("C4", "E4", "G4"), ("C4", "F4"), ("D4", "G4")],
                          barlines=[150.0])
        before = panels.fix_steps(walled)
        assert pipeline.set_accidental(walled, 0, 0, 0, 0, value="sharp")
        assert panels.changed_panels(before, panels.fix_steps(walled)) == [1]

    def test_moving_a_notehead_moves_one_panel(self, doc):
        before = panels.fix_steps(doc)
        step = pitches.parse_name("A4")[0]
        assert pipeline.set_pitch(doc, 0, 2, 0, 0, step=step)
        assert panels.changed_panels(before, panels.fix_steps(doc)) == [3]

    def test_asking_for_the_accidental_a_note_already_sounds_moves_nothing(self, doc):
        before = panels.fix_steps(doc)
        assert not pipeline.set_accidental(doc, 0, 0, 0, 0, value="natural")
        assert panels.changed_panels(before, panels.fix_steps(doc)) == []


class TestTheNoteThatChanged:
    """One pitch arrives and at most one leaves, which is what lets the overlay play the
    note an edit just made without being told which row it was in.  Settling a chord
    re-sorts it, so the row is not a reliable answer and the pitches are."""

    @staticmethod
    def midis(doc, step):
        return [note["midi"]
                for note in panels.fix_steps(doc)[step - 1]["hands"][0]["notes"]]

    @staticmethod
    def arrived(was, now):
        left = list(was)
        fresh = []
        for midi in now:
            if midi in left:
                left.remove(midi)
            else:
                fresh.append(midi)
        return fresh

    @staticmethod
    def midi_of(name):
        return pitches.step_to_midi(*pitches.parse_name(name))

    def test_sharpening_brings_in_the_sharpened_pitch(self, doc):
        """Step 3 is D4 and G4, so sharpening the D brings in D#4 and retires D4."""
        was = self.midis(doc, 3)
        pipeline.set_accidental(doc, 0, 2, 0, 0, value="sharp")
        assert self.arrived(was, self.midis(doc, 3)) == [self.midi_of("D#4")]

    def test_moving_brings_in_where_it_landed(self, doc):
        was = self.midis(doc, 3)
        pipeline.set_pitch(doc, 0, 2, 0, 0, step=pitches.parse_name("A4")[0])
        assert self.arrived(was, self.midis(doc, 3)) == [self.midi_of("A4")]

    def test_adding_brings_in_the_note_added(self, doc):
        was = self.midis(doc, 3)
        pipeline.add_note(doc, 0, 2, 0, step=pitches.parse_name("B4")[0])
        assert self.arrived(was, self.midis(doc, 3)) == [self.midi_of("B4")]

    def test_removing_brings_in_nothing_so_there_is_nothing_to_play(self, doc):
        was = self.midis(doc, 1)
        pipeline.delete_note(doc, 0, 0, 0, 0)
        assert self.arrived(was, self.midis(doc, 1)) == []
