#!/usr/bin/env python
"""Generate the ground-truth fixture corpus.

Each case is a short piece of music whose pitches we type out once, here.  The
same table drives two things: a music21 score, engraved to a clean PNG through
verovio, and the entry in ``manifest.json`` that says what a correct reading of
that PNG looks like.  Nothing is transcribed twice, so the manifest cannot drift
from the picture.

For every clean case we also synthesise a phone-camera version: rotation,
keystone, uneven light, blur, noise, JPEG artefacts, and a page margin so the
staff lines do not run edge to edge.  All of it is seeded from the case name,
so two runs produce byte-identical files.

    python tools/make_fixtures.py [--out tests/fixtures] [--only NAME] [--no-photos]

The PNG and JPG files are gitignored; the manifest is not.
"""

import argparse
import json
import os
import re
import sys
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import numpy
from PIL import Image, ImageFilter

from sheeter import pitches, schema

MANIFEST_SCHEMA = 1

# Pitch names below are in the schema's ASCII spelling ("Bb2", "F#5").  They are
# converted to music21 spelling on the way in and read back off the music21
# objects on the way out, which exercises the round trip.


def ev(ql, **parts):
    """One simultaneity: a pitch list per staff label, and how long it lasts.

    *ql* is normally a single quarterLength for the whole simultaneity.  Pass a dict
    keyed by staff label instead to give the hands different note values, which is the
    only way to get a hollow notehead and a filled one into the same event, a case the
    plan's test matrix calls for by name.  The bar has to stay full either way, so a
    hand given a shorter value is padded with a rest.
    """
    return (ql, parts)


def _ql_for(ql, label):
    return ql.get(label, max(ql.values())) if isinstance(ql, dict) else ql


def _ql_total(ql):
    return max(ql.values()) if isinstance(ql, dict) else ql


