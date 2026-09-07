"""Tests for the key signature pictures.

The point of interest is the accidental positions.  A picture the reader matches
against the page is worth nothing if it is drawn a step out, and "it looked right"
is not a check, so the rows are converted back into pitches through the same
y_to_step the reader uses on a photo and compared with what a printed treble
signature actually says.
"""

import xml.etree.ElementTree as ET

import pytest

from sheeter import keysig, pitches

#: A staff to measure the pictures against: five lines, 20px apart, top line first.
LINES = [0.0, 20.0, 40.0, 60.0, 80.0]
SPACE = 20.0

#: What a treble key signature is printed as, in the order it is written.
SHARPS = ["F#5", "C#5", "G#5", "D#5", "A#4", "E#5", "B#4"]
FLATS = ["Bb4", "Eb5", "Ab4", "Db5", "Gb4", "Cb5", "Fb4"]


def drawn_pitches(fifths):
    """The pictured accidentals, read back as pitch names off a real staff."""
    out = []
    for glyph, row in keysig.positions(fifths):
        step, err = pitches.y_to_step(LINES[0] + row * SPACE, LINES, "treble")
        assert err < 1.0, "row %r is not on a staff position at all" % (row,)
        out.append(pitches.step_to_name(step, 1 if glyph == "♯" else -1))
    return out


class TestWhereTheAccidentalsGo:
    def test_seven_sharps_are_where_a_printer_puts_them(self):
        assert drawn_pitches(7) == SHARPS

    def test_seven_flats_are_where_a_printer_puts_them(self):
        assert drawn_pitches(-7) == FLATS

    @pytest.mark.parametrize("fifths", range(1, 8))
    def test_a_signature_is_the_first_n_of_them_and_nothing_else(self, fifths):
        assert drawn_pitches(fifths) == SHARPS[:fifths]
        assert drawn_pitches(-fifths) == FLATS[:fifths]

    def test_the_order_follows_the_circle_of_fifths(self):
        letters = [name[0] for name in drawn_pitches(7)]
        assert letters == pitches.SHARP_ORDER
        assert [name[0] for name in drawn_pitches(-7)] == pitches.FLAT_ORDER

    def test_c_major_draws_nothing(self):
        assert keysig.positions(0) == []

    def test_only_the_third_sharp_leaves_the_staff(self):
        # G#5 sits in the space above the top line; everything else is between the
        # lines, which is what keeps the pictures the same height.
        outside = [row for _glyph, row in keysig.positions(7) if not 0 <= row <= 4]
        assert outside == [-0.5]
        assert all(0 <= row <= 4 for _glyph, row in keysig.positions(-7))

    @pytest.mark.parametrize("fifths", [-8, 8, 100])
    def test_an_impossible_signature_is_refused(self, fifths):
        with pytest.raises(ValueError):
            keysig.positions(fifths)


class TestTheDrawing:
    @pytest.mark.parametrize("fifths", keysig.FIFTHS)
    def test_every_picture_is_well_formed_xml(self, fifths):
        root = ET.fromstring(keysig.svg(fifths))
        assert root.tag.endswith("svg")

    @pytest.mark.parametrize("fifths", keysig.FIFTHS)
    def test_every_picture_has_five_staff_lines_and_its_accidentals(self, fifths):
        root = ET.fromstring(keysig.svg(fifths))
        lines = [e for e in root if e.tag.endswith("line")]
        glyphs = [e.text for e in root if e.tag.endswith("text")]
        assert len(lines) == 5
        assert len(glyphs) == abs(fifths)
        assert set(glyphs) <= {"♯" if fifths > 0 else "♭"}

    def test_the_accidentals_sit_clear_of_each_other(self):
        root = ET.fromstring(keysig.svg(7))
        xs = [float(e.get("x")) for e in root if e.tag.endswith("text")]
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        assert all(gap > keysig.SPACE for gap in gaps), "they would overlap"

    def test_it_takes_its_colour_from_the_text_around_it(self):
        # So one drawing works in both themes without knowing which one it is in.
        assert 'stroke="currentColor"' in keysig.svg(-2)

    def test_a_picture_beside_its_own_name_is_not_read_out_twice(self):
        assert 'aria-hidden="true"' in keysig.svg(-2, describe=False)
        assert 'aria-label="2 flats"' in keysig.svg(-2)


class TestNames:
    def test_it_names_both_keys_a_signature_can_mean(self):
        assert keysig.label(-2) == "B♭ major / G minor"
        assert keysig.label(0) == "C major / A minor"
        assert keysig.label(4) == "E major / C♯ minor"

    def test_it_counts_them_for_the_reader_who_counts(self):
        assert keysig.count(0) == "no sharps or flats"
        assert keysig.count(1) == "1 sharp"
        assert keysig.count(-1) == "1 flat"
        assert keysig.count(-2) == "2 flats"
        assert keysig.count(7) == "7 sharps"

    def test_the_relative_minor_is_three_semitones_down(self):
        # Not decoration: it is the check that the two tables line up, since a wrong
        # row here would put a name on the wrong picture.
        for fifths in keysig.FIFTHS:
            major = pitches.parse_name(keysig.MAJORS[fifths].replace("♭", "b")
                                       .replace("♯", "#") + "4")
            minor = pitches.parse_name(keysig.MINORS[fifths].replace("♭", "b")
                                       .replace("♯", "#") + "4")
            apart = (pitches.step_to_midi(*major) - pitches.step_to_midi(*minor)) % 12
            assert apart == 3, "%r and %r are not relatives" % (
                keysig.MAJORS[fifths], keysig.MINORS[fifths])


class TestChoices:
    def test_it_offers_every_key_from_seven_flats_to_seven_sharps(self):
        rows = keysig.choices()
        assert [row["fifths"] for row in rows] == list(range(-7, 8))
        assert len(rows) == 15

    def test_each_row_carries_what_the_picker_shows(self):
        row = [r for r in keysig.choices() if r["fifths"] == -2][0]
        assert row["label"] == "B♭ major / G minor"
        assert row["count"] == "2 flats"
        assert row["svg"].startswith("<svg")
