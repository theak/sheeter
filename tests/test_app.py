"""Route level tests, driven through WSGI so no server and no extra dependency.

Renaming and deleting are the only places the app changes something a person cares
about, and renaming takes a redirect target from the form, which is worth pinning down.
"""

import io
import json
import urllib.parse

import pytest
from PIL import Image

from sheeter import geometry, naming, pitches, schema, store


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("SHEETER_DATA_DIR", str(root))
    return root


@pytest.fixture
def app():
    import app as application

    return application.app


def png_bytes(width=600, height=400):
    out = io.BytesIO()
    Image.new("L", (width, height), 200).save(out, "PNG")
    return out.getvalue()


def make_doc(raw, title):
    names = ["C4", "E4", "G4"]
    notes = [pitches.make_note(*pitches.parse_name(n), 100.0, 140.0) for n in names]
    notes.sort(key=lambda n: n["midi"])
    part = {"staff": 0, "hand": None, "duration_hint": "quarter-or-shorter",
            "dotted": False, "notes": notes, "chord": naming.name_chord(names)}
    event = {"index": 0, "x": 100.0, "x_range": [80, 120], "confidence": 0.9,
             "note": None, "parts": [part], "combined": naming.name_combined(names)}
    doc = schema.new_analysis(
        store.analysis_id(raw), "2026-09-05T20:31:00Z", title,
        {"filename": title, "content_type": "image/png", "bytes": len(raw),
         "width": 900, "height": 600},
        {"width": 600, "height": 400, "scale": 1.0, "deskew_deg": 0.0})
    doc["systems"] = [{
        "index": 0, "y_range": [80, 200], "x_range": [0, 600], "grand": False,
        "cut_off": False,
        "staves": [{"index": 0, "hand": None, "clef": "treble", "clef_confidence": 0.9,
                    "lines": [100.0, 120.0, 140.0, 160.0, 180.0], "unit": 20.0,
                    "x_range": [0, 600], "key_fifths": 0, "key_confidence": 0.9,
                    "cut_off": False}],
        "events": [event]}]
    return schema.validate_analysis(doc)


@pytest.fixture
def saved():
    raw = b"a photo of some music"
    return store.save(make_doc(raw, "IMG_4021.jpg"), raw, "jpg", png_bytes())


def call(app, method, path, form=None):
    """Drive the WSGI app directly.  Returns ``(status, headers, body)``."""
    body = urllib.parse.urlencode(form or {}).encode()
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
        "SERVER_NAME": "testserver", "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body), "wsgi.errors": io.BytesIO(),
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = app(environ, start_response)
    return captured["status"], captured["headers"], b"".join(chunks)


class TestRename:
    def test_renames_and_returns_to_the_gallery(self, app, saved):
        status, headers, _ = call(app, "POST", "/a/%s/rename" % saved,
                                  {"title": "Sophisticated Lady", "next": "/"})
        assert status.startswith("303")
        assert headers["Location"].endswith("/")
        assert store.load(saved)["title"] == "Sophisticated Lady"

    def test_returns_to_the_reading_when_asked(self, app, saved):
        _status, headers, _ = call(app, "POST", "/a/%s/rename" % saved,
                                   {"title": "Kept", "next": "/a/%s" % saved})
        assert headers["Location"].endswith("/a/%s" % saved)

    @pytest.mark.parametrize("target", [
        "https://evil.example/x", "//evil.example", "http://evil.example",
        "/../../etc", "javascript:alert(1)", "/a/notanid",
    ])
    def test_refuses_to_be_sent_off_site(self, app, saved, target):
        """The form supplies the redirect, so anyone who can put a link in front of you
        picks it. Anything that is not a page of this app falls back to the reading."""
        _status, headers, _ = call(app, "POST", "/a/%s/rename" % saved,
                                   {"title": "Fine", "next": target})
        assert headers["Location"].endswith("/a/%s" % saved)

    def test_an_empty_title_leaves_the_name_alone(self, app, saved):
        call(app, "POST", "/a/%s/rename" % saved, {"title": "   ", "next": "/"})
        assert store.load(saved)["title"] == "IMG_4021.jpg"

    def test_a_long_title_is_clamped(self, app, saved):
        call(app, "POST", "/a/%s/rename" % saved, {"title": "z" * 500, "next": "/"})
        assert len(store.load(saved)["title"]) == store.MAX_TITLE

    def test_renaming_something_that_is_gone_does_not_break(self, app):
        status, _headers, _ = call(app, "POST", "/a/ffffffffffff/rename",
                                   {"title": "Nope", "next": "/"})
        assert status.startswith("303")


