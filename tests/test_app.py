"""Route level tests, driven through WSGI so no server and no extra dependency.

Renaming and deleting are the only places the app changes something a person cares
about, and renaming takes a redirect target from the form, which is worth pinning down.
"""

import io
import json
import re
import urllib.parse

import pytest
from PIL import Image

from sheeter import geometry, keysig, naming, pitches, schema, store


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


LINES = [100.0, 120.0, 140.0, 160.0, 180.0]


def make_doc(raw, title):
    names = ["C4", "E4", "G4"]
    # Each notehead sits at the y its own pitch is written on.  A document where
    # every note claims the middle line is not one the reader could produce, and
    # anything that re-derives a pitch from where the notehead is would be right to
    # make nonsense of it.
    notes = [pitches.make_note(*pitches.parse_name(n), 100.0,
                               pitches.step_to_y(pitches.parse_name(n)[0], LINES,
                                                 "treble"))
             for n in names]
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
                    "lines": LINES, "unit": 20.0,
                    "x_range": [0, 600], "key_fifths": 0, "key_confidence": 0.9,
                    "cut_off": False, "barlines": []}],
        "events": [event]}]
    return schema.validate_analysis(doc)


@pytest.fixture
def pickable():
    """A reading whose stored photo is a real image, so it can be read again.

    The plain ``saved`` fixture keeps a string where the photo goes, which is enough
    for everything that never re-reads it and no use to anything that does.
    """
    raw = png_bytes(width=604)
    return store.save(make_doc(raw, "Bars 1 to 4"), raw, "png", raw)


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


class TestKeyPicker:
    """Picking the key signature off the photo, when the reader got it wrong.

    The key decides the alteration of every notehead without an accidental of its
    own, so one misread signature respells a whole page.  A person can see the
    signature in a second, and this is how they say so.
    """

    @staticmethod
    def names(analysis_id):
        doc = store.load(analysis_id)
        return [note["name"]
                for system in doc["systems"]
                for event in system["events"]
                for part in event["parts"]
                for note in part["notes"]]

    def test_picking_a_key_respells_the_page_and_is_remembered(self, app, saved):
        assert self.names(saved) == ["C4", "E4", "G4"]
        status, headers, _ = call(app, "POST", "/a/%s/key" % saved,
                                  {"fifths": "-3"})
        assert status.startswith("303")
        assert headers["Location"].endswith("/a/%s" % saved)
        assert self.names(saved) == ["C4", "Eb4", "G4"], "three flats reach the E"
        assert store.load(saved)["key_override"] == -3

    def test_the_pick_reaches_every_staff(self, app, saved):
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "2"})
        doc = store.load(saved)
        assert all(staff["key_fifths"] == 2
                   for system in doc["systems"] for staff in system["staves"])

    def test_the_chord_is_named_again_under_the_new_key(self, app, saved):
        was = store.load(saved)["systems"][0]["events"][0]["parts"][0]["chord"]
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "-3"})
        now = store.load(saved)["systems"][0]["events"][0]["parts"][0]["chord"]
        assert was["symbol"] != now["symbol"]

    def test_playback_follows_the_new_spelling(self, app, saved):
        before = self.midis(saved)
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "-3"})
        assert self.midis(saved) == [before[0], before[1] - 1, before[2]]

    @staticmethod
    def midis(analysis_id):
        doc = store.load(analysis_id)
        return [note["midi"] for system in doc["systems"]
                for event in system["events"] for part in event["parts"]
                for note in part["notes"]]

    def test_going_back_to_the_photo_clears_the_choice(self, app, pickable):
        call(app, "POST", "/a/%s/key" % pickable, {"fifths": "-3"})
        assert store.load(pickable)["key_override"] == -3
        status, _headers, _ = call(app, "POST", "/a/%s/key" % pickable,
                                   {"fifths": "auto"})
        assert status.startswith("303")
        assert store.load(pickable)["key_override"] is None

    def test_going_back_reads_the_photo_again_rather_than_reusing_the_choice(
            self, app, pickable):
        """The bug the save-before-re-analyze ordering exists to stop.

        _reanalyze reads the document back off disk and carries the choice forward,
        so clearing it only in memory would hand the old key straight to the fresh
        reading and the reset would do nothing at all.
        """
        call(app, "POST", "/a/%s/key" % pickable, {"fifths": "-3"})
        call(app, "POST", "/a/%s/key" % pickable, {"fifths": "auto"})
        doc = store.load(pickable)
        assert doc["key_override"] is None
        # The stored photo is blank, so a real re-reading finds no staves at all and
        # says it was made by the engine running now.  Both would be untrue of the
        # hand-built document that was there before.
        assert doc["engine"]["geometry"] == geometry.GEOMETRY_VERSION
        assert doc["systems"] == []

    def test_a_reset_with_nothing_to_reset_leaves_the_reading_alone(self, app, saved):
        was = store.load(saved)["created_at"]
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "auto"})
        assert store.load(saved)["created_at"] == was, "not re-read for nothing"

    @pytest.mark.parametrize("bad", ["", "8", "-8", "two flats", "1.5", "0x2"])
    def test_a_key_nobody_could_have_picked_changes_nothing(self, app, saved, bad):
        call(app, "POST", "/a/%s/key" % saved, {"fifths": bad})
        assert self.names(saved) == ["C4", "E4", "G4"]
        assert store.load(saved)["key_override"] is None

    def test_picking_for_something_that_is_gone_does_not_break(self, app):
        status, _headers, _ = call(app, "POST", "/a/000000000000/key",
                                   {"fifths": "-2"})
        assert status.startswith("404")

    def test_the_page_offers_every_key_as_a_picture(self, app, saved):
        _s, _h, body = call(app, "GET", "/a/%s" % saved)
        page = body.decode("utf-8")
        assert page.count('name="fifths" value=') == 15
        assert "B♭ major / G minor" in page
        assert page.count("<svg class=\"keysig\"") == 16, "fifteen, plus the current one"

    def test_the_page_marks_the_key_it_is_in(self, app, saved):
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "-2"})
        _s, _h, body = call(app, "GET", "/a/%s" % saved)
        page = body.decode("utf-8")
        marked = re.findall(r'value="(-?\d+)"\s*aria-current="true"', page)
        assert marked == ["-2"], "one choice is marked, and it is the one in force"
        assert "You picked this" in page
        assert "Use the key read from the photo" in page

    def test_the_way_back_is_only_offered_when_there_is_one(self, app, saved):
        _s, _h, body = call(app, "GET", "/a/%s" % saved)
        page = body.decode("utf-8")
        assert "Read from the photo" in page
        assert "Use the key read from the photo" not in page

    def test_a_picked_key_survives_re_analysis(self, app, pickable):
        call(app, "POST", "/a/%s/key" % pickable, {"fifths": "-3"})
        status, _headers, _ = call(app, "POST", "/a/%s/reanalyze" % pickable)
        assert status.startswith("303")
        doc = store.load(pickable)
        assert doc["engine"]["geometry"] == geometry.GEOMETRY_VERSION, "really re-read"
        assert doc["key_override"] == -3, "a better reading, still the same key"

    def test_the_picker_is_not_offered_when_nothing_was_read(self, app):
        """A page with no staves has nothing to respell, so there is nothing to pick."""
        raw = png_bytes(width=603)
        doc = make_doc(raw, "Blank")
        doc["systems"] = []
        ident = store.save(doc, raw, "png", raw)
        _s, _h, body = call(app, "GET", "/a/%s" % ident)
        assert "key-picker" not in body.decode("utf-8")

    def test_the_pictures_are_the_ones_keysig_draws(self, app, saved):
        _s, _h, body = call(app, "GET", "/a/%s" % saved)
        assert keysig.svg(-2, False) in body.decode("utf-8")


