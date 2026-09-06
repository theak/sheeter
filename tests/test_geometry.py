"""Unit tests for the measuring parts of the reader.

These build their own images rather than leaning on the fixture corpus, so they run in
milliseconds and pin down the primitives the whole pipeline is balanced on.
"""

import os

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


class TestClef:
    """A misread clef is the worst failure this reader has.

    Nothing looks wrong: the noteheads are found, the chord is named, the overlay lines
    up. Every pitch on that staff is just a twelfth out, because the treble and bass
    reference lines are twelve diatonic steps apart. It is silent and total, so the
    detector has to be robust to the things a real page does to a glyph.
    """

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "grand_2flats.png")

    def _staves(self, mask):
        thickness, unit = geometry.estimate_scale(mask)
        staves = geometry.find_staves(mask, thickness, unit)
        lines = geometry.staff_line_mask(mask, thickness, unit, 2.0)
        cleaned = geometry.remove_staff_lines(mask, lines, thickness, unit)
        return staves, cleaned, unit, mask.shape[0]

    @pytest.fixture
    def page(self):
        from sheeter import preprocess as pre

        if not os.path.isfile(self.FIXTURE):
            pytest.skip("run tools/make_fixtures.py first")
        with open(self.FIXTURE, "rb") as handle:
            _gray, mask, _info = pre.prepare(handle.read())
        return mask

    def _read(self, mask):
        staves, cleaned, unit, height = self._staves(mask)
        out = []
        for index in range(len(staves)):
            band = geometry.staff_band(staves, index, unit, height)
            out.append(geometry.detect_clef(cleaned, staves[index], band, unit)[0])
        return out

    def test_reads_a_grand_staff(self, page):
        assert self._read(page) == ["treble", "bass"]

    def test_a_blotted_bass_clef_is_still_a_bass_clef(self, page):
        """The real one. A photocopied book page thickens every stroke, and a bass clef
        fattened that way measured 5.47 staff spaces tall. The detector used to call
        anything over 4 a treble clef, so the whole left hand came out a twelfth high
        while looking perfectly plausible."""
        from scipy import ndimage

        staves, _cleaned, unit, _height = self._staves(page)
        lower = staves[1]
        left = lower["x_range"][0]
        box = (slice(int(lower["lines"][0] - unit), int(lower["lines"][-1] + unit)),
               slice(max(0, left - int(unit)), left + int(unit * 3)))
        blotted = page.copy()
        blotted[box] = ndimage.binary_dilation(blotted[box], structure=np.ones((5, 5)))
        assert self._read(blotted) == ["treble", "bass"]

    def test_a_barline_welded_to_the_clef_does_not_change_it(self, page):
        """Braces, opening barlines and stems are stripped before the glyph is measured.
        Left in, they join it into one component spanning the whole band."""
        staves, _cleaned, unit, _height = self._staves(page)
        upper, lower = staves[0], staves[1]
        left = lower["x_range"][0]
        welded = page.copy()
        welded[int(upper["lines"][0]):int(lower["lines"][-1] + unit * 2),
               max(0, left - 2):left + int(unit * 0.5)] = True
        assert self._read(welded) == ["treble", "bass"]

    def test_a_long_stem_below_the_staff_is_not_a_clef(self, page):
        staves, _cleaned, unit, _height = self._staves(page)
        lower = staves[1]
        left = lower["x_range"][0]
        stemmed = page.copy()
        bottom = int(lower["lines"][-1])
        stemmed[bottom:bottom + int(unit * 5),
                left + int(unit * 5):left + int(unit * 5) + 3] = True
        assert self._read(stemmed) == ["treble", "bass"]


