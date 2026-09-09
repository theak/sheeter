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



# --------------------------------------------------------------------------
# correcting a reading by hand
#
# What is durable and what has to be replayed is decided by one thing: whether the
# field a correction writes is re-derived from the geometry afterwards.
#
# Moving, adding and removing a notehead all change the geometry itself, so they
# survive anything that re-reads it: restate_staff walks the noteheads that are
# there and works each pitch out from its own y.  A written accidental is the one
# correction that does not survive, because re-deriving reads that field and will
# resolve it against the key instead.  So that is the only kind kept for replay, and
# replaying a structural change onto a structure that already contains it is a
# question this deliberately never has to answer.


#: What a reader may write in front of a notehead by hand.  Double accidentals are
#: deliberately absent: they exist in ACCIDENTAL_ALTER because the reader can detect
#: one on the page, but nobody fixing a misread note by eye reaches for a double sharp,
#: and offering five buttons where three will do is how a simple tool stops being one.
EDITABLE_ACCIDENTALS = ("sharp", "flat", "natural")

#: How far a stored correction's notehead may have moved before the correction is taken
#: to be about some other note.  A quarter of a step, and a step is half a staff space,
#: so it scales with the photo instead of being a pixel count: a page at unit 20 allows
#: 2.5px, which is loose enough to survive a re-reading nudging a notehead and tight
#: enough that a correction can never slide onto the staff position next door.
EDIT_ANCHOR_STEP_FRACTION = 0.25

#: How far above and below its own staff the pitch menu reaches, in diatonic steps.
#: An octave either side covers what a grand staff is actually written in, including
#: the ledger lines the reader can read, without a menu nobody can scroll.
PITCH_MENU_REACH = 7


def _anchor_slack(staff):
    return (staff["unit"] or 20.0) / 2.0 * EDIT_ANCHOR_STEP_FRACTION


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


def pitch_choices(staff):
    """The pitches the menu offers on *staff*, low to high.

    Spelled without an accidental, because the three accidental buttons are what say
    that: the menu picks the staff position, which is the thing a notehead in the wrong
    space actually got wrong.
    """
    bottom = pitches.CLEF_BOTTOM_LINE_STEP[staff["clef"]]
    out = []
    for step in range(bottom - PITCH_MENU_REACH, bottom + 9 + PITCH_MENU_REACH):
        out.append({"step": step, "name": pitches.step_to_name(step),
                    "pretty": pitches.step_to_pretty(step)})
    return out


# ---------------------------------------------------------------- finding things


def _system_of(document, system_index):
    return next((s for s in document["systems"] if s["index"] == system_index), None)


def _staff_of(system, staff_index):
    return next((s for s in system["staves"] if s["index"] == staff_index), None)


def _locate(document, system_index, event_index, staff_index):
    """``(system, staff, event, part)``, any of which may be None.

    *part* is None when that staff plays nothing in that event, which is not an error:
    it is exactly the case a reader is in when a whole hand went missing and they want
    to put a note back.
    """
    system = _system_of(document, system_index)
    if system is None:
        return None, None, None, None
    staff = _staff_of(system, staff_index)
    event = next((e for e in system["events"] if e["index"] == event_index), None)
    if staff is None or event is None:
        return system, staff, event, None
    return system, staff, event, next(
        (p for p in event["parts"] if p["staff"] == staff_index), None)


def _at_index(part, position):
    if part is None or not isinstance(position, int):
        return None
    return part["notes"][position] if 0 <= position < len(part["notes"]) else None


def _at_height(part, staff, y):
    """The notehead a stored correction points at, found by where it sits.

    By height and not by index, so inserting or removing a notehead does not silently
    re-point every correction after it in the chord.  The nearest within the slack
    wins, and two noteheads are never that close: a staff position is four times it.
    """
    if part is None:
        return None
    slack = _anchor_slack(staff)
    best = None
    for note in part["notes"]:
        gap = abs(note["y"] - y)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, note)
    return best[1] if best else None


