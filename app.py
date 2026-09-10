"""Sheeter: point your phone at sheet music and read the notes back off the photo."""

import json
import os
import re
import traceback

import bottle
from bottle import Bottle, redirect, request, response, static_file, template

from sheeter import geometry, keysig, panels, pipeline, store

app = Bottle()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
bottle.TEMPLATE_PATH.insert(0, os.path.join(BASE_DIR, "templates"))

#: Phone photos are big.  Anything past this is not a photo of one page of music.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

EXTENSIONS = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/heic": "heic", "image/heif": "heif", "image/gif": "gif",
    "image/tiff": "tiff", "image/bmp": "bmp",
}


#: Where a form post is allowed to send the browser afterwards.  Anything not on this
#: site is refused rather than followed: a "next" taken straight from a form is an open
#: redirect, and this one is reachable by anybody who can get a link in front of you.
SAFE_NEXT = re.compile(r"^/(a/[0-9a-f]{12})?$")


def _safe_next(default):
    wanted = (request.forms.get("next") or "").strip()
    return wanted if SAFE_NEXT.match(wanted) else default


def _render_index(error=None, status=200):
    response.status = status
    return template("index.html", items=store.listing(), usage=store.usage(),
                    error=error)


@app.route("/")
def index():
    return _render_index()


@app.route("/static/<filepath:path>")
def serve_static(filepath):
    return static_file(filepath, root=STATIC_DIR)


@app.route("/favicon.svg")
def favicon():
    return static_file("favicon.svg", root=STATIC_DIR, mimetype="image/svg+xml")


@app.route("/health")
def health():
    usage = store.usage()
    return {"ok": True, "analyses": usage["count"]}


@app.route("/upload", method="POST")
def upload():
    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.raw_filename:
        return _render_index("Pick a photo first.", 400)

    raw = upload_file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return _render_index("That photo is larger than 24MB. Try a smaller one.", 413)
    if not raw:
        return _render_index("That file was empty.", 400)

    content_type = upload_file.content_type or "image/jpeg"
    extension = EXTENSIONS.get(content_type.split(";")[0].strip().lower(), "img")

    # The same bytes always get the same id.  If that reading exists and the engine
    # that made it is the one running now, it is the answer.  If an older engine made
    # it, read the page again: otherwise an image analysed before a fix shows the
    # reading from before the fix for as long as it is in the store.
    existing = pipeline.analysis_id(raw)
    previous = _load(existing) if store.exists(existing) else None
    if previous is not None and _current(previous):
        return redirect("/a/%s" % existing)

    try:
        analysis_id = _analyze_and_save(raw, upload_file.raw_filename, content_type,
                                        extension, previous)
    except Exception:
        traceback.print_exc()
        return _render_index(
            "Could not read that image. It may be an unsupported format, or too "
            "small to find any staff lines in.", 400)
    return redirect("/a/%s" % analysis_id)


def _current(document):
    """Was this reading made by the engine that is running now?"""
    return document["engine"].get("geometry") == geometry.GEOMETRY_VERSION


def _analyze_and_save(raw, filename, content_type, extension, previous=None):
    """Read *raw* and store the result.  Returns the analysis id.

    *previous* is the reading being replaced, if any; its title is kept, because the
    reader may have renamed it and a better reading of the same page is still that page.
    """
    document, processed = pipeline.analyze_bytes(
        raw, filename, content_type, title=previous["title"] if previous else None)
    _keep_picked_key(document, previous)
    return store.save(document, raw, extension, processed)


def _keep_picked_key(document, previous):
    """Carry a key the reader chose onto a fresh reading of the same page.

    A better reading of the page does not make the key a different key.  Somebody who
    looked at the photo and said what it was should not have to say it again after
    every re-analysis, so the choice outlives the reading it was made on.
    """
    picked = (previous or {}).get("key_override")
    if picked is not None:
        document["key_override"] = picked
        pipeline.set_key(document, picked)
    return document