# Each case: staves are (hand, clef) top to bottom.  The staff label used to key
# the events is the hand, or the clef name when there is no hand (a lone staff).
CASES = [
    {
        "name": "treble_quarters",
        "tags": ["single-staff", "single-notes"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [(None, "treble")],
        "accidentals": [],
        "systems": [[
            ev(1.0, treble=["E4"]),
            ev(1.0, treble=["A4"]),
            ev(1.0, treble=["C5"]),
            ev(1.0, treble=["F5"]),
        ]],
    },
    {
        # The case that breaks naive x-clustering: a second forces one notehead
        # of the pair to sit on the far side of the stem.
        "name": "grand_2flats",
        "tags": ["grand-staff", "seconds", "key-flats"],
        "fifths": -2,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(1.0, right=["C5", "D5", "F5", "A5"], left=["Bb2", "F3"]),
            ev(1.0, right=["Bb4", "C5", "Eb5", "G5"], left=["Eb3", "Bb3"]),
            ev(1.0, right=["D5", "Eb5", "G5", "Bb5"], left=["G2", "D3"]),
            ev(1.0, right=["C5", "D5", "F5", "A5"], left=["F2", "C3"]),
        ]],
    },
    {
        "name": "grand_4sharps",
        "tags": ["grand-staff", "triads", "key-sharps"],
        "fifths": 4,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(1.0, right=["E4", "G#4", "B4"], left=["G#2", "B2", "E3"]),
            ev(1.0, right=["F#4", "A4", "C#5"], left=["A2", "C#3", "F#3"]),
            ev(1.0, right=["F#4", "B4", "D#5"], left=["B2", "D#3", "F#3"]),
            ev(1.0, right=["E4", "A4", "C#5"], left=["A2", "C#3", "E3"]),
        ]],
    },
    {
        # Five stacked thirds fuse into one dark blob at photo resolution.
        "name": "grand_3flats_stack",
        "tags": ["grand-staff", "stacked-thirds", "key-flats"],
        "fifths": -3,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(1.0, right=["Eb4", "G4", "Bb4", "D5", "F5"], left=["Eb2", "Bb2"]),
            ev(1.0, right=["F4", "Ab4", "C5", "Eb5", "G5"], left=["F2", "C3"]),
            ev(1.0, right=["G4", "Bb4", "D5", "F5", "Ab5"], left=["G2", "D3"]),
            ev(1.0, right=["Ab4", "C5", "Eb5", "G5", "Bb5"], left=["Ab2", "Eb3"]),
        ]],
    },
    {
        # One and two ledger lines, above and below, on both staves.  Event 1 is
        # the same sounding pitch written on both staves.
        "name": "ledger_lines",
        "tags": ["grand-staff", "ledger-lines", "single-notes"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(1.0, right=["C4"], left=["C4"]),
            ev(1.0, right=["A3"], left=["E4"]),
            ev(1.0, right=["A5"], left=["E2"]),
            ev(1.0, right=["C6"], left=["C2"]),
        ]],
    },
    {
        "name": "accidentals_natural",
        "tags": ["grand-staff", "accidentals", "key-flats"],
        "fifths": -2,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": ["natural", "sharp", "natural"],
        "systems": [[
            ev(1.0, right=["Bb4", "D5"], left=["Bb2", "F3"]),
            ev(1.0, right=["B4", "D5"], left=["B2", "F3"]),
            ev(1.0, right=["F#5", "A5"], left=["D3", "A3"]),
            ev(1.0, right=["Eb5", "G5"], left=["Eb3", "G3"]),
        ]],
    },
    {
        "name": "note_shapes",
        "tags": ["grand-staff", "hollow", "filled"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            # Hollow over filled in the same event: the plan's matrix asks for it, and
            # it is the case that catches a hollow-versus-filled test done per event
            # rather than per notehead.
            ev({"right": 2.0, "left": 1.0}, right=["C5", "E5"], left=["C3", "G3"]),
            ev(1.0, right=["D5", "F5"], left=["D3", "A3"]),
            ev(1.0, right=["E5", "G5"], left=["E3", "B3"]),
            ev(4.0, right=["C5", "E5", "G5"], left=["C3", "G3"]),
        ]],
    },
    {
        "name": "two_systems",
        "tags": ["grand-staff", "multi-system", "key-sharps"],
        "fifths": 1,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [
            [
                ev(1.0, right=["G4", "B4", "D5"], left=["G2", "D3"]),
                ev(1.0, right=["A4", "C5", "E5"], left=["A2", "E3"]),
                ev(1.0, right=["B4", "D5", "F#5"], left=["B2", "F#3"]),
                ev(1.0, right=["C5", "E5", "G5"], left=["C3", "G3"]),
            ],
            [
                ev(1.0, right=["D5", "F#5", "A5"], left=["D3", "A3"]),
                ev(1.0, right=["C5", "E5", "G5"], left=["C3", "G3"]),
                ev(1.0, right=["B4", "D5", "G5"], left=["B2", "D3"]),
                ev(1.0, right=["G4", "B4", "D5"], left=["G2", "D3"]),
            ],
        ],
    },
    {
        "name": "lead_sheet",
        "tags": ["single-staff", "single-notes", "key-flats"],
        "fifths": -1,
        "meter": "4/4",
        "staves": [(None, "treble")],
        "accidentals": [],
        "systems": [[
            ev(1.0, treble=["F4"]),
            ev(1.0, treble=["G4"]),
            ev(1.0, treble=["A4"]),
            ev(1.0, treble=["Bb4"]),
            ev(1.0, treble=["C5"]),
            ev(1.0, treble=["Bb4"]),
            ev(1.0, treble=["A4"]),
            ev(1.0, treble=["F4"]),
        ]],
    },
    {
        # Four octaves in one grab: C2 sits two ledgers under the bass staff,
        # C6 two ledgers over the treble.
        "name": "wide_range",
        "tags": ["grand-staff", "ledger-lines", "wide-range"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(1.0, right=["C5", "G5", "C6"], left=["C2", "G2", "C3"]),
            ev(1.0, right=["B4", "F5", "B5"], left=["D2", "A2", "D3"]),
            ev(1.0, right=["C5", "G5", "C6"], left=["E2", "B2", "E3"]),
            ev(1.0, right=["G4", "D5", "G5"], left=["C2", "G2", "C3"]),
        ]],
    },
    {
        "name": "dotted_notes",
        "tags": ["grand-staff", "dotted", "eighths"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(3.0, right=["C5", "E5"], left=["C3", "G3"]),
            ev(1.0, right=["D5", "F5"], left=["D3", "A3"]),
            ev(1.5, right=["E5", "G5"], left=["E3", "B3"]),
            ev(0.5, right=["F5", "A5"], left=["F3", "A3"]),
            ev(2.0, right=["G5", "B5"], left=["G2", "D3"]),
        ]],
    },
    {
        # Beams are thick horizontal bars close to the noteheads.
        "name": "beamed_eighths",
        "tags": ["grand-staff", "eighths", "beams", "single-notes"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(0.5, right=["C5"], left=["C3"]),
            ev(0.5, right=["D5"], left=["D3"]),
            ev(0.5, right=["E5"], left=["E3"]),
            ev(0.5, right=["F5"], left=["F3"]),
            ev(0.5, right=["G5"], left=["G3"]),
            ev(0.5, right=["F5"], left=["F3"]),
            ev(0.5, right=["E5"], left=["E3"]),
            ev(0.5, right=["D5"], left=["D3"]),
        ]],
    },
    {
        # Three adjacent steps, so one of the three noteheads is displaced sideways.
        "name": "cluster_seconds",
        "tags": ["grand-staff", "seconds", "clusters"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(1.0, right=["C5", "D5", "E5"], left=["C3", "D3", "E3"]),
            ev(1.0, right=["D5", "E5", "F5"], left=["D3", "E3", "F3"]),
            ev(1.0, right=["E5", "F5", "G5"], left=["E3", "F3", "G3"]),
            ev(1.0, right=["F5", "G5", "A5"], left=["F3", "G3", "A3"]),
        ]],
    },
    {
        # Two events where the left hand is silent, so an event has one part.
        "name": "rest_left_hand",
        "tags": ["grand-staff", "rests"],
        "fifths": 0,
        "meter": "4/4",
        "staves": [("right", "treble"), ("left", "bass")],
        "accidentals": [],
        "systems": [[
            ev(1.0, right=["C5", "E5", "G5"], left=["C3", "G3"]),
            ev(1.0, right=["D5", "F5", "A5"], left=None),
            ev(1.0, right=["B4", "E5", "G5"], left=None),
            ev(1.0, right=["C5", "E5", "G5"], left=["C3", "G3"]),
        ]],
    },
]


