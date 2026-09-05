# Sheeter analysis JSON (schema version 1)

One analysis == one uploaded photo. Stored at `data/<id>/analysis.json`.

Every pixel coordinate in this document is in **processed-image space**: the
deskewed, downscaled grayscale PNG saved as `data/<id>/processed.png`, whose
dimensions are `image.width` x `image.height`. The overlay is drawn on that
image, so no inverse transform is ever needed. Positions are converted to
percentages client-side (`x / image.width * 100`) so the overlay survives
arbitrary mobile scaling.

```jsonc
{
  "schema_version": 1,
  "id": "8f3a1c2d9b4e",              // 12 hex chars, sha256 of the original bytes
  "created_at": "2026-09-05T20:31:00Z",
  "title": "IMG_4021.jpg",           // user-editable label, defaults to filename

  "source": {
    "filename": "IMG_4021.jpg",
    "content_type": "image/jpeg",
    "bytes": 2874113,
    "width": 3024, "height": 4032     // original pixel dims (post EXIF rotation)
  },

  "image": {
    "width": 2000, "height": 2666,    // processed.png dims
    "scale": 0.6614,                  // processed = original * scale (after deskew)
    "deskew_deg": -1.25               // rotation applied, degrees, ccw positive
  },

  "engine": {
    "geometry": "1",                  // geometry module version
    "verifier": "claude-sonnet-5",    // model id, or null if the vision pass did not run
    "verified": true,                 // a verifier response was applied.  On a system the
                                      // geometry was sure about that may be commentary only
    "verifier_error": null            // human-readable reason the pass was skipped/failed
  },

  "warnings": [                       // shown to the user above the image
    "Bottom staff runs off the edge of the photo - retake with more margin."
  ],

  "systems": [                        // left-to-right, top-to-bottom on the page
    {
      "index": 0,
      "y_range": [310, 1180],
      "x_range": [80, 1900],
      "grand": true,                  // two staves braced as one piano system
      "cut_off": false,               // touches an image border

      "staves": [
        {
          "index": 0,                 // index within this system, top to bottom
          "hand": "right",            // "right" | "left" | null (null = not a grand staff)
          "clef": "treble",           // validator accepts treble|bass|alto|tenor; the
                                      // geometry only ever emits treble or bass
          "clef_confidence": 0.92,
          "lines": [336.5, 395.1, 453.8, 512.4, 571.0],   // 5 y values, top to bottom
          "unit": 58.6,               // staff space in px = (lines[4]-lines[0]) / 4
          "x_range": [80, 1900],
          "key_fifths": -2,           // -7..+7, negative = flats
          "key_confidence": 0.85,
          "cut_off": false
        }
      ],

      "events": [                     // simultaneities, left to right
        {
          "index": 0,
          "x": 512.0,
          "x_range": [480, 560],
          "confidence": 0.86,
          "note": null,               // one short sentence from the verifier, or null

          "parts": [                  // one entry per staff that has notes at this x
            {
              "staff": 0,
              "hand": "right",
              "duration_hint": "half", // "whole"|"half"|"quarter"|"eighth"|"shorter"|null
              "dotted": false,
              "notes": [               // ALWAYS sorted low pitch -> high pitch
                {
                  "step": "B",         // letter A-G, no alteration
                  "alter": -1,         // -2..2 semitone alteration
                  "octave": 4,         // scientific pitch notation octave
                  "name": "Bb4",       // ascii, music21-parseable via B-4 conversion
                  "pretty": "B♭4",// unicode, for display
                  "midi": 70,
                  "x": 512.0, "y": 336.5,
                  "w": 34.0, "h": 26.0,
                  "hollow": true,
                  "accidental": "flat",// explicit glyph found next to this notehead:
                                       // flat|sharp|natural|double-flat|double-sharp|null.
                                       // The geometry reads the first three
                  "from_key": false,   // alteration came from the key signature
                  "ledger": 1,         // ledger lines used: +n above staff, -n below, 0 none
                  "step_err_px": 2.1,  // distance off the pitch grid; > 0.3*unit is suspect
                  "confidence": 0.9,
                  "changed": false     // the verifier changed this pitch
                }
              ],
              "chord": {               // null when the part has a single note
                "symbol": "Bbmaj9",
                "common_name": "Bb-major-ninth chord",
                "root": "Bb",
                "bass": "Bb",
                "quality": "major",
                "intervals_from_bass": ["P1", "M3", "P5", "M7", "M9"]
              }
            }
          ],

          "combined": {                // both hands pooled; this is the headline chord
            "names": ["Bb3", "F4", "C5", "D5", "F5", "A5"],
            "pretty": ["B♭3", "F4", "C5", "D5", "F5", "A5"],
            "symbol": "Bbmaj9",
            "common_name": "Bb-major-ninth chord",
            "root": "Bb",
            "bass": "Bb",
            "quality": "major",
            "roman": "I",              // numeral in the key_fifths major key, or null.
                                       // The naming pass may replace it with its own
                                       // short label, so treat it as text
            // music21's ordering of the chord's own tones, not parallel to "names"
            "intervals_from_bass": ["P1", "P5", "M9", "M3", "P5", "M7"]
          }
        }
      ]
    }
  ]
}
```

## Rules that are part of the contract

1. `notes` inside a part are **always** sorted ascending by midi. Callers may rely on it.
2. `name` is ASCII: letter, then `#`/`b` repeated `|alter|` times, then octave. `pretty`
   uses U+266F / U+266D / U+266E. Convert to music21 with `name.replace("b", "-")` on the
   accidental portion only - use `sheeter.pitches.to_music21()`, never do it by hand.
3. `hand` is the user-facing vocabulary. Never surface `staff index` or `treble/bass` as the
   primary label in the UI; a grand staff is right hand (top) and left hand (bottom).
   A lone staff gets `hand: null` and the UI falls back to the clef name.
4. Every field above is always present. Unknown values are `null`, never missing keys.
   `sheeter.schema.validate_analysis()` enforces this and is run on every write.
5. Bumping `schema_version` means stored analyses are re-rendered from whatever they have;
   `store.load()` refuses to serve an analysis whose version it does not understand.
6. The vision pass only overrules a system the geometry was unsure of.
   `verify.geometry_is_sure()` is false when the system is `cut_off`, when a staff has
   `clef_confidence` or `key_confidence` under 0.9, or when any note has `confidence`
   under 0.88. On a sure system the clef, `key_fifths`, `parts` and `combined` are
   restored from the geometry after the merge, so `changed` stays false there and only
   `event.note` and the appended page-level line in `warnings` come from the model.
