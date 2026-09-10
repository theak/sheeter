"""The rows the fix panel is drawn from.

One row per step, each carrying what a reader needs to correct that chord: the note
names, the staff position each notehead sits on, which accidental is in force, and the
pitch menu for its staff.  Lifted out of ``templates/analysis.html`` so that the page and
a single re-rendered panel are built from the same rows: an edit re-renders only the
panels whose row changed, and comparing rows is how the routes decide which those are.

Presentation, not reading.  Nothing here measures anything or touches the photo; it is a
pure function of a stored document, which is what makes it comparable and testable.
"""

import re

from . import pipeline, pitches

HAND_LABEL = {"right": "Right hand", "left": "Left hand"}

#: Low to high, left to right: flat lowers the note, natural leaves it where the key put
#: it, sharp raises it.  So the row reads in the direction it moves the pitch, and natural
#: is in the middle because it is the one between the other two rather than a third option
#: after them.
ACCIDENTAL_BUTTONS = (("flat", "♭"), ("natural", "♮"), ("sharp", "♯"))

#: alter to the button that is in force.  The state a reader wants marked is what the
#: note sounds now, not only what is printed in front of it: a note sharpened by the key
#: signature shows sharp, because that is what it is.
ALTER_BUTTON = {-1: "flat", 0: "natural", 1: "sharp"}


def fix_steps(doc):
    """One row per step, in the order the steps are numbered."""
    rows = []
    for system in doc["systems"]:
        # One pitch menu per staff, not per notehead: the options are the same for every
        # note on it, and a page of dense chords would otherwise build the list many times.
        menus = {}
        for staff in system["staves"]:
            menus[staff["index"]] = pipeline.pitch_choices(staff)
        for event in system["events"]:
            hands = []
            for staff in system["staves"]:
                part = None
                for candidate in event["parts"]:
                    if candidate["staff"] == staff["index"]:
                        part = candidate
                slack = (staff["unit"] or 20.0) / 8.0
                notes = []
                for position in range(len(part["notes"]) if part else 0):
                    note = part["notes"][position]
                    corrected = False
                    for edit in doc["edits"]:
                        if (edit["system"] == system["index"]
                                and edit["event"] == event["index"]
                                and edit["staff"] == staff["index"]
                                and abs(edit["y"] - note["y"]) <= slack):
                            corrected = True
                    step, _err = pitches.y_to_step(note["y"], staff["lines"],
                                                   staff["clef"])
                    notes.append({
                        "position": position,
                        # "G# 4" not "G#4", the same spacing overlay.js uses on the labels.
                        "pretty": re.sub(r"(\d+)$", r" \1", note["pretty"]),
                        "step": step,
                        "midi": note["midi"],
                        "current": ALTER_BUTTON.get(note["alter"], ""),
                        "by_hand": corrected,
                    })
                notes.reverse()          # high to low, the way a chord is read off the page
                hands.append({
                    "staff": staff["index"],
                    "label": HAND_LABEL.get(staff["hand"]) or (
                        str(staff["clef"]).capitalize() + " staff"),
                    "notes": notes,
                    "choices": menus[staff["index"]],
                    # The middle of the staff, which is the least surprising place for a
                    # notehead somebody is adding from scratch to start.
                    "add_default": pitches.CLEF_BOTTOM_LINE_STEP[staff["clef"]] + 4,
                })
            combined = event["combined"] or {}
            rows.append({
                "number": len(rows) + 1,
                "system": system["index"],
                "event": event["index"],
                "symbol": combined.get("symbol") or "",
                "common_name": combined.get("common_name") or "",
                "note": event["note"] or "",
                "hands": hands,
            })
    return rows


def changed_panels(before, after):
    """The step numbers whose row moved, given the rows from before and after an edit.

    Positional, and that is safe rather than lucky: a panel's markup is a pure function of
    its row, and anything that changes how many steps there are reloads the page instead
    of coming through here, so the two lists are the same length and the same steps.
    """
    return [row["number"] for row, was in zip(after, before) if row != was]
