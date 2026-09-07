"""Photo bytes in, a finished analysis document out."""

import datetime
import hashlib

from . import naming, geometry, pitches, preprocess, schema, store


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def analysis_id(raw_bytes):
    """Content address, so re-uploading the same photo lands on the same analysis."""
    return hashlib.sha256(raw_bytes).hexdigest()[:12]


def analyze_bytes(raw_bytes, filename="photo.jpg", content_type="image/jpeg",
                  title=None):
    """Run the geometry pipeline.  Returns ``(document, processed_png_bytes)``."""
    gray, mask, info = preprocess.prepare(raw_bytes)
    systems, warnings = geometry.analyze(mask)

    # The filename comes from the client and is the default title, so it needs the
    # same clamp store.rename applies.  Left alone, a 600 character name renders as a
    # page-sized heading that pushes every control off a phone screen.
    label = str(title or filename or "photo").strip()[:store.MAX_TITLE] or "photo"
    document = schema.new_analysis(
        analysis_id(raw_bytes), _now(), label,
        {
            "filename": filename,
            "content_type": content_type,
            "bytes": len(raw_bytes),
            "width": info["orig_width"],
            "height": info["orig_height"],
        },
        {
            "width": info["width"],
            "height": info["height"],
            "scale": round(info["scale"], 4),
            "deskew_deg": info["deskew_deg"],
        },
    )
    document["engine"]["geometry"] = geometry.GEOMETRY_VERSION
    document["warnings"] = warnings
    document["systems"] = systems
    name_everything(document)
    return schema.validate_analysis(document), preprocess.to_png_bytes(gray)


def name_everything(document):
    """Fill in every chord name in place, per hand and for both hands together."""
    for system in document["systems"]:
        keys = dict((staff["index"], staff["key_fifths"]) for staff in system["staves"])
        # Per staff, because name_chord's tie-break asks what that hand played last, and
        # the two hands are separate lines.  It was being tracked and never passed, so
        # the geometry path and the verifier's renaming chained differently.
        previous = {}
        for event in system["events"]:
            pooled = []
            for part in event["parts"]:
                names = [note["name"] for note in part["notes"]]
                part["chord"] = naming.name_chord(names, keys.get(part["staff"], 0),
                                                  previous.get(part["staff"]))
                previous[part["staff"]] = names
                pooled.extend(names)
            event["combined"] = naming.name_combined(_sorted_unique(pooled),
                                                     keys.get(0, 0))
    return document


def _sorted_unique(names):
    from . import pitches
    seen, out = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return sorted(out, key=lambda n: pitches.step_to_midi(*pitches.parse_name(n)))


def summarize(document):
    """A one-line reading of the whole document, for the gallery and the copy button."""
    symbols = []
    for system in document["systems"]:
        for event in system["events"]:
            combined = event.get("combined") or {}
            symbol = combined.get("symbol")
            if symbol:
                symbols.append(symbol)
    return " - ".join(symbols[:8]) + (" ..." if len(symbols) > 8 else "")


#: What a written accidental does to the note it sits in front of.
ACCIDENTAL_ALTER = {
    "flat": -1, "sharp": 1, "natural": 0, "double-flat": -2, "double-sharp": 2,
}


def restate_note(note, step, alter, staff, from_key):
    """A note at *step*/*alter* that keeps the geometry we already measured."""
    y = note["y"]
    new = pitches.make_note(
        step, alter, note["x"], y,
        w=note["w"], h=note["h"], hollow=note["hollow"],
        accidental=note["accidental"], from_key=from_key,
        ledger=pitches.ledger_count(step, staff["clef"]),
        step_err_px=abs(y - pitches.step_to_y(step, staff["lines"], staff["clef"])),
        confidence=note["confidence"], changed=note["changed"],
    )
    if new["name"] != note["name"]:
        new["changed"] = True
    return new


def staff_notes(system, staff_index):
    for event in system["events"]:
        for part in event["parts"]:
            if part["staff"] == staff_index:
                yield event, part


def restate_staff(system, staff):
    """Re-derive every pitch on *staff* from its y, under the current clef and key.

    A corrected clef or key signature invalidates the reading of every notehead
    on the staff, not only the ones the model bothered to mention.  Notes whose
    name moves are marked changed: the verifier did change them, by way of the
    clef, and the overlay should say so.
    """
    alterations = pitches.key_alterations(staff["key_fifths"])
    for _event, part in staff_notes(system, staff["index"]):
        notes = []
        for note in part["notes"]:
            step, _err = pitches.y_to_step(note["y"], staff["lines"], staff["clef"])
            if note["accidental"] in ACCIDENTAL_ALTER:
                alter = ACCIDENTAL_ALTER[note["accidental"]]
                from_key = False
            else:
                alter = alterations.get(pitches.step_letter(step), 0)
                from_key = alter != 0
            notes.append(restate_note(note, step, alter, staff, from_key))
        part["notes"] = notes


def set_key(document, fifths):
    """Put the whole reading in one key signature and re-spell every note.

    This is what happens when the reader looks at the photo and says what the key is.
    Nothing is measured again and the photo is not needed: a notehead's y is a staff
    position whatever the key is, so the spelling follows by arithmetic from what is
    already stored.  The chords are re-named too, because a chord is named from the
    pitches under it.

    The confidence goes to 1.0 rather than staying where the geometry left it.  A person
    who has the page in front of them is a better source than the measurement, and
    ``verify.geometry_is_sure`` reads this field, so it also stops the vision pass
    quietly putting its own key back.
    """
    fifths = int(fifths)
    if not -7 <= fifths <= 7:
        raise ValueError("key signature must be -7..7 fifths, got %r" % (fifths,))
    for system in document["systems"]:
        for staff in system["staves"]:
            staff["key_fifths"] = fifths
            staff["key_confidence"] = 1.0
            restate_staff(system, staff)
    name_everything(document)
    return document
