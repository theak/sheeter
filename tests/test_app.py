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


@pytest.fixture
def saved_pair():
    """Two chords in one measure, so a written accidental has something to carry to."""
    raw = b"two chords worth of music"
    doc = make_doc(raw, "Bars 5 to 6")
    system = doc["systems"][0]
    second = json.loads(json.dumps(system["events"][0]))
    second["index"] = 1
    second["x"] = 200.0
    second["x_range"] = [180, 220]
    for note in second["parts"][0]["notes"]:
        note["x"] = 200.0
    system["events"].append(second)
    return store.save(schema.validate_analysis(doc), raw, "jpg", png_bytes())


def call(app, method, path, form=None, accept=None):
    """Drive the WSGI app directly.  Returns ``(status, headers, body)``.

    *accept* is what the caller says it will take.  Left out, this is a form post from a
    browser, which is the path a page with no scripting takes; the overlay asks for
    ``application/json`` and gets the panels an edit moved instead of a redirect.
    """
    body = urllib.parse.urlencode(form or {}).encode()
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
        "SERVER_NAME": "testserver", "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body), "wsgi.errors": io.BytesIO(),
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
    }
    if accept is not None:
        environ["HTTP_ACCEPT"] = accept
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

    def test_the_player_reports_whether_it_can_be_heard(self, app):
        """The per-note previews are gated on this, and a hover cannot ask twice.

        Moving a mouse is not a user gesture, so it cannot resume a suspended audio
        context; notes scheduled against one wait and then all sound at once when a
        later click resumes it.  Without ``live`` to ask, overlay.js has no way to
        tell the difference and the previews arrive as a chord.
        """
        _status, _headers, body = call(app, "GET", "/static/js/audio.js")
        assert b"live" in body

    def test_offers_a_play_button_per_notehead(self, app, saved):
        """One per note, carrying the pitch it plays and named after it.

        The panel is where these live rather than the labels over the photo: a label
        with a chord in it leaves each note a few pixels to aim at, where a panel row
        is already thumb sized.
        """
        _status, _headers, body = call(app, "GET", "/a/%s" % saved)
        page = body.decode("utf-8")
        rows = page.count('class="fix-note"')
        buttons = page.count('class="fix-hear"')
        assert rows and buttons == rows
        assert 'data-midi=' in page
        assert "Hear " in page

    def test_the_play_buttons_are_drawn_and_only_with_a_player(self, app):
        """No rule, no button: the glyph is a pseudo-element, not a character.

        And the column they sit in is cut into the row by the same class that draws
        them, so with no scripting, or no Web Audio, the row is the five it was rather
        than five and an empty gap.
        """
        _status, _headers, body = call(app, "GET", "/static/css/sheeter.css")
        css = body.decode("utf-8")
        assert "button.fix-hear" in css
        assert ".fixes.can-hear button.fix-hear" in css
        assert ".fixes.can-hear .fix-note" in css

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


def step_of(name):
    """The diatonic step a pitch name sits on, which is what the menus post."""
    return pitches.parse_name(name)[0]


