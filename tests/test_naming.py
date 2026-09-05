"""Tests for sheeter.naming.

Run from the repo root so that ``sheeter`` is importable:
``/home/user/.venv/bin/python -m pytest tests/test_naming.py``
"""

import random

import pytest

from sheeter import naming, pitches, schema


# Every symbol the plan names, plus the smaller cases from the brief.
REFERENCE = [
    (["Bb3", "F4", "C5", "D5", "F5", "A5"], "Bbmaj9"),
    (["Eb3", "Bb3", "G4", "C5", "F5"], "Eb6/9"),
    (["G2", "F3", "F4", "Bb4", "C5"], "Gm11"),
    (["G3", "B3", "D4", "F#4", "A4"], "Gmaj9"),
    (["F2", "Eb3", "A3", "D4", "F#4"], "F7(b9,13)"),
    (["Bb2", "Db3", "G3", "C4", "F4"], "Bbm6/9"),

    (["C4", "E4", "G4"], "C"),
    (["C4", "Eb4", "G4"], "Cm"),
    (["C4", "Eb4", "Gb4"], "Cdim"),
    (["C4", "E4", "G#4"], "Caug"),

    (["C4", "E4", "G4", "Bb4"], "C7"),
    (["C4", "E4", "G4", "B4"], "Cmaj7"),
    (["C4", "Eb4", "G4", "Bb4"], "Cm7"),
    (["C4", "Eb4", "Gb4", "Bbb4"], "Cdim7"),
    (["C4", "Eb4", "Gb4", "Bb4"], "Cm7b5"),

    (["E4", "G4", "C5"], "C/E"),

    (["C4", "G4"], "C5"),
    (["C4", "E4"], "C+M3"),

    (["C4", "E4", "G4", "C5"], "C"),
]


@pytest.mark.parametrize("names,symbol", REFERENCE)
def test_reference_symbols(names, symbol):
    assert naming.name_chord(names)["symbol"] == symbol


@pytest.mark.parametrize("names,symbol", REFERENCE)
def test_prev_names_only_breaks_ties(names, symbol):
    """prev_names must never change a reading that is already unambiguous."""
    for prev in (None, ["C4", "E4", "G4"], ["F#2", "A#3", "C#4"], []):
        assert naming.name_chord(names, prev_names=prev)["symbol"] == symbol


def test_more_symbols():
    cases = [
        (["C4", "D4", "G4"], "Csus2"),
        (["C4", "F4", "G4"], "Csus4"),
        (["C4", "F4", "G4", "Bb4"], "C7sus4"),
        (["C4", "E4", "G4", "D5"], "Cadd9"),
        (["C4", "E4", "G4", "A4"], "C6"),
        (["C4", "Eb4", "G4", "A4"], "Cm6"),
        (["C4", "E4", "G4", "Bb4", "D5"], "C9"),
        (["C4", "E4", "G4", "Bb4", "D5", "A5"], "C13"),
        (["C4", "Eb4", "G4", "Bb4", "D5"], "Cm9"),
        (["C4", "E4", "Bb4", "Eb5"], "C7(#9)"),
        (["C4", "E4", "G#4", "Bb4"], "C7#5"),
        (["C4", "E4", "Gb4", "Bb4"], "C7b5"),
        (["C4", "Eb4", "G4", "B4"], "CmMaj7"),
        (["G3", "C4", "E4"], "C/G"),
        (["Bb3", "C4", "E4", "G4"], "C7/Bb"),   # third inversion, not a slash triad
    ]
    for names, symbol in cases:
        assert naming.name_chord(names)["symbol"] == symbol, names


def test_octave_duplication_never_changes_the_symbol():
    random.seed(11)
    for _ in range(300):
        notes = random.sample(range(20, 60), random.randint(2, 5))
        names = [pitches.step_to_name(s, random.randint(-1, 1)) for s in notes]
        base = naming.name_chord(sorted(names, key=_midi))
        if base is None:
            continue
        doubled = names + [_up_an_octave(n) for n in random.sample(names, 2)]
        got = naming.name_chord(sorted(doubled, key=_midi))
        assert got["symbol"] == base["symbol"], names


def _midi(name):
    step, alter = pitches.parse_name(name)
    return pitches.step_to_midi(step, alter)


def _up_an_octave(name):
    step, alter = pitches.parse_name(name)
    return pitches.step_to_name(step + 7, alter)


def test_none_for_fewer_than_two_pitch_classes():
    for names in ([], ["D5"], ["C4", "C5"], ["C4", "C4", "C6"]):
        assert naming.name_chord(names) is None
        assert naming.name_combined(names) is None


