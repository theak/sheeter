"""Little pictures of the fifteen key signatures, for picking one off the page.

Reading the key off a photo is the one mistake that spoils a whole page: the key
decides the alteration of every notehead that has no accidental of its own, so one
misread glyph respells everything after it.  A person with the page in front of them
can see the signature in a second, and matching a picture is quicker and surer than
counting sharps and naming a key.  So the picker shows all fifteen as pictures.

The drawings are generated here rather than in JavaScript so the picker works with
scripting off, and so the positions can be tested rather than eyeballed.

There is no clef in them.  A treble clef is either a large hand-drawn path or a font
glyph, and U+1D11E renders as a missing-glyph box on plenty of systems, which would
be worse than nothing.  The accidentals are drawn where they are printed, which is
what the eye matches on, and each picture carries its name beside it.
"""

#: Where each accidental of a signature sits, in staff spaces below the top line,
#: for a treble staff.  Half a space is one step, so the five lines are 0 to 4 and
#: the spaces are the halves between them.  The orders are pitches.SHARP_ORDER and
#: pitches.FLAT_ORDER, so index i here is the i'th accidental of the signature.
#: The third sharp, G#5, is the one above the staff.
SHARP_ROWS = (0.0, 1.5, -0.5, 1.0, 2.5, 0.5, 2.0)
FLAT_ROWS = (2.0, 0.5, 2.5, 1.0, 3.0, 1.5, 3.5)

MAJORS = {
    -7: "C♭", -6: "G♭", -5: "D♭", -4: "A♭", -3: "E♭",
    -2: "B♭", -1: "F", 0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B",
    6: "F♯", 7: "C♯",
}
MINORS = {
    -7: "A♭", -6: "E♭", -5: "B♭", -4: "F", -3: "C", -2: "G",
    -1: "D", 0: "A", 1: "E", 2: "B", 3: "F♯", 4: "C♯", 5: "G♯",
    6: "D♯", 7: "A♯",
}

#: Every key, flats first, the way the chart on a practice room wall runs.
FIFTHS = tuple(range(-7, 8))

#: One staff space, in the SVG's own units.  Everything else is derived, so the
#: picture scales by changing this and nothing else.
SPACE = 5

#: Room above the top line for the third sharp and below the bottom for the seventh
#: flat, both of which sit outside the five lines.
PAD_TOP = 3 * SPACE
PAD_BOTTOM = 1.5 * SPACE
STAFF_WIDTH = 5 * SPACE
STEP_X = 2.2 * SPACE


def positions(fifths):
    """``[(glyph, row), ...]`` for a signature, in the order it is written.

    *row* is in staff spaces below the top line, so 0 is the top line, 0.5 the space
    under it, and a negative row is above the staff.
    """
    if not -7 <= int(fifths) <= 7:
        raise ValueError("key signature must be -7..7 fifths, got %r" % (fifths,))
    fifths = int(fifths)
    if fifths > 0:
        return [("♯", SHARP_ROWS[i]) for i in range(fifths)]
    return [("♭", FLAT_ROWS[i]) for i in range(-fifths)]


def label(fifths):
    """``-2`` -> ``'B♭ major / G minor'``.

    Both names, because which one a page is in is a question about the music and not
    about the signature, and a reader matching a picture should not have to care.
    """
    return "%s major / %s minor" % (MAJORS[int(fifths)], MINORS[int(fifths)])


def count(fifths):
    """``-2`` -> ``'2 flats'``, for the reader who counts rather than matches."""
    fifths = int(fifths)
    if fifths == 0:
        return "no sharps or flats"
    kind = "sharp" if fifths > 0 else "flat"
    return "%d %s%s" % (abs(fifths), kind, "" if abs(fifths) == 1 else "s")


def svg(fifths, describe=True):
    """A five line staff with the signature written on it, as an ``<svg>`` string.

    The colour comes from ``currentColor``, so the picture follows the text around it
    into whatever theme the browser is in.
    """
    marks = positions(fifths)
    width = STAFF_WIDTH + STEP_X * max(len(marks), 1) + SPACE
    height = PAD_TOP + 4 * SPACE + PAD_BOTTOM
    out = ['<svg class="keysig" viewBox="0 0 %g %g" width="%g" height="%g" '
           'fill="none" stroke="currentColor" stroke-width="0.7" %s>'
           % (width, height, width, height,
              'role="img" aria-label="%s"' % count(fifths) if describe
              else 'aria-hidden="true"')]
    for line in range(5):
        y = PAD_TOP + line * SPACE
        out.append('<line x1="0" y1="%g" x2="%g" y2="%g"></line>' % (y, width, y))
    for i, (glyph, row) in enumerate(marks):
        # The glyph is centred on its staff position, and text hangs from a baseline,
        # so the dominant-baseline shift is what puts it on the line rather than under.
        x = STAFF_WIDTH + i * STEP_X
        y = PAD_TOP + row * SPACE
        out.append('<text x="%g" y="%g" font-size="%g" text-anchor="middle" '
                   'dominant-baseline="central" stroke="none" fill="currentColor">'
                   '%s</text>' % (x, y, 3.6 * SPACE, glyph))
    out.append('</svg>')
    return "".join(out)


def choices():
    """Every key, flats through sharps, as the picker's rows."""
    return [{"fifths": f, "label": label(f), "count": count(f), "svg": svg(f, False)}
            for f in FIFTHS]


def current(document):
    """The key this reading is in, as one number.

    A document keeps a key per staff, since a page could in principle be engraved
    with two, and the picker sets one for all of them.  The first staff of the first
    system is what it shows.
    """
    for system in document["systems"]:
        for staff in system["staves"]:
            return staff["key_fifths"]
    return document["key_override"] or 0