def _reanalyze(analysis_id):
    """Run the current engine over a stored upload.  Returns the id, or None."""
    previous = _load(analysis_id)
    name = store.original_name(analysis_id)
    if previous is None or name is None:
        return None
    extension = name.split(".", 1)[1]
    content_type = next((mime for mime, ext in EXTENSIONS.items() if ext == extension),
                        "image/jpeg")
    with open(store.path(analysis_id, name), "rb") as handle:
        raw = handle.read()
    filename = (previous.get("source") or {}).get("filename") or name
    return _analyze_and_save(raw, filename, content_type, extension, previous)


def _load(analysis_id):
    try:
        return store.load(analysis_id)
    except (KeyError, ValueError):
        return None


@app.route("/a/<analysis_id>")
def show(analysis_id):
    document = _load(analysis_id)
    if document is None:
        return _render_index("That analysis is no longer here.", 404)
    return template("analysis.html", doc=document,
                    analysis_json=json.dumps(document),
                    items=store.listing(limit=12),
                    stale=not _current(document),
                    engine_version=geometry.GEOMETRY_VERSION,
                    keysig=keysig, key_fifths=keysig.current(document),
                    key_picked=document["key_override"] is not None,
                    # The fix panels are drawn from rows this builds; the same rows an
                    # edit compares to decide which panels it has to send back.
                    panels=panels)


@app.route("/a/<analysis_id>/analysis.json")
def raw_json(analysis_id):
    document = _load(analysis_id)
    if document is None:
        response.status = 404
        return {"error": "not found"}
    response.content_type = "application/json"
    return json.dumps(document)


@app.route("/a/<analysis_id>/processed.png")
def processed(analysis_id):
    return _file(analysis_id, "processed.png")


@app.route("/a/<analysis_id>/thumb.jpg")
def thumb(analysis_id):
    return _file(analysis_id, "thumb.jpg")


@app.route("/a/<analysis_id>/original")
def original(analysis_id):
    document = _load(analysis_id)
    if document is None:
        response.status = 404
        return "not found"
    name = store.original_name(analysis_id)
    if name is None:
        response.status = 404
        return "not found"
    return _file(analysis_id, name)


def _file(analysis_id, name):
    try:
        root = store.directory(analysis_id)
    except ValueError:
        response.status = 404
        return "not found"
    return static_file(name, root=root)


@app.route("/a/<analysis_id>/rename", method="POST")
def rename(analysis_id):
    # Renaming happens from the gallery as well as from the reading, so the form says
    # where it came from and we go back there.
    target = _safe_next("/a/%s" % analysis_id)
    title = (request.forms.get("title") or "").strip()[:store.MAX_TITLE]
    if title:
        try:
            store.rename(analysis_id, title)
        except (KeyError, ValueError):
            pass
    return redirect(target)


@app.route("/a/<analysis_id>/reanalyze", method="POST")
def reanalyze(analysis_id):
    """Read a stored page again with the engine that is running now."""
    try:
        done = _reanalyze(analysis_id)
    except Exception:
        traceback.print_exc()
        done = None
    if done is None:
        return _render_index("That analysis could not be re-analyzed.", 404)
    return redirect("/a/%s" % analysis_id)


@app.route("/a/<analysis_id>/key", method="POST")
def set_key(analysis_id):
    """Put a reading in the key the reader picked off the photo.

    No image is needed and nothing is measured again: a notehead's y is a staff
    position whatever the key is, so this is arithmetic on the stored reading and
    returns straight away.  "auto" is the way back, and it does need the photo, since
    the detected key is not kept anywhere once a choice has overwritten it.
    """
    wanted = (request.forms.get("fifths") or "").strip()
    document = _load(analysis_id)
    if document is None:
        return _render_index("That analysis is no longer here.", 404)

    if wanted == "auto":
        if document["key_override"] is not None:
            # Saved before re-analyzing, because _reanalyze reads the document back off
            # disk and carries the choice forward: clearing it only in memory here
            # would hand the old choice straight back to the new reading.
            document["key_override"] = None
            store.save_doc(document)
            if _reanalyze(analysis_id) is None:
                return _render_index("That analysis could not be re-analyzed.", 404)
        return redirect("/a/%s" % analysis_id)

    try:
        fifths = int(wanted)
        pipeline.set_key(document, fifths)
        # set_key goes through restate_staff, which resolves an accidental against the
        # key alone and drops one carried from earlier in the measure.  Re-applying the
        # edits afterwards puts the carry back.
        pipeline.apply_edits(document)
    except ValueError:
        # A key nobody could have picked is a broken form, not something to act on.
        return redirect("/a/%s" % analysis_id)
    document["key_override"] = fifths
    store.save_doc(document)
    return redirect("/a/%s" % analysis_id)


