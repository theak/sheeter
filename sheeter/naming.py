"""Chord naming: pitch lists in, the symbol a player would write out.

The centre of this module is :func:`name_chord`.  It takes the ascii pitch names
of one simultaneity, low to high, and returns the schema's chord dict.

Why we do not just ask music21 for the answer: music21's ``Chord.root()`` picks
the root that stacks in thirds most tidily, which is not the root a player reads.
On music21 10.5 it returns C for E-flat 6/9, D for F7(b9,13) and G for B-flat
m6/9, so three of the six reference chords in the plan would come out wrong, and
``Chord.quality`` inherits that root.  ``pitchedCommonName`` is still the best
one-line English description there is, so it fills ``common_name`` and nothing
else.

The symbol is built from the pitch class set relative to a chosen root.  Every
present pitch class is tried as the root; each reading is charged a cost for the
things that make it an unlikely reading (no third, a tension that only makes
sense over a dominant, a rare quality such as a minor triad on an augmented
fifth), and the cheapest reading wins.  The bass gets a 0.5 discount, which is
what makes C-Eb-G-Bb-F read as Cm11 over C and as Eb6/9 over Eb.

Nothing here raises.  Unreadable input yields None, and a chord too dense or too
chromatic to name yields a slash joined pitch list such as "C/Db/D/Eb".
"""

from . import pitches

#: Semitones above the root for each simple interval number.
_SEMI = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}

_PERFECT_QUALITIES = {0: "P", 1: "A", -1: "d", 2: "AA", -2: "dd"}
_MAJOR_QUALITIES = {0: "M", -1: "m", 1: "A", -2: "d", 2: "AA", -3: "dd"}

#: Label for a tension, keyed by its semitone distance above the root.
_TENSION_LABELS = {
    1: "b9", 2: "9", 3: "#9", 5: "11", 6: "#11", 8: "b13", 9: "13", 11: "maj7",
}

_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII"]
_MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11)

#: A reading costing more than this is not worth showing as a symbol.
_FALLBACK_COST = 2.6


# --------------------------------------------------------------------------
# pitch helpers
# --------------------------------------------------------------------------

def _ascii_name(step, alter):
    if -2 <= alter <= 2:
        return pitches.step_to_name(step, alter)
    acc = "#" * alter if alter > 0 else "b" * -alter
    return "%s%s%d" % (pitches.step_letter(step), acc, pitches.step_octave(step))


def _pretty_name(step, alter):
    if -2 <= alter <= 2:
        return pitches.step_to_pretty(step, alter)
    acc = "♯" * alter if alter > 0 else "♭" * -alter
    return "%s%s%d" % (pitches.step_letter(step), acc, pitches.step_octave(step))


def _pc_name(step, alter):
    """The name without an octave: ``(27, -1) -> 'Bb'``."""
    return _ascii_name(step, alter).rstrip("0123456789")


def _parse(names):
    """``['Bb3', ...] -> [(step, alter, midi, pc)]``, ascending, bad names dropped."""
    out = []
    try:
        items = list(names)
    except TypeError:
        return out
    for name in items:
        try:
            step, alter = pitches.parse_name(name)
            midi = pitches.step_to_midi(step, alter)
        except Exception:
            continue
        out.append((step, alter, midi, midi % 12))
    out.sort(key=lambda n: (n[2], n[0]))
    return out


def _quality_letter(simple_number, diff):
    table = _PERFECT_QUALITIES if simple_number in (1, 4, 5) else _MAJOR_QUALITIES
    if diff in table:
        return table[diff]
    if diff > 0:
        return "A" * min(diff, 4)
    return "d" * min(-diff, 4)


def _interval_parts(lo, hi):
    """``(octaves, simple_number, quality)`` between two ``(step, alter, midi, pc)``."""
    if hi[0] < lo[0] or (hi[0] == lo[0] and hi[2] < lo[2]):
        lo, hi = hi, lo
    d = hi[0] - lo[0]
    octaves, rem = divmod(d, 7)
    simple_number = rem + 1
    diff = (hi[2] - lo[2]) - 12 * octaves - _SEMI[simple_number]
    return octaves, simple_number, _quality_letter(simple_number, diff)


def interval_name(a, b):
    """Interval between two ascii pitch names, e.g. ``('Bb3', 'C4') -> 'M2'``.

    Direction independent (the smaller pitch is taken as the lower one) and
    compound intervals keep their compound number, so an octave is ``P8`` and a
    ninth is ``M9``.  Raises ValueError if either name cannot be parsed.
    """
    lo_step, lo_alter = pitches.parse_name(a)
    hi_step, hi_alter = pitches.parse_name(b)
    lo = (lo_step, lo_alter, pitches.step_to_midi(lo_step, lo_alter), 0)
    hi = (hi_step, hi_alter, pitches.step_to_midi(hi_step, hi_alter), 0)
    if (hi[2], hi[0]) < (lo[2], lo[0]):
        lo, hi = hi, lo
    octaves, simple_number, quality = _interval_parts(lo, hi)
    return "%s%d" % (quality, simple_number + 7 * octaves)