def _forget(document, system_index, event_index, staff_index, staff, y):
    """Drop any stored correction on the notehead at *y*.  One per notehead."""
    slack = _anchor_slack(staff)
    document["edits"] = [
        edit for edit in document["edits"]
        if not (edit["system"] == system_index and edit["event"] == event_index
                and edit["staff"] == staff_index and abs(edit["y"] - y) <= slack)]


def _sort_notes(part):
    """Low to high, the order every reader of a chord expects and the overlay assumes."""
    part["notes"].sort(key=lambda note: (note["midi"], -note["y"]))


def _settle(document, system, staff):
    measure_accidentals(system, staff)
    for _event, part in staff_notes(system, staff["index"]):
        _sort_notes(part)
    name_everything(document)
    return True


# ---------------------------------------------------------------- the corrections


def set_accidental(document, system_index, event_index, staff_index, position, value):
    """Write an accidental on one notehead by hand, or clear it with None.

    Returns True when the document changed.  Asking for the accidental the note already
    sounds is not a change: the buttons show which one is in force, so clicking that one
    is a reader agreeing with the page, and recording a correction for it would fill the
    log with edits that correct nothing.
    """
    if value is not None and value not in EDITABLE_ACCIDENTALS:
        raise ValueError("accidental must be one of %r or None, got %r"
                         % (EDITABLE_ACCIDENTALS, value))
    system, staff, _event, part = _locate(document, system_index, event_index,
                                          staff_index)
    note = _at_index(part, position)
    if note is None:
        return False
    if value is None:
        if note["accidental"] is None:
            return False
    elif ACCIDENTAL_ALTER[value] == note["alter"]:
        return False

    y = note["y"]
    _forget(document, system_index, event_index, staff_index, staff, y)
    if value is not None:
        document["edits"].append({
            "kind": "accidental", "system": system_index, "event": event_index,
            "staff": staff_index, "value": value, "y": y,
        })
    note["accidental"] = value
    return _settle(document, system, staff)


def set_pitch(document, system_index, event_index, staff_index, position, step):
    """Move one notehead to another staff position.

    The pitch follows the position rather than being stored beside it, so this writes
    the y and lets the same arithmetic as everything else name it.  That is also what
    makes it outlive a key pick with nothing replayed: re-deriving reads the y.
    """
    system, staff, _event, part = _locate(document, system_index, event_index,
                                          staff_index)
    note = _at_index(part, position)
    if note is None or not isinstance(step, int):
        return False
    lowest = pitches.CLEF_BOTTOM_LINE_STEP[staff["clef"]] - PITCH_MENU_REACH
    if not lowest <= step < lowest + 9 + 2 * PITCH_MENU_REACH:
        return False
    y = pitches.step_to_y(step, staff["lines"], staff["clef"])
    if abs(y - note["y"]) <= _anchor_slack(staff):
        return False                      # already on that position

    # The correction on this notehead, if it has one, moves with it: it is the same
    # notehead, and its anchor is where the notehead is.
    carried = next((edit for edit in document["edits"]
                    if edit["system"] == system_index and edit["event"] == event_index
                    and edit["staff"] == staff_index
                    and abs(edit["y"] - note["y"]) <= _anchor_slack(staff)), None)
    note["y"] = float(y)
    note["ledger"] = pitches.ledger_count(step, staff["clef"])
    note["step_err_px"] = 0.0
    note["changed"] = True
    if carried is not None:
        carried["y"] = float(y)
    return _settle(document, system, staff)


def delete_note(document, system_index, event_index, staff_index, position):
    """Take one notehead out of a chord.

    A hand left with no noteheads is not a hand that plays a rest, it is a hand the
    reader says has nothing here, so the part goes; an event with no parts at all is
    the whole chord gone, and that is delete_event's job including the renumbering.
    """
    system, staff, event, part = _locate(document, system_index, event_index,
                                         staff_index)
    note = _at_index(part, position)
    if note is None:
        return False
    _forget(document, system_index, event_index, staff_index, staff, note["y"])
    part["notes"].pop(position)
    if not part["notes"]:
        event["parts"] = [other for other in event["parts"] if other is not part]
    if not event["parts"]:
        return delete_event(document, system_index, event_index)
    return _settle(document, system, staff)