class TestManualEditing:
    """Correcting a reading by hand, when the reader got a note or a chord wrong.

    Four things go wrong often enough to be worth fixing by eye rather than by
    re-reading the photo: a missed accidental, a notehead read a space off, a note
    the reader invented, and one it never saw.  Every control is a submit button in
    its own form, so all of it works with scripting off, like the key picker.
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
    def form(**over):
        form = {"system": "0", "event": "0", "staff": "0", "note": "1",
                "step_number": "1"}
        form.update(over)
        return form

    # ---------------------------------------------------------------- accidentals

    def test_sharpening_a_note_sticks(self, app, saved):
        assert self.names(saved) == ["C4", "E4", "G4"]
        status, _headers, _ = call(app, "POST", "/a/%s/note" % saved,
                                   self.form(value="sharp"))
        assert status.startswith("303")
        assert self.names(saved) == ["C4", "E#4", "G4"]
        edit = store.load(saved)["edits"][0]
        assert edit["kind"] == "accidental" and edit["value"] == "sharp"
        # Anchored by height, not by index, so adding or removing a notehead cannot
        # re-point it at the one next door.
        assert "note" not in edit and isinstance(edit["y"], float)

    def test_flat_and_natural_work_the_same_way(self, app, saved):
        call(app, "POST", "/a/%s/note" % saved, self.form(value="flat"))
        assert self.names(saved) == ["C4", "Eb4", "G4"]
        call(app, "POST", "/a/%s/note" % saved, self.form(value="natural"))
        assert self.names(saved) == ["C4", "E4", "G4"]

    def test_asking_for_the_accidental_it_already_sounds_records_nothing(
            self, app, saved):
        """The buttons show which one is in force, so clicking that one is a reader
        agreeing with the page.  Recording it would fill the log with corrections that
        correct nothing, and put "corrected by hand" on an untouched reading."""
        call(app, "POST", "/a/%s/note" % saved, self.form(value="natural"))
        assert self.names(saved) == ["C4", "E4", "G4"]
        assert store.load(saved)["edits"] == []

    def test_it_comes_back_to_the_step_that_was_edited(self, app, saved):
        """The fix panel belongs to one step, so a redirect to the top of the page with
        nothing selected would lose the reader's place after every button."""
        _status, headers, _ = call(app, "POST", "/a/%s/note" % saved,
                                   self.form(value="sharp"))
        assert headers["Location"].endswith("/a/%s#step-1" % saved)

    def test_undoing_puts_the_reading_and_the_log_back(self, app, saved):
        call(app, "POST", "/a/%s/note" % saved, self.form(value="sharp"))
        call(app, "POST", "/a/%s/note" % saved, self.form(value="clear"))
        assert self.names(saved) == ["C4", "E4", "G4"]
        assert store.load(saved)["edits"] == []

    @pytest.mark.parametrize("bad", [
        {"value": "double-sharp"}, {"value": "wobbly"}, {"value": "sharp", "note": "9"},
        {"value": "sharp", "event": "9"}, {"value": "sharp", "staff": "9"},
        {"value": "sharp", "system": "9"}, {"value": "sharp", "note": "-1"},
        {"value": "sharp", "note": "one"}, {"value": "sharp", "system": ""},
    ])
    def test_a_form_nobody_could_have_submitted_changes_nothing(self, app, saved, bad):
        status, _headers, _ = call(app, "POST", "/a/%s/note" % saved, self.form(**bad))
        assert status.startswith("303")
        assert self.names(saved) == ["C4", "E4", "G4"]
        assert store.load(saved)["edits"] == []

    # ---------------------------------------------------------------- the pitch itself

    def test_a_notehead_read_a_space_off_can_be_moved(self, app, saved):
        """E4 is four steps above the bottom line of a treble staff; F4 is the next one
        up.  Nothing is measured again: the pitch follows the staff position."""
        status, headers, _ = call(app, "POST", "/a/%s/note/pitch" % saved,
                                  self.form(step=str(step_of("F4"))))
        assert status.startswith("303")
        assert headers["Location"].endswith("#step-1")
        assert self.names(saved) == ["C4", "F4", "G4"]

    def test_moving_a_note_keeps_the_chord_in_order(self, app, saved):
        """Low to high is what the overlay and the interval list both assume."""
        call(app, "POST", "/a/%s/note/pitch" % saved,
             self.form(step=str(step_of("E5"))))
        assert self.names(saved) == ["C4", "G4", "E5"]

    def test_moving_a_note_outlives_a_key_pick_with_nothing_replayed(self, app, saved):
        """The pitch is stored as a staff position, so re-deriving finds it again.  This
        is why a move needs no entry in the correction log."""
        call(app, "POST", "/a/%s/note/pitch" % saved,
             self.form(step=str(step_of("B4"))))
        assert store.load(saved)["edits"] == []
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "-1"})
        # One flat reaches the B, which is the key doing its job on a notehead the
        # reader moved: the move is a staff position, not a spelling.
        assert self.names(saved) == ["C4", "G4", "Bb4"]

    def test_a_correction_moves_with_the_notehead_it_was_made_on(self, app, saved):
        call(app, "POST", "/a/%s/note" % saved, self.form(value="sharp"))
        call(app, "POST", "/a/%s/note/pitch" % saved,
             self.form(step=str(step_of("F4"))))
        assert self.names(saved) == ["C4", "F#4", "G4"], "the sharp followed it"
        assert len(store.load(saved)["edits"]) == 1, "and is still one correction"

    @pytest.mark.parametrize("bad", ["", "999", "-999", "up a bit", "4.5"])
    def test_a_pitch_nobody_could_have_picked_changes_nothing(self, app, saved, bad):
        call(app, "POST", "/a/%s/note/pitch" % saved, self.form(step=bad))
        assert self.names(saved) == ["C4", "E4", "G4"]

    # ---------------------------------------------------------------- adding, removing

    def test_one_note_of_a_chord_can_go(self, app, saved):
        status, _headers, _ = call(app, "POST", "/a/%s/note/delete" % saved, self.form())
        assert status.startswith("303")
        assert self.names(saved) == ["C4", "G4"]

    def test_deleting_the_last_note_of_a_hand_takes_the_hand(self, app, saved):
        for _ in range(3):
            call(app, "POST", "/a/%s/note/delete" % saved, self.form(note="0"))
        doc = store.load(saved)
        assert self.names(saved) == []
        # An event with no parts is the whole chord gone, and the schema will not hold
        # an event with none, so it goes rather than sitting there empty.
        assert doc["systems"][0]["events"] == []
        schema.validate_analysis(doc)

    def test_a_missed_note_can_be_added(self, app, saved):
        status, headers, _ = call(app, "POST", "/a/%s/note/add" % saved,
                                  {"system": "0", "event": "0", "staff": "0",
                                   "step": str(step_of("B4")), "step_number": "1"})
        assert status.startswith("303")
        assert headers["Location"].endswith("#step-1")
        assert self.names(saved) == ["C4", "E4", "G4", "B4"]

    def test_adding_the_note_that_is_already_there_changes_nothing(self, app, saved):
        call(app, "POST", "/a/%s/note/add" % saved,
             {"system": "0", "event": "0", "staff": "0", "step": str(step_of("E4")),
              "step_number": "1"})
        assert self.names(saved) == ["C4", "E4", "G4"]

    def test_an_added_note_is_marked_as_not_the_photo_s(self, app, saved):
        """The overlay draws changed notes differently, and an added one is the clearest
        case there is of a note the photo did not supply."""
        call(app, "POST", "/a/%s/note/add" % saved,
             {"system": "0", "event": "0", "staff": "0", "step": str(step_of("B4")),
              "step_number": "1"})
        notes = store.load(saved)["systems"][0]["events"][0]["parts"][0]["notes"]
        added = next(note for note in notes if note["name"] == "B4")
        assert added["changed"] is True
        # This fixture's noteheads carry no measured size, so the added one falls back
        # to the staff's own scale rather than to nothing, which the overlay cannot draw.
        assert added["w"] and added["h"]

    def test_a_spurious_chord_can_be_deleted(self, app, saved):
        status, headers, _ = call(app, "POST", "/a/%s/chord/delete" % saved,
                                  {"system": "0", "event": "0", "step_number": "1"})
        assert status.startswith("303")
        assert headers["Location"].endswith("#step-1")
        assert store.load(saved)["systems"][0]["events"] == []

    @pytest.mark.parametrize("bad", [
        {"system": "0", "event": "9"}, {"system": "9", "event": "0"},
        {"system": "", "event": "0"}, {"system": "0", "event": "x"},
    ])
    def test_deleting_a_chord_that_is_not_there_changes_nothing(self, app, saved, bad):
        call(app, "POST", "/a/%s/chord/delete" % saved, bad)
        assert len(store.load(saved)["systems"][0]["events"]) == 1

    # ---------------------------------------------------------------- persistence

    def test_an_edit_outlives_a_key_pick(self, app, saved):
        """A key change re-spells every notehead on the staff, so without the replay
        picking a key would quietly undo the correction made before it."""
        call(app, "POST", "/a/%s/note" % saved, self.form(value="flat"))
        assert self.names(saved) == ["C4", "Eb4", "G4"]
        call(app, "POST", "/a/%s/key" % saved, {"fifths": "2"})
        # Two sharps reach the C, which is the key doing its job.  The E does not
        # follow it, because a person said what that notehead is.
        assert self.names(saved) == ["C#4", "Eb4", "G4"]
        assert len(store.load(saved)["edits"]) == 1

    def test_re_analyzing_starts_from_the_photo_again(self, app, pickable):
        """A fresh reading renumbers everything, so there is no honest anchor for a
        correction made on the old one.  The page says so rather than pretending."""
        call(app, "POST", "/a/%s/note" % pickable, self.form(value="sharp"))
        assert len(store.load(pickable)["edits"]) == 1
        call(app, "POST", "/a/%s/reanalyze" % pickable, {})
        assert store.load(pickable)["edits"] == []

    # ---------------------------------------------------------------- the page itself

    def test_every_step_gets_a_panel_and_it_needs_no_scripting(self, app, saved):
        _status, _headers, body = call(app, "GET", "/a/%s" % saved)
        text = body.decode()
        assert 'id="fix-1"' in text and 'data-step="1"' in text
        for action in ("note", "note/pitch", "note/delete", "note/add",
                       "chord/delete"):
            assert 'action="/a/%s/%s"' % (saved, action) in text
        for kind in ("sharp", "flat", "natural"):
            assert 'name="value" value="%s"' % kind in text
        # Rendered visible, not hidden: with no scripting every panel is simply there.
        assert 'class="fix-panel"' in text
        assert 'class="fix-panel" hidden' not in text

    def test_the_two_sections_it_replaces_are_gone(self, app, saved):
        """The step detail read back what the labels on the photo already say, and the
        whole-reading table said it a third time."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert 'id="detail"' not in text
        assert "The whole reading" not in text
        assert "Every note, from the bass up" not in text

    def test_the_page_marks_what_each_note_sounds_now(self, app, saved):
        """Not only what is printed: a note sharpened by the key signature shows sharp,
        because that is what it is."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        marked = re.findall(r'name="value" value="(\w+)"[^>]*aria-current="true"', text)
        assert marked == ["natural"] * 3, "C4 E4 G4 with no key are all natural"
        call(app, "POST", "/a/%s/note" % saved, self.form(value="flat"))
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        marked = re.findall(r'name="value" value="(\w+)"[^>]*aria-current="true"', text)
        assert sorted(marked) == ["flat", "natural", "natural"]
        assert 'value="clear"' in text, "and there is a way back"

    def test_the_pitch_menu_offers_the_staff_and_a_little_either_side(self, app, saved):
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        panel = text.split('id="fix-1"')[1]
        assert 'name="step"' in panel
        # An octave either side of the staff: E3 to F6 on a treble staff, which covers
        # what a hand is written in without a menu nobody can scroll.
        for name in ("E3", "C4", "E4", "F4", "A5", "F6"):
            assert ">%s<" % name in panel, "%s is reachable from a treble staff" % name
        assert ">C2<" not in panel, "and it stops somewhere"
        selected = re.findall(r'<option value="(-?\d+)" selected>([A-G]\d)<', panel)
        assert [name for _step, name in selected][:3] == ["G4", "E4", "C4"], \
            "each menu starts on the note it is for, high to low"

    def test_a_reading_with_nothing_left_in_it_says_so(self, app, saved):
        """This fixture has one hand, so emptying it empties the reading.  A hand that
        read nothing on a grand staff still gets its own Add a note, which needs two
        staves to show and is covered in test_pipeline."""
        for _ in range(3):
            call(app, "POST", "/a/%s/note/delete" % saved, self.form(note="0"))
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert "there is nothing to correct" in text
        assert 'class="fix-panel"' not in text

    def test_the_page_says_what_re_analyzing_would_cost(self, app, saved):
        """Losing a hand correction to a button press is worth a word of warning.

        The sentence is in the page either way and hidden while there is nothing to
        warn about, rather than being rendered only once there is.  A correction lands
        without a page load, so the count in it is one the overlay has to be able to
        update, and it can only update something that is there.
        """
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert 'id="edits-note" hidden' in text, "no corrections, so nothing to say yet"
        assert ">0</span> note(s) corrected" in text

        call(app, "POST", "/a/%s/note" % saved, self.form(value="sharp"))
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert "clears them" in text, "the re-analyze button warns first"
        assert 'id="edits-note" hidden' not in text
        assert ">1</span> note(s) corrected" in text

    def test_move_is_rendered_and_left_for_the_overlay_to_hide(self, app, saved):
        """It applies a pitch picked from the menu, so it has nothing to do until the
        menu changes and only asks to be puzzled over.  The overlay hides it, which is
        the direction that fails safe: with no scripting the menu keeps its submit
        button rather than becoming a control that cannot be submitted."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert 'class="fix-go secondary outline"' in text
        assert 'class="fix-go secondary outline" hidden' not in text
        assert 'formaction="/a/%s/note/pitch"' % saved in text

    def test_the_staves_table_is_gone(self, app, saved):
        """The key it listed is what the picker at the top of the page already shows."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert "<h2>Staves</h2>" not in text
        assert 'class="staves"' not in text

    def test_delete_this_chord_comes_before_the_noteheads(self, app, saved):
        """It is the one control about the whole step rather than about a notehead, and
        last on a panel long enough to scroll it sat under the hand just corrected."""
        panel = call(app, "GET", "/a/%s" % saved)[2].decode().split('id="fix-1"')[1]
        assert panel.index("chord/delete") < panel.index('name="step"'), \
            "before the first notehead's pitch menu"
        assert panel.index("chord/delete") < panel.index("fix-acc"), \
            "and before the first accidental button"

    def test_the_labels_are_always_both_and_the_mode_sits_where_they_did(
            self, app, saved):
        """Notes-only and chords-only were three buttons for a choice nobody needs to
        make; the choice that matters on a dense page is how many labels at once."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert "view-button" not in text and 'data-view' not in text
        # The mode buttons now sit in the bar above the photo, where the labels are.
        above = text.split('id="viewport"')[0]
        assert 'data-mode="step"' in above and 'data-mode="all"' in above
        assert 'data-mode' not in text.split('id="step-bar"')[1]

    def test_the_state_chip_sits_on_the_summary_line(self, app, saved):
        """Two states of one field, so one place for it, and the sentence under it said
        in prose what the chip already says."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        summary = text.split('class="muted summary-row"')[1].split("</p>")[0]
        assert "Geometry only" in summary and "1 system" in summary
        assert summary.index("Geometry only") < summary.index("1 system")
        assert "measuring notehead positions" not in text
        assert "No model has" not in text

    def test_the_hint_line_is_gone(self, app, saved):
        """Instructions for controls that are already labelled, and the longest thing on
        the page above the photo."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        assert "stage-hint" not in text
        assert "Tap any label to read that step" not in text
        assert "drag to move around" not in text

    def test_copy_as_text_sits_with_the_other_whole_reading_actions(self, app, saved):
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        actions = text.split('class="provenance-actions"')[1].split("</section>")[0]
        assert 'id="copy-button"' in actions
        assert "Re-analyze" in actions

    def test_renaming_is_at_the_title_and_needs_no_scripting(self, app, saved):
        """Rendered visible with the pencil hidden, so the overlay hides the form rather
        than revealing it: with no scripting the title is editable where it stands."""
        text = call(app, "GET", "/a/%s" % saved)[2].decode()
        row = text.split('class="title-row"')[1].split("</div>")[0]
        assert 'id="doc-title"' in row
        assert 'id="rename-toggle"' in row and "js-only" in row
        assert 'id="rename-form"' in row and 'name="title"' in row
        assert 'id="rename-form" hidden' not in text
        # And it is not still down in the manage section as well.
        assert text.count('id="title-input"') == 1

    def test_renaming_still_works_from_the_new_place(self, app, saved):
        status, _headers, _ = call(app, "POST", "/a/%s/rename" % saved,
                                   {"title": "Old Folks"})
        assert status.startswith("303")
        assert store.load(saved)["title"] == "Old Folks"

    def test_removing_a_note_is_a_bin_and_not_a_fourth_accidental(self, app, saved):
        """Sharp, flat and natural spell a note; the bin takes it away.  Drawn the same
        as the other three it read as a fourth thing the note could be."""
        panel = call(app, "GET", "/a/%s" % saved)[2].decode().split('id="fix-1"')[1]
        drop = panel.split('class="fix-acc fix-drop"')[1].split("</button>")[0]
        assert "<svg" in drop, "a bin, drawn"
        assert "×" not in drop and "&times;" not in drop
        assert "Remove" in drop, "and named for a screen reader, the icon being decorative"

    def test_a_notehead_is_one_row_of_five(self, app, saved):
        """The menu is the first column, not a line of its own above the buttons, so the
        four buttons line up down the panel however wide the menus are."""
        panel = call(app, "GET", "/a/%s" % saved)[2].decode().split('id="fix-1"')[1]
        row = panel.split('class="fix-note"')[1].split("</form>")[0]
        assert row.count('class="fix-acc"') == 3, "three spellings"
        assert row.count("fix-drop") == 1, "and the bin"
        # The wrapper that made the buttons their own line is gone, so they are children
        # of the row itself.
        assert "fix-buttons" not in row
        assert row.index("fix-pitch") < row.index('class="fix-acc"')


JSON = "application/json"
#: What a browser sends when a form is posted, which must never look like the overlay.
BROWSER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class TestInstantEditing:
    """A correction the overlay applies in place, instead of reloading the page.

    The page is 86% fix panels on a dense reading, and an accidental moves one or two of
    them, so the answer to a correction is those panels rather than the whole page.  It
    is the same route either way and the same edit: only what comes back differs, which
    is what keeps the no-scripting path exactly as it was.
    """

    @staticmethod
    def form(**over):
        form = {"system": "0", "event": "0", "staff": "0", "note": "1",
                "step_number": "1"}
        form.update(over)
        return form

    def answer(self, app, saved, path="/note", **over):
        status, headers, body = call(app, "POST", "/a/%s%s" % (saved, path),
                                     self.form(**over), accept=JSON)
        assert status.startswith("200"), status
        assert "application/json" in headers.get("Content-Type", "")
        return json.loads(body)

    # ---------------------------------------------------------------- both ways out

    def test_a_form_post_still_redirects_to_its_step(self, app, saved):
        """The whole feature with no scripting, and it must not have moved an inch."""
        status, headers, _ = call(app, "POST", "/a/%s/note" % saved,
                                  self.form(value="sharp"), accept=BROWSER)
        assert status.startswith("303")
        assert headers["Location"].endswith("/a/%s#step-1" % saved)
        assert store.load(saved)["systems"][0]["events"][0]["parts"][0]["notes"][1][
            "name"] == "E#4"

    def test_no_accept_header_at_all_still_redirects(self, app, saved):
        status, _headers, _ = call(app, "POST", "/a/%s/note" % saved,
                                   self.form(value="sharp"))
        assert status.startswith("303")

    def test_asking_for_json_gets_the_reading_and_the_panels(self, app, saved):
        answer = self.answer(app, saved, value="sharp")
        assert sorted(answer) == ["document", "panels"]
        assert list(answer["panels"]) == ["1"]
        assert answer["document"]["systems"][0]["events"][0]["parts"][0]["notes"][1][
            "name"] == "E#4"
        assert len(answer["document"]["edits"]) == 1

    def test_the_reading_it_answers_with_is_the_one_on_disk(self, app, saved):
        """Not the copy in hand: a rename could have landed while the edit ran, and what
        the overlay holds afterwards should be what the store has."""
        answer = self.answer(app, saved, value="sharp")
        assert answer["document"] == store.load(saved)

    # ---------------------------------------------------------------- which panels

    def test_a_panel_it_sends_is_the_markup_the_page_would_have(self, app, saved_pair):
        """One renderer, so a panel swapped into the page in the browser is the panel
        that page would have been served.  Asserted rather than assumed, because two
        renderers agreeing today is how they come to disagree later.

        On a reading with two steps rather than one, so that the "of N" in the heading
        is a number this can be wrong about.
        """
        answer = self.answer(app, saved_pair, value="flat")
        page = call(app, "GET", "/a/%s" % saved_pair)[2].decode()
        assert len(answer["panels"]) == 2
        for number, markup in answer["panels"].items():
            assert markup in page, "panel %s is not what the page renders" % number
            assert 'class="fix-of">2<' in markup

    def test_an_accidental_sends_the_panels_it_carries_to(self, app, saved_pair):
        """It holds to the end of its measure, so the later chord at that staff position
        is re-spelled too and its panel is as wrong as the edited one until it is
        redrawn.  This is why the answer is a set of panels, not the one clicked in."""
        answer = self.answer(app, saved_pair, value="sharp")
        assert sorted(answer["panels"], key=int) == ["1", "2"]

    def test_asking_for_the_accidental_a_note_has_sends_no_panels(self, app, saved):
        """Clicking the one already in force is a reader agreeing with the page."""
        answer = self.answer(app, saved, value="natural")
        assert answer["panels"] == {}
        assert answer["document"]["edits"] == []

    def test_moving_a_notehead_sends_its_panel(self, app, saved):
        answer = self.answer(app, saved, path="/note/pitch",
                             step=str(step_of("A4")))
        assert list(answer["panels"]) == ["1"]
        assert "A 4" in answer["panels"]["1"]

    def test_adding_a_notehead_sends_its_panel(self, app, saved):
        answer = self.answer(app, saved, path="/note/add", step=str(step_of("B4")))
        assert list(answer["panels"]) == ["1"]
        assert "B 4" in answer["panels"]["1"]

    def test_undoing_a_correction_sends_its_panel(self, app, saved):
        """The one control that changes the shape of its row: the undo button is there
        because the note is corrected, so undoing takes the button away with it."""
        assert "fix-clear" in self.answer(app, saved, value="sharp")["panels"]["1"]
        answer = self.answer(app, saved, value="clear")
        assert list(answer["panels"]) == ["1"]
        assert "fix-clear" not in answer["panels"]["1"]
        assert answer["document"]["edits"] == []
        assert answer["document"]["systems"][0]["events"][0]["parts"][0]["notes"][1][
            "name"] == "E4"

    # ---------------------------------------------------------------- reload instead

    def test_deleting_a_chord_names_the_step_that_went(self, app, saved_pair):
        """A chord going renumbers every step after it, and with it the heading and the
        indices in every one of their forms.  None of that is sent: the overlay stamps
        it onto the panels it has, from the reading, which is the only thing that knows
        what each step is now.  So the answer is the reading and which step went.
        """
        status, _headers, body = call(app, "POST", "/a/%s/chord/delete" % saved_pair,
                                      {"system": "0", "event": "0",
                                       "step_number": "1"}, accept=JSON)
        assert status.startswith("200")
        answer = json.loads(body)
        assert answer["deleted"] == 1
        assert answer["panels"] == {}, "the survivors differ only in what is stamped"
        assert len(answer["document"]["systems"][0]["events"]) == 1
        assert len(store.load(saved_pair)["systems"][0]["events"]) == 1

    def test_the_reading_says_what_the_renumbered_steps_are(self, app, saved_pair):
        """Which is what the overlay stamps from, so it has to be in the answer rather
        than left for the browser to work out by subtracting one."""
        answer = json.loads(call(app, "POST", "/a/%s/chord/delete" % saved_pair,
                                 {"system": "0", "event": "0", "step_number": "1"},
                                 accept=JSON)[2])
        events = answer["document"]["systems"][0]["events"]
        assert [event["index"] for event in events] == [0], \
            "the surviving chord is event 0 now, not event 1"

    def test_deleting_the_second_chord_leaves_the_first_alone(self, app, saved_pair):
        """Nothing before the cut is renumbered, so nothing about it moves."""
        answer = json.loads(call(app, "POST", "/a/%s/chord/delete" % saved_pair,
                                 {"system": "0", "event": "1", "step_number": "2"},
                                 accept=JSON)[2])
        assert answer["deleted"] == 2
        assert answer["panels"] == {}

    def test_emptying_a_chord_by_removing_notes_names_it_too(self, app, saved):
        """Taking the last notehead off the last staff takes the chord with it, so a
        note delete can be a chord delete and is answered as one."""
        for _ in range(2):
            answer = self.answer(app, saved, path="/note/delete", note="0")
            assert "deleted" not in answer
        answer = self.answer(app, saved, path="/note/delete", note="0")
        assert answer["deleted"] == 1
        assert answer["document"]["systems"][0]["events"] == []

    def test_a_chord_delete_naming_a_chord_that_is_not_there_moves_nothing(
            self, app, saved_pair):
        """A stale form, which is not a fault: nothing was deleted, so nothing moved and
        no step is named as gone.  The overlay leaves the page as it is."""
        status, _headers, body = call(app, "POST", "/a/%s/chord/delete" % saved_pair,
                                      {"system": "0", "event": "9",
                                       "step_number": "1"}, accept=JSON)
        assert status.startswith("200")
        answer = json.loads(body)
        assert answer["panels"] == {}
        assert "deleted" not in answer
        assert len(store.load(saved_pair)["systems"][0]["events"]) == 2

    def test_a_form_naming_nothing_real_asks_for_a_reload(self, app, saved):
        status, _headers, body = call(app, "POST", "/a/%s/note" % saved,
                                      self.form(system="not-a-number"), accept=JSON)
        assert status.startswith("200")
        assert json.loads(body) == {"reload": True}

    def test_a_reading_that_is_gone_is_still_a_404(self, app):
        status, _headers, _ = call(app, "POST", "/a/aaaaaaaaaaaa/note",
                                   self.form(value="sharp"), accept=JSON)
        assert status.startswith("404")

    # ---------------------------------------------------------------- the overlay end

    def served(self, app, path):
        status, _headers, body = call(app, "GET", path)
        assert status.startswith("200")
        return body.decode("utf-8")

    def test_the_overlay_asks_for_json_and_falls_back_to_a_reload(self, app):
        overlay = self.served(app, "/static/js/overlay.js")
        assert "Accept: 'application/json'" in overlay.replace('"', "'")
        assert "location.reload()" in overlay
        # Never re-posting the form: a replayed post after a chord went lands on a
        # different chord, because deleting one renumbers the events.
        assert "requestSubmit" not in overlay

    def test_the_overlay_reads_the_pressed_button_safely(self, app):
        """The formaction property is the address of the page when the attribute is
        absent, not the form's action, and the accidental buttons are the ones without
        it.  Read unguarded it posts an accidental at /a/<id> instead of /a/<id>/note.
        """
        overlay = self.served(app, "/static/js/overlay.js")
        assert "hasAttribute('formaction')" in overlay

    def test_the_overlay_sends_one_correction_at_a_time(self, app):
        """The page reload used to serialize these for free.  Writing an edit back is
        read, splice, write, so two in flight together have the second drop the first.
        """
        overlay = self.served(app, "/static/js/overlay.js")
        assert "var busy = false;" in overlay
        assert "aria-busy" in overlay
        css = self.served(app, "/static/css/sheeter.css")
        assert '.fix-note [aria-busy="true"]' in css

    def test_the_overlay_puts_focus_back_after_swapping_a_panel(self, app):
        """innerHTML destroys the button that was pressed, so somebody working by
        keyboard would land on the body and have to tab in from the top of the page
        between pressing sharp and pressing flat on the same note."""
        overlay = self.served(app, "/static/js/overlay.js")
        assert "function refocus(" in overlay
        assert ".focus()" in overlay

    def test_the_correction_count_is_in_the_page_for_the_overlay_to_update(self, app,
                                                                          saved):
        page = self.served(app, "/a/%s" % saved)
        assert 'id="edits-count"' in page
        assert 'id="reanalyze-form"' in page
        # The confirm moved into overlay.js, because the count written into it went
        # stale the moment a correction stopped reloading the page.
        assert "onsubmit" not in page.split('id="reanalyze-form"')[1][:400]
        assert "correction(s) you made by hand will be cleared" in self.served(
            app, "/static/js/overlay.js")
