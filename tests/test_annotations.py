"""Text on the page must not be read as music.

Real sheet music is covered in things that are not notes: chord symbols over the staff,
titles, fingerings, analysis scribbled between the staves.  Letters are about a staff
space tall and several of them are round, so once holes are filled they are the right
size and shape to score against a notehead template.  What keeps them out is the
combination of the grid test and the ledger line requirement, and this pins that down.

The excerpt is modelled on a jazz harmony textbook page: whole note voicings under
chord symbols, with three lines of prose sitting in the gap between the staves.
"""

import os

import pytest

from sheeter import pipeline

pytest.importorskip("verovio")
pytest.importorskip("cairosvg")

# (chord symbol, left hand, right hand), low to high, as written on the page.
VOICINGS = [
    ("B", ["B2", "D#3"], ["F#3", "A#3", "D#4"]),
    ("D7", ["D3", "A3"], ["F#3", "C4", "E4"]),
    ("G", ["G2", "B2"], ["D3", "F#3", "B3"]),
    ("B-7", ["Bb2", "Ab3"], ["D3", "F3", "Bb3"]),
    ("E-", ["Eb2", "G2"], ["Bb2", "D3", "G3"]),
]

ANNOTATIONS = ["descending major third", "John Coltrane's Giant Steps", "major 3rd"]


@pytest.fixture(scope="module")
def page():
    import cairosvg
    import verovio
    from music21 import chord, clef, expressions, harmony, key, meter, stream

    score = stream.Score()
    top = stream.Part()
    bottom = stream.Part()
    for part, which in ((top, clef.TrebleClef()), (bottom, clef.BassClef())):
        part.append(which)
        part.append(key.KeySignature(0))
        part.append(meter.TimeSignature("4/4"))
    for symbol, left, right in VOICINGS:
        label = harmony.ChordSymbol(symbol)
        label.writeAsChord = False
        top.append(label)
        top.append(chord.Chord(right, quarterLength=4))
        bottom.append(chord.Chord(left, quarterLength=4))
    for offset, text in zip((0, 4, 8), ANNOTATIONS):
        top.insert(offset, expressions.TextExpression(text))
    score.insert(0, top)
    score.insert(0, bottom)

    toolkit = verovio.toolkit()
    toolkit.setOptions({"pageWidth": 2600, "pageHeight": 900, "scale": 110,
                        "adjustPageHeight": True, "adjustPageWidth": True,
                        "footer": "none", "header": "none"})
    with open(score.write("musicxml")) as handle:
        assert toolkit.loadData(handle.read())
    return cairosvg.svg2png(bytestring=toolkit.renderToSVG(1).encode(),
                            background_color="white", scale=2.0)


@pytest.fixture(scope="module")
def doc(page):
    document, _png = pipeline.analyze_bytes(page, "annotated.png", "image/png")
    return document


def test_reads_one_grand_staff(doc):
    assert len(doc["systems"]) == 1
    staves = doc["systems"][0]["staves"]
    assert [(s["hand"], s["clef"]) for s in staves] == [("right", "treble"),
                                                        ("left", "bass")]


def test_prose_and_chord_symbols_do_not_become_events(doc):
    """Five chords are written, so five steps are read.  Three lines of prose sit
    between the staves and five chord symbols sit above them, and none of it counts."""
    assert len(doc["systems"][0]["events"]) == len(VOICINGS)


def test_every_event_has_the_right_number_of_noteheads(doc):
    counts = []
    for event in doc["systems"][0]["events"]:
        by_hand = dict((part["hand"], len(part["notes"])) for part in event["parts"])
        counts.append((by_hand.get("left"), by_hand.get("right")))
    assert counts == [(len(left), len(right)) for _symbol, left, right in VOICINGS]


def test_reads_the_pitches_that_are_drawn(doc):
    """Verovio omits one of the three accidentals on the first chord, so the page says
    F natural there.  The reader is expected to report the page, not the intention."""
    drawn = [(left, right) for _symbol, left, right in VOICINGS]
    drawn[0] = (drawn[0][0], ["F3", "A#3", "D#4"])
    for expected, event in zip(drawn, doc["systems"][0]["events"]):
        got = dict((part["hand"], [note["name"] for note in part["notes"]])
                   for part in event["parts"])
        assert (got["left"], got["right"]) == expected


def test_whole_notes_are_reported_as_whole_notes(doc):
    """A chord of whole notes has no stem, but its outer walls survive the same
    vertical erosion a stem does, and were being counted as one."""
    hints = set(part["duration_hint"]
                for event in doc["systems"][0]["events"] for part in event["parts"])
    assert hints == {"whole"}


def test_reads_an_iphone_heic_photo():
    """iPhones shoot HEIC. Safari converts on upload but a file picked out of Files
    does not, and Pillow cannot decode one without help, so this is the difference
    between working and a blank error on the app's most likely input."""
    import io

    from PIL import Image

    from sheeter import preprocess

    if not preprocess.HEIC_SUPPORTED:
        pytest.skip("pillow-heif is not installed")
    source = os.path.join(os.path.dirname(__file__), "fixtures",
                          "grand_2flats_photo.jpg")
    if not os.path.isfile(source):
        pytest.skip("run tools/make_fixtures.py first")
    buf = io.BytesIO()
    Image.open(source).convert("RGB").save(buf, format="HEIF", quality=88)

    doc, _png = pipeline.analyze_bytes(buf.getvalue(), "IMG_0001.HEIC", "image/heic")
    events = [e for s in doc["systems"] for e in s["events"]]
    assert len(events) == 4
    first = dict((part["hand"], [note["name"] for note in part["notes"]])
                 for part in events[0]["parts"])
    assert first == {"right": ["C5", "D5", "F5", "A5"], "left": ["Bb2", "F3"]}
