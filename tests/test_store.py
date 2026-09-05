"""Tests for sheeter.store.

Run from the repo root so that ``sheeter`` is importable:
``/home/user/.venv/bin/python -m pytest tests/test_store.py``
"""

import io
import json
import os

import pytest
from PIL import Image

from sheeter import naming, pitches, schema, store


CHORDS = [
    ["Bb3", "F4", "C5", "D5", "F5", "A5"],
    ["Eb3", "Bb3", "G4", "C5", "F5"],
    ["G2", "F3", "F4", "Bb4", "C5"],
    ["C4", "E4", "G4"],
]

BAD_IDS = [
    "../../etc",
    "..",
    ".",
    "",
    "/",
    "abc",
    "0123456789abc",
    "0123456789AB",
    "0123456789ab/../..",
    "data/0123456789ab",
    None,
    12,
]


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("SHEETER_DATA_DIR", str(root))
    return root


def png_bytes(width=600, height=400, shade=200):
    im = Image.new("L", (width, height), shade)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _staff():
    return {
        "index": 0,
        "hand": "right",
        "clef": "treble",
        "clef_confidence": 0.9,
        "lines": [100.0, 120.0, 140.0, 160.0, 180.0],
        "unit": 20.0,
        "x_range": [0, 600],
        "key_fifths": -2,
        "key_confidence": 0.8,
        "cut_off": False,
    }


def _event(index, names):
    notes = []
    for name in names:
        step, alter = pitches.parse_name(name)
        notes.append(pitches.make_note(step, alter, 100.0 + 40 * index, 140.0))
    notes.sort(key=lambda n: n["midi"])
    ascii_names = [n["name"] for n in notes]
    part = {
        "staff": 0,
        "hand": "right",
        "duration_hint": "quarter",
        "dotted": False,
        "notes": notes,
        "chord": naming.name_chord(ascii_names, -2),
    }
    x = 100.0 + 40 * index
    return {
        "index": index,
        "x": x,
        "x_range": [x - 15, x + 15],
        "confidence": 0.9,
        "note": None,
        "parts": [part],
        "combined": naming.name_combined(ascii_names, -2),
    }


def make_doc(raw, chords=CHORDS, created_at="2026-09-05T20:31:00Z",
             title="IMG_4021.jpg"):
    source = {
        "filename": title,
        "content_type": "image/jpeg",
        "bytes": len(raw),
        "width": 900,
        "height": 600,
    }
    image = {"width": 600, "height": 400, "scale": 0.6667, "deskew_deg": -1.25}
    doc = schema.new_analysis(store.analysis_id(raw), created_at, title, source, image)
    doc["systems"] = [{
        "index": 0,
        "y_range": [80, 200],
        "x_range": [0, 600],
        "grand": False,
        "cut_off": False,
        "staves": [_staff()],
        "events": [_event(i, names) for i, names in enumerate(chords)],
    }]
    return schema.validate_analysis(doc)


def save_one(raw=b"photo-one", **kw):
    doc = make_doc(raw, **kw)
    return store.save(doc, raw, "jpg", png_bytes()), doc


# --- data_dir -------------------------------------------------------------

def test_data_dir_reads_the_env_var_every_call(data_root, monkeypatch, tmp_path):
    assert store.data_dir() == str(data_root)
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("SHEETER_DATA_DIR", str(other))
    assert store.data_dir() == str(other)


def test_data_dir_defaults_into_the_repo(monkeypatch):
    monkeypatch.delenv("SHEETER_DATA_DIR", raising=False)
    assert store.data_dir().endswith(os.path.join("sheeter", "data"))


# --- round trip -----------------------------------------------------------