def staff_label(hand, clef_name):
    """Key used for this staff in the manifest's event dicts."""
    return hand if hand is not None else clef_name


# ---------------------------------------------------------------------------
# score construction
# ---------------------------------------------------------------------------

def _clef_object(name):
    from music21 import clef
    return {"treble": clef.TrebleClef, "bass": clef.BassClef}[name]()


def _make_object(names, ql):
    from music21 import chord, note
    if not names:
        return note.Rest(quarterLength=ql)
    if len(names) == 1:
        return note.Note(pitches.to_music21(names[0]), quarterLength=ql)
    return chord.Chord([pitches.to_music21(n) for n in names], quarterLength=ql)


def build_score(case):
    """Return ``(score, objects)``.

    ``objects[system][event][label]`` is the music21 object that was placed on
    that staff, so the manifest can be read back off the score itself.
    """
    from music21 import key, layout, meter, stream

    ts_string = case["meter"]
    fifths = case["fifths"]
    labels = [staff_label(h, c) for h, c in case["staves"]]

    for system in case["systems"]:
        for _, parts in system:
            unknown = set(parts) - set(labels)
            if unknown:
                raise ValueError("%s: event keys %s are not staff labels %s"
                                 % (case["name"], sorted(unknown), labels))

    score = stream.Score()
    parts = []
    for hand, clef_name in case["staves"]:
        part = stream.Part()
        part.partName = ""
        part.partAbbreviation = ""
        part.append(_clef_object(clef_name))
        part.append(key.KeySignature(fifths))
        part.append(meter.TimeSignature(ts_string))
        parts.append(part)

    objects = []
    bar_length = meter.TimeSignature(ts_string).barDuration.quarterLength
    measure_number = 1

    for sys_index, system in enumerate(case["systems"]):
        objects.append([{} for _ in system])
        first_measure_of_system = measure_number
        for part, label in zip(parts, labels):
            measure_number = first_measure_of_system
            measure = stream.Measure(number=measure_number)
            used = 0.0
            measures = []
            for ev_index, (ql, by_label) in enumerate(system):
                mine = _ql_for(ql, label)
                obj = _make_object(by_label.get(label), mine)
                objects[sys_index][ev_index][label] = obj
                measure.append(obj)
                used += mine
                # A hand given a shorter note value than the event waits out the rest
                # of it, so the bar still adds up and the next event still lines up
                # across the staves.
                pad = _ql_total(ql) - mine
                if pad > 1e-9:
                    from music21 import note as m21note
                    measure.append(m21note.Rest(quarterLength=pad))
                    used += pad
                if used > bar_length + 1e-9:
                    raise ValueError("%s: event %d overflows the bar"
                                     % (case["name"], ev_index))
                if abs(used - bar_length) < 1e-9:
                    measures.append(measure)
                    measure_number += 1
                    measure = stream.Measure(number=measure_number)
                    used = 0.0
            if used > 1e-9:
                raise ValueError("%s: system %d does not fill whole bars"
                                 % (case["name"], sys_index))
            if sys_index > 0:
                measures[0].insert(0, layout.SystemLayout(isNew=True))
            for m in measures:
                m.makeAccidentals(useKeySignature=key.KeySignature(fifths),
                                  inPlace=True)
                m.makeBeams(inPlace=True)
                part.append(m)

    for part in parts:
        score.insert(0, part)
    if len(parts) > 1:
        score.insert(0, layout.StaffGroup(parts, symbol="brace", barTogether=True))
    return score, objects