class TestGlyphColumns:
    """Measuring an accidental's width past the staff lines that crossed it.

    Removing the staff lines leaves a whisker of line either side of anything it
    crossed.  The bounding box counts it, and on a real page that took sharps from
    1.00 staff spaces wide to 1.55, past the width test, so a chord lost every
    accidental in it.
    """

    STROKES = ((4, 8), (16, 20))        # a sharp's two verticals, as (start, stop)

    @classmethod
    def sharp(cls, height=69, whisker=0):
        """Two vertical strokes, optionally with a line remnant sticking out each side.

        The remnant is one pixel tall, which is what staff line removal actually leaves
        behind on the page this came from.
        """
        width = cls.STROKES[-1][1] + whisker * 2
        mask = np.zeros((height, width), dtype=bool)
        for begin, end in cls.STROKES:
            mask[4:height - 4, whisker + begin:whisker + end] = True
        if whisker:
            mask[height // 2, :] = True
        return mask

    def test_ignores_the_line_remnant(self):
        whisker = 8
        first, last = geometry._glyph_columns(self.sharp(whisker=whisker), 4)
        assert (first, last) == (whisker + self.STROKES[0][0],
                                 whisker + self.STROKES[-1][1] - 1)

    def test_a_bare_glyph_measures_the_same(self):
        """Trimming must be a no-op when there is nothing to trim."""
        first, last = geometry._glyph_columns(self.sharp(), 4)
        assert (first, last) == (self.STROKES[0][0], self.STROKES[-1][1] - 1)

    def test_does_not_eat_a_glyph_on_a_high_resolution_render(self):
        """Why the cut follows line thickness and not a share of the height.

        At 0.15 of the height this read 24 pixels on a 158 pixel tall glyph, chewed
        most of it away, and turned an empty key signature into one sharp.  The thin
        but real part here holds 10 pixels: well under that cut and well over the
        thickness, so only the thickness rule keeps it.
        """
        tall = np.zeros((158, 40), dtype=bool)
        tall[:, 0:4] = True                      # a full height stroke
        tall[0:10, 4:36] = True                  # thin, real, and only 10 pixels deep
        tall[:, 36:40] = True
        assert geometry._glyph_columns(tall, 4) == (0, 39)

    def test_all_remnant_and_no_glyph_measures_nothing(self):
        only_line = np.zeros((60, 30), dtype=bool)
        only_line[30, :] = True
        assert geometry._glyph_columns(only_line, 4) is None


class TestAccidentalShape:
    """What makes something an accidental beyond its size.

    Every accidental is a closed shape that encloses background: the square of a sharp,
    the bowl of a flat.  A stem fragment the right height is not, and two of them were
    read as sharps on one page, one of them then as a G major key signature.
    """

    @staticmethod
    def ring(h=60, w=20, wall=4):
        out = np.zeros((h, w), dtype=bool)
        out[:, :] = True
        out[wall:h - wall, wall:w - wall] = False
        return out

    def test_a_closed_shape_has_a_hole(self):
        assert geometry._holes(self.ring()) == 1

    def test_a_filled_shape_has_none(self):
        assert geometry._holes(np.ones((60, 20), dtype=bool)) == 0

    def test_a_ring_cut_open_has_none(self):
        """A hollow notehead cut down the middle is a C, which is why a chord of whole
        notes no longer passes as two accidentals."""
        half = self.ring()[:, :10]
        assert geometry._holes(half) == 0

    def test_sized_but_open_is_not_an_accidental(self):
        unit = 22.0
        stem = np.zeros((int(unit * 3), int(unit * 0.9)), dtype=bool)
        stem[:, 8:12] = True                             # a bare vertical stroke
        comp = {"h": stem.shape[0], "w": stem.shape[1], "patch": stem}
        assert geometry._accidental_sized(comp, unit)
        assert not geometry._whole_accidental(comp, unit)

    def test_sized_and_closed_is(self):
        unit = 22.0
        glyph = self.ring(h=int(unit * 3), w=int(unit * 0.9))
        comp = {"h": glyph.shape[0], "w": glyph.shape[1], "patch": glyph}
        assert geometry._whole_accidental(comp, unit)


class TestStemInATimeSignature:
    """A time signature never has a stem in it.

    The fallback for a fused time signature is a single tall glyph centred on the middle
    line.  A note on the middle line with its stem up is exactly that, and on one page
    it was the first left-hand note, which the notehead search then started past.
    """

    def test_a_stem_runs_most_of_the_height(self):
        glyph = np.zeros((90, 30), dtype=bool)
        glyph[0:80, 14:18] = True                       # 89% of the height, 4 wide
        glyph[70:90, 4:28] = True                       # a notehead at the bottom
        assert geometry._has_stem(glyph, thickness=4)

    def test_a_digits_upright_does_not(self):
        """The upright of a 4 is about half of a fused pair's height."""
        pair = np.zeros((90, 30), dtype=bool)
        pair[2:42, 14:18] = True                        # upper 4's upright, 44%
        pair[48:88, 14:18] = True                       # lower 4's upright
        pair[20:26, 2:28] = True                        # a crossbar each
        pair[66:72, 2:28] = True
        assert not geometry._has_stem(pair, thickness=4)

    def test_a_nicked_stem_still_counts(self):
        """Staff line removal can leave a one-line gap in a stem; that is not two glyphs."""
        glyph = np.zeros((90, 30), dtype=bool)
        glyph[0:80, 14:18] = True
        glyph[40:43, 14:18] = False                     # a nick no wider than a line
        glyph[70:90, 4:28] = True
        assert geometry._has_stem(glyph, thickness=4)

    def test_a_wide_column_is_not_a_stem(self):
        block = np.ones((90, 30), dtype=bool)           # solid, every column is tall
        assert not geometry._has_stem(block, thickness=4)