def _degree_interval(bass, note):
    """Interval from the bass expressed as the degree a player reads.

    Simple intervals keep their name; above the octave a 2nd, 4th or 6th reads
    as a 9th, 11th or 13th and everything else reduces to its simple form.  So
    over a B-flat 3 bass, C5 is M9, D5 is M3, F5 is P5 and A5 is M7, which is
    exactly the listing in docs/schema.md.
    """
    octaves, simple_number, quality = _interval_parts(bass, note)
    if octaves >= 1 and simple_number in (2, 4, 6):
        return "%s%d" % (quality, simple_number + 7)
    return "%s%d" % (quality, simple_number)


# --------------------------------------------------------------------------
# reading a pitch class set against a candidate root
# --------------------------------------------------------------------------

def _combo_cost(third, fifth, seventh):
    """How unusual this third/fifth/seventh combination is."""
    if third is None:
        return 1.2 if fifth in ("d", "A") else 0.0
    if third == "M":
        if fifth in ("P", None):
            return 0.0
        if fifth == "A":
            return {None: 0.2, "m": 0.3, "M": 0.5}.get(seventh, 0.5)
        return {None: 1.5, "m": 0.3, "M": 0.8}.get(seventh, 1.0)      # b5
    if fifth in ("P", None):
        return 0.3 if seventh == "M" else 0.0                          # mMaj7
    if fifth == "d":
        return 1.5 if seventh == "M" else 0.0                          # m7b5, dim
    return 1.5                                                         # m(#5)


def _tension_cost(semitone, third, dominant):
    if semitone in (1, 3):
        return 0.6 if dominant else 1.4                                # b9, #9
    if semitone == 2:
        return 0.1
    if semitone == 5:
        return 1.2 if third == "M" else 0.25                           # 11
    if semitone in (6, 8):
        return 0.8                                                     # #11, b13
    if semitone == 9:
        return 0.15                                                    # 13
    return 1.4


def _read(root_pc, pcs):
    """Read *pcs* as a chord on *root_pc*.  Returns the reading and its cost."""
    intervals = set((pc - root_pc) % 12 for pc in pcs)
    used = set([0])

    third = None
    if 4 in intervals:
        third = "M"
        used.add(4)
    elif 3 in intervals:
        third = "m"
        used.add(3)

    fifth = None
    for semitone, label in ((7, "P"), (6, "d"), (8, "A")):
        if semitone in intervals:
            fifth = label
            used.add(semitone)
            break

    seventh = None
    if 10 in intervals:
        seventh = "m"
        used.add(10)
    elif 11 in intervals:
        seventh = "M"
        used.add(11)
    elif third == "m" and fifth == "d" and 9 in intervals:
        seventh = "dd"                                                 # dim7
        used.add(9)

    sixth = seventh is None and 9 in intervals
    if sixth:
        used.add(9)

    sus = None
    if third is None:
        if 5 in intervals:
            sus = "4"
            used.add(5)
        elif 2 in intervals:
            sus = "2"
            used.add(2)

    tensions = sorted(intervals - used)

    cost = _combo_cost(third, fifth, seventh)
    if third is None:
        cost += {"4": 0.9, "2": 1.0}.get(sus, 1.4)
    if fifth is None:
        cost += 0.4
    if sixth:
        cost += 0.1
    dominant = third == "M" and seventh == "m"
    for semitone in tensions:
        cost += _tension_cost(semitone, third, dominant)

    return {
        "root_pc": root_pc,
        "third": third,
        "fifth": fifth,
        "seventh": seventh,
        "sixth": sixth,
        "sus": sus,
        "tensions": tensions,
        "cost": cost,
    }


def _quality(reading):
    third, fifth = reading["third"], reading["fifth"]
    if third == "M":
        if fifth in ("P", None):
            return "major"
        if fifth == "A":
            return "augmented"
    if third == "m":
        if fifth in ("P", None):
            return "minor"
        if fifth == "d":
            return "diminished"
    return "other"


# --------------------------------------------------------------------------
# turning a reading into a symbol
# --------------------------------------------------------------------------

