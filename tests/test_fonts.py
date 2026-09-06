"""The reader must not be tuned to one engraver's noteheads.

Every fixture in the main corpus is rendered by Verovio in its default font, so on its
own it cannot tell a reader that generalises from one that has memorised Bravura.  This
renders the same music in each music font Verovio ships and expects the same answer.

Petaluma is a handwritten-style face.  Handwritten manuscript is out of scope and this
does not change that, but a font drawn to look handwritten stretches the shapes further
than any engraver would, which is exactly what makes it worth reading.
"""

import pytest

from sheeter import pipeline

pytest.importorskip("verovio")
pytest.importorskip("cairosvg")

FONTS = ["Bravura", "Leipzig", "Gootville", "Petaluma", "Leland"]

# (left hand, right hand) per event, low to high, in two flats.
EXPECTED = [
    (["Bb2", "F3"], ["C5", "D5", "F5", "A5"]),
    (["Eb3", "Bb3"], ["Bb4", "C5", "Eb5", "G5"]),
    (["G2", "D3"], ["D5", "Eb5", "G5", "Bb5"]),
    (["F2", "C3"], ["C5", "D5", "F5", "A5"]),
]


@pytest.fixture(scope="module")
def musicxml():
    from music21 import chord, clef, key, meter, stream
    from sheeter import pitches

    score = stream.Score()
    top, bottom = stream.Part(), stream.Part()
    for part, which in ((top, clef.TrebleClef()), (bottom, clef.BassClef())):
        part.append(which)
        part.append(key.KeySignature(-2))
        part.append(meter.TimeSignature("4/4"))
    for left, right in EXPECTED:
        top.append(chord.Chord([pitches.to_music21(n) for n in right], quarterLength=1))
        bottom.append(chord.Chord([pitches.to_music21(n) for n in left], quarterLength=1))
    score.insert(0, top)
    score.insert(0, bottom)
    with open(score.write("musicxml")) as handle:
        return handle.read()


def render(musicxml, font):
    import cairosvg
    import verovio

    toolkit = verovio.toolkit()
    toolkit.setOptions({"pageWidth": 2200, "pageHeight": 900, "scale": 110,
                        "adjustPageHeight": True, "adjustPageWidth": True,
                        "footer": "none", "header": "none", "font": font})
    if not toolkit.loadData(musicxml):
        pytest.skip("verovio in this environment cannot load the %s font" % font)
    return cairosvg.svg2png(bytestring=toolkit.renderToSVG(1).encode(),
                            background_color="white", scale=2.0)


@pytest.mark.parametrize("font", FONTS)
def test_reads_the_same_music_in_any_font(musicxml, font):
    doc, _png = pipeline.analyze_bytes(render(musicxml, font), font + ".png", "image/png")
    events = [event for system in doc["systems"] for event in system["events"]]
    assert len(events) == len(EXPECTED), "%s: read %d events" % (font, len(events))
    for (left, right), event in zip(EXPECTED, events):
        got = dict((part["hand"], [note["name"] for note in part["notes"]])
                   for part in event["parts"])
        assert got.get("left") == left, "%s event %d left hand" % (font, event["index"])
        assert got.get("right") == right, "%s event %d right hand" % (font, event["index"])
