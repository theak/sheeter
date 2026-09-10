"""The analysis document: scaffolds and a validator.

``docs/schema.md`` is the prose contract; this module is the executable one.
Every write to disk goes through :func:`validate_analysis`, so a malformed
document fails at the point it is produced rather than in the browser.
"""

SCHEMA_VERSION = 2

#: What the geometry will commit to.  "quarter-or-shorter" rather than "quarter" and
#: "eighth" because telling those apart means reading the flag or the beam, and the
#: measurements say that cannot be done reliably here.  See geometry.duration_hint.
DURATION_HINTS = ("whole", "half", "quarter-or-shorter", None)
ACCIDENTALS = ("flat", "sharp", "natural", "double-flat", "double-sharp", None)
CLEFS = ("treble", "bass", "alto", "tenor")
HANDS = ("right", "left", None)

NOTE_KEYS = (
    "step", "alter", "octave", "name", "pretty", "midi", "x", "y", "w", "h",
    "hollow", "accidental", "from_key", "ledger", "step_err_px", "confidence",
    "changed",
)
CHORD_KEYS = (
    "symbol", "common_name", "root", "bass", "quality", "intervals_from_bass",
)
COMBINED_KEYS = (
    "names", "pretty", "symbol", "common_name", "root", "bass", "quality",
    "roman", "intervals_from_bass",
)
PART_KEYS = ("staff", "hand", "duration_hint", "dotted", "notes", "chord")
EVENT_KEYS = ("index", "x", "x_range", "confidence", "note", "parts", "combined")
STAFF_KEYS = (
    "index", "hand", "clef", "clef_confidence", "lines", "unit", "x_range",
    "key_fifths", "key_confidence", "cut_off", "barlines",
)
SYSTEM_KEYS = ("index", "y_range", "x_range", "grand", "cut_off", "staves", "events")
TOP_KEYS = (
    "schema_version", "id", "created_at", "title", "source", "image", "engine",
    "warnings", "systems", "key_override", "edits",
)


class SchemaError(ValueError):
    """Raised when an analysis document does not match the contract."""


