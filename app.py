"""Sheeter: point your phone at sheet music and read the notes back off the photo."""

import json
import os
import traceback

import bottle
from bottle import Bottle, redirect, request, response, static_file, template

from sheeter import pipeline, store, verify

app = Bottle()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
bottle.TEMPLATE_PATH.insert(0, os.path.join(BASE_DIR, "templates"))

#: Phone photos are big.  Anything past this is not a photo of one page of music.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

#: When the Claude pass runs.  "manual" is the default and means the button on the
#: analysis page: the geometry reading is what the app is for, it takes a second or
#: two, and the vision pass adds five to twenty-five on top for a second opinion that
#: on a clear photo says the same thing.  Set "auto" to run it on every upload anyway.
VERIFY_MODE = (os.environ.get("SHEETER_VERIFY") or "manual").strip().lower()

EXTENSIONS = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/heic": "heic", "image/heif": "heif", "image/gif": "gif",
    "image/tiff": "tiff", "image/bmp": "bmp",
}


def _verify_offered():
    return VERIFY_MODE != "off" and verify.available()


def _render_index(error=None, status=200):
    response.status = status
    return template("index.html", items=store.listing(), usage=store.usage(),
                    verify_available=_verify_offered(), error=error)


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
    return {"ok": True, "analyses": usage["count"], "verify": _verify_offered(),
            "verify_mode": VERIFY_MODE}


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

    existing = pipeline.analysis_id(raw)
    if store.exists(existing):
        return redirect("/a/%s" % existing)

    try:
        document, processed = pipeline.analyze_bytes(
            raw, upload_file.raw_filename, content_type)
    except Exception:
        traceback.print_exc()
        return _render_index(
            "Could not read that image. It may be an unsupported format, or too "
            "small to find any staff lines in.", 400)

    if VERIFY_MODE == "auto" and verify.available():
        document = verify.verify_analysis(document, processed)
        pipeline.name_everything(document)

    analysis_id = store.save(document, raw, extension, processed)
    return redirect("/a/%s" % analysis_id)


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
                    verify_available=_verify_offered())


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
    title = (request.forms.get("title") or "").strip()[:120]
    if title:
        try:
            store.rename(analysis_id, title)
        except (KeyError, ValueError):
            pass
    return redirect("/a/%s" % analysis_id)


@app.route("/a/<analysis_id>/delete", method="POST")
def delete(analysis_id):
    try:
        store.delete(analysis_id)
    except ValueError:
        pass
    return redirect("/")


@app.route("/a/<analysis_id>/verify", method="POST")
def reverify(analysis_id):
    document = _load(analysis_id)
    if document is None:
        return _render_index("That analysis is no longer here.", 404)
    if not _verify_offered():
        return redirect("/a/%s" % analysis_id)
    with open(store.path(analysis_id, "processed.png"), "rb") as handle:
        document = verify.verify_analysis(document, handle.read())
    pipeline.name_everything(document)
    store.save_doc(document)
    return redirect("/a/%s" % analysis_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            server="waitress")
