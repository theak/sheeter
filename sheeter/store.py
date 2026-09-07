"""The filesystem database: one directory per analysis.

    <data_dir>/<id>/original.<ext>   the bytes exactly as uploaded
    <data_dir>/<id>/processed.png    deskewed grayscale image the overlay sits on
    <data_dir>/<id>/thumb.jpg        max 480px preview for the gallery
    <data_dir>/<id>/analysis.json    the schema document

The id is the first 12 hex chars of the sha256 of the original upload, so the
same photo always lands in the same directory and re-uploading it costs
nothing.  ``analysis.json`` is written last and atomically, which means a
directory without one is a crashed or in-flight save and :func:`listing`
simply skips it.  There is no index file to fall out of sync: the gallery is
built by scanning directories.

``THUMB_URL_FMT`` is the one place the storage layout meets the web routes.
It must match the route ``app.py`` serves thumbnails on.
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime

from PIL import Image

from . import schema

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[0-9a-f]{12}$")
EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}\.[a-z0-9]{1,8}$")

THUMB_MAX = 480
THUMB_URL_FMT = "/a/%s/thumb.jpg"
SUMMARY_CHORDS = 3
MAX_TITLE = 120

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """Where analyses live.  ``SHEETER_DATA_DIR`` wins, else ``<repo>/data``."""
    return os.environ.get("SHEETER_DATA_DIR") or os.path.join(_REPO_ROOT, "data")


def analysis_id(raw_bytes):
    """Content address of an upload: 12 hex chars of its sha256."""
    return hashlib.sha256(raw_bytes).hexdigest()[:12]


def _check_id(analysis_id):
    if not isinstance(analysis_id, str) or not ID_RE.match(analysis_id):
        raise ValueError("bad analysis id %r" % (analysis_id,))
    return analysis_id


def _dir(analysis_id):
    return os.path.join(data_dir(), _check_id(analysis_id))


def directory(analysis_id):
    """Absolute path to an analysis directory, id validated."""
    return _dir(analysis_id)


def path(analysis_id, name):
    """Absolute path to a file inside an analysis directory."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("bad file name %r" % (name,))
    return os.path.join(_dir(analysis_id), name)


def _check_ext(ext):
    ext = str(ext or "").lstrip(".").lower()
    if not EXT_RE.match(ext):
        raise ValueError("bad file extension %r" % (ext,))
    return ext


def exists(analysis_id):
    return os.path.isfile(path(analysis_id, "analysis.json"))


def original_name(analysis_id):
    """Name of the stored upload, e.g. ``original.jpg``, or None if missing."""
    directory = _dir(analysis_id)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in sorted(names):
        if name.startswith("original.") and NAME_RE.match(name):
            return name
    return None


def _write_atomic(dest, data):
    """Write *data* to *dest* via a temp file in the same directory."""
    directory = os.path.dirname(dest)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _make_thumb(png_bytes):
    im = Image.open(io.BytesIO(png_bytes))
    im = im.convert("L")
    im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


def save(doc, original_bytes, original_ext, processed_png_bytes):
    """Write a complete analysis and return its id.

    Everything is checked before a byte is written, and ``analysis.json`` goes
    last, so an interrupted save leaves a directory the gallery ignores rather
    than a half-readable analysis.
    """
    schema.validate_analysis(doc)
    ident = analysis_id(original_bytes)
    if doc["id"] != ident:
        raise ValueError("doc id %r does not match the uploaded bytes (%s)"
                         % (doc["id"], ident))
    ext = _check_ext(original_ext)

    directory = _dir(ident)
    os.makedirs(directory, exist_ok=True)

    thumb = _make_thumb(processed_png_bytes)
    _write_atomic(os.path.join(directory, "original." + ext), original_bytes)
    for stale in os.listdir(directory):
        if stale.startswith("original.") and stale != "original." + ext:
            os.unlink(os.path.join(directory, stale))
    _write_atomic(os.path.join(directory, "processed.png"), processed_png_bytes)
    _write_atomic(os.path.join(directory, "thumb.jpg"), thumb)
    _write_doc(directory, doc)
    return ident


def _write_doc(directory, doc):
    body = json.dumps(doc, indent=1, sort_keys=False).encode("utf-8")
    _write_atomic(os.path.join(directory, "analysis.json"), body)


def save_doc(doc):
    """Re-write just ``analysis.json``, after a rename or a verify pass."""
    schema.validate_analysis(doc)
    directory = _dir(doc["id"])
    if not os.path.isdir(directory):
        raise KeyError(doc["id"])
    _write_doc(directory, doc)