class TestDelete:
    def test_deletes_and_returns_to_the_gallery(self, app, saved):
        status, headers, _ = call(app, "POST", "/a/%s/delete" % saved)
        assert status.startswith("303")
        assert headers["Location"].endswith("/")
        assert not store.exists(saved)
        with pytest.raises(KeyError):
            store.load(saved)

    def test_deleting_twice_does_not_break(self, app, saved):
        call(app, "POST", "/a/%s/delete" % saved)
        status, _headers, _ = call(app, "POST", "/a/%s/delete" % saved)
        assert status.startswith("303")

    def test_a_bad_id_does_not_break(self, app):
        status, _headers, _ = call(app, "POST", "/a/....../delete")
        assert status.startswith(("303", "404"))


class TestGallery:
    def test_offers_rename_and_delete_on_every_card(self, app, saved):
        _status, _headers, body = call(app, "GET", "/")
        page = body.decode("utf-8")
        assert "IMG_4021.jpg" in page
        assert '/a/%s/rename' % saved in page
        assert '/a/%s/delete' % saved in page
        assert "Rename or delete" in page

    def test_the_rename_field_starts_from_the_current_name(self, app, saved):
        store.rename(saved, "Bars 18 and 19")
        _status, _headers, body = call(app, "GET", "/")
        assert 'value="Bars 18 and 19"' in body.decode("utf-8")


class TestPlayback:
    """What the server is responsible for: shipping the player and its button.

    Whether a triangle oscillator actually sounds is not something a WSGI call can
    answer, and these do not pretend to.  They cover the ways the feature silently
    fails to arrive: a script that 404s, or a button that never renders.
    """

    def test_serves_the_audio_module(self, app):
        status, headers, body = call(app, "GET", "/static/js/audio.js")
        assert status.startswith("200")
        assert "javascript" in dict(headers).get("Content-Type", "")
        assert b"SheeterAudio" in body

    def test_the_analysis_page_loads_it_before_the_overlay(self, app, saved):
        _status, _headers, body = call(app, "GET", "/a/%s" % saved)
        page = body.decode("utf-8")
        # overlay.js reads window.SheeterAudio at startup, so the order is load bearing.
        assert page.index("/static/js/audio.js") < page.index("/static/js/overlay.js")

    def test_offers_a_sound_toggle_that_starts_on(self, app, saved):
        _status, _headers, body = call(app, "GET", "/a/%s" % saved)
        page = body.decode("utf-8")
        assert 'id="sound-toggle"' in page
        # Playback is on by default, so the button starts pressed.
        assert 'aria-pressed="true"' in page.split('id="sound-toggle"')[1][:80]

    def test_every_note_carries_the_midi_the_player_needs(self, app, saved):
        _status, _headers, body = call(app, "GET", "/a/%s/analysis.json" % saved)
        doc = json.loads(body)
        notes = [note
                 for system in doc["systems"]
                 for event in system["events"]
                 for part in event["parts"]
                 for note in part["notes"]]
        assert notes
        assert all(isinstance(note["midi"], int) for note in notes)