def test_dyads_report_their_interval():
    assert naming.name_chord(["C4", "E4"])["symbol"] == "C+M3"
    assert naming.name_chord(["C4", "Eb4"])["symbol"] == "C+m3"
    assert naming.name_chord(["E4", "C5"])["symbol"] == "E+m6"
    assert naming.name_chord(["C4", "G4"])["symbol"] == "C5"
    assert naming.name_chord(["C4", "G5"])["symbol"] == "C5"      # compound fifth
    assert naming.name_chord(["C4", "E5"])["symbol"] == "C+M3"    # compound third


def test_cluster_falls_back_to_a_pitch_list():
    symbol = naming.name_chord(["C4", "Db4", "D4", "Eb4"])["symbol"]
    assert symbol == "C/Db/D/Eb"
    dense = naming.name_chord(["C4", "D4", "E4", "F4", "G4", "A4", "B4", "C#5"])
    assert "/" in dense["symbol"] and dense["root"] is None


def test_chord_dict_matches_the_schema_example():
    chord = naming.name_chord(["Bb3", "F4", "C5", "D5", "F5", "A5"], key_fifths=-2)
    assert chord["root"] == "Bb"
    assert chord["bass"] == "Bb"
    assert chord["quality"] == "major"
    assert chord["intervals_from_bass"] == ["P1", "P5", "M9", "M3", "P5", "M7"]
    assert chord["common_name"]


def test_combined_carries_names_pretty_and_roman():
    combined = naming.name_combined(["Bb3", "F4", "C5", "D5", "F5", "A5"], -2)
    assert combined["names"] == ["Bb3", "F4", "C5", "D5", "F5", "A5"]
    assert combined["pretty"] == ["B♭3", "F4", "C5", "D5", "F5", "A5"]
    assert combined["symbol"] == "Bbmaj9"
    assert combined["roman"] == "I"


def test_names_are_normalised_to_the_schema_spelling():
    combined = naming.name_combined(["B-3", "D4", "F4"])   # music21 spelling in
    assert combined["names"] == ["Bb3", "D4", "F4"]
    assert combined["pretty"][0] == "B♭3"


ROMAN_CASES = [
    (0, ["C4", "E4", "G4"], "I"),
    (0, ["F3", "A3", "C4"], "IV"),
    (0, ["G3", "B3", "D4"], "V"),
    (0, ["A3", "C4", "E4"], "vi"),
    (0, ["B3", "D4", "F4"], "viio"),
    (0, ["C4", "E4", "G4", "B4"], "I"),
    (2, ["D4", "F#4", "A4"], "I"),
    (2, ["G3", "B3", "D4"], "IV"),
    (2, ["A3", "C#4", "E4"], "V"),
    (2, ["B3", "D4", "F#4"], "vi"),
    (-2, ["Bb3", "D4", "F4"], "I"),
    (-2, ["Eb4", "G4", "Bb4"], "IV"),
    (-2, ["F3", "A3", "C4"], "V"),
    (-2, ["G3", "Bb3", "D4"], "vi"),
]


@pytest.mark.parametrize("fifths,names,roman", ROMAN_CASES)
def test_roman_numerals(fifths, names, roman):
    assert naming.name_combined(names, fifths)["roman"] == roman


def test_roman_is_none_outside_the_key():
    assert naming.name_combined(["F#3", "A3", "C4"], 0)["roman"] is None
    assert naming.name_combined(["C4", "E4", "G4"], 99)["roman"] is None
    assert naming.name_chord(["C4", "Db4", "D4", "Eb4"])["root"] is None


def test_roman_needs_a_third():
    """Diatonic is not enough: a numeral on a sus chord or a dyad says nothing."""
    for names in (["C4", "F4", "G4"], ["C4", "F4", "G4", "D5"], ["C4", "G4"]):
        assert naming.name_combined(names, 0)["roman"] is None


def test_two_digit_octaves_do_not_corrupt_the_root():
    chord = naming.name_chord(["C10", "E10", "G10"])
    assert (chord["root"], chord["bass"], chord["symbol"]) == ("C", "C", "C")