def score_to_musicxml(score):
    from music21.musicxml import m21ToXml
    return m21ToXml.GeneralObjectExporter(score).parse().decode("utf-8")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def _note_names(obj):
    """Schema-spelled pitch names for a music21 note or chord, low to high."""
    if obj is None or obj.isRest:
        return None
    out = []
    for p in obj.pitches:
        step = p.octave * 7 + pitches.STEP_NAMES.index(p.step)
        alter = int(round(p.alter))
        out.append((pitches.step_to_midi(step, alter), pitches.step_to_name(step, alter)))
    out.sort()
    return [name for _, name in out]


#: music21 duration types that the schema collapses into "quarter-or-shorter".
#: geometry.duration_hint cannot tell them apart: flag and beam ink measures the same as
#: a plain stem, so the manifest may only claim what a correct reading could claim.
SHORT_DURATIONS = ("quarter", "eighth", "16th", "32nd", "64th", "128th")


def _duration_hint(obj):
    if obj is None or obj.isRest:
        return None
    kind = obj.duration.type
    if kind in SHORT_DURATIONS:
        kind = "quarter-or-shorter"
    if kind not in schema.DURATION_HINTS:
        # Anything longer than a whole note, or a complex tied duration, has no honest
        # hint.  Say so rather than filing it under the shortest bucket.
        raise ValueError("duration %r is not a schema duration hint" % (kind,))
    return kind


def manifest_case(case, objects):
    labels = [staff_label(h, c) for h, c in case["staves"]]
    staves = [
        {"hand": hand, "clef": clef_name, "key_fifths": case["fifths"],
         "label": staff_label(hand, clef_name)}
        for hand, clef_name in case["staves"]
    ]
    systems = []
    for sys_index, system in enumerate(case["systems"]):
        events = []
        for ev_index in range(len(system)):
            objs = objects[sys_index][ev_index]
            event = {}
            durations = {}
            dotted = {}
            for label in labels:
                obj = objs.get(label)
                event[label] = _note_names(obj)
                durations[label] = _duration_hint(obj)
                dotted[label] = bool(obj.duration.dots) if event[label] else None
            event["durations"] = durations
            event["dotted"] = dotted
            events.append(event)
        systems.append({"staves": staves, "events": events})
    return {
        "name": case["name"],
        "file": case["name"] + ".png",
        "photo": case["name"] + "_photo.jpg",
        "systems": systems,
        "tags": list(case["tags"]),
    }


# ---------------------------------------------------------------------------
# engraving
# ---------------------------------------------------------------------------

VEROVIO_OPTIONS = {
    "pageWidth": 8000,
    "pageHeight": 3000,
    "scale": 110,
    "adjustPageWidth": True,
    "adjustPageHeight": True,
    "footer": "none",
    "header": "none",
    "mnumInterval": 64,   # far enough apart that no measure number is drawn
    "spacingStaff": 2,
    "minLastJustification": 0,
    "pageMarginLeft": 30,
    "pageMarginRight": 30,
    "pageMarginTop": 30,
    "pageMarginBottom": 30,
}

PNG_SCALE = 2.6
MAX_CLEAN_WIDTH = 2600
CROP_PAD = 36


