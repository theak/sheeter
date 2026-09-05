"""Tests for the optional Claude vision pass.

No network and no API key: every call goes through a fake client object.
"""

import base64
import copy
import io
import json

import pytest
from PIL import Image

from sheeter import naming, pitches, schema, verify


# --------------------------------------------------------------------------
# fixtures


def _staff(index, hand, clef, top):
    lines = [float(top + 20 * i) for i in range(5)]
    return {
        "index": index, "hand": hand, "clef": clef, "clef_confidence": 0.8,
        "lines": lines, "unit": 20.0, "x_range": [50.0, 750.0],
        "key_fifths": 0, "key_confidence": 0.5, "cut_off": False,
    }


def _note(staff, name, x):
    step, alter = pitches.parse_name(name)
    return pitches.make_note(
        step, alter, x, pitches.step_to_y(step, staff["lines"], staff["clef"]),
        w=10.0, h=8.0, hollow=False,
        ledger=pitches.ledger_count(step, staff["clef"]),
    )


def _event(index, x, parts):
    event = {
        "index": index, "x": float(x), "x_range": [x - 20.0, x + 20.0],
        "confidence": 0.7, "note": None, "parts": parts, "combined": None,
    }
    pooled = sorted((n["midi"], n["name"]) for p in parts for n in p["notes"])
    event["combined"] = naming.name_combined([n for _m, n in pooled], 0)
    return event


def _part(staff, names, x):
    notes = sorted((_note(staff, n, x) for n in names), key=lambda n: n["midi"])
    return {
        "staff": staff["index"], "hand": staff["hand"], "duration_hint": "quarter",
        "dotted": False, "notes": notes,
        "chord": naming.name_chord([n["name"] for n in notes], 0),
    }


def _system(index, top):
    right = _staff(0, "right", "treble", top)
    left = _staff(1, "left", "bass", top + 200)
    events = [
        _event(0, 200, [_part(right, ["E4", "G4", "C5"], 200),
                        _part(left, ["C3"], 200)]),
        _event(1, 400, [_part(right, ["F4", "A4"], 400),
                        _part(left, ["F2"], 400)]),
        # One pitch class in both hands, so combined and chord are both null.
        _event(2, 600, [_part(right, ["F4"], 600),
                        _part(left, ["F3"], 600)]),
    ]
    return {
        "index": index, "y_range": [float(top - 40), float(top + 320)],
        "x_range": [50.0, 750.0], "grand": True, "cut_off": False,
        "staves": [right, left], "events": events,
    }


def make_doc(systems=1):
    doc = schema.new_analysis(
        "abc123def456", "2026-09-05T20:31:00Z", "test.jpg",
        {"filename": "test.jpg", "content_type": "image/jpeg", "bytes": 1000,
         "width": 800, "height": 1000},
        {"width": 800, "height": 1000, "scale": 1.0, "deskew_deg": 0.0},
    )
    doc["systems"] = [_system(i, 100 + 400 * i) for i in range(systems)]
    return schema.validate_analysis(doc)


def make_png(width, height):
    buf = io.BytesIO()
    Image.new("L", (width, height), 255).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def doc():
    return make_doc()


@pytest.fixture
def png():
    return make_png(800, 1000)


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SHEETER_VERIFY_MODEL", raising=False)
    monkeypatch.delenv("SHEETER_SYMBOL_MODEL", raising=False)


class Block:
    def __init__(self, name, payload):
        self.type = "tool_use"
        self.name = name
        self.input = payload


class Response:
    def __init__(self, blocks, stop_reason="tool_use"):
        self.content = blocks
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.messages = FakeMessages(response, error)


def correction(systems, system_notes=""):
    return {"systems": systems, "system_notes": system_notes}


def pitch_names(doc, system=0, event=0, staff=0):
    part = next(p for p in doc["systems"][system]["events"][event]["parts"]
                if p["staff"] == staff)
    return [n["name"] for n in part["notes"]]


# --------------------------------------------------------------------------
# availability


def test_available_false_without_key():
    assert verify.available() is False