@app.route("/a/<analysis_id>/note", method="POST")
def set_note_accidental(analysis_id):
    """Sharp, flat or natural one notehead, by hand.

    No image and nothing measured again: a notehead's y is a staff position whatever
    is written in front of it, so this is arithmetic on the stored reading.  The
    accidental then carries to the rest of its measure the way a printed one does.
    """
    def edit(document, target):
        wanted = (request.forms.get("value") or "").strip()
        value = None if wanted in ("", "clear") else wanted
        if value is not None and value not in pipeline.EDITABLE_ACCIDENTALS:
            # An accidental nobody could have clicked is a broken form, not an
            # instruction.
            return False
        return pipeline.set_accidental(document, *target, value=value)
    return _edit(analysis_id, edit, NOTE_FIELDS)


@app.route("/a/<analysis_id>/note/pitch", method="POST")
def set_note_pitch(analysis_id):
    """Move one notehead to the staff position the reader picked."""
    def edit(document, target):
        return pipeline.set_pitch(document, *target, step=_step_form())
    return _edit(analysis_id, edit, NOTE_FIELDS)


@app.route("/a/<analysis_id>/note/delete", method="POST")
def delete_note(analysis_id):
    """Take one notehead out of a chord, leaving the rest of it alone."""
    return _edit(analysis_id, lambda document, target:
                 pipeline.delete_note(document, *target), NOTE_FIELDS)


@app.route("/a/<analysis_id>/note/add", method="POST")
def add_note(analysis_id):
    """Put a notehead the reader missed into a chord, or a hand it missed entirely."""
    return _edit(analysis_id, lambda document, target:
                 pipeline.add_note(document, *target, step=_step_form()),
                 STAFF_FIELDS)


@app.route("/a/<analysis_id>/chord/delete", method="POST")
def delete_chord(analysis_id):
    """Drop a chord the reader can see is not on the page."""
    return _edit(analysis_id, lambda document, target:
                 pipeline.delete_event(document, *target), EVENT_FIELDS)


#: What each correction form names.  Deleting a whole chord has no staff to name and
#: must not be made to invent one: a form asked for a field it has no business carrying
#: would look like a stale page and be redirected away unchanged.
NOTE_FIELDS = ("system", "event", "staff", "note")
STAFF_FIELDS = ("system", "event", "staff")
EVENT_FIELDS = ("system", "event")


def _step_form():
    """The diatonic step a pitch menu posted, or None."""
    try:
        return int(request.forms.get("step"))
    except (TypeError, ValueError):
        return None


def _wants_json():
    """Is the overlay asking, rather than a form post from the browser?

    A browser form post sends "text/html,...,*/*;q=0.8", which does not contain this, so
    with no scripting every correction takes the redirect below exactly as it always did.
    """
    return "application/json" in (request.headers.get("Accept") or "")