def measure_count(case):
    """Bars in the whole case; verovio draws one <g class="staff"> per bar per staff."""
    from music21 import meter
    bar = meter.TimeSignature(case["meter"]).barDuration.quarterLength
    total = sum(_ql_total(ql) for system in case["systems"] for ql, _ in system)
    return int(round(total / bar))


def render_svg(case, xml):
    import verovio
    options = dict(VEROVIO_OPTIONS)
    if len(case["systems"]) > 1:
        options["breaks"] = "encoded"
    tk = verovio.toolkit()
    tk.setOptions(options)
    if not tk.loadData(xml):
        raise RuntimeError("%s: verovio could not load the MusicXML" % case["name"])
    if tk.getPageCount() != 1:
        raise RuntimeError("%s: expected 1 page, verovio made %d"
                           % (case["name"], tk.getPageCount()))
    svg = tk.renderToSVG(1)

    want_systems = len(case["systems"])
    got_systems = len(re.findall(r'class="system"', svg))
    if got_systems != want_systems:
        raise RuntimeError("%s: expected %d systems, verovio drew %d"
                           % (case["name"], want_systems, got_systems))
    want_staves = measure_count(case) * len(case["staves"])
    got_staves = len(re.findall(r'class="staff"', svg))
    if got_staves != want_staves:
        raise RuntimeError("%s: expected %d staves, verovio drew %d"
                           % (case["name"], want_staves, got_staves))
    return svg


def svg_to_png(svg, path):
    import cairosvg
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=path,
                     background_color="white", scale=PNG_SCALE)
    img = Image.open(path).convert("L")
    img = _trim(img, CROP_PAD)
    if img.width > MAX_CLEAN_WIDTH:
        height = int(round(img.height * MAX_CLEAN_WIDTH / float(img.width)))
        img = img.resize((MAX_CLEAN_WIDTH, height), Image.LANCZOS)
    img.convert("RGB").save(path)
    return img.size


def _trim(img, pad):
    """Crop to the ink, then re-add a uniform white border."""
    a = numpy.asarray(img)
    dark = a < 250
    rows = numpy.flatnonzero(dark.any(axis=1))
    cols = numpy.flatnonzero(dark.any(axis=0))
    if not len(rows) or not len(cols):
        return img
    box = (max(0, cols[0] - pad), max(0, rows[0] - pad),
           min(img.width, cols[-1] + 1 + pad), min(img.height, rows[-1] + 1 + pad))
    return img.crop(box)


def staff_spacing_px(img):
    """Rough staff-line spacing, used only as a sanity number in the report."""
    a = numpy.asarray(img.convert("L"), dtype=float)
    darkness = (255.0 - a).sum(axis=1)
    peaks = numpy.flatnonzero(darkness > 0.55 * darkness.max())
    if len(peaks) < 2:
        return 0.0
    gaps = numpy.diff(peaks)
    gaps = gaps[gaps > 2]
    return float(numpy.median(gaps)) if len(gaps) else 0.0


# ---------------------------------------------------------------------------
# phone-camera synthesis
# ---------------------------------------------------------------------------

PAPER = 244.0          # page white after the camera's exposure
MAX_PHOTO_WIDTH = 2400


def _perspective_coeffs(dst_quad, src_quad):
    """Coefficients for PIL's PERSPECTIVE transform, which maps output to input."""
    rows = []
    for (dx, dy), (sx, sy) in zip(dst_quad, src_quad):
        rows.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        rows.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
    a = numpy.array(rows, dtype=float)
    b = numpy.array(src_quad, dtype=float).reshape(8)
    return numpy.linalg.solve(a, b)