def test_available_true_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    assert verify.available() is True


def test_verify_analysis_without_key_keeps_the_reading(doc, png):
    before = copy.deepcopy(doc)
    out = verify.verify_analysis(doc, png)
    assert doc == before
    assert out["engine"]["verified"] is False
    assert out["engine"]["verifier"] is None
    assert "ANTHROPIC_API_KEY" in out["engine"]["verifier_error"]
    assert out["systems"] == before["systems"]
    schema.validate_analysis(out)


def test_name_events_without_key_is_a_noop(doc):
    assert verify.name_events(doc) is doc


# --------------------------------------------------------------------------
# build_request


def test_build_request_blocks(doc, png):
    request = verify.build_request(doc, png)
    content = request["messages"][0]["content"]
    assert request["messages"][0]["role"] == "user"
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[-1]["type"] == "text"
    assert "E4" in content[-1]["text"]
    assert request["tools"][0]["name"] == verify.CORRECTION_TOOL
    assert request["tools"][0]["strict"] is True
    assert request["tool_choice"] == {"type": "tool", "name": verify.CORRECTION_TOOL}
    assert request["max_tokens"] > 1000
    # Sonnet 5 rejects sampling parameters, so none may be sent.
    assert "temperature" not in request
    assert "thinking" not in request


def test_build_request_slim_view_hides_the_chords(doc, png):
    text = verify.build_request(doc, png)["messages"][0]["content"][-1]["text"]
    body = json.loads(text[text.index("{"):text.rindex("}") + 1])
    staff = body["systems"][0]["staves"][0]
    assert set(staff) == {"index", "hand", "clef", "key_fifths", "unit", "lines"}
    event = body["systems"][0]["events"][0]
    assert set(event) == {"index", "x_range", "parts"}
    assert set(event["parts"][0]["notes"][0]) == {"name", "y"}


def test_narrow_page_sends_one_image(doc, png):
    content = verify.build_request(doc, png)["messages"][0]["content"]
    assert sum(1 for b in content if b["type"] == "image") == 1


