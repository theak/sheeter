"""Photo bytes in, a finished analysis document out."""

import datetime
import hashlib

from . import naming, geometry, preprocess, schema, store


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
        previous = None
        for event in system["events"]:
            pooled = []
            for part in event["parts"]:
                names = [note["name"] for note in part["notes"]]
                part["chord"] = naming.name_chord(names, keys.get(part["staff"], 0))
                pooled.extend(names)
            fifths = keys.get(0, 0)
            pooled = _sorted_unique(pooled)
            event["combined"] = naming.name_combined(pooled, fifths)
            previous = pooled
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
