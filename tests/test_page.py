"""A whole page, not one line of it.

The main corpus tops out at two systems, and a photo of a page is the thing this app
is most likely to be pointed at.  What a page adds is system segmentation: the gap
between two systems is barely larger than the gap inside a grand staff, so anything
that separates them by measuring gaps alone gets it wrong and every hand label on the
page goes with it.
"""

import pytest

from sheeter import pipeline

pytest.importorskip("verovio")
pytest.importorskip("cairosvg")

RIGHT = [["D5", "F#5", "A5"], ["C#5", "E5", "A5"],
         ["B4", "D5", "G5"], ["A4", "C#5", "E5"]]
LEFT = [["D3", "A3"], ["A2", "E3"], ["G2", "D3"], ["A2", "E3"]]
BARS = 10


@pytest.fixture(scope="module")
def page():
    import cairosvg
    import verovio
    from music21 import chord, clef, key, meter, stream
    from sheeter import pitches

    score = stream.Score()
    top, bottom = stream.Part(), stream.Part()
    for part, which in ((top, clef.TrebleClef()), (bottom, clef.BassClef())):
        part.append(which)
        part.append(key.KeySignature(2))
        part.append(meter.TimeSignature("4/4"))
    for bar in range(BARS):
        left, right = LEFT[bar % len(LEFT)], RIGHT[bar % len(RIGHT)]
        top.append(chord.Chord([pitches.to_music21(n) for n in right], quarterLength=4))
        bottom.append(chord.Chord([pitches.to_music21(n) for n in left], quarterLength=4))
    score.insert(0, top)
    score.insert(0, bottom)

    toolkit = verovio.toolkit()
    toolkit.setOptions({"pageWidth": 1700, "pageHeight": 2200, "scale": 42,
                        "adjustPageHeight": True, "footer": "none", "header": "none"})
    with open(score.write("musicxml")) as handle:
        assert toolkit.loadData(handle.read())
    return cairosvg.svg2png(bytestring=toolkit.renderToSVG(1).encode(),
                            background_color="white", scale=2.4)


@pytest.fixture(scope="module")
def doc(page):
    document, _png = pipeline.analyze_bytes(page, "page.png", "image/png")
    return document


def test_the_page_is_more_than_one_system(doc):
    assert len(doc["systems"]) > 1


def test_every_system_is_a_grand_staff_with_two_hands(doc):
    """Merging two systems into one is what a gap-based rule does here, and it costs
    every hand label on the page, since a four staff system has no left and right."""
    for system in doc["systems"]:
        assert system["grand"] is True, "system %d" % system["index"]
        assert [s["hand"] for s in system["staves"]] == ["right", "left"]
        assert [s["clef"] for s in system["staves"]] == ["treble", "bass"]


def test_the_key_signature_is_read_on_every_staff_of_every_system(doc):
    for system in doc["systems"]:
        for staff in system["staves"]:
            assert staff["key_fifths"] == 2


def test_every_bar_reads_correctly(doc):
    events = [event for system in doc["systems"] for event in system["events"]]
    assert len(events) == BARS
    for bar, event in enumerate(events):
        got = dict((part["hand"], [note["name"] for note in part["notes"]])
                   for part in event["parts"])
        assert got.get("right") == RIGHT[bar % len(RIGHT)], "bar %d right hand" % bar
        assert got.get("left") == LEFT[bar % len(LEFT)], "bar %d left hand" % bar
