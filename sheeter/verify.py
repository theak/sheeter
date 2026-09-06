"""The optional Claude vision pass that corrects the geometry reading.

Nothing here is required to ship an analysis.  The geometry reading is the
product; this module is a second opinion on it.  Every entry point is total: if
there is no API key, no network, or the model answers with rubbish, the caller
gets its document back with ``engine.verified`` False and ``engine.verifier_error``
set to a sentence a human can read.

Two passes, both optional and independent:

``verify_analysis``
    One vision call per document.  Sends the processed PNG (plus zoomed crops of
    each system, which the model reads far better than the full page) together
    with a slim JSON view of the current reading, and asks for corrections
    through a tool with a strict schema.  Following plan section 5 there is no
    OMR engine in front of this, so the clef and key signature are geometry's
    guesses and the model is told to re-read both from the image.

Merging is deliberately timid.  A pitch moves only when the model returned a
well formed name for that exact slot; everything else is re-derived from
``sheeter.pitches`` so the document stays internally consistent, and the merged
copy has to pass ``schema.validate_analysis`` before it is allowed out.
"""

import base64
import copy
import io
import json
import os
import sys

from PIL import Image

from . import naming, pitches, schema

DEFAULT_VERIFY_MODEL = "claude-sonnet-5"

CORRECTION_TOOL = "corrected_reading"

#: Sonnet 5 is in the high resolution vision tier: 2576 px on the long edge,
#: above which the API downscales the image for us.  Doing it here instead keeps
#: the request small and keeps us in control of the resampling filter.
MAX_IMAGE_PX = 1568

#: Below this width the whole page is legible in one block and per-system crops
#: would only repeat it.
WIDE_IMAGE_PX = 1600

#: Total image blocks in one request, full page included.  A page of six systems
#: at 2576 px is roughly 4800 visual tokens each, so an uncapped request would be
#: both slow and expensive for very little extra signal.
MAX_IMAGE_BLOCKS = 8

VERIFY_MAX_TOKENS = 8000

_ACCIDENTAL_ALTER = {
    "flat": -1, "sharp": 1, "natural": 0, "double-flat": -2, "double-sharp": 2,
}

SYSTEM_PROMPT = """\
You are an expert music engraver and jazz pianist verifying an automated optical
music recognition result. You will receive printed sheet music as one or more
images, plus a JSON reading of it produced by a deterministic pipeline. Your job
is to check the reading against the image and correct it.

The clef and key signature in the JSON were guessed from staff order alone and
have not been read from the image. First identify the clef and key signature of
every staff from the image, then correct every pitch accordingly.

Rules:
- Work event by event, in left-to-right order. Count noteheads in the image for
  each event and compare.
- Pitch = vertical position on the staff. Use the clef, ledger lines, and the
  staff-space unit in the JSON to place every notehead; do not guess from the
  shape of the chord.
- Apply the key signature to every note unless an accidental on that note
  overrides it. An accidental's vertical reference is the midpoint of its
  crossbars (sharp, natural) or the centre of its bowl (flat).
- Notes a second apart are drawn side by side; the left-displaced notehead
  belongs to the same chord.
- Hollow noteheads are half or whole notes; filled are quarters or shorter.
- If a clef change or new key signature appears mid-system, report it in
  system_notes and re-read everything to its right.
- If part of a staff is cut off by the image edge, say so; do not invent notes
  for it.
- Only change a pitch if you are confident the image contradicts the reading.
  Give a confidence 0 to 1 per event.
- Return every event you were given, corrected or not, through the
  corrected_reading tool. Pitch names are ascii: a letter, then # or b repeated
  once per semitone, then the octave, e.g. Bb3, F#5, C4.
"""


# --------------------------------------------------------------------------
# availability