def new_analysis(analysis_id, created_at, title, source, image):
    """An empty but valid analysis document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "id": analysis_id,
        "created_at": created_at,
        "title": title,
        "source": source,
        "image": image,
        "engine": {
            "geometry": "1",
        },
        "warnings": [],
        "systems": [],
        "key_override": None,
        "edits": [],
    }


def normalize(doc):
    """Fill in fields added after this schema version was first written.

    ``key_override`` arrived with the key signature picker, ``edits`` and a staff's
    ``barlines`` with manual note editing, so every analysis stored before each of
    those lacks the field.  Bumping SCHEMA_VERSION for that would have been worse
    than useless: store.load refuses a version it does not know, and store.listing
    swallows the error to keep one bad reading from emptying the gallery, so every
    older build would have silently lost every reading this one saved.  Filling the
    field in on the way through costs nothing and keeps rule 4 true, that a document
    in memory always has every field.

    A reading stored before barlines were recorded gets an empty list, which is not a
    guess: it is what a page with no barline detected on it stores anyway, and both
    mean the same thing to the code that reads it, that the staff is one measure from
    end to end.  Re-analyzing such a reading fills them in properly.

    The same door works in the other direction.  ``engine`` once carried three fields
    for a vision pass that checked the reading against the photo; that pass is gone,
    and a reading it wrote is still a reading, so the fields are dropped here rather
    than refused by the validator.  Its corrected pitches stay, since they are the
    reading, and the ``changed`` marks and per-step notes it left stay with them.
    """
    if isinstance(doc, dict):
        doc.setdefault("key_override", None)
        doc.setdefault("edits", [])
        engine = doc.get("engine")
        if isinstance(engine, dict):
            for gone in ("verifier", "verified", "verifier_error"):
                engine.pop(gone, None)
        for system in doc.get("systems") or []:
            if not isinstance(system, dict):
                continue
            for staff in system.get("staves") or []:
                if isinstance(staff, dict):
                    staff.setdefault("barlines", [])
    return doc


def _require(obj, keys, where):
    if not isinstance(obj, dict):
        raise SchemaError("%s: expected an object, got %s" % (where, type(obj).__name__))
    missing = [k for k in keys if k not in obj]
    if missing:
        raise SchemaError("%s: missing keys %s" % (where, ", ".join(missing)))
    extra = [k for k in obj if k not in keys]
    if extra:
        raise SchemaError("%s: unexpected keys %s" % (where, ", ".join(sorted(extra))))


EDIT_KINDS = ("accidental",)
ACCIDENTALS_WRITABLE = ("sharp", "flat", "natural")


def _check_edits(edits):
    """The corrections a person made by hand, which outlive the reading they touched."""
    if not isinstance(edits, list):
        raise SchemaError("edits: expected a list")
    for index, edit in enumerate(edits):
        where = "edits[%d]" % index
        if not isinstance(edit, dict):
            raise SchemaError("%s: expected an object" % where)
        if edit.get("kind") not in EDIT_KINDS:
            raise SchemaError("%s: unknown kind %r" % (where, edit.get("kind")))
        # No note index: a correction is anchored by the height it was made at, so
        # that adding or removing a notehead cannot re-point the ones after it.
        _require(edit, ("kind", "system", "event", "staff", "value", "y"), where)
        for field in ("system", "event", "staff"):
            value = edit[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SchemaError("%s.%s: expected an index, got %r"
                                  % (where, field, value))
        if edit["value"] not in ACCIDENTALS_WRITABLE:
            raise SchemaError("%s.value: expected one of %r, got %r"
                             % (where, ACCIDENTALS_WRITABLE, edit["value"]))
        if not isinstance(edit["y"], (int, float)) or isinstance(edit["y"], bool):
            raise SchemaError("%s.y: expected a number, got %r" % (where, edit["y"]))


def validate_analysis(doc):
    """Check *doc* against the schema.  Returns *doc*; raises :class:`SchemaError`."""
    _require(doc, TOP_KEYS, "analysis")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(
            "analysis: schema_version %r, this build understands %r"
            % (doc["schema_version"], SCHEMA_VERSION)
        )
    _require(doc["source"], ("filename", "content_type", "bytes", "width", "height"),
             "source")
    _require(doc["image"], ("width", "height", "scale", "deskew_deg"), "image")
    _require(doc["engine"], ("geometry",), "engine")
    if not isinstance(doc["warnings"], list):
        raise SchemaError("warnings: expected a list")
    override = doc["key_override"]
    if override is not None and not (isinstance(override, int)
                                     and not isinstance(override, bool)
                                     and -7 <= override <= 7):
        raise SchemaError("key_override: expected null or -7..7, got %r" % (override,))
    _check_edits(doc["edits"])

    for si, system in enumerate(doc["systems"]):
        where = "system[%d]" % si
        _require(system, SYSTEM_KEYS, where)
        staff_indices = set()
        for staff in system["staves"]:
            sw = "%s.staff[%s]" % (where, staff.get("index"))
            _require(staff, STAFF_KEYS, sw)
            if staff["clef"] not in CLEFS:
                raise SchemaError("%s: bad clef %r" % (sw, staff["clef"]))
            if staff["hand"] not in HANDS:
                raise SchemaError("%s: bad hand %r" % (sw, staff["hand"]))
            if len(staff["lines"]) != 5:
                raise SchemaError("%s: expected 5 staff lines, got %d"
                                  % (sw, len(staff["lines"])))
            if not -7 <= staff["key_fifths"] <= 7:
                raise SchemaError("%s: key_fifths out of range" % sw)
            staff_indices.add(staff["index"])

        for ei, event in enumerate(system["events"]):
            ew = "%s.event[%d]" % (where, ei)
            _require(event, EVENT_KEYS, ew)
            if event["index"] != ei:
                raise SchemaError("%s: index %r out of order" % (ew, event["index"]))
            if not event["parts"]:
                raise SchemaError("%s: has no parts" % ew)
            for part in event["parts"]:
                pw = "%s.part[staff %s]" % (ew, part.get("staff"))
                _require(part, PART_KEYS, pw)
                if part["staff"] not in staff_indices:
                    raise SchemaError("%s: staff %r is not in this system"
                                      % (pw, part["staff"]))
                if part["duration_hint"] not in DURATION_HINTS:
                    raise SchemaError("%s: bad duration_hint %r"
                                      % (pw, part["duration_hint"]))
                if not part["notes"]:
                    raise SchemaError("%s: has no notes" % pw)
                midis = []
                for note in part["notes"]:
                    _require(note, NOTE_KEYS, "%s.note" % pw)
                    if note["accidental"] not in ACCIDENTALS:
                        raise SchemaError("%s: bad accidental %r"
                                          % (pw, note["accidental"]))
                    midis.append(note["midi"])
                if midis != sorted(midis):
                    raise SchemaError("%s: notes are not sorted low to high" % pw)
                if part["chord"] is not None:
                    _require(part["chord"], CHORD_KEYS, "%s.chord" % pw)
            if event["combined"] is not None:
                _require(event["combined"], COMBINED_KEYS, "%s.combined" % ew)
    return doc


def iter_notes(doc):
    """Yield ``(system, event, part, note)`` for every notehead in the document."""
    for system in doc["systems"]:
        for event in system["events"]:
            for part in event["parts"]:
                for note in part["notes"]:
                    yield system, event, part, note


def note_count(doc):
    return sum(1 for _ in iter_notes(doc))