class TestManualEditing:
    """Correcting a reading by hand, when the reader got a note or a chord wrong.

    The two things worth fixing by eye: a chord the reader invented, and a notehead
    whose accidental it missed.  Both are plain forms, like the key picker, because a
    correction is exactly the sort of thing that should not need scripting to work.
    """

    @staticmethod
    def names(analysis_id):
        doc = store.load(analysis_id)
        return [note["name"]
                for system in doc["systems"]
                for event in system["events"]
                for part in event["parts"]
                for note in part["notes"]]

    @staticmethod
    def note_form(**over):
        form = {"system": "0", "event": "0", "staff": "0", "note": "1"}
        form.update(over)
        return form

    def test_sharpening_a_note_sticks(self, app, saved):
        assert self.names(saved) == ["C4", "E4", "G4"]
        status, headers, _ = call(app, "POST", "/a/%s/note" % saved,
                                  self.note_form(value="sharp"))
        assert status.startswith("303")
        assert self.names(saved) == ["C4", "E#4", "G4"]
        assert store.load(saved)["edits"] == [
            {"kind": "accidental", "system": 0, "event": 0, "staff": 0, "note": 1,
             "value": "sharp", "y": store.load(saved)["systems"][0]["events"][0]
             ["parts"][0]["notes"][1]["y"]},
        ]

    def test_flat_and_natural_work_the_same_way(self, app, saved):
        call(app, "POST", "/a/%s/note" % saved, self.note_form(value="flat"))
        assert self.names(saved) == ["C4", "Eb4", "G4"]
        call(app, "POST", "/a/%s/note" % saved, self.note_form(value="natural"))
        assert self.names(saved) == ["C4", "E4", "G4"]

    def test_it_comes_back_to_the_row_that_was_edited(self, app, saved):
        """The reading table is long and the forms are in it, so a redirect to the top
        of the page loses the reader's place after every button."""
        _status, headers, _ = call(app, "POST", "/a/%s/note" % saved,
                                   self.note_form(value="sharp"))
        assert headers["Location"].endswith("/a/%s#row-0-0" % saved)

    def test_undoing_puts_the_reading_and_the_log_back(self, app, saved):
        call(app, "POST", "/a/%s/note" % saved, self.note_form(value="sharp"))
        call(app, "POST", "/a/%s/note" % saved, self.note_form(value="clear"))
        assert self.names(saved) == ["C4", "E4", "G4"]
        assert store.load(saved)["edits"] == []

    @pytest.mark.parametrize("bad", [
        {"value": "double-sharp"}, {"value": "wobbly"}, {"value": "sharp", "note": "9"},
        {"value": "sharp", "event": "9"}, {"value": "sharp", "staff": "9"},
        {"value": "sharp", "system": "9"}, {"value": "sharp", "note": "-1"},
        {"value": "sharp", "note": "one"}, {"value": "sharp", "system": ""},
    ])
    def test_a_form_nobody_could_have_submitted_changes_nothing(self, app, saved, bad):
        status, _headers, _ = call(app, "POST", "/a/%s/note" % saved,
                                   self.note_form(**bad))
        assert status.startswith("303")
        assert self.names(saved) == ["C4", "E4", "G4"]
        assert store.load(saved)["edits"] == []

    def test_a_spurious_chord_can_be_deleted(self, app, saved):
        status, headers, _ = call(app, "POST", "/a/%s/chord/delete" % saved,
                                  {"system": "0", "event": "0"})
        assert status.startswith("303")
        assert headers["Location"].endswith("/a/%s#row-0-0" % saved)
        assert store.load(saved)["systems"][0]["events"] == []
        assert self.names(saved) == []

    @pytest.mark.parametrize("bad", [
        {"system": "0", "event": "9"}, {"system": "9", "event": "0"},
        {"system": "", "event": "0"}, {"system": "0", "event": "x"},
    ])
    def test_deleting_a_chord_that_is_not_there_changes_nothing(self, app, saved, bad):
        call(app, "POST", "/a/%s/chord/delete" % saved, bad)
        assert len(store.load(saved)["systems"][0]["events"]) == 1

    def test_an_edit_outlives_a_key_pick(self, app, saved):
        """A key change re-spells every notehead on the staff, so without the replay
        picking a key would quietly undo the correction made before it."""
        call(app, "POST", "/a/%s/note" % saved, self.note_form(value="flat"))
        assert self.names(saved) == ["C4", "Eb4", "G4"]
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "2"})
        # Two sharps reach the C, which is the key doing its job.  The E does not
        # follow it, because a person said what that notehead is.
        assert self.names(saved) == ["C#4", "Eb4", "G4"]
        assert len(store.load(saved)["edits"]) == 1

    def test_re_analyzing_starts_from_the_photo_again(self, app, pickable):
        """A fresh reading renumbers everything, so there is no honest anchor for an
        edit made on the old one.  The page says so rather than pretending."""
        call(app, "POST", "/a/%s/note" % pickable, self.note_form(value="sharp"))
        assert len(store.load(pickable)["edits"]) == 1
        call(app, "POST", "/a/%s/reanalyze" % pickable, {})
        assert store.load(pickable)["edits"] == []

    def test_the_page_offers_the_controls_without_scripting(self, app, saved):
        _status, _headers, body = call(app, "GET", "/a/%s" % saved)
        text = body.decode()
        assert 'action="/a/%s/note"' % saved in text
        assert 'action="/a/%s/chord/delete"' % saved in text
        # Every accidental is its own submit button, so none of this needs javascript.
        for kind in ("sharp", "flat", "natural"):
            assert 'name="value" value="%s"' % kind in text
        assert 'class="js-only"' not in text.split('class="fix"')[1].split('</details>')[0]

    def test_the_page_marks_the_accidental_in_force(self, app, saved):
        call(app, "POST", "/a/%s/note" % saved, self.note_form(value="flat"))
        _status, _headers, body = call(app, "GET", "/a/%s" % saved)
        text = body.decode()
        marked = re.findall(r'name="value" value="(\w+)"[^>]*aria-current="true"', text)
        assert marked == ["flat"], "one accidental is marked, and it is the one written"
        assert 'value="clear"' in text, "and there is a way back"

    def test_the_page_says_what_re_analyzing_would_cost(self, app, saved):
        """Losing a hand correction to a button press is worth a word of warning."""
        _s, _h, before = call(app, "GET", "/a/%s" % saved)
        assert "corrected by hand" not in before.decode()
        call(app, "POST", "/a/%s/note" % saved, self.note_form(value="sharp"))
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert "corrected by hand" in text, "the row says so"
        assert "clears them" in text, "and the re-analyze button warns first"