def _core(reading):
    """The quality part of the symbol, the tensions it did not absorb, and any
    extra labels (an altered fifth on a chord with no third) to list with them."""
    third = reading["third"]
    fifth = reading["fifth"]
    seventh = reading["seventh"]
    tensions = list(reading["tensions"])

    # An altered fifth is part of the quality name, so a sixth on top of one has
    # nowhere to go in the core and is listed as a label instead.
    sixth = ["6"] if reading["sixth"] else []
    if third == "m" and fifth == "d":
        core = {"m": "m7b5", "dd": "dim7", "M": "mMaj7b5"}.get(seventh, "dim")
        return core, tensions, sixth
    if third == "M" and fifth == "A":
        core = {"m": "7#5", "M": "maj7#5"}.get(seventh, "aug")
        return core, tensions, sixth
    if third == "M" and fifth == "d":
        if seventh in ("m", "M"):
            return {"m": "7b5", "M": "maj7b5"}[seventh], tensions, []
        return "", tensions, ["b5"] + sixth

    if third is None:
        extra = {"d": ["b5"], "A": ["#5"]}.get(fifth, [])
        suffix = {"4": "sus4", "2": "sus2"}.get(reading["sus"], "")
        if seventh == "m":
            core = "7" + suffix
        elif seventh == "M":
            core = "maj7" + suffix
        elif reading["sixth"]:
            core = "6" + suffix
        elif suffix:
            core = suffix
        else:
            core = "5"                                                 # power chord
        return core, tensions, extra

    prefix = "m" if third == "m" else ""
    if seventh == "m":
        core = prefix + "7"
    elif seventh == "M":
        core = "mMaj7" if third == "m" else "maj7"
    elif reading["sixth"]:
        core = prefix + "6"
        if 2 in tensions:
            core = prefix + "6/9"
            tensions.remove(2)
    else:
        core = prefix
        if 2 in tensions:
            core = prefix + "add9"
            tensions.remove(2)
    if third == "m" and fifth == "A":
        core += "#5"
    return core, tensions, []


def _fold_extension(core, tensions, reading):
    """Fold natural tensions into the name: maj7 + 9 -> maj9, m7 + 11 -> m11.

    Only when nothing is altered.  As soon as one tension is altered the symbol
    stays at the seventh and every tension is listed, which is how F7(b9,13) is
    written rather than F13(b9).
    """
    if reading["seventh"] not in ("m", "M") or reading["fifth"] not in ("P", None):
        return core, tensions
    naturals = [t for t in tensions if t in (2, 5, 9)]
    if not naturals or len(naturals) != len(tensions):
        return core, tensions
    ext = {2: "9", 5: "11", 9: "13"}[max(naturals)]
    for base, folded in (("mMaj7", "mMaj"), ("maj7", "maj"), ("m7", "m"), ("7", "")):
        if core.endswith(base):
            return core[: -len(base)] + folded + ext, []
    return core, tensions


def _symbol(root_name, reading, bass_name):
    core, tensions, extra = _core(reading)
    core, tensions = _fold_extension(core, tensions, reading)
    symbol = root_name + core
    labels = extra + [_TENSION_LABELS.get(t, "+%d" % t) for t in tensions]
    if labels:
        symbol += "(%s)" % ",".join(labels)
    if bass_name != root_name:
        symbol += "/" + bass_name
    return symbol


# --------------------------------------------------------------------------
# root choice
# --------------------------------------------------------------------------

def _spell_root(root_pc, notes, key_fifths):
    """Name the root, preferring how it is actually written in the chord."""
    for step, alter, _midi, pc in notes:
        if pc == root_pc and -1 <= alter <= 1:
            return _pc_name(step, alter)
    flat = key_fifths < 0
    sharp_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    flat_names = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
    return (flat_names if flat else sharp_names)[root_pc % 12]


def _choose(notes, pcs, bass_pc, prev_names):
    """Rank candidate roots.  Cheapest reading wins; the bass breaks near ties."""
    prev_pcs = set(n[3] for n in _parse(prev_names or []))
    fifth_related = set()
    for pc in prev_pcs:
        fifth_related.add((pc + 5) % 12)
        fifth_related.add((pc + 7) % 12)

    scored = []
    for root_pc in sorted(pcs):
        reading = _read(root_pc, pcs)
        total = reading["cost"] - (0.5 if root_pc == bass_pc else 0.0)
        scored.append((
            round(total, 6),
            0 if root_pc == bass_pc else 1,
            0 if root_pc in fifth_related else 1,
            root_pc,
            reading,
        ))
    scored.sort(key=lambda s: s[:4])
    return scored[0][4]


def _consecutive_run(pcs):
    """Longest run of adjacent semitones in the pitch class set."""
    best = run = 0
    for i in range(24):                       # two turns of the circle catch wrap
        if (i % 12) in pcs:
            run += 1
            best = max(best, min(run, len(pcs)))
        else:
            run = 0
    return best


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def _common_name(notes):
    try:
        from music21 import chord as m21chord
        names = [pitches.to_music21(_ascii_name(s, a)) for s, a, _m, _p in notes]
        return m21chord.Chord(names).pitchedCommonName
    except Exception:
        return None