def test_round_trip(data_root):
    raw = b"a photo of some music"
    ident, doc = save_one(raw)

    assert ident == store.analysis_id(raw)
    assert store.exists(ident)
    assert store.load(ident) == doc

    directory = data_root / ident
    assert sorted(os.listdir(directory)) == [
        "analysis.json", "original.jpg", "processed.png", "thumb.jpg"]
    assert (directory / "original.jpg").read_bytes() == raw
    assert store.original_name(ident) == "original.jpg"
    assert store.path(ident, "thumb.jpg") == str(directory / "thumb.jpg")


def test_thumb_is_small_and_keeps_its_aspect(data_root):
    raw = b"tall photo"
    doc = make_doc(raw)
    ident = store.save(doc, raw, "png", png_bytes(1200, 900))
    with Image.open(data_root / ident / "thumb.jpg") as im:
        assert max(im.size) == store.THUMB_MAX
        assert im.size == (480, 360)


def test_content_addressing_does_not_duplicate(data_root):
    raw = b"the very same bytes"
    first, _ = save_one(raw)
    second, _ = save_one(raw, title="re-uploaded.jpg")
    assert first == second
    assert os.listdir(data_root) == [first]


def test_different_photos_get_different_directories(data_root):
    first, _ = save_one(b"photo-one")
    second, _ = save_one(b"photo-two")
    assert first != second
    assert sorted(os.listdir(data_root)) == sorted([first, second])


def test_save_rejects_a_doc_that_does_not_match_the_bytes(data_root):
    doc = make_doc(b"one")
    with pytest.raises(ValueError):
        store.save(doc, b"two", "jpg", png_bytes())
    assert os.listdir(data_root) == []


def test_save_validates_before_writing_anything(data_root):
    raw = b"broken doc"
    doc = make_doc(raw)
    del doc["engine"]["verified"]
    with pytest.raises(schema.SchemaError):
        store.save(doc, raw, "jpg", png_bytes())
    assert os.listdir(data_root) == []


def test_save_replaces_a_stale_original_extension(data_root):
    raw = b"same bytes, new extension"
    ident, doc = save_one(raw)
    store.save(doc, raw, "png", png_bytes())
    assert store.original_name(ident) == "original.png"
    assert not os.path.exists(data_root / ident / "original.jpg")


# --- atomicity ------------------------------------------------------------

def test_analysis_json_is_never_partially_readable(data_root):
    ident, doc = save_one()
    later = store.load(ident)
    later["title"] = "half written"
    later["warnings"] = ["x" * 5000]

    def boom(*args, **kw):
        raise RuntimeError("crash mid-write")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", boom)
        with pytest.raises(RuntimeError):
            store.save_doc(later)

    assert store.load(ident) == doc
    assert store.load(ident)["title"] == "IMG_4021.jpg"
    assert sorted(os.listdir(data_root / ident)) == [
        "analysis.json", "original.jpg", "processed.png", "thumb.jpg"]
    assert store.listing()[0]["title"] == "IMG_4021.jpg"


def test_save_doc_rewrites_only_the_json(data_root):
    ident, doc = save_one()
    before = (data_root / ident / "processed.png").read_bytes()
    doc["warnings"] = ["Bottom staff runs off the edge of the photo."]
    store.save_doc(doc)
    assert store.load(ident)["warnings"] == doc["warnings"]
    assert (data_root / ident / "processed.png").read_bytes() == before


def test_save_doc_needs_an_existing_analysis():
    doc = make_doc(b"never saved")
    with pytest.raises(KeyError):
        store.save_doc(doc)


# --- load -----------------------------------------------------------------

def test_load_missing_raises_keyerror():
    with pytest.raises(KeyError):
        store.load("0123456789ab")


def test_load_refuses_another_schema_version(data_root):
    ident, doc = save_one()
    doc["schema_version"] = 999
    (data_root / ident / "analysis.json").write_text(json.dumps(doc))
    with pytest.raises(schema.SchemaError):
        store.load(ident)


def test_load_of_broken_json_raises_schemaerror(data_root):
    ident, _ = save_one()
    (data_root / ident / "analysis.json").write_text("{not json")
    with pytest.raises(schema.SchemaError):
        store.load(ident)


