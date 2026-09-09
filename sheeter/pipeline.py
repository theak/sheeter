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


#: What a reader may write in front of a notehead by hand.  Double accidentals are
#: deliberately absent: they exist in ACCIDENTAL_ALTER because the reader can detect
#: one on the page, but nobody fixing a misread note by eye reaches for a double sharp,
#: and offering five buttons where three will do is how a simple tool stops being one.
EDITABLE_ACCIDENTALS = ("sharp", "flat", "natural")

#: How far a stored edit's notehead may have moved before the edit is taken to be about
#: a different note and dropped.  Half a staff space: less than the gap between one
#: staff position and the next, so an edit can never slide onto its neighbour.
EDIT_ANCHOR_SLACK = 0.5


def measure_accidentals(system, staff):
    """Re-derive every pitch on *staff*, honouring the measure the accidental sits in.

    Written accidentals hold to the end of their measure at that staff position, which
    is what the notation means and what ``geometry.apply_alterations`` does on the way
    in.  This is the same rule applied to a stored reading, where the measures come
    from the barlines the geometry recorded rather than from the mask.

    Deliberately not folded into :func:`restate_staff`, which ignores the measure rule
    and so drops a carried accidental every time a key is picked.  That is a real
    inconsistency, but it is on the path every key change and every verify pass takes,
    and changing it would move readings that have nothing to do with a manual edit.
    """
    alterations = pitches.key_alterations(staff["key_fifths"])
    edges = sorted(staff["barlines"])
    slots = [(note, part, position)
             for _event, part in staff_notes(system, staff["index"])
             for position, note in enumerate(part["notes"])]
    slots.sort(key=lambda slot: slot[0]["x"])

    active = {}
    measure = 0
    for note, part, position in slots:
        while measure < len(edges) and note["x"] > edges[measure]:
            measure += 1
            active = {}
        step, _err = pitches.y_to_step(note["y"], staff["lines"], staff["clef"])
        written = note["accidental"]
        if written in ACCIDENTAL_ALTER:
            alter = ACCIDENTAL_ALTER[written]
            from_key = False
            active[step] = alter
        elif step in active:
            # The carry.  Not from_key: the key is not what made this note flat, the
            # accidental earlier in the measure is, and the overlay says so differently.
            alter = active[step]
            from_key = False
        else:
            alter = alterations.get(pitches.step_letter(step), 0)
            from_key = alter != 0
        part["notes"][position] = restate_note(note, step, alter, staff, from_key)


def _staff_of(system, staff_index):
    return next((s for s in system["staves"] if s["index"] == staff_index), None)


def _note_at(document, edit):
    """The note a stored edit points at, or None if it no longer points at one."""
    system = next((s for s in document["systems"]
                   if s["index"] == edit.get("system")), None)
    if system is None:
        return None, None
    event = next((e for e in system["events"] if e["index"] == edit.get("event")), None)
    if event is None:
        return None, None
    part = next((p for p in event["parts"] if p["staff"] == edit.get("staff")), None)
    if part is None:
        return None, None
    position = edit.get("note")
    if not isinstance(position, int) or not 0 <= position < len(part["notes"]):
        return None, None
    note = part["notes"][position]
    # The y is the anchor of last resort.  Indices move when a chord is deleted and are
    # fixed up when it is, but a re-analysis renumbers everything, and an edit that has
    # come to point at some other notehead is worse than an edit that is dropped.
    if abs(note["y"] - edit.get("y", note["y"])) > EDIT_ANCHOR_SLACK:
        return None, None
    return system, note


def apply_edits(document):
    """Re-apply every stored accidental edit, then re-spell and re-name.

    Called after anything that re-derives pitches from the key or from the vision
    pass, both of which re-read a notehead's accidental off the staff and would
    otherwise quietly undo a person's correction.  Edits that no longer point at a
    notehead are dropped from the document rather than kept as dead weight.
    """
    kept, touched = [], []
    for edit in document["edits"]:
        if edit.get("kind") != "accidental":
            kept.append(edit)
            continue
        system, note = _note_at(document, edit)
        if note is None:
            continue
        note["accidental"] = edit.get("value")
        kept.append(edit)
        pair = (system["index"], edit.get("staff"))
        if pair not in touched:
            touched.append(pair)
    document["edits"] = kept
    for system_index, staff_index in touched:
        system = next(s for s in document["systems"] if s["index"] == system_index)
        staff = _staff_of(system, staff_index)
        if staff is not None:
            measure_accidentals(system, staff)
    if touched:
        name_everything(document)
    return document


def set_accidental(document, system_index, event_index, staff_index, position, value):
    """Write an accidental on one notehead by hand, or clear it with None.

    Returns True when the document changed.  The accidental then carries to the rest of
    its measure exactly as a printed one would, because the same rule resolves both.
    """
    if value is not None and value not in EDITABLE_ACCIDENTALS:
        raise ValueError("accidental must be one of %r or None, got %r"
                         % (EDITABLE_ACCIDENTALS, value))
    edit = {"kind": "accidental", "system": system_index, "event": event_index,
            "staff": staff_index, "note": position, "value": value}
    system = next((s for s in document["systems"] if s["index"] == system_index), None)
    if system is None:
        return False
    probe = dict(edit)
    probe.pop("y", None)
    system, note = _note_at(document, probe)
    if note is None:
        return False
    edit["y"] = note["y"]

    # One edit per notehead: writing a sharp and then a flat on the same note is a
    # correction of the correction, not two corrections, and a log that grows without
    # bound would replay the earlier answer over the later one.
    document["edits"] = [e for e in document["edits"]
                         if not (e.get("kind") == "accidental"
                                 and (e.get("system"), e.get("event"), e.get("staff"),
                                      e.get("note"))
                                 == (system_index, event_index, staff_index, position))]
    if value is not None:
        document["edits"].append(edit)
    note["accidental"] = value
    staff = _staff_of(system, staff_index)
    if staff is None:
        return False
    measure_accidentals(system, staff)
    name_everything(document)
    return True


def delete_event(document, system_index, event_index):
    """Drop a chord the reader says is not there.  Returns True when one went.

    The events left behind are renumbered, because the schema requires an event's
    index to be its position and everything that merges into a reading matches on it.
    Stored edits are moved along with them, and any edit on the deleted chord goes
    with the chord.
    """
    system = next((s for s in document["systems"] if s["index"] == system_index), None)
    if system is None:
        return False
    if not any(e["index"] == event_index for e in system["events"]):
        return False
    system["events"] = [e for e in system["events"] if e["index"] != event_index]
    for position, event in enumerate(system["events"]):
        event["index"] = position

    moved = []
    for edit in document["edits"]:
        if edit.get("system") != system_index or not isinstance(edit.get("event"), int):
            moved.append(edit)
            continue
        if edit["event"] == event_index:
            continue
        if edit["event"] > event_index:
            edit["event"] -= 1
        moved.append(edit)
    document["edits"] = moved
    name_everything(document)
    return True