def make_photo(clean_path, photo_path, name, index):
    seed = zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
    rng = numpy.random.RandomState(seed)

    img = Image.open(clean_path).convert("L")
    img = img.point(lambda v: int(v * PAPER / 255.0))

    # A page margin, so the staff lines stop well short of the frame edges.
    mx = int(0.09 * img.width) + 40
    my = int(0.22 * img.height) + 40
    page = Image.new("L", (img.width + 2 * mx, img.height + 2 * my), int(PAPER))
    page.paste(img, (mx, my))

    w, h = page.size
    lean = 0.016 * w * (1 if index % 2 == 0 else -1)
    drop = 0.010 * h
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    dst = [(lean, drop), (w - lean * 0.4, 0.0), (w, h - drop * 0.5), (0.0, h)]
    page = page.transform((w, h), Image.PERSPECTIVE,
                          _perspective_coeffs(dst, src),
                          resample=Image.BICUBIC, fillcolor=int(PAPER))

    angle = (1.0 + 0.9 * (index % 3)) * (1 if index % 2 == 0 else -1)
    page = page.rotate(angle, resample=Image.BICUBIC, fillcolor=int(PAPER))

    if page.width > MAX_PHOTO_WIDTH:
        height = int(round(page.height * MAX_PHOTO_WIDTH / float(page.width)))
        page = page.resize((MAX_PHOTO_WIDTH, height), Image.LANCZOS)

    a = numpy.asarray(page, dtype=numpy.float64)
    h, w = a.shape
    ys = numpy.linspace(0.0, 1.0, h).reshape(h, 1)
    xs = numpy.linspace(0.0, 1.0, w).reshape(1, w)

    # Uneven light: a diagonal ramp plus a broad falloff towards the corners.
    tilt = 0.30 * (index % 4) / 3.0 + 0.14
    ramp = 1.0 - tilt * (0.65 * xs + 0.35 * ys)
    if index % 2:
        ramp = ramp[:, ::-1]
    radius = ((xs - 0.5) ** 2 + (ys - 0.5) ** 2) / 0.5
    a = a * ramp * (1.0 - 0.16 * radius)

    page = Image.fromarray(numpy.clip(a, 0, 255).astype(numpy.uint8))
    page = page.filter(ImageFilter.GaussianBlur(0.8 + 0.25 * (index % 3)))

    a = numpy.asarray(page, dtype=numpy.float64)
    a += rng.normal(0.0, 3.5, a.shape)
    a = numpy.clip(a, 0, 255)

    # A warm paper cast, the way a phone sees an indoor page.
    rgb = numpy.stack([a, a * 0.985, a * 0.952], axis=2)
    photo = Image.fromarray(numpy.clip(rgb, 0, 255).astype(numpy.uint8), "RGB")
    photo.save(photo_path, "JPEG", quality=85)
    return photo.size


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def check_accidentals(case, xml):
    found = re.findall(r"<accidental[^>]*>([^<]*)</accidental>", xml)
    want = list(case["accidentals"])
    if found != want:
        raise RuntimeError("%s: expected accidentals %s, the score has %s"
                           % (case["name"], want, found))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "tests", "fixtures"),
                        help="output directory (default tests/fixtures)")
    parser.add_argument("--only", metavar="NAME", action="append",
                        help="render only this case; repeatable")
    parser.add_argument("--no-photos", action="store_true",
                        help="skip the phone-camera JPEGs")
    args = parser.parse_args(argv)

    known = [c["name"] for c in CASES]
    if args.only:
        bad = [n for n in args.only if n not in known]
        if bad:
            parser.error("unknown case(s): %s\nknown: %s"
                         % (", ".join(bad), ", ".join(known)))

    os.makedirs(args.out, exist_ok=True)
    manifest = {"schema": MANIFEST_SCHEMA, "cases": []}

    for index, case in enumerate(CASES):
        score, objects = build_score(case)
        xml = score_to_musicxml(score)
        check_accidentals(case, xml)
        entry = manifest_case(case, objects)
        manifest["cases"].append(entry)

        if args.only and case["name"] not in args.only:
            print("%-20s skipped" % case["name"])
            continue

        png_path = os.path.join(args.out, entry["file"])
        svg = render_svg(case, xml)
        size = svg_to_png(svg, png_path)
        line = "%-20s %4dx%-4d spacing %4.1fpx" % (
            case["name"], size[0], size[1],
            staff_spacing_px(Image.open(png_path)))

        if not args.no_photos:
            photo_path = os.path.join(args.out, entry["photo"])
            psize = make_photo(png_path, photo_path, case["name"], index)
            line += "   photo %dx%d" % psize
        print(line)

    manifest_path = os.path.join(args.out, "manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False)
        fh.write("\n")
    notes = sum(len(v) for c in manifest["cases"] for s in c["systems"]
                for e in s["events"] for k, v in e.items()
                if k not in ("durations", "dotted") and v)
    print("\n%d cases, %d noteheads -> %s"
          % (len(manifest["cases"]), notes, manifest_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
