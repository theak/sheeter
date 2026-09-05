"""Unit tests for the measuring parts of the reader.

These build their own images rather than leaning on the fixture corpus, so they run in
milliseconds and pin down the primitives the whole pipeline is balanced on.
"""

import numpy as np
import pytest

from sheeter import geometry, pitches, preprocess


def staff_image(unit=20, thickness=2, width=600, height=240, top=60, margin=40):
    """A blank five line staff, drawn to order, with page margin either side."""
    mask = np.zeros((height, width), dtype=bool)
    for index in range(5):
        y = int(top + index * unit)
        mask[y:y + thickness, margin:width - margin] = True
    return mask, [float(top + i * unit + (thickness - 1) / 2.0) for i in range(5)]


def draw_notehead(mask, x, y, unit, filled=True):
    """A notehead-sized blob, good enough for the correlator to find.

    The hollow one is drawn with a rim about a fifth of a staff space thick, which is
    what an engraved half note has.  A hairline rim is not a fair test: where a staff
    line crosses it there is nothing thick enough to tell the two apart, and no reader
    could keep the rim and lose the line.
    """
    kernel = geometry.ellipse_kernel(unit).astype(bool)
    if not filled:
        kernel = kernel & ~geometry._shrink(kernel, max(2, int(unit * 0.22)))
    kh, kw = kernel.shape
    y0, x0 = int(y - kh // 2), int(x - kw // 2)
    mask[y0:y0 + kh, x0:x0 + kw] |= kernel


class TestScale:
    def test_measures_line_thickness_and_staff_space(self):
        mask, _ = staff_image(unit=20, thickness=2)
        thickness, unit = geometry.estimate_scale(mask)
        assert thickness == 2
        assert unit == pytest.approx(20, abs=1)

    def test_works_at_a_different_resolution(self):
        mask, _ = staff_image(unit=45, thickness=5, width=1400, height=500, top=120)
        thickness, unit = geometry.estimate_scale(mask)
        assert thickness == 5
        assert unit == pytest.approx(45, abs=1)

    def test_gives_up_on_a_blank_page(self):
        assert geometry.estimate_scale(np.zeros((100, 100), dtype=bool)) == (None, None)


class TestStaffFinding:
    def test_finds_a_staff_that_does_not_span_the_frame(self):
        """The failure the design plan actually hit: a row-coverage threshold needs the
        staff to cross the whole image, and a photo of a page has margins."""
        mask, lines = staff_image(margin=180, width=900)
        thickness, unit = geometry.estimate_scale(mask)
        staves = geometry.find_staves(mask, thickness, unit)
        assert len(staves) == 1
        assert staves[0]["lines"] == pytest.approx(lines, abs=1.5)

    def test_finds_two_staves_and_pairs_them(self):
        mask, _ = staff_image(height=420)
        lower, _ = staff_image(height=420, top=260)
        mask |= lower
        mask[140:265, 45:48] = True          # a barline joining the two, as a brace would
        thickness, unit = geometry.estimate_scale(mask)
        staves = geometry.find_staves(mask, thickness, unit)
        assert len(staves) == 2
        assert geometry.group_systems(staves, mask, unit) == [[0, 1]]

    def test_separates_two_systems_by_the_gap(self):
        mask, _ = staff_image(height=700)
        second, _ = staff_image(height=700, top=460)
        mask |= second
        thickness, unit = geometry.estimate_scale(mask)
        staves = geometry.find_staves(mask, thickness, unit)
        assert geometry.group_systems(staves, mask, unit) == [[0], [1]]


class TestNoteheads:
    def _read(self, filled=True, unit=20):
        mask, lines = staff_image(unit=unit)
        staff = {"lines": lines, "unit": float(unit), "x_range": [40, 560]}
        for index, x in enumerate((200, 260, 320)):
            draw_notehead(mask, x, lines[4] - index * unit, unit, filled)
        thickness, measured = geometry.estimate_scale(mask)
        cleaned = geometry.remove_staff_lines(
            mask, geometry.staff_line_mask(mask, thickness, measured), thickness, measured)
        return geometry.detect_noteheads(mask, cleaned, staff, (0, mask.shape[0]),
                                         float(unit), thickness, 100)

    def test_finds_filled_noteheads_on_the_grid(self):
        notes = self._read(filled=True)
        assert [note["k"] for note in notes] == [0, 2, 4]
        assert all(not note["hollow"] for note in notes)

    def test_finds_hollow_noteheads_too(self):
        notes = self._read(filled=False)
        assert [note["k"] for note in notes] == [0, 2, 4]
        assert all(note["hollow"] for note in notes)

    def test_a_chord_of_thirds_is_three_noteheads_not_one_blob(self):
        """Stacked noteheads touch, so a connected-component reader sees one shape.
        Correlating an outline separates them, which is why it is used."""
        unit = 20
        mask, lines = staff_image(unit=unit)
        staff = {"lines": lines, "unit": float(unit), "x_range": [40, 560]}
        for index in range(3):
            draw_notehead(mask, 300, lines[4] - index * unit, unit)
        thickness, measured = geometry.estimate_scale(mask)
        cleaned = geometry.remove_staff_lines(
            mask, geometry.staff_line_mask(mask, thickness, measured), thickness, measured)
        notes = geometry.detect_noteheads(mask, cleaned, staff, (0, mask.shape[0]),
                                          float(unit), thickness, 100)
        assert sorted(note["k"] for note in notes) == [0, 2, 4]

    def test_a_note_below_the_staff_needs_its_ledger_line(self):
        unit = 20
        mask, lines = staff_image(unit=unit)
        staff = {"lines": lines, "unit": float(unit), "x_range": [40, 560]}
        below = lines[4] + unit          # first ledger position under the staff
        draw_notehead(mask, 300, below, unit)
        thickness, measured = geometry.estimate_scale(mask)

        def read(image):
            cleaned = geometry.remove_staff_lines(
                image, geometry.staff_line_mask(image, thickness, measured),
                thickness, measured)
            return geometry.detect_noteheads(image, cleaned, staff, (0, image.shape[0]),
                                             float(unit), thickness, 100)

        assert read(mask) == []          # no ledger drawn, so it is not a note
        with_ledger = mask.copy()
        with_ledger[int(below):int(below) + 2, 278:323] = True   # about 2 spaces wide
        assert [note["k"] for note in read(with_ledger)] == [-2]


class TestKeySignature:
    def _accidental(self, kind, x, y):
        return {"kind": kind, "x": float(x), "x0": float(x - 6), "x1": float(x + 6),
                "y": float(y), "h": 50}

    def test_reads_two_flats_as_b_flat_major(self):
        _mask, lines = staff_image(unit=20)
        staff = {"lines": lines, "unit": 20.0, "x_range": [40, 560]}
        b_flat = pitches.step_to_y(pitches.parse_name("B4")[0], lines, "treble")
        e_flat = pitches.step_to_y(pitches.parse_name("E5")[0], lines, "treble")
        run = [self._accidental("flat", 100, b_flat), self._accidental("flat", 122, e_flat)]
        fifths, confidence = geometry.read_key_signature(run, staff, "treble")
        assert fifths == -2
        assert confidence > 0.9
        assert pitches.key_alterations(fifths) == {"B": -1, "E": -1}

    def test_doubts_accidentals_that_are_not_in_signature_order(self):
        _mask, lines = staff_image(unit=20)
        staff = {"lines": lines, "unit": 20.0, "x_range": [40, 560]}
        wrong = [self._accidental("flat", 100,
                                  pitches.step_to_y(pitches.parse_name(name)[0],
                                                    lines, "treble"))
                 for name in ("G4", "A4")]
        _fifths, confidence = geometry.read_key_signature(wrong, staff, "treble")
        assert confidence < 0.6

    def test_run_stops_at_the_gap_before_the_first_note(self):
        run = geometry.key_signature_run(
            [self._accidental("flat", 100, 0), self._accidental("flat", 122, 0),
             self._accidental("sharp", 400, 0)], clef_end=60, unit=20)
        assert len(run) == 2

    def test_a_lone_natural_is_not_a_key_signature(self):
        assert geometry.key_signature_run(
            [self._accidental("natural", 70, 0)], clef_end=60, unit=20) == []


class TestPreprocess:
    def test_binarizes_the_middle_of_a_solid_notehead(self):
        """An adaptive window narrower than a filled notehead reads its centre as paper
        and punches a hole through it, which then leaks when holes are filled."""
        gray = np.full((400, 400), 250, dtype=np.uint8)
        gray[100:104, 40:360] = 0                 # a staff line, to set the scale
        gray[150:154, 40:360] = 0
        gray[118:160, 180:240] = 0                # a blob wider than a small window
        mask = preprocess.binarize(gray, unit_hint=50)
        assert mask[139, 210]

    def test_finds_the_skew_of_a_tilted_page(self):
        mask, _ = staff_image(unit=30, width=800, height=400, top=140)
        gray = np.where(mask, 0, 255).astype(np.uint8)
        from scipy import ndimage
        tilted = ndimage.rotate(gray, -2.5, reshape=False, order=1, cval=255)
        assert preprocess.estimate_skew(tilted) == pytest.approx(2.5, abs=0.6)