def add_note(document, system_index, event_index, staff_index, step):
    """Put a notehead the reader missed into a chord.

    Also the way a whole hand comes back: a staff that plays nothing in this event has
    no part to add to, so one is made.  The new notehead is given the size of the ones
    already on the staff, so the overlay marker matches its neighbours rather than
    announcing itself.
    """
    system, staff, event, part = _locate(document, system_index, event_index,
                                         staff_index)
    if system is None or staff is None or event is None or not isinstance(step, int):
        return False
    lowest = pitches.CLEF_BOTTOM_LINE_STEP[staff["clef"]] - PITCH_MENU_REACH
    if not lowest <= step < lowest + 9 + 2 * PITCH_MENU_REACH:
        return False
    y = pitches.step_to_y(step, staff["lines"], staff["clef"])
    if part is not None and _at_height(part, staff, y) is not None:
        return False                      # that position is already sounding

    sizes = [(other["w"], other["h"], other["hollow"])
             for _e, other_part in staff_notes(system, staff_index)
             for other in other_part["notes"] if other["w"] and other["h"]]
    width, height, hollow = sizes[0] if sizes else (staff["unit"] * 1.2,
                                                    staff["unit"] * 0.8, False)
    note = pitches.make_note(
        step, 0, event["x"], y, w=width, h=height, hollow=hollow,
        ledger=pitches.ledger_count(step, staff["clef"]),
        confidence=1.0, changed=True)
    if part is None:
        part = {"staff": staff_index, "hand": staff["hand"],
                "duration_hint": "quarter-or-shorter", "dotted": False,
                "notes": [], "chord": None}
        event["parts"].append(part)
        event["parts"].sort(key=lambda entry: entry["staff"])
    part["notes"].append(note)
    return _settle(document, system, staff)


def delete_event(document, system_index, event_index):
    """Drop a chord the reader says is not there.  Returns True when one went.

    The events left behind are renumbered, because the schema requires an event's
    index to be its position and everything that merges into a reading matches on it.
    Stored corrections are moved along with them, and any correction on the deleted
    chord goes with the chord.
    """
    system = _system_of(document, system_index)
    if system is None:
        return False
    if not any(event["index"] == event_index for event in system["events"]):
        return False
    system["events"] = [event for event in system["events"]
                        if event["index"] != event_index]
    for position, event in enumerate(system["events"]):
        event["index"] = position

    moved = []
    for edit in document["edits"]:
        if edit["system"] != system_index:
            moved.append(edit)
        elif edit["event"] == event_index:
            continue
        else:
            if edit["event"] > event_index:
                edit["event"] -= 1
            moved.append(edit)
    document["edits"] = moved
    name_everything(document)
    return True


def apply_edits(document):
    """Re-apply every stored accidental, then re-spell and re-name.

    Called after anything that re-derives a pitch from the key or from the vision pass,
    both of which read a notehead's accidental and would otherwise quietly undo a
    person's correction.  Corrections that no longer point at a notehead are dropped
    rather than kept as dead weight, and never moved onto whatever is nearest.
    """
    kept, touched = [], []
    for edit in document["edits"]:
        system, staff, _event, part = _locate(
            document, edit["system"], edit["event"], edit["staff"])
        if staff is None:
            continue
        note = _at_height(part, staff, edit["y"])
        if note is None:
            continue
        note["accidental"] = edit["value"]
        kept.append(edit)
        if (system["index"], staff["index"]) not in touched:
            touched.append((system["index"], staff["index"]))
    document["edits"] = kept
    for system_index, staff_index in touched:
        system = _system_of(document, system_index)
        _settle(document, system, _staff_of(system, staff_index))
    return document