def post_file(app, path, raw, filename="photo.png", content_type="image/png"):
    """POST one file as multipart form data, the way the upload form does."""
    boundary = "----sheeter-test-boundary"
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: %s\r\n\r\n' % (boundary, filename, content_type)).encode()
    body = head + raw + ("\r\n--%s--\r\n" % boundary).encode()
    environ = {
        "REQUEST_METHOD": "POST", "PATH_INFO": path, "QUERY_STRING": "",
        "SERVER_NAME": "testserver", "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body), "wsgi.errors": io.BytesIO(),
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "multipart/form-data; boundary=%s" % boundary,
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = app(environ, start_response)
    return captured["status"], captured["headers"], b"".join(chunks)


class TestReadingAgain:
    """A reading made by an older engine is read again, not served as is.

    Uploads are content addressed, so the same image always lands on the same analysis.
    That is right while the reader is the same reader; after a fix it means the page
    keeps showing the reading from before the fix.  This is the bug where a pull of
    the branch that fixed a page appeared not to have fixed it.
    """

    @staticmethod
    def stored(title, current):
        """A stored reading of a blank image, made by an older engine or this one."""
        raw = png_bytes()
        doc = make_doc(raw, title)           # schema's default engine version is "1"
        if current:
            doc["engine"]["geometry"] = geometry.GEOMETRY_VERSION
        assert (doc["engine"]["geometry"] == geometry.GEOMETRY_VERSION) is current
        return store.save(doc, raw, "png", raw), raw

    def test_reuploading_an_older_reading_reads_it_again_and_keeps_the_title(self, app):
        ident, raw = self.stored("Bars 1 to 4", current=False)
        status, headers, _ = post_file(app, "/upload", raw)
        assert status.startswith("303") and headers["Location"].endswith("/a/" + ident)
        doc = store.load(ident)
        assert doc["engine"]["geometry"] == geometry.GEOMETRY_VERSION
        assert doc["title"] == "Bars 1 to 4", "the reader's own title must survive"

    def test_reuploading_a_current_reading_is_served_as_is(self, app):
        ident, raw = self.stored("Bars 1 to 4", current=True)
        status, headers, _ = post_file(app, "/upload", raw)
        assert status.startswith("303") and headers["Location"].endswith("/a/" + ident)
        doc = store.load(ident)
        # make_doc has one system; a fresh reading of a blank image would have none.
        assert len(doc["systems"]) == 1, "a current reading must not be recomputed"
        assert doc["created_at"] == "2026-09-05T20:31:00Z"

    def test_re_analyze_is_always_offered_and_the_stale_notice_only_when_stale(self, app):
        # Two different images, so both readings are in the store at once.
        old_raw, new_raw = png_bytes(width=601), png_bytes(width=602)
        stale = store.save(make_doc(old_raw, "Old"), old_raw, "png", old_raw)
        fresh_doc = make_doc(new_raw, "New")
        fresh_doc["engine"]["geometry"] = geometry.GEOMETRY_VERSION
        fresh = store.save(fresh_doc, new_raw, "png", new_raw)

        _s, _h, body = call(app, "GET", "/a/%s" % stale)
        page = body.decode("utf-8")
        assert "Re-analyze" in page and "/a/%s/reanalyze" % stale in page
        assert "Older reading" in page
        _s, _h, body = call(app, "GET", "/a/%s" % fresh)
        page = body.decode("utf-8")
        assert "Re-analyze" in page, "offered whatever the version, that is the point"
        assert "Older reading" not in page

    def test_re_analyze_updates_the_engine_and_keeps_the_title(self, app):
        ident, _raw = self.stored("Bars 1 to 4", current=False)
        status, headers, _ = call(app, "POST", "/a/%s/reanalyze" % ident)
        assert status.startswith("303") and headers["Location"].endswith("/a/" + ident)
        doc = store.load(ident)
        assert doc["engine"]["geometry"] == geometry.GEOMETRY_VERSION
        assert doc["title"] == "Bars 1 to 4"
        _s, _h, body = call(app, "GET", "/a/%s" % ident)
        assert "Older reading" not in body.decode("utf-8"), "no longer stale"

    def test_re_analyzing_something_that_is_gone_does_not_break(self, app):
        status, _headers, _ = call(app, "POST", "/a/000000000000/reanalyze")
        assert status.startswith("404")
