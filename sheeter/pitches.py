"""Diatonic staff-position arithmetic and pitch spelling.

The whole pitch pipeline is built on one integer: the *diatonic step*, defined as
``octave * 7 + letter_index`` with ``C=0 D=1 E=2 F=3 G=4 A=5 B=6``.  Middle C (C4)
is step 28.  A step is a position on the staff, independent of any accidental --
which is exactly what a notehead's vertical position tells you.
"""

STEP_NAMES = "CDEFGAB"

#: Diatonic step of the *bottom* staff line for each clef.
#: treble bottom line = E4 = 4*7+2 = 30; bass bottom line = G2 = 2*7+4 = 18.
CLEF_BOTTOM_LINE_STEP = {
    "treble": 4 * 7 + 2,
    "bass": 2 * 7 + 4,
    "alto": 3 * 7 + 3,   # middle line C4; bottom line F3
    "tenor": 3 * 7 + 1,  # bottom line D3
}

#: Semitone offset of each letter above C.
_LETTER_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

#: Order in which sharps and flats appear in a key signature.
SHARP_ORDER = ["F", "C", "G", "D", "A", "E", "B"]
FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]

_PRETTY = {-2: "♭♭", -1: "♭", 0: "", 1: "♯", 2: "♯♯"}
_ASCII = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}


def step_letter(step):
    """Letter name (A-G) for a diatonic step."""
    return STEP_NAMES[step % 7]


def step_octave(step):
    """Scientific-pitch-notation octave for a diatonic step."""
    return step // 7


def step_to_name(step, alter=0):
    """``(30, -1) -> 'Eb4'``."""
    return "%s%s%d" % (step_letter(step), _ASCII[alter], step_octave(step))


def step_to_pretty(step, alter=0):
    """``(30, -1) -> 'E♭4'``."""
    return "%s%s%d" % (step_letter(step), _PRETTY[alter], step_octave(step))


def step_to_midi(step, alter=0):
    """MIDI note number.  C4 (step 28) with alter 0 is 60."""
    return (step_octave(step) + 1) * 12 + _LETTER_SEMITONES[step_letter(step)] + alter


def parse_name(name):
    """``'Bb4' -> (step, alter)``.  Accepts ``#``/``b``/``-`` and unicode accidentals."""
    letter = name[0].upper()
    if letter not in _LETTER_SEMITONES:
        raise ValueError("bad pitch name %r" % (name,))
    i = 1
    alter = 0
    while i < len(name) and name[i] in "#b-♯♭♮":
        ch = name[i]
        if ch in ("#", "♯"):
            alter += 1
        elif ch in ("b", "-", "♭"):
            alter -= 1
        i += 1
    octave = int(name[i:])
    return octave * 7 + STEP_NAMES.index(letter), alter


def to_music21(name):
    """``'Bb4' -> 'B-4'`` -- music21 spells flats with a hyphen."""
    step, alter = parse_name(name)
    return "%s%s%d" % (step_letter(step), "-" * -alter + "#" * alter, step_octave(step))


def from_music21(name):
    """``'B-4' -> 'Bb4'``."""
    step, alter = parse_name(name)
    return step_to_name(step, alter)


def key_alterations(fifths):
    """Letter -> alteration map implied by a key signature.

    ``key_alterations(-2) == {'B': -1, 'E': -1}``
    """
    if fifths > 0:
        return dict((L, 1) for L in SHARP_ORDER[:fifths])
    if fifths < 0:
        return dict((L, -1) for L in FLAT_ORDER[:-fifths])
    return {}


def key_name(fifths):
    """Human-readable major key, e.g. ``-2 -> 'B♭ major'``."""
    majors = {
        -7: "Cb", -6: "Gb", -5: "Db", -4: "Ab", -3: "Eb", -2: "Bb", -1: "F",
        0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#", 7: "C#",
    }
    root = majors.get(fifths, "C")
    return "%s major" % root.replace("b", "♭").replace("#", "♯")


def y_to_step(y, lines, clef):
    """Map a notehead's vertical centre to a diatonic step.

    ``lines`` is the five staff-line y values, top to bottom.  Returns
    ``(step, err_px)`` where ``err_px`` is how far the notehead sits off the
    half-space grid -- anything over ``0.3 * unit`` deserves a second look.
    """
    unit = (lines[-1] - lines[0]) / 4.0
    half = unit / 2.0
    pos = (lines[-1] - y) / half      # 0 = bottom line, 1 = space above it, ...
    k = int(round(pos))
    return CLEF_BOTTOM_LINE_STEP[clef] + k, abs(pos - k) * half


def step_to_y(step, lines, clef):
    """Inverse of :func:`y_to_step` -- the y a given step sits at."""
    unit = (lines[-1] - lines[0]) / 4.0
    k = step - CLEF_BOTTOM_LINE_STEP[clef]
    return lines[-1] - k * (unit / 2.0)


def ledger_count(step, clef):
    """Ledger lines a step needs: ``+n`` above the staff, ``-n`` below, 0 for none.

    The staff runs from ``k == 0`` (the bottom line) to ``k == 8`` (the top line), and
    the space immediately outside either end still needs no ledger, so nothing is drawn
    until ``k`` reaches 10 or -2.  A note in a space beyond that sits above or below the
    last ledger line it needs, not on one of its own.
    """
    k = step - CLEF_BOTTOM_LINE_STEP[clef]   # half-space position above the bottom line
    if k > 9:
        return (k - 8) // 2
    if k < -1:
        return -((-k) // 2)
    return 0


def make_note(step, alter, x, y, **kw):
    """Build a schema-conformant note dict from a step and alteration."""
    note = {
        "step": step_letter(step),
        "alter": int(alter),
        "octave": step_octave(step),
        "name": step_to_name(step, alter),
        "pretty": step_to_pretty(step, alter),
        "midi": step_to_midi(step, alter),
        "x": float(x),
        "y": float(y),
        "w": None,
        "h": None,
        "hollow": None,
        "accidental": None,
        "from_key": False,
        "ledger": 0,
        "step_err_px": 0.0,
        "confidence": 1.0,
        "changed": False,
    }
    note.update(kw)
    return note