def test_wide_page_sends_crops_and_honours_the_cap():
    doc = make_doc(systems=8)
    content = verify.build_request(doc, make_png(2000, 4000))["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert 1 < len(images) <= verify.MAX_IMAGE_BLOCKS


def test_images_are_downscaled_to_the_tier_limit(doc):
    content = verify.build_request(doc, make_png(4000, 3000))["messages"][0]["content"]
    for block in content:
        if block["type"] != "image":
            continue
        image = Image.open(io.BytesIO(base64.standard_b64decode(
            block["source"]["data"])))
        assert max(image.size) <= verify.MAX_IMAGE_PX


# --------------------------------------------------------------------------
# apply_correction


def test_apply_correction_changes_a_pitch(doc):
    before = copy.deepcopy(doc)
    y = doc["systems"][0]["events"][0]["parts"][0]["notes"][2]["y"]
    out = verify.apply_correction(doc, correction([
        {"index": 0, "staves": [], "events": [
            {"index": 0, "confidence": 0.95, "note": "Top note is C#5.",
             "parts": [{"staff": 0, "notes": ["E4", "G4", "C#5"]}]},
        ]},
    ]))
    assert doc == before
    notes = out["systems"][0]["events"][0]["parts"][0]["notes"]
    assert [n["name"] for n in notes] == ["E4", "G4", "C#5"]
    assert notes[2]["changed"] is True
    assert notes[2]["alter"] == 1
    assert notes[2]["midi"] == 73
    assert notes[2]["pretty"] == "C♯5"
    assert notes[2]["y"] == y
    assert notes[0]["changed"] is False
    assert out["systems"][0]["events"][0]["confidence"] == 0.95
    assert out["systems"][0]["events"][0]["note"] == "Top note is C#5."
    schema.validate_analysis(out)


def test_apply_correction_renames_the_chord(doc):
    out = verify.apply_correction(doc, correction([
        {"index": 0, "staves": [], "events": [
            {"index": 0, "confidence": 0.9, "note": "",
             "parts": [{"staff": 0, "notes": ["E4", "G4", "B4"]}]},
        ]},
    ]))
    assert out["systems"][0]["events"][0]["parts"][0]["chord"]["symbol"] == "Em"


def test_untouched_events_keep_their_symbols(doc):
    before = copy.deepcopy(doc)
    out = verify.apply_correction(doc, correction([
        {"index": 0, "staves": [], "events": [
            {"index": 0, "confidence": 0.9, "note": "",
             "parts": [{"staff": 0, "notes": ["E4", "G4", "B4"]}]},
        ]},
    ]))
    assert out["systems"][0]["events"][0]["combined"] != \
        before["systems"][0]["events"][0]["combined"]
    for index in (1, 2):
        assert out["systems"][0]["events"][index] == \
            before["systems"][0]["events"][index]


def test_a_top_down_answer_lands_on_the_right_noteheads(doc):
    staff = doc["systems"][0]["staves"][0]
    out = verify.apply_correction(doc, correction([
        {"index": 0, "staves": [], "events": [
            {"index": 0, "confidence": 0.9, "note": "",
             "parts": [{"staff": 0, "notes": ["D5", "G4", "F4"]}]},
        ]},
    ]))
    notes = out["systems"][0]["events"][0]["parts"][0]["notes"]
    assert [n["name"] for n in notes] == ["F4", "G4", "D5"]
    assert [n["midi"] for n in notes] == sorted(n["midi"] for n in notes)
    # The y values are geometry's and must not have been shuffled with them.
    assert [n["y"] for n in notes] == [
        pitches.step_to_y(pitches.parse_name(n)[0], staff["lines"], staff["clef"])
        for n in ["E4", "G4", "C5"]
    ]
    schema.validate_analysis(out)


def test_new_clef_rereads_every_note_on_the_staff(doc):
    out = verify.apply_correction(doc, correction([
        {"index": 0, "staves": [
            {"index": 0, "clef": "bass", "key_fifths": 0, "cut_off": False}],
         "events": []},
    ]))
    # Same pixel positions, read a sixth lower: E4 G4 C5 becomes G2 B2 E3.
    assert pitch_names(out) == ["G2", "B2", "E3"]
    assert pitch_names(out, event=1) == ["A2", "C3"]
    assert all(n["changed"] for n in out["systems"][0]["events"][0]["parts"][0]["notes"])
    assert pitch_names(out, staff=1) == ["C3"]
    schema.validate_analysis(out)


def test_new_key_signature_alters_untouched_notes(doc):
    out = verify.apply_correction(doc, correction([
        {"index": 0, "staves": [
            {"index": 0, "clef": "treble", "key_fifths": -3, "cut_off": False}],
         "events": []},
    ]))
    assert pitch_names(out) == ["Eb4", "G4", "C5"]
    assert pitch_names(out, event=1) == ["F4", "Ab4"]
    note = out["systems"][0]["events"][1]["parts"][0]["notes"][1]
    assert note["from_key"] is True
    assert note["changed"] is True


def test_note_count_change_reanchors_the_overlay(doc):
    staff = doc["systems"][0]["staves"][0]
    out = verify.apply_correction(doc, correction([
        {"index": 0, "staves": [], "events": [
            {"index": 1, "confidence": 0.8, "note": "",
             "parts": [{"staff": 0, "notes": ["F4", "A4", "C5", "F5"]}]},
        ]},
    ]))
    notes = out["systems"][0]["events"][1]["parts"][0]["notes"]
    assert [n["name"] for n in notes] == ["F4", "A4", "C5", "F5"]
    for note in notes:
        step, _alter = pitches.parse_name(note["name"])
        assert note["y"] == pitches.step_to_y(step, staff["lines"], staff["clef"])
        assert note["changed"] is True
    schema.validate_analysis(out)


def test_system_notes_become_a_warning(doc):
    out = verify.apply_correction(
        doc, correction([], "Key signature is two flats.")
    )
    assert "Key signature is two flats." in out["warnings"]


BAD_PAYLOADS = [
    "not a dict",
    None,
    {"systems": "not a list", "system_notes": ""},
    correction("not a list"),
    correction([{"index": 99, "staves": [], "events": []}]),
    correction([{"index": "nope", "staves": [], "events": []}]),
    correction([{"index": 0, "staves": [{"index": 0, "clef": "bogus",
                                        "key_fifths": 99, "cut_off": "yes"}],
                 "events": []}]),
    correction([{"index": 0, "staves": [], "events": [
        {"index": 0, "confidence": 5, "note": 7,
         "parts": [{"staff": 0, "notes": ["H4", "zzz", "C5"]}]}]}]),
    correction([{"index": 0, "staves": [], "events": [
        {"index": 0, "confidence": 0.5, "note": "",
         "parts": [{"staff": 0, "notes": ["C4", "junk", "E9999", "G4"]}]}]}]),
    correction([{"index": 0, "staves": [], "events": [
        {"index": 0, "confidence": 0.5, "note": "",
         "parts": [{"staff": 7, "notes": ["C4"]}]}]}]),
    correction([{"index": 0, "staves": [], "events": [
        {"index": 0, "confidence": 0.5, "note": "", "parts": [{"staff": 0,
                                                               "notes": []}]}]}]),
    correction([{"index": 0, "staves": [], "events": [
        {"index": 0, "confidence": 0.5, "note": "",
         "parts": [{"staff": 0, "notes": [1, 2, 3]}]}]}]),
]


@pytest.mark.parametrize("payload", BAD_PAYLOADS)
def test_malformed_payloads_do_not_corrupt_the_document(doc, payload):
    before = copy.deepcopy(doc)
    out = verify.apply_correction(doc, payload)
    assert doc == before
    schema.validate_analysis(out)
    assert pitch_names(out) == ["E4", "G4", "C5"]
    assert out["systems"][0]["staves"][0]["clef"] == "treble"
    assert out["systems"][0]["staves"][0]["key_fifths"] == 0
    assert all(not n["changed"] for _s, _e, _p, n in schema.iter_notes(out))


def test_a_merge_that_would_not_validate_returns_the_original(doc, monkeypatch):
    def broken(_doc, _payload):
        out = copy.deepcopy(_doc)
        out["systems"][0]["staves"][0]["key_fifths"] = 99
        return out

    monkeypatch.setattr(verify, "_merge", broken)
    assert verify.apply_correction(doc, correction([])) is doc


# --------------------------------------------------------------------------
# verify_analysis with a fake client


def test_verify_analysis_applies_the_correction(doc, png):
    client = FakeClient(Response([Block(verify.CORRECTION_TOOL, correction([
        {"index": 0, "staves": [], "events": [
            {"index": 0, "confidence": 0.99, "note": "",
             "parts": [{"staff": 0, "notes": ["E4", "G4", "B4"]}]}]},
    ]))]))
    out = verify.verify_analysis(doc, png, client=client, model="claude-sonnet-5")
    assert out["engine"]["verified"] is True
    assert out["engine"]["verifier"] == "claude-sonnet-5"
    assert out["engine"]["verifier_error"] is None
    assert pitch_names(out) == ["E4", "G4", "B4"]
    assert client.messages.calls[0]["model"] == "claude-sonnet-5"
    schema.validate_analysis(out)


def test_verify_analysis_reads_dict_blocks(doc, png):
    client = FakeClient({"stop_reason": "tool_use", "content": [
        {"type": "text", "text": "ignored"},
        {"type": "tool_use", "name": verify.CORRECTION_TOOL,
         "input": correction([])},
    ]})
    out = verify.verify_analysis(doc, png, client=client)
    assert out["engine"]["verified"] is True


def test_verify_analysis_uses_the_environment_model(doc, png, monkeypatch):
    monkeypatch.setenv("SHEETER_VERIFY_MODEL", "claude-opus-5")
    client = FakeClient(Response([Block(verify.CORRECTION_TOOL, correction([]))]))
    out = verify.verify_analysis(doc, png, client=client)
    assert client.messages.calls[0]["model"] == "claude-opus-5"
    assert out["engine"]["verifier"] == "claude-opus-5"


def test_client_exception_becomes_a_verifier_error(doc, png):
    before = copy.deepcopy(doc)
    client = FakeClient(error=RuntimeError("connection reset by peer"))
    out = verify.verify_analysis(doc, png, client=client)
    assert doc == before
    assert out["engine"]["verified"] is False
    assert out["engine"]["verifier"] is None
    assert "connection reset by peer" in out["engine"]["verifier_error"]
    assert out["systems"] == before["systems"]
    schema.validate_analysis(out)


def test_a_refusal_is_an_error_not_a_correction(doc, png):
    client = FakeClient(Response([Block(verify.CORRECTION_TOOL, correction([]))],
                                 stop_reason="refusal"))
    out = verify.verify_analysis(doc, png, client=client)
    assert out["engine"]["verified"] is False
    assert "corrected_reading" in out["engine"]["verifier_error"]


def test_a_response_without_the_tool_is_an_error(doc, png):
    client = FakeClient(Response([{"type": "text", "text": "sorry"}]))
    out = verify.verify_analysis(doc, png, client=client)
    assert out["engine"]["verified"] is False
    assert out["engine"]["verifier_error"]


def test_a_bad_correction_leaves_the_document_alone(doc, png, monkeypatch):
    def broken(_doc, _payload):
        out = copy.deepcopy(_doc)
        out["systems"][0]["events"][0]["parts"][0]["notes"] = []
        return out

    monkeypatch.setattr(verify, "_merge", broken)
    client = FakeClient(Response([Block(verify.CORRECTION_TOOL, correction([]))]))
    out = verify.verify_analysis(doc, png, client=client)
    assert out["engine"]["verified"] is False
    assert "schema" in out["engine"]["verifier_error"]
    assert pitch_names(out) == ["E4", "G4", "C5"]


# --------------------------------------------------------------------------
# name_events


def test_name_events_upgrades_symbols_and_notes(doc):
    client = FakeClient(Response([Block(verify.SYMBOL_TOOL, {"events": [
        {"system": 0, "index": 0, "symbol": "Cmaj7/E", "role": "I",
         "note": "The bass moves down a fifth into the next chord."},
    ]})]))
    before = copy.deepcopy(doc)
    out = verify.name_events(doc, client=client)
    assert doc == before
    event = out["systems"][0]["events"][0]
    assert event["combined"]["symbol"] == "Cmaj7/E"
    assert event["combined"]["roman"] == "I"
    assert event["note"].startswith("The bass moves")
    assert client.messages.calls[0]["model"] == verify.DEFAULT_SYMBOL_MODEL
    assert client.messages.calls[0]["temperature"] == 0
    schema.validate_analysis(out)


def test_name_events_survives_a_broken_response(doc):
    client = FakeClient(Response([Block(verify.SYMBOL_TOOL, {"events": [
        {"system": 9, "index": 0, "symbol": "X"},
        "not a dict",
        {"system": 0, "index": 0, "symbol": 12, "role": None, "note": ""},
    ]})]))
    before = copy.deepcopy(doc)
    out = verify.name_events(doc, client=client)
    assert doc == before
    assert out["systems"][0]["events"][0]["combined"]["symbol"] == \
        before["systems"][0]["events"][0]["combined"]["symbol"]
    schema.validate_analysis(out)


def test_name_events_never_raises(doc):
    client = FakeClient(error=ValueError("no"))
    assert verify.name_events(doc, client=client) is doc


def test_name_events_leaves_an_unnameable_event_alone(doc):
    assert doc["systems"][0]["events"][2]["combined"] is None
    client = FakeClient(Response([Block(verify.SYMBOL_TOOL, {"events": [
        {"system": 0, "index": 2, "symbol": "F", "role": "IV",
         "note": "Both hands land on F."},
    ]})]))
    out = verify.name_events(doc, client=client)
    assert out["systems"][0]["events"][2]["combined"] is None
    assert out["systems"][0]["events"][2]["note"] == "Both hands land on F."
    schema.validate_analysis(out)