def available():
    """True when a verification pass could actually run."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _new_client():
    from . import claude

    return claude.Client()


def _reason(exc):
    text = "%s: %s" % (exc.__class__.__name__, " ".join(str(exc).split()))
    return text[:157] + "..." if len(text) > 160 else text


def _skipped(doc, reason):
    out = copy.deepcopy(doc)
    out["engine"]["verifier"] = None
    out["engine"]["verified"] = False
    out["engine"]["verifier_error"] = reason
    return out


# --------------------------------------------------------------------------
# the request


def _resized(image):
    long_side = max(image.size)
    if long_side <= MAX_IMAGE_PX:
        return image
    scale = MAX_IMAGE_PX / float(long_side)
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    return image.resize(size, Image.LANCZOS)


def _png_block(image):
    buf = io.BytesIO()
    _resized(image).save(buf, format="PNG")
    return _raw_block(buf.getvalue())


def _raw_block(png_bytes):
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png_bytes).decode("ascii"),
        },
    }


#: Events per zoomed crop, and the staff space those crops are scaled to.  Dense
#: chords are read far better at this size than at the size a phone photo of a whole
#: page puts them, which is the whole reason for cropping rather than sending the page.
EVENTS_PER_CROP = 3
TARGET_UNIT_PX = 46


def _system_box(image, system, unit):
    top = max(0, int(system["y_range"][0] - unit * 2))
    bottom = min(image.height, int(system["y_range"][1] + unit * 2))
    return top, bottom


def _zoom(image, box, unit):
    """Crop and scale so a staff space lands near TARGET_UNIT_PX."""
    crop = image.crop(box)
    if crop.width < 8 or crop.height < 8:
        return None
    factor = TARGET_UNIT_PX / float(unit) if unit else 1.0
    factor = min(factor, MAX_IMAGE_PX / float(max(crop.size)))
    if factor > 1.02:
        crop = crop.resize((max(1, int(crop.width * factor)),
                            max(1, int(crop.height * factor))), Image.LANCZOS)
    return crop


def _system_blocks(image, system, limit):
    """A look at the clef and key signature, then the events a few at a time.

    *limit* caps how many crops are cut.  Cropping, resizing and PNG-encoding a crop is
    the expensive part of building the request, and without the cap a page of six
    systems does that work three times over for crops the budget then throws away.
    """
    units = [s["unit"] for s in system["staves"] if s.get("unit")]
    if not units:
        return []
    unit = max(units)
    top, bottom = _system_box(image, system, unit)
    if bottom - top < 8:
        return []

    blocks = []
    if limit <= 0:
        return blocks
    left = min(s["x_range"][0] for s in system["staves"])
    prelude = _zoom(image, (max(0, int(left - unit)), top,
                            min(image.width, int(left + unit * 12)), bottom), unit)
    if prelude is not None:
        blocks.append({"type": "text", "text":
                       "System %d, clef and key signature:" % system["index"]})
        blocks.append(_raw_png(prelude))

    events = system["events"]
    for start in range(0, len(events), EVENTS_PER_CROP):
        if len(blocks) // 2 >= limit:
            break
        chunk = events[start:start + EVENTS_PER_CROP]
        x0 = max(0, int(min(e["x_range"][0] for e in chunk) - unit * 1.5))
        x1 = min(image.width, int(max(e["x_range"][1] for e in chunk) + unit * 1.5))
        crop = _zoom(image, (x0, top, x1, bottom), unit)
        if crop is None:
            continue
        blocks.append({"type": "text", "text": "System %d, events %d to %d:" % (
            system["index"], chunk[0]["index"], chunk[-1]["index"])})
        blocks.append(_raw_png(crop))
    return blocks


def _raw_png(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return _raw_block(buf.getvalue())


def _image_blocks(doc, image_png_bytes):
    try:
        page = Image.open(io.BytesIO(image_png_bytes))
        page.load()
    except Exception:
        return [{"type": "text", "text": "Page:"}, _raw_block(image_png_bytes)]

    blocks = [{"type": "text", "text": "Whole page:"}, _png_block(page)]
    budget = MAX_IMAGE_BLOCKS - 1
    # Breadth first: every system gets its clef and key signature looked at before any
    # system gets a second detailed crop, so a page is never read from its first line.
    systems = doc["systems"] or []
    share = max(1, budget // max(1, len(systems)))
    per_system = [_system_blocks(page, system, share + 1) for system in systems]
    for round_index in range(0, max([len(b) for b in per_system] or [0]), 2):
        for system_blocks in per_system:
            if round_index + 1 >= len(system_blocks) or budget <= 0:
                continue
            blocks.extend(system_blocks[round_index:round_index + 2])
            budget -= 1
    return blocks


def _slim(doc):
    """The reading as the model should see it: positions and names, no chords."""
    systems = []
    for system in doc["systems"]:
        staves = [
            {
                "index": s["index"],
                "hand": s["hand"],
                "clef": s["clef"],
                "key_fifths": s["key_fifths"],
                "unit": round(s["unit"], 1),
                "lines": [round(y, 1) for y in s["lines"]],
            }
            for s in system["staves"]
        ]
        events = []
        for event in system["events"]:
            events.append({
                "index": event["index"],
                "x_range": [round(v, 1) for v in event["x_range"]],
                "parts": [
                    {
                        "staff": p["staff"],
                        "notes": [
                            {"name": n["name"], "y": round(n["y"], 1)}
                            for n in p["notes"]
                        ],
                    }
                    for p in event["parts"]
                ],
            })
        systems.append({"index": system["index"], "staves": staves, "events": events})
    return {"systems": systems}


def _correction_tool():
    staff = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "staff index within the system"},
            # Only the two clefs the reader supports.  schema.CLEFS also lists alto
            # and tenor, and accepting one of those would re-spell every notehead on
            # the staff several steps off with nothing downstream to catch it.
            "clef": {"type": "string", "enum": ["treble", "bass"]},
            "key_fifths": {
                "type": "integer",
                # Strict tool schemas reject minimum and maximum on an integer, so the
                # range is stated here and enforced when the response is applied.
                "description": "-7 to 7: sharps positive, flats negative, 0 for none",
            },
            "cut_off": {"type": "boolean",
                        "description": "the staff runs off the edge of the photo"},
        },
        "required": ["index", "clef", "key_fifths", "cut_off"],
        "additionalProperties": False,
    }
    part = {
        "type": "object",
        "properties": {
            "staff": {"type": "integer"},
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "corrected pitch names low to high, e.g. Bb3",
            },
        },
        "required": ["staff", "notes"],
        "additionalProperties": False,
    }
    event = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "parts": {"type": "array", "items": part},
            "confidence": {"type": "number",
                           "description": "0 to 1, how sure you are of this event"},
            "note": {"type": "string",
                     "description": "one sentence about this event, or empty"},
        },
        "required": ["index", "parts", "confidence", "note"],
        "additionalProperties": False,
    }
    system = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "staves": {"type": "array", "items": staff},
            "events": {"type": "array", "items": event},
        },
        "required": ["index", "staves", "events"],
        "additionalProperties": False,
    }
    return {
        "name": CORRECTION_TOOL,
        "description": "Return the corrected reading of the sheet music.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "systems": {"type": "array", "items": system},
                "system_notes": {"type": "string",
                                 "description": "one line about the page, or empty"},
            },
            "required": ["systems", "system_notes"],
            "additionalProperties": False,
        },
    }


def build_request(doc, image_png_bytes):
    """The Messages payload for the vision pass, minus the model id."""
    content = _image_blocks(doc, image_png_bytes)
    content.append({
        "type": "text",
        "text": "Here is the pipeline's reading:\n%s\nReturn the corrected reading."
                % _json(_slim(doc)),
    })
    return {
        "max_tokens": VERIFY_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "tools": [_correction_tool()],
        "tool_choice": {"type": "tool", "name": CORRECTION_TOOL},
    }


def _json(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _field(block, name):
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _tool_input(response, tool_name):
    """The input of the first ``tool_name`` block, or None."""
    if _field(response, "stop_reason") == "refusal":
        return None
    for block in _field(response, "content") or []:
        if _field(block, "type") == "tool_use" and _field(block, "name") == tool_name:
            value = _field(block, "input")
            return value if isinstance(value, dict) else None
    return None


# --------------------------------------------------------------------------
# merging a response back into the document


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_pitch(name):
    """``(step, alter)`` for a well formed audible pitch name, else None."""
    if not isinstance(name, str):
        return None
    try:
        step, alter = pitches.parse_name(name.strip())
    except Exception:
        return None
    if not -2 <= alter <= 2:
        return None
    if not 0 <= pitches.step_to_midi(step, alter) <= 127:
        return None
    return step, alter


def _rebuild(note, step, alter, staff, from_key):
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


def _staff_notes(system, staff_index):
    for event in system["events"]:
        for part in event["parts"]:
            if part["staff"] == staff_index:
                yield event, part


def _reread(system, staff):
    """Re-derive every pitch on *staff* from its y, under the current clef and key.

    A corrected clef or key signature invalidates the reading of every notehead
    on the staff, not only the ones the model bothered to mention.  Notes whose
    name moves are marked changed: the verifier did change them, by way of the
    clef, and the overlay should say so.
    """
    alterations = pitches.key_alterations(staff["key_fifths"])
    for _event, part in _staff_notes(system, staff["index"]):
        notes = []
        for note in part["notes"]:
            step, _err = pitches.y_to_step(note["y"], staff["lines"], staff["clef"])
            if note["accidental"] in _ACCIDENTAL_ALTER:
                alter = _ACCIDENTAL_ALTER[note["accidental"]]
                from_key = False
            else:
                alter = alterations.get(pitches.step_letter(step), 0)
                from_key = alter != 0
            notes.append(_rebuild(note, step, alter, staff, from_key))
        part["notes"] = notes


def _apply_staves(system, payload_staves):
    """Apply clef, key and cut_off; returns the staves that actually changed."""
    changed = []
    by_index = dict((s["index"], s) for s in system["staves"])
    for entry in payload_staves or []:
        if not isinstance(entry, dict):
            continue
        staff = by_index.get(_int(entry.get("index")))
        if staff is None:
            continue
        moved = False
        clef = entry.get("clef")
        if clef in ("treble", "bass") and clef != staff["clef"]:
            staff["clef"] = clef
            moved = True
        fifths = _int(entry.get("key_fifths"))
        if fifths is not None and -7 <= fifths <= 7 and fifths != staff["key_fifths"]:
            staff["key_fifths"] = fifths
            moved = True
        if isinstance(entry.get("cut_off"), bool):
            staff["cut_off"] = entry["cut_off"]
        if moved:
            changed.append(staff)
    return changed


def _apply_names(part, names, staff, event):
    """Replace *part*'s pitches with the model's, when they are well formed."""
    if not isinstance(names, list) or not names:
        return
    parsed = [_parse_pitch(n) for n in names]
    alterations = pitches.key_alterations(staff["key_fifths"])

    # Our note lists run low to high, but a model reading a chord off the page
    # naturally reads it top to bottom.  Slot i has to mean the same notehead on both
    # sides or the overlay lands on the wrong line, so sort rather than guess the
    # direction: an answer that is neither strictly ascending nor strictly descending,
    # which one transposition or one repeated pitch is enough to produce, would
    # otherwise be zipped on in whatever order it arrived.
    # Sort the entries that parsed and leave the rest where they are, so a single junk
    # name cannot silently turn off the ordering and zip every other correction onto the
    # wrong notehead.  _rename_chords re-sorts by midi afterwards, which would hide it.
    usable = sorted((index for index, pitch in enumerate(parsed) if pitch is not None),
                    key=lambda index: pitches.step_to_midi(*parsed[index]))
    ordered = list(parsed)
    for slot, index in zip(sorted(usable), usable):
        ordered[slot] = parsed[index]
    parsed = ordered

    if len(parsed) == len(part["notes"]):
        notes = []
        for note, pitch in zip(part["notes"], parsed):
            if pitch is None:
                notes.append(note)
                continue
            step, alter = pitch
            from_key = (note["accidental"] is None and alter != 0
                        and alterations.get(pitches.step_letter(step)) == alter)
            notes.append(_rebuild(note, step, alter, staff, from_key))
        part["notes"] = notes
        return

    # A different note count means the model saw a chord we did not.  There is no
    # geometry to keep, so the noteheads are re-anchored onto the staff's own
    # pitch grid and the overlay still lands on the right line.
    if any(p is None for p in parsed):
        return
    notes = []
    for step, alter in parsed:
        notes.append(pitches.make_note(
            step, alter, event["x"],
            pitches.step_to_y(step, staff["lines"], staff["clef"]),
            from_key=alterations.get(pitches.step_letter(step)) == alter and alter != 0,
            ledger=pitches.ledger_count(step, staff["clef"]),
            confidence=event["confidence"], changed=True,
        ))
    part["notes"] = notes


def _apply_events(system, payload_events, staves):
    by_index = dict((e["index"], e) for e in system["events"])
    for entry in payload_events or []:
        if not isinstance(entry, dict):
            continue
        event = by_index.get(_int(entry.get("index")))
        if event is None:
            continue
        confidence = entry.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            if 0.0 <= confidence <= 1.0:
                event["confidence"] = float(confidence)
        text = entry.get("note")
        if isinstance(text, str) and text.strip():
            event["note"] = text.strip()[:200]
        for part_entry in entry.get("parts") or []:
            if not isinstance(part_entry, dict):
                continue
            index = _int(part_entry.get("staff"))
            part = next((p for p in event["parts"] if p["staff"] == index), None)
            staff = staves.get(index)
            if part is None or staff is None:
                continue
            _apply_names(part, part_entry.get("notes"), staff, event)


def _rename_chords(system, before):
    """Re-run naming wherever the pitches moved, and leave the rest alone.

    An untouched event keeps the symbol the pipeline gave it.  Renaming the whole
    system would reseed ``name_chord``'s prev_names chain from event 0, which can
    change a symbol nothing in the response asked about.
    """
    staves = dict((s["index"], s) for s in system["staves"])
    key = system["staves"][0]["key_fifths"] if system["staves"] else 0
    was = dict(((e["index"], p["staff"]), [n["name"] for n in p["notes"]])
               for e in before["events"] for p in e["parts"])
    previous = {}
    for event in system["events"]:
        moved = False
        for part in event["parts"]:
            part["notes"].sort(key=lambda n: n["midi"])
            names = [n["name"] for n in part["notes"]]
            if names != was.get((event["index"], part["staff"])):
                staff = staves.get(part["staff"])
                part["chord"] = naming.name_chord(
                    names, staff["key_fifths"] if staff else key,
                    previous.get(part["staff"]),
                )
                moved = True
            previous[part["staff"]] = names
        if moved:
            # De-duplicated the same way pipeline.name_everything does it: a pitch
            # doubled between the hands is one note of the chord, not two, and listing
            # it twice would show up in combined.names on the page.
            pooled, seen = [], set()
            for note in sorted((n for p in event["parts"] for n in p["notes"]),
                               key=lambda n: n["midi"]):
                if note["name"] not in seen:
                    seen.add(note["name"])
                    pooled.append(note["name"])
            event["combined"] = naming.name_combined(pooled, key)


def _merge(doc, payload):
    """Returns ``(document, systems_resolved)``.

    The count matters: a response naming no system we know about merges cleanly and
    changes nothing, and without it the caller cannot tell that from a real check.
    """
    resolved = 0
    out = copy.deepcopy(doc)
    by_index = dict((s["index"], s) for s in out["systems"])
    # out is a copy, so doc still holds each system as it was before the merge.
    before = dict((s["index"], s) for s in doc["systems"])
    for entry in payload.get("systems") or []:
        if not isinstance(entry, dict):
            continue
        system = by_index.get(_int(entry.get("index")))
        if system is None:
            continue
        resolved += 1
        for staff in _apply_staves(system, entry.get("staves")):
            _reread(system, staff)
        _apply_events(system, entry.get("events"),
                      dict((s["index"], s) for s in system["staves"]))
        _rename_chords(system, before[system["index"]])

    notes = payload.get("system_notes")
    if isinstance(notes, str) and notes.strip():
        line = notes.strip()[:300]
        if line not in out["warnings"]:
            out["warnings"].append(line)
    return out, resolved


#: A geometry reading this sure is not overruled from a photograph.  Measured against
#: the fixture corpus the geometry reads 157 of 157 noteheads and the vision pass reads
#: 151, all six of its losses being notes on ledger lines, where counting staff
#: positions by eye is exactly what a language model is worst at.  So the vision pass
#: arbitrates where the geometry admits doubt and confirms where it does not, and its
#: commentary is kept either way.
CONFIDENT_NOTE = 0.88
CONFIDENT_STAFF = 0.9


def geometry_is_sure(system):
    """Did the geometry read this system without reservation?"""
    if system["cut_off"]:
        return False
    for staff in system["staves"]:
        if min(staff["key_confidence"], staff["clef_confidence"]) < CONFIDENT_STAFF:
            return False
    for event in system["events"]:
        for part in event["parts"]:
            if any(note["confidence"] < CONFIDENT_NOTE for note in part["notes"]):
                return False
    return True


def _enforce_trust(before, after):
    """Put back the measurements of any system the geometry was sure about.

    The verifier keeps its per-event notes and its page-level remarks, which are the
    part of its answer that is worth having on a reading that was already right.
    """
    for original, revised in zip(before["systems"], after["systems"]):
        if not geometry_is_sure(original):
            continue
        for was, now in zip(original["staves"], revised["staves"]):
            now["clef"] = was["clef"]
            now["key_fifths"] = was["key_fifths"]
        if len(revised["events"]) != len(original["events"]):
            revised["events"] = copy.deepcopy(original["events"])
            continue
        for was, now in zip(original["events"], revised["events"]):
            now["parts"] = copy.deepcopy(was["parts"])
            now["combined"] = copy.deepcopy(was["combined"])
    return after


def apply_correction(doc, payload):
    """Merge a ``corrected_reading`` payload into *doc*.

    Returns a new document, or *doc* itself when the merge would not survive
    :func:`schema.validate_analysis`.  Anything malformed in *payload* is
    ignored rather than acted on, so a partly broken response still buys the
    corrections it got right.
    """
    return _correct(doc, payload)[0]


def _correct(doc, payload):
    """``(document, systems_resolved)``.  Resolved is 0 when nothing was applied."""
    if not isinstance(payload, dict):
        return doc, 0
    try:
        merged, resolved = _merge(doc, payload)
        out = _enforce_trust(doc, merged)
        schema.validate_analysis(out)
    except Exception:
        return doc, 0
    return out, resolved


# --------------------------------------------------------------------------
# the two passes


def verify_analysis(doc, image_png_bytes, client=None, model=None):
    """Correct *doc* against its own photo.  Returns a new document, never raises.

    On any failure the input document comes back with ``engine.verified`` False
    and ``engine.verifier_error`` explaining why.
    """
    model = model or os.environ.get("SHEETER_VERIFY_MODEL") or DEFAULT_VERIFY_MODEL
    if client is None:
        if not available():
            return _skipped(doc, "no ANTHROPIC_API_KEY set, kept the geometry reading")
        try:
            client = _new_client()
        except Exception as exc:
            return _skipped(doc, _reason(exc))
    try:
        request = build_request(doc, image_png_bytes)
        # No temperature: the current top tier models reject sampling parameters,
        # and no thinking parameter either, which leaves Sonnet 5 on adaptive.
        response = client.messages.create(model=model, **request)
        payload = _tool_input(response, CORRECTION_TOOL)
    except Exception as exc:
        return _skipped(doc, _reason(exc))
    if payload is None:
        return _skipped(doc, "the verifier returned no corrected_reading")

    out, resolved = _correct(doc, payload)
    if out is doc:
        return _skipped(doc, "the corrected reading did not fit the schema")
    if not resolved:
        # A response that named no system we know about merges cleanly and changes
        # nothing.  Calling that verified would put "checked by Claude" on the page and
        # take the button away, so it counts as a miss.
        return _skipped(doc, "the verifier did not answer about this page")
    out["engine"]["verifier"] = model
    out["engine"]["verified"] = True
    out["engine"]["verifier_error"] = None
    return out