def _chord_dict(symbol, common_name, root, bass, quality, notes):
    return {
        "symbol": symbol,
        "common_name": common_name,
        "root": root,
        "bass": bass,
        "quality": quality,
        "intervals_from_bass": [_degree_interval(notes[0], n) for n in notes],
    }


def _analyse(notes, key_fifths, prev_names):
    """Shared body of name_chord and name_combined.  Returns a chord dict."""
    pcs = set(n[3] for n in notes)
    bass_name = _pc_name(notes[0][0], notes[0][1])
    common_name = _common_name(notes)

    if len(pcs) == 2:
        lo, hi = notes[0], [n for n in notes if n[3] != notes[0][3]][0]
        _octaves, simple_number, quality = _interval_parts(lo, hi)
        name = "%s%d" % (quality, simple_number)
        if name == "P5":
            symbol = bass_name + "5"                                   # power chord
        else:
            symbol = "%s+%s" % (bass_name, name)
        return _chord_dict(symbol, common_name, bass_name, bass_name, "other", notes)

    reading = _choose(notes, pcs, notes[0][3], prev_names)
    if (reading["cost"] > _FALLBACK_COST or len(pcs) >= 8
            or _consecutive_run(pcs) >= 4):
        seen = []
        for step, alter, _midi, pc in notes:
            if pc not in [s[1] for s in seen]:
                seen.append((_pc_name(step, alter), pc))
        symbol = "/".join(name for name, _pc in seen)
        return _chord_dict(symbol, common_name, None, bass_name, "other", notes)

    root_name = _spell_root(reading["root_pc"], notes, key_fifths)
    return _chord_dict(
        _symbol(root_name, reading, bass_name), common_name,
        root_name, bass_name, _quality(reading), notes,
    )


def name_chord(names, key_fifths=0, prev_names=None):
    """Name one simultaneity.

    *names* are ascii pitch names low to high, e.g.
    ``['Bb3','F4','C5','D5','F5','A5']``.  Returns the schema's chord dict
    (symbol, common_name, root, bass, quality, intervals_from_bass), or None
    when there is nothing to name: an empty list, a single note, or a stack of
    octaves, all of which are one pitch class and get None so the caller can
    store null.  Octave doubling never changes the symbol.

    *key_fifths* only guides the spelling of a root that is not written in the
    chord itself.  *prev_names* is the previous event's pitches; it breaks exact
    ties towards a root a fifth away from the previous chord and does nothing
    else.  This never raises, whatever it is given.
    """
    try:
        notes = _parse(names)
        if len(set(n[3] for n in notes)) < 2:
            return None
        return _analyse(notes, key_fifths, prev_names)
    except Exception:
        return None


def name_combined(names, key_fifths=0):
    """Name the pooled pitches of both hands, with a roman numeral.

    Returns the schema's combined dict: the chord fields plus normalised
    ``names`` and ``pretty`` lists and ``roman``, the numeral relative to
    *key_fifths* read as a major key, or None when the chord does not sit in
    that key.  None overall for fewer than two distinct pitch classes.
    """
    try:
        notes = _parse(names)
        if len(set(n[3] for n in notes)) < 2:
            return None
        chord = _analyse(notes, key_fifths, None)
        combined = {
            "names": [_ascii_name(s, a) for s, a, _m, _p in notes],
            "pretty": [_pretty_name(s, a) for s, a, _m, _p in notes],
            "roman": _roman(chord, set(n[3] for n in notes), key_fifths),
        }
        combined.update(chord)
        return combined
    except Exception:
        return None


def _roman(chord, pcs, key_fifths):
    """The numeral, or None: the chord has to be in the key and have a third.

    A sus chord or a dyad is diatonic often enough, but "I" over C-F-G tells a
    player nothing true, so quality "other" gets no numeral.
    """
    if chord["root"] is None or chord["quality"] == "other":
        return None
    if not -7 <= key_fifths <= 7:
        return None
    tonic = (7 * key_fifths) % 12
    degrees = [(pc - tonic) % 12 for pc in pcs]
    if any(d not in _MAJOR_SCALE for d in degrees):
        return None
    try:
        root_step, root_alter = pitches.parse_name(chord["root"] + "4")
    except Exception:
        return None
    root_pc = pitches.step_to_midi(root_step, root_alter) % 12
    if (root_pc - tonic) % 12 not in _MAJOR_SCALE:
        return None
    numeral = _ROMAN[_MAJOR_SCALE.index((root_pc - tonic) % 12)]
    quality = chord["quality"]
    if quality in ("minor", "diminished"):
        numeral = numeral.lower()
    if quality == "diminished":
        numeral += "o"
    elif quality == "augmented":
        numeral += "+"
    return numeral