def _read_doc(directory):
    with open(os.path.join(directory, "analysis.json"), "rb") as fh:
        doc = json.loads(fh.read().decode("utf-8"))
    if not isinstance(doc, dict):
        raise schema.SchemaError("analysis: expected an object")
    if doc.get("schema_version") != schema.SCHEMA_VERSION:
        raise schema.SchemaError(
            "analysis: schema_version %r, this build understands %r"
            % (doc.get("schema_version"), schema.SCHEMA_VERSION))
    # A file written before a field was added is still this version, so fill the
    # field in here rather than making every reader of a document check for it.
    return schema.normalize(doc)


def load(analysis_id):
    """The stored document.  KeyError if absent, SchemaError if unreadable."""
    directory = _dir(analysis_id)
    try:
        return _read_doc(directory)
    except FileNotFoundError:
        raise KeyError(analysis_id)
    except ValueError as exc:
        if isinstance(exc, schema.SchemaError):
            raise
        raise schema.SchemaError("%s: %s" % (analysis_id, exc))


def delete(analysis_id):
    """Remove an analysis.  True if it was there."""
    directory = _dir(analysis_id)
    if not os.path.isdir(directory):
        return False
    shutil.rmtree(directory)
    return True


def rename(analysis_id, title):
    """Give an analysis a new title and return the updated document."""
    title = str(title or "").strip()[:MAX_TITLE]
    if not title:
        raise ValueError("title must not be empty")
    doc = load(analysis_id)
    doc["title"] = title
    save_doc(doc)
    return doc


def _summary(doc):
    """A one-line taste of the music, e.g. ``Bbmaj9 - Eb6/9 - Gm11``.

    A single melodic line has no chords to list, and saying so on the card reads like
    the reading failed when it did not, so fall back to the notes themselves.
    """
    symbols = []
    notes = []
    for system in doc.get("systems") or []:
        for event in system.get("events") or []:
            combined = event.get("combined") or {}
            symbol = combined.get("symbol")
            if symbol:
                symbols.append(symbol)
            elif len(notes) < SUMMARY_CHORDS:
                for part in event.get("parts") or []:
                    for note in part.get("notes") or []:
                        notes.append(note.get("pretty") or note.get("name"))
                        break
                    break
            if len(symbols) >= SUMMARY_CHORDS:
                return " - ".join(symbols)
    if symbols:
        return " - ".join(symbols)
    return " - ".join(n for n in notes[:SUMMARY_CHORDS] if n)


def _timestamp(doc, directory):
    created = doc.get("created_at")
    if isinstance(created, str):
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return os.path.getmtime(directory)


def _entry(ident, directory):
    doc = _read_doc(directory)
    events = 0
    notes = 0
    for system in doc["systems"]:
        for event in system["events"]:
            events += 1
            for part in event["parts"]:
                notes += len(part["notes"])
    return {
        "id": ident,
        "title": doc.get("title") or ident,
        "created_at": doc.get("created_at"),
        "thumb_url": THUMB_URL_FMT % ident,
        "note_count": notes,
        "system_count": len(doc["systems"]),
        "event_count": events,
        "verified": bool((doc.get("engine") or {}).get("verified")),
        "summary": _summary(doc),
        "_sort": _timestamp(doc, directory),
    }


def listing(limit=None):
    """Gallery summaries, newest first.  Unreadable directories are skipped."""
    root = data_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []

    entries = []
    for name in names:
        directory = os.path.join(root, name)
        if not ID_RE.match(name) or not os.path.isdir(directory):
            continue
        try:
            entries.append(_entry(name, directory))
        except Exception as exc:
            # A crashed upload or an analysis from another schema version.
            # One bad directory must not empty the gallery.
            log.warning("skipping analysis %s: %s", name, exc)

    entries.sort(key=lambda e: e["_sort"], reverse=True)
    for entry in entries:
        del entry["_sort"]
    if limit is not None:
        entries = entries[:limit]
    return entries


def usage():
    """``{"count": n, "bytes": total}`` for the footer."""
    root = data_dir()
    count = 0
    total = 0
    try:
        names = os.listdir(root)
    except OSError:
        return {"count": 0, "bytes": 0}

    for name in names:
        directory = os.path.join(root, name)
        if not ID_RE.match(name) or not os.path.isdir(directory):
            continue
        if os.path.isfile(os.path.join(directory, "analysis.json")):
            count += 1
        # The scandir call itself has to be guarded, not just the loop body: a delete
        # landing between the isdir check above and this line raises out of usage(),
        # and usage() is on the path that renders the gallery and the health check.
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    return {"count": count, "bytes": total}