def _edit(analysis_id, change, fields):
    """The shape every hand correction shares: locate, change, write back, come back.

    *change* is handed the document and the indices the form named and returns whether
    anything moved.  A form naming something that is not there redirects unchanged
    rather than erroring: it is a stale page, not a fault, and the reading it lands on
    will show why.

    "Come back" is whichever way the caller asked.  A form post gets the redirect to the
    step it was made on, which is the whole feature with no scripting.  The overlay asks
    for JSON and gets the panels this change moved, so it can swap them into the page it
    already has rather than fetching a page that is 86% panels to alter one of them.
    """
    document = _load(analysis_id)
    if document is None:
        return _render_index("That analysis is no longer here.", 404)
    wants_json = _wants_json()
    target = _edit_target(fields)
    # The rows as they stand, so the answer can say which panels the change moved.  Not
    # built for a form post, which is sending the whole page back either way.
    before = (panels.fix_steps(document)
              if wants_json and target is not None else None)
    latest = document
    if target is not None:
        try:
            if change(document, target):
                latest = _save_edited(analysis_id, document)
        except ValueError:
            pass
    if not wants_json:
        return redirect(_back_to(analysis_id))
    if target is None or latest is None:
        # A form naming indices that are not there, or a reading that left the store
        # while this ran.  Either way the page in front of the reader is not this one.
        return {"reload": True}
    return _panels_for(analysis_id, before, latest, target)


def _panels_for(analysis_id, before, document, target):
    """The panels a correction moved, rendered, for the overlay to swap in place.

    Only the ones that moved, which is the point: on a dense page the panels are 86% of
    the document and an accidental moves one or two.  Two when the note it was written on
    is not the last in its measure at that staff position, because the accidental now
    carries to the ones after it, and those panels are as wrong as the edited one until
    they are redrawn.

    Asking for the accidental a note already sounds moves nothing, and then this sends no
    panels at all rather than pretending something happened.

    A chord that went is named as well, in "deleted".  What it renumbers is not sent: the
    overlay stamps the new step numbers and event indices onto the panels it already has,
    from the reading below, which is the only thing that knows them.
    """
    after = panels.fix_steps(document)
    gone = None
    if len(after) == len(before) - 1:
        gone = _step_that_went(before, target)
        if gone is None:
            return {"reload": True}
        moved = panels.changed_after_delete(before, after, gone)
    elif len(after) == len(before):
        moved = panels.changed_panels(before, after)
    else:
        # Nothing here removes two chords at once.  If something ever does, the page it
        # comes back with is right and a guess at how to patch this one would not be.
        return {"reload": True}

    answer = {
        "document": document,
        "panels": dict(
            (str(number), template("fix_panel.html", fix=after[number - 1],
                                   analysis_id=analysis_id, total=len(after)))
            for number in moved),
    }
    if gone is not None:
        answer["deleted"] = gone
    return answer


def _step_that_went(before, target):
    """Which step a chord delete removed, as it was numbered before it went.

    The event a correction named, since deleting a chord names it directly and removing
    the last notehead of the last hand takes that same chord with it.
    """
    system_index, event_index = target[0], target[1]
    return next((row["number"] for row in before
                 if row["system"] == system_index and row["event"] == event_index), None)


def _edit_target(fields):
    """The indices a correction form names, or None if they are not all whole."""
    try:
        indices = tuple(int(request.forms.get(field)) for field in fields)
    except (TypeError, ValueError):
        return None
    return indices if all(index >= 0 for index in indices) else None


def _back_to(analysis_id):
    """Where a correction returns to: the step it was made on, still selected.

    The fix panel belongs to one step and the overlay reads the fragment on load, so
    coming back to #step-N keeps the reader exactly where they were instead of at the
    top of the page with nothing selected.
    """
    step = (request.forms.get("step_number") or "").strip()
    if step.isdigit():
        return "/a/%s#step-%s" % (analysis_id, step)
    return "/a/%s" % analysis_id


def _save_edited(analysis_id, document):
    """Write back an edited reading without clobbering a concurrent rename.

    Waitress serves other requests while this one runs, so the document was read
    before the edit and only the reading and its edit log are this request's to write.

    Returns what was written, which is this reading plus anything the store has gained
    since it was read, or None if it is no longer there to write to.  The overlay is
    handed that rather than the copy in hand, so what it holds is what is on disk.
    """
    try:
        latest = store.load(analysis_id)
    except (KeyError, ValueError):
        return None
    for field in ("systems", "edits", "warnings"):
        latest[field] = document[field]
    store.save_doc(latest)
    return latest


@app.route("/a/<analysis_id>/delete", method="POST")
def delete(analysis_id):
    try:
        store.delete(analysis_id)
    except ValueError:
        pass
    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            server="waitress")