# --- listing --------------------------------------------------------------

def test_listing_is_newest_first_with_summaries(data_root):
    old, _ = save_one(b"older", created_at="2026-01-01T09:00:00Z", title="Older")
    new, _ = save_one(b"newer", created_at="2026-06-01T09:00:00Z", title="Newer")

    entries = store.listing()
    assert [e["id"] for e in entries] == [new, old]

    entry = entries[0]
    assert entry["title"] == "Newer"
    assert entry["created_at"] == "2026-06-01T09:00:00Z"
    assert entry["thumb_url"].endswith("thumb.jpg") and new in entry["thumb_url"]
    assert entry["system_count"] == 1
    assert entry["event_count"] == len(CHORDS)
    assert entry["note_count"] == sum(len(c) for c in CHORDS)
    assert entry["verified"] is False
    assert entry["summary"] == "Bbmaj9 - Eb6/9 - Gm11"

    assert [e["id"] for e in store.listing(limit=1)] == [new]


def test_listing_reports_the_verified_flag(data_root):
    ident, doc = save_one()
    doc["engine"]["verified"] = True
    doc["engine"]["verifier"] = "claude-sonnet-5"
    store.save_doc(doc)
    assert store.listing()[0]["verified"] is True


def test_listing_survives_corrupt_and_half_written_directories(data_root):
    good, _ = save_one(b"the good one")

    broken = data_root / "aaaaaaaaaaaa"
    broken.mkdir()
    (broken / "analysis.json").write_text("{ truncated")

    wrong_version = data_root / "bbbbbbbbbbbb"
    wrong_version.mkdir()
    (wrong_version / "analysis.json").write_text('{"schema_version": 999}')

    mid_write = data_root / "cccccccccccc"
    mid_write.mkdir()
    (mid_write / "original.jpg").write_bytes(b"only the upload so far")

    (data_root / "not-an-id").mkdir()
    (data_root / "stray.txt").write_text("junk")

    entries = store.listing()
    assert [e["id"] for e in entries] == [good]


def test_listing_falls_back_to_mtime_for_a_bad_timestamp(data_root):
    ident, _ = save_one(created_at="whenever")
    entries = store.listing()
    assert [e["id"] for e in entries] == [ident]
    assert entries[0]["created_at"] == "whenever"


def test_listing_is_empty_when_nothing_is_stored():
    assert store.listing() == []


# --- delete and rename ----------------------------------------------------

def test_delete(data_root):
    ident, _ = save_one()
    assert store.delete(ident) is True
    assert store.exists(ident) is False
    assert os.listdir(data_root) == []
    assert store.delete(ident) is False


def test_rename(data_root):
    ident, _ = save_one()
    doc = store.rename(ident, "  Autumn Leaves  ")
    assert doc["title"] == "Autumn Leaves"
    assert store.load(ident)["title"] == "Autumn Leaves"
    assert store.listing()[0]["title"] == "Autumn Leaves"


def test_rename_rejects_an_empty_title(data_root):
    ident, _ = save_one()
    with pytest.raises(ValueError):
        store.rename(ident, "   ")
    assert store.load(ident)["title"] == "IMG_4021.jpg"


def test_rename_truncates_a_silly_title(data_root):
    ident, _ = save_one()
    doc = store.rename(ident, "z" * 500)
    assert len(doc["title"]) == store.MAX_TITLE


def test_rename_of_a_missing_analysis_raises_keyerror():
    with pytest.raises(KeyError):
        store.rename("0123456789ab", "nope")


# --- path traversal -------------------------------------------------------