INTERVALS = [
    ("Bb3", "C4", "M2"),
    ("C4", "C4", "P1"),
    ("C4", "E4", "M3"),
    ("C4", "Eb4", "m3"),
    ("C4", "F4", "P4"),
    ("C4", "F#4", "A4"),
    ("C4", "Gb4", "d5"),
    ("C4", "G4", "P5"),
    ("C4", "B4", "M7"),
    ("C4", "Bb4", "m7"),
    ("C4", "C5", "P8"),
    ("C4", "D5", "M9"),
    ("C4", "G5", "P12"),
    ("G4", "F4", "M2"),          # direction does not matter
    ("F#4", "G4", "m2"),
]


@pytest.mark.parametrize("a,b,name", INTERVALS)
def test_interval_name(a, b, name):
    assert naming.interval_name(a, b) == name


def test_interval_name_is_symmetric():
    assert naming.interval_name("C4", "A4") == naming.interval_name("A4", "C4")


def test_result_keys_match_the_schema():
    """validate_analysis rejects missing and extra keys, so check every path."""
    samples = [names for names, _ in REFERENCE]
    samples += [["C4", "Db4", "D4", "Eb4"], ["C4", "E4"], ["B-3", "D4", "F4"]]
    for names in samples:
        chord = naming.name_chord(names)
        assert set(chord) == set(schema.CHORD_KEYS), names
        assert len(chord["intervals_from_bass"]) == len(names)
        combined = naming.name_combined(names)
        assert set(combined) == set(schema.COMBINED_KEYS), names
        assert len(combined["names"]) == len(combined["pretty"]) == len(names)


def test_fuzz_never_throws():
    random.seed(3)
    for _ in range(2000):
        names = []
        for _ in range(random.randint(0, 8)):
            step = random.randint(7, 63)
            names.append(pitches.step_to_name(step, random.randint(-2, 2)))
        fifths = random.randint(-9, 9)
        chord = naming.name_chord(names, fifths, random.choice([None, ["C4", "G4"]]))
        combined = naming.name_combined(names, fifths)
        if chord is not None:
            assert set(chord) == set(schema.CHORD_KEYS)
            assert chord["symbol"]
        if combined is not None:
            assert set(combined) == set(schema.COMBINED_KEYS)


def test_garbage_input_never_throws():
    for junk in (None, 123, "C4E4", ["", "x", "H4", "C"], [None, 4], ["C4", None],
                 {"C4": 1}):
        assert naming.name_chord(junk) is None
        assert naming.name_combined(junk) is None
    # unreadable entries are dropped, the readable ones are still named
    assert naming.name_chord(["C4", "E4", object()])["symbol"] == "C+M3"


def test_fuzz_clusters():
    """Draw crowded, chromatic sets so the fallback path is the common case."""
    random.seed(5)
    fell_back = 0
    for _ in range(1000):
        bottom = random.randint(20, 55)
        steps = random.sample(range(bottom, bottom + 6), random.randint(3, 6))
        names = [pitches.step_to_name(s, random.randint(-1, 1)) for s in sorted(steps)]
        chord = naming.name_chord(names)
        if chord is None:
            continue
        assert set(chord) == set(schema.CHORD_KEYS), names
        assert chord["symbol"], names
        if chord["root"] is None:
            fell_back += 1
            assert "/" in chord["symbol"], names
            assert naming.name_combined(names)["roman"] is None
    assert fell_back > 20, "the fallback path is barely being exercised"


def test_symbol_never_silently_drops_a_note():
    """Every chord tone the reading found has to show up in the symbol.

    Walks every interval set on a C root, which is how the sixth added to an
    altered fifth (Caug(6)) was caught going missing.
    """
    from itertools import combinations

    for size in range(2, 6):
        for rest in combinations(range(1, 12), size):
            intervals = set((0,) + rest)
            reading = naming._read(0, intervals)
            symbol = naming._symbol("C", reading, "C")
            tail = symbol[1:]
            if reading["third"] == "m":
                assert "m" in tail or "dim" in tail, symbol
            if reading["fifth"] == "d":
                assert "b5" in tail or "dim" in tail, symbol
            if reading["fifth"] == "A":
                assert "#5" in tail or "aug" in tail, symbol
            if reading["sixth"]:
                assert "6" in tail, symbol
            if reading["sus"]:
                assert "sus" in tail, symbol
            if reading["seventh"] == "M":
                assert "maj" in tail.lower(), symbol
            for tension in reading["tensions"]:
                label = naming._TENSION_LABELS[tension]
                if tension in (2, 5, 9):
                    # a folded extension implies the natural tensions below it,
                    # which is why Cm11 is written for a chord with a 9th in it
                    assert any(e in tail for e in ("9", "11", "13")), symbol
                else:
                    assert label in tail, (symbol, label)