@pytest.mark.parametrize("bad", BAD_IDS)
def test_bad_ids_are_rejected_before_touching_the_disk(bad, data_root):
    for call in (
        lambda: store.exists(bad),
        lambda: store.load(bad),
        lambda: store.delete(bad),
        lambda: store.rename(bad, "x"),
        lambda: store.path(bad, "analysis.json"),
        lambda: store.original_name(bad),
    ):
        with pytest.raises(ValueError):
            call()
    assert os.listdir(data_root) == []


@pytest.mark.parametrize("name", [
    "..", "../analysis.json", "sub/thumb.jpg", "/etc/passwd", "",
    "analysis.json/..", "thumb", ".hidden", "a" * 40 + ".png",
])
def test_bad_file_names_are_rejected(name):
    with pytest.raises(ValueError):
        store.path("0123456789ab", name)


@pytest.mark.parametrize("ext", ["../evil", "jp/g", "", ".", "toolongextension", None])
def test_bad_extensions_are_rejected_before_writing(ext, data_root):
    raw = b"a photo"
    doc = make_doc(raw)
    with pytest.raises(ValueError):
        store.save(doc, raw, ext, png_bytes())
    assert os.listdir(data_root) == []


def test_traversal_cannot_escape_the_data_dir(data_root, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    with pytest.raises(ValueError):
        store.path("../..", "secret.txt")
    assert outside.read_text() == "private"


# --- usage ----------------------------------------------------------------

def test_usage_counts_analyses_and_bytes(data_root):
    assert store.usage() == {"count": 0, "bytes": 0}

    raw = b"a photo" * 100
    png = png_bytes()
    doc = make_doc(raw)
    store.save(doc, raw, "jpg", png)
    store.save(make_doc(b"another"), b"another", "jpg", png_bytes(320, 240))

    stats = store.usage()
    assert stats["count"] == 2
    assert stats["bytes"] >= len(raw) + len(png)


def test_usage_ignores_half_written_directories(data_root):
    save_one()
    mid_write = data_root / "cccccccccccc"
    mid_write.mkdir()
    (mid_write / "original.jpg").write_bytes(b"x" * 10)
    stats = store.usage()
    assert stats["count"] == 1
    assert stats["bytes"] > 10


# --- first run ------------------------------------------------------------

def test_works_when_the_data_dir_does_not_exist_yet(tmp_path, monkeypatch):
    fresh = tmp_path / "brand-new" / "data"
    monkeypatch.setenv("SHEETER_DATA_DIR", str(fresh))
    assert store.listing() == []
    assert store.usage() == {"count": 0, "bytes": 0}

    ident, doc = save_one(b"the first ever upload")
    assert store.load(ident) == doc
    assert [e["id"] for e in store.listing()] == [ident]
    assert store.usage()["count"] == 1


# --- melody, no chords ----------------------------------------------------

def test_single_note_events_are_summarised_by_their_notes(data_root):
    """A melody line has no chord symbols, but the card still has to say something.
    Leaving it blank reads as a failed reading when the reading was fine."""
    ident, _ = save_one(b"a melody line", chords=[["C4"], ["D4"], ["E4"]])
    entry = store.listing()[0]
    assert entry["summary"] == "C4 - D4 - E4"
    assert entry["event_count"] == 3
    assert entry["note_count"] == 3


def test_a_long_filename_does_not_become_a_long_title(data_root):
    """The filename comes from the client and is the default title. Unclamped, a
    600 character name renders as a page-sized heading on the analysis page."""
    from sheeter import pipeline

    doc = make_doc(b"x", title="A" * 600 + ".jpg")
    assert len(doc["title"]) == 600 + 4      # the fixture builder does not clamp

    png = png_bytes(400, 300)
    built, _ = pipeline.analyze_bytes(png, "A" * 600 + ".png", "image/png")
    assert len(built["title"]) == store.MAX_TITLE


def test_a_blank_filename_still_gets_a_title(data_root):
    from sheeter import pipeline

    built, _ = pipeline.analyze_bytes(png_bytes(400, 300), "   ", "image/png")
    assert built["title"] == "photo"
