# Sheet Music Note Analyzer: Implementation Plan for Claude Code

**Goal:** A simple mobile web app. The user photographs a line (system) of printed sheet music with their phone camera, the app figures out the pitches in every chord/note, names the chords, and draws that analysis as an overlay on top of the original photo.

**Audience for this doc:** Claude Code. It documents (1) exactly how the pitches in the two sample images were worked out by hand in a Python sandbox, (2) which of those steps should become production code, (3) which existing OMR libraries to lean on instead of reinventing them, and (4) where and how Claude (the LLM) fits in the loop, with concrete prompts and JSON schemas.

The bias throughout is toward a pipeline that works reliably on real printed piano/lead-sheet music, not one that handles every edge case in music notation.

---

## 0. TL;DR of the recommended architecture

```
phone camera photo (one system of music)
        │
        ▼
[1] Preprocess (OpenCV): grayscale → deskew → binarize → optional dewarp
        │
        ▼
[2] Deterministic geometry (our code, ~200 lines):
    staff line detection → staff-space unit → pitch grid per staff
    staff-line removal → connected components → notehead candidates with (x, y)
        │
        ▼
[3] Symbolic OMR (homr, off the shelf): image → MusicXML
    Gives pitches + rhythm + accidentals + key sig per staff, in left-to-right order
        │
        ▼
[4] Merge: align homr's note sequence with our notehead coordinates (by staff, then x-order)
    → every note now has pitch AND pixel position
        │
        ▼
[5] music21: group simultaneous notes into chords, name them, compute intervals
        │
        ▼
[6] Claude (Sonnet 5) verification pass: image crop + our structured reading → corrected JSON
    Only stage where the image is sent to an LLM
        │
        ▼
[7] Overlay: draw note names + chord symbols at the merged (x, y) positions on a canvas
```

If you want the absolute simplest v0 that still works: skip [3] and [4], do [2] yourself, send the crop plus your geometry to Claude, and let Claude produce the pitch list. That is essentially what happened in the chat session that produced this doc. It works, but it is slower, costs tokens per image, and is less reliable than homr for rhythm. Both paths are described below.

---

## 1. Simplifying assumptions (make these product decisions, they buy a lot of reliability)

1. **One system at a time.** The user frames a single line of music (one staff or one grand staff pair). Full-page support is a later feature: detect systems, split, and run the same pipeline per system. Every downstream step gets easier with one system.
2. **Printed, engraved music only.** No handwritten manuscripts. Standard fonts (Bravura, Maestro, Opus, Petrucci, Finale/Sibelius/MuseScore output). This is what homr and the geometry below are tuned for.
3. **Treble and bass clefs only** for v1. Alto/tenor clefs can be added later (it is just a different pitch-mapping table).
4. **Pitch is the priority, rhythm is secondary.** The product promise is "what notes are these." Durations are nice-to-have and homr gives them mostly for free, but do not block on them.
5. **Reasonably flat, evenly lit photo.** Ask the user to fill the frame with the system and hold the phone parallel to the page. Provide a live guide rectangle in the camera UI. Do deskew, but do not try to fix heavy perspective warp or shadows in v1.
6. **Overlay = labels near noteheads, not a re-rendered score.** Draw text badges at note positions on top of the user's own photo. No need to render MusicXML back into notation.

---

## 2. What was actually done by hand (and why it worked)

This section is the ground truth of the approach. Everything here was done with `PIL`, `numpy`, and `scipy.ndimage` in a sandbox, on two images:

- Image A: a tiny 334×360 scan of two chords from a jazz voicing textbook (grand staff).
- Image B: a 2444×470 crop of one grand-staff system (two flats, dotted chords, beamed eighths, ties, a triplet run).

Both were read correctly down to the accidental. The steps, in order:

### 2.1 Load, inspect dimensions, upscale for human viewing

```python
from PIL import Image
im = Image.open(path)
print(im.size, im.mode)          # e.g. (2444, 470) RGBA
```

Upscaling (LANCZOS 3–6×) was only for eyeballing crops. The numeric analysis ran on the original pixels. Note that `RGBA` PNGs from phone/scan apps are common; convert with `.convert("L")` and make sure alpha is fully opaque (it was, `alpha.min() == 255`) or composite over white first.

### 2.2 Find staff lines with a row-darkness profile

Staff lines are the only thing in the image that is dark across (almost) the full width. A horizontal projection finds them instantly.

```python
import numpy as np
g = np.array(im.convert("L"))
dark = (g < 128).sum(axis=1)               # dark pixel count per row
candidates = [y for y, v in enumerate(dark) if v > 0.6 * g.shape[1]]
```

For Image B this returned rows `110, 131, 152, 173, 194` (treble) and `388, 409` (bass). **Only two bass lines showed up** because the other three were anti-aliased lighter than the 128 threshold. Two fixes, use both:

- Scan a column band that is known to be empty of symbols (e.g. a gap between chords) and look at the **row mean intensity** instead of a hard count. The faint lines were obvious there (`mean ≈ 140` vs `255`).
- Once you have any two adjacent lines, you know the spacing. Predict the other three and confirm them.

Group candidate rows into runs (adjacent rows = same line, take the mean), then cluster into groups of 5 with near-equal spacing. Each group of 5 is a staff.

```python
def group_lines(rows, max_gap=3):
    lines, cur = [], [rows[0]]
    for r in rows[1:]:
        if r - cur[-1] <= max_gap: cur.append(r)
        else: lines.append(np.mean(cur)); cur = [r]
    lines.append(np.mean(cur))
    return lines

def cluster_staves(lines, tol=0.25):
    staves = []
    i = 0
    while i + 4 < len(lines):
        spacings = np.diff(lines[i:i+5])
        if np.allclose(spacings, spacings.mean(), rtol=tol):
            staves.append(lines[i:i+5]); i += 5
        else:
            i += 1
    return staves
```

Result for Image B: treble lines at `[110, 131, 152, 173, 194]`, bass lines at `[367, 388, 409, 430, 451]`, **unit (staff space) = 21 px**, half-space = 10.5 px.

### 2.3 Build the pitch grid for each staff

Everything about pitch reduces to: *which staff position (line or space) is the notehead's vertical center closest to?* Positions are integer steps of half a staff space above/below the bottom line.

```python
# Diatonic step index: C=0 D=1 E=2 F=3 G=4 A=5 B=6, octave-aware "staff step" = octave*7 + index
# Bottom line reference:  treble bottom line = E4 (step 4*7+2 = 30), bass bottom line = G2 (step 2*7+4 = 18)
CLEF_BOTTOM_LINE_STEP = {"treble": 30, "bass": 18, "alto": 3*7+3, "tenor": 3*7+1}
STEP_NAMES = "CDEFGAB"

def y_to_step(y, staff_lines, clef):
    """staff_lines: 5 y-values top→bottom. Returns diatonic step (int) and residual error in px."""
    unit = (staff_lines[-1] - staff_lines[0]) / 4.0
    half = unit / 2.0
    bottom = staff_lines[-1]
    pos = (bottom - y) / half              # 0 = bottom line, 1 = space above, 2 = 2nd line ... negative = below
    k = int(round(pos))
    return CLEF_BOTTOM_LINE_STEP[clef] + k, abs(pos - k) * half

def step_to_name(step):
    return f"{STEP_NAMES[step % 7]}{step // 7}"   # e.g. 33 -> 'A4'
```

Sanity values for Image B (unit 21):

| Treble y | position | note | Bass y | position | note |
|---|---|---|---|---|---|
| 89 | ledger 1 above | A5 | 314.5 | space above 2nd ledger | F4 |
| 99.5 | space above top | G5 | 325 | ledger 2 above | E4 |
| 110 | top line | F5 | 346 | ledger 1 above | C4 |
| 141.5 | 3rd space | C5 | 356.5 | space above top | B3 |
| 194 | bottom line | E4 | 367 | top line | A3 |
| 215 | ledger 1 below | C4 | 398.5 | 2nd space | E3 |
| 236 | ledger 2 below | A3 | 451 | bottom line | G2 |
| 246.5 | space below ledger 2 | G3 | 461.5 | space below bottom | F2 |

Ledger lines confirm positions: they are short horizontal runs (wider than a notehead, ~1.6–1.8× notehead width) at exactly `bottom + k*unit` for k ≥ 1 or `top - k*unit`. Their presence is a strong check that a low/high notehead is where you think it is. In Image A, the ledger at the A3 position *above* a notehead (not through it) is what proved the note was G3, not A3.

### 2.4 Remove staff lines and find symbol blobs

Vertical erosion kills anything thinner than N pixels vertically. Staff lines are 2–3 px thick; noteheads, stems, accidentals, clefs are all thicker.

```python
from scipy import ndimage
from scipy.ndimage import binary_erosion, binary_dilation

b = g < 150                                       # binarize (threshold ~150 worked for anti-aliased PNGs)
no_staff = binary_erosion(b, structure=np.ones((7, 1)))   # kernel height ≈ 2.5× line thickness
lab, n = ndimage.label(no_staff)
for sl in ndimage.find_objects(lab):
    ys, xs = sl
    h, w = ys.stop - ys.start, xs.stop - xs.start
    # noteheads: h ≈ 0.9–1.1 unit, w ≈ 1.2–1.4 unit (filled) or same bbox but hollow
    # stems: w ≤ 3 px, h ≥ 2.5 unit
    # accidentals: h ≈ 2.5–3 unit, w ≈ 0.4–0.6 unit
```

**Gotchas hit in practice:**

- Dilating before labeling (to reconnect broken glyphs) merged a whole beamed group into one 7,500-pixel component. Do not dilate before labeling. Label first, then merge by rules.
- Hollow noteheads (half/whole notes) split into a left half and a right half after erosion. Recognize the pair: two blobs, same y-range, ~1 unit apart horizontally, combined width ≈ 1.3 unit. Or skip erosion for hollow detection and use contour hole-count instead (OpenCV `RETR_CCOMP`: a notehead with a child contour is hollow).
- Stacked seconds are drawn side by side: the lower note of a second is shifted one notehead-width to the left (Image B, bar 1: D5 with C5 displaced left; bar 2: C5 with B♭4 displaced left). Two blobs with overlapping y-ranges offset by ~1.2 unit horizontally are one chord, not two events.
- Filled noteheads in a tight chord (thirds) fuse into one tall blob. A blob that is ~2× unit tall with no waist is two noteheads; its total height tells you how many. Split at `k * unit` intervals from the top.
- A 5-note stack of thirds (Image B, the Gmaj9) fused into a single 105-px blob. The row-width profile had visible "waists" every 21 px at the seams between noteheads. Use the waist minima to split.

### 2.5 The ASCII dump trick (the single most useful debugging tool)

For any region you are unsure about, print it as characters. This is how every ambiguous case in the session was resolved, and it is worth keeping as a debug endpoint in the app.

```python
def dump(b, x0, x1, y0, y1):
    for y in range(y0, y1):
        print(y, "".join("#" if v else "." for v in b[y, x0:x1]))
```

Example: the sharp+natural in Image B, bar 2. The dump made it obvious there was one natural (x 1711–1724, y 195–241) and one sharp (x 1739–1756, y 156–216), and let the crossbar rows be read off directly.

### 2.6 Accidentals: which note do they belong to?

An accidental applies to the notehead at the same vertical position, to its right. The vertical reference point differs per glyph:

| Glyph | Height (units) | Reference y | How to find it |
|---|---|---|---|
| ♯ sharp | ~2.8 | midpoint between the two thick crossbars | rows where blob width jumps to full glyph width |
| ♮ natural | ~2.7 | midpoint between the two crossbars | same |
| ♭ flat | ~2.5 | center of the bowl (the round part at the bottom) | rows where width is max in lower half |

Both bounding-box center and crossbar midpoint were computed; the crossbar midpoint was more accurate (bbox center was 7 px off for the natural in Image B, crossbar midpoint was within 1 px of B3). When several accidentals precede one chord they are staggered horizontally; the one furthest left generally belongs to a lower note, but always match by y, not by x-order.

**Key signature:** accidentals immediately after the clef (and before the first notehead) are the key signature. Count them and apply to every matching letter name in the system unless a natural cancels it. Image B had 2 flats → every B and E is flat unless marked. Missing this would have turned B♭maj9 into Bmaj9.

**Accidental scope:** in strict notation an accidental lasts until the end of the bar. For a "what are these notes" tool, applying it to the marked note only (plus same-pitch repeats in the same bar) is fine for v1.

### 2.7 Dots, stems, ties: use them as confirmation, not as primary signal

- **Augmentation dots** sit in the space of the note (or the space above if the note is on a line), ~0.8 unit to the right of the notehead. In Image B, matching 4 dots to 4 noteheads by y confirmed the top note was A5 on a ledger line (dot at B5 space).
- **Stem side** tells you which noteheads belong together: one stem, many noteheads = one chord.
- **Ties/slurs** are thin curves (after erosion they mostly vanish). If the same pitch set appears twice with a tie between them, the second event is a continuation, not a new chord. Useful for de-duplicating the overlay.

### 2.8 From pitches to a chord name (the "musical reasoning" part)

Once each event had its pitch list, naming was done by inspection: B♭-D-F-A-C → B♭maj9; E♭-G-B♭-C-F → E♭6/9; G-B♭-C-F → Gm11; G-B-D-F♯-A → Gmaj9. This is exactly what `music21.chord.Chord(...).pitchedCommonName` does, and what Claude can do well from a pitch list without ever seeing the image. See §5.

### 2.9 What the by-hand process could NOT do reliably (this is why homr + Claude are in the plan)

- Rhythm was read only roughly (dotted vs. plain, eighth vs. half). Beaming, tuplets, and rests were not parsed.
- Clef changes mid-line (Image B had a small treble clef appearing in the bass staff) were noticed visually, not detected programmatically.
- Anything cropped at the image edge (the triplet run at the bottom of Image B) was unrecoverable. The app must detect a cut-off staff and tell the user to retake.
- Every ambiguous blob needed a human (well, an LLM) to look at the ASCII dump and decide. That judgment step is what the Claude verification pass replaces.

---

## 3. Library survey: use homr, keep our geometry for coordinates

Investigated Sept 2026. Do not reinvent symbol recognition; the open-source state of the art is good enough for printed music and installs with one command.

### 3.1 homr (recommended primary OMR engine)

- PyPI `homr` 0.7.0 (released June 2026), AGPL-3.0, Python 3.11–3.15. `pip install homr` or `uvx homr <img>`. Runs CPU-only (slow-ish, tens of seconds) or with CUDA 12.1.
- Purpose-built for **camera pictures of sheet music → MusicXML**. This is our exact input.
- Two-stage pipeline: UNet segmentation (staff lines, noteheads, stems, rests, barlines, clefs, key sigs) inherited from oemer, then a transformer (Polyphonic-TrOMR) per dewarped staff that emits a symbol sequence with pitch, accidentals, rhythm, tuplets, slurs, articulations.
- Handles grand staffs (brace/bracket detection), multiple voices, perspective (per-staff dewarp), non-uniform staff spacing.
- **Limitation that matters to us:** the transformer output is a *sequence* with no pixel coordinates. The project notes that attention centers can approximate focus points, but there is no public API for per-note bounding boxes. **This is why stage [2] (our geometry) stays in the pipeline: it supplies the (x, y) for the overlay, and homr supplies the pitch/rhythm truth. Merge them.**
- Also stated: focuses on pitch and rhythm for treble/bass clef; neglects dynamics, double sharps/flats, and other symbols. Fine for us.
- Has an Android app built on it (Andromr), which is evidence it works on phone photos.

Install and run:

```bash
pip install homr            # or: uvx homr photo.jpg
homr photo.jpg              # writes photo.musicxml next to the input
```

Programmatic use: import the module and call its main entry with the image path, or shell out. Check the repo for the current function name; treat the CLI as the stable contract and the `.musicxml` file as the interface between homr and the rest of the app.

### 3.2 oemer (homr's predecessor, fallback)

- `pip install oemer`. Also image → MusicXML. Older, uses SVMs for symbol classification after segmentation, 3–5 minutes per image on GPU. homr's author is the same lineage and explicitly recommends homr over oemer. Keep as a fallback only if homr fails to install.

### 3.3 Audiveris (Java, most mature, heaviest)

- Desktop-grade OMR, excellent on clean scans, has a headless batch mode. Java dependency makes it awkward for a small web backend. Skip for v1; revisit if homr accuracy is not enough on scans.

### 3.4 music21 (symbolic analysis, mandatory)

- `pip install music21`. Parse MusicXML → iterate measures → get notes/chords with pitches, accidentals, offsets, key signature. `Chord.pitchedCommonName`, `Chord.root()`, `Chord.quality`, `interval.Interval(n1, n2).niceName`. Also renders to MusicXML for optional re-display.

### 3.5 OpenCV (preprocessing)

- Grayscale, `cv2.adaptiveThreshold` (Gaussian, block ~31, C ~10) for uneven phone lighting, `cv2.HoughLinesP` or the projection-profile method for skew angle, `cv2.warpAffine` to deskew. Contour analysis with `RETR_CCOMP` to tell hollow from filled noteheads.

### 3.6 Frontend rendering (optional)

- OpenSheetMusicDisplay (OSMD) or Verovio can render MusicXML in the browser if you ever want a "clean" view next to the photo. Not needed for the overlay-on-photo MVP.

### 3.7 Claude (LLM in the loop)

- Vision is supported on all current models. Use `claude-sonnet-5` for the verification pass (best speed/intelligence trade-off, ~seconds per image). Escalate to `claude-opus-5` only for low-confidence systems. `claude-haiku-4-5-20251001` is fine for the text-only chord-naming/explanation call. Model IDs, image size limits, and pricing: https://docs.claude.com/en/docs/build-with-claude/vision and https://docs.claude.com/en/docs/about-claude/models. Verify limits in the docs at build time rather than hardcoding from memory.

---

## 4. The production pipeline, stage by stage

### Stage 1: Capture and preprocess (client + server)

Client (mobile web): `<input type="file" accept="image/*" capture="environment">` or `getUserMedia` with a guide rectangle overlay. Downscale to max 2500 px on the long side before upload (homr and our geometry both work at ~15–25 px staff spacing; a phone photo of one system at 2500 px wide gives ~20 px spacing). Send JPEG quality 90.

Server:

```python
import cv2, numpy as np

def preprocess(img_bgr):
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # 1. deskew: estimate angle from staff lines via projection sharpness search
    best_angle, best_score = 0, -1
    for a in np.arange(-5, 5.01, 0.25):
        M = cv2.getRotationMatrix2D((g.shape[1]/2, g.shape[0]/2), a, 1)
        r = cv2.warpAffine(g, M, (g.shape[1], g.shape[0]), borderValue=255)
        score = np.var((r < 128).sum(axis=1))      # sharp staff lines -> high row-variance
        if score > best_score: best_angle, best_score = a, score
    M = cv2.getRotationMatrix2D((g.shape[1]/2, g.shape[0]/2), best_angle, 1)
    g = cv2.warpAffine(g, M, (g.shape[1], g.shape[0]), borderValue=255)
    # 2. binarize for uneven lighting
    b = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
    return g, b < 128       # grayscale (for homr/Claude) and boolean mask (for geometry)
```

Reject early if: fewer than 5 staff lines found, staff spacing < 12 px (too far away, ask to move closer), or a staff touches the image border (cropped, ask to retake).

### Stage 2: Geometry (our code)

Implement `staff_geometry.py` from §2.2–2.4:

```python
@dataclass
class Staff:
    lines: list[float]          # 5 y-values, top→bottom
    unit: float                 # staff space in px
    clef: str | None            # filled in later from homr or Claude
    x_range: tuple[int, int]

@dataclass
class NoteheadCandidate:
    x: float; y: float          # center
    w: float; h: float
    hollow: bool
    staff_idx: int
    step: int                   # diatonic step from y_to_step, once clef known
    step_err_px: float          # how far off-grid; > 0.3*unit means suspicious

def detect_staves(mask) -> list[Staff]: ...
def detect_noteheads(mask, staves) -> list[NoteheadCandidate]: ...
def detect_ledger_lines(mask, staves) -> list[tuple[x0, x1, y]]: ...
def group_into_events(noteheads, unit) -> list[list[NoteheadCandidate]]:
    """Cluster by x within ~1.5 units, allowing for displaced seconds; one cluster per staff = one event."""
```

Output: a list of **events** per staff (each event = one or more noteheads at the same time), each with pixel positions. No pitch letters yet unless you already know the clef and key signature; those come from homr (Stage 3) or Claude (Stage 6).

### Stage 3: homr → MusicXML

```python
import subprocess, tempfile, shutil
def run_homr(gray_img_path: str) -> str:
    subprocess.run(["homr", gray_img_path], check=True, timeout=180)
    return gray_img_path.rsplit(".", 1)[0] + ".musicxml"
```

Run it on the deskewed grayscale image (not the binary mask; homr does its own preprocessing). Cache by image hash. Expect 10–60 s on CPU; run it as a background job and stream status to the client.

### Stage 4: Merge homr's notes with our coordinates

Parse the MusicXML with music21, walk each part (part 0 = top staff = usually treble, part 1 = bass in a grand staff), and produce an ordered list of events per staff: `[{offset, pitches: [...], duration}]`. Then align with Stage 2's per-staff event list, which is also ordered left to right.

Alignment is a sequence-matching problem with the same length most of the time. Use a simple dynamic-programming alignment (Needleman-Wunsch with cost = |#noteheads_homr − #noteheads_ours| + penalty for skips). When lengths match exactly, it degenerates to zip. Within an aligned event, sort both pitch lists by height (homr: by MIDI number; ours: by y descending) and pair them. Attach `(x, y)` to each pitch.

Disagreements to record (they feed the Claude prompt): event counts differ, notehead counts within an event differ, or our `step` (from geometry + homr's clef + key sig) disagrees with homr's pitch letter.

### Stage 5: Musical analysis (music21, no LLM)

```python
from music21 import chord, pitch, interval
def analyze_event(pitch_names: list[str]) -> dict:
    c = chord.Chord(pitch_names)                # e.g. ['Bb3','F4','C5','D5','F5','A5']
    return {
        "pitches": [p.nameWithOctave for p in sorted(c.pitches)],
        "root": c.root().name,
        "quality": c.quality,
        "common_name": c.pitchedCommonName,     # 'Bb-major ninth chord' style
        "intervals_from_bass": [interval.Interval(c.bass(), p).name for p in c.pitches],
    }
```

For voice-leading annotations between consecutive events (like the "minor 2nd" label in Image A), compute the interval between the top notes and between the bass notes of adjacent events. music21's common names are verbose and academic; the Claude text call in Stage 6b turns them into the jazz-style symbols a player expects (B♭maj9, E♭6/9, Gm11).

### Stage 6a: Claude vision verification (the only stage that sees the image)

Purpose: catch the errors the deterministic path is known to make (fused noteheads, accidental attribution, missed key signature, clef changes, hollow vs. filled), using an actual reading of the picture as the tiebreaker. Do **not** ask Claude to read pitches from a raw photo with no help; LLMs are poor at counting staff positions from pixels alone. Give it the structured reading and ask it to confirm or correct.

What to send, per staff or per system:

1. The deskewed grayscale crop of the system, plus, if the system is wider than ~1600 px, additional zoomed crops of 3–4 events at a time (Claude reads dense chords far better at 2–3× zoom; the sandbox session did exactly this).
2. A JSON block: staves with their clef, key signature, unit size; each event with x-range and the candidate pitch list from Stage 4; a list of flagged disagreements.
3. Optionally, an annotated copy of the crop with thin colored boxes and event numbers drawn on it so Claude can refer to "event 3" unambiguously. This helps a lot.

Use the Messages API with an `image` content block (base64 or URL) followed by the text. Request JSON output; either use structured outputs / a tool with a strict schema, or instruct "respond with only JSON" and parse.

System prompt:

```
You are an expert music engraver and jazz pianist verifying an automated optical music recognition result.
You will receive one system of printed sheet music as an image, plus a JSON reading of that system produced by a
deterministic pipeline. Your job is to check the reading against the image and correct it.

Rules:
- Work event by event, in left-to-right order. Count noteheads in the image for each event and compare.
- Pitch = vertical position on the staff. Use the clef, ledger lines, and the staff-space unit in the JSON to place
  every notehead; do not guess from the shape of the chord.
- Apply the key signature to every note unless an accidental on that note overrides it. An accidental's vertical
  reference is the midpoint of its crossbars (sharp, natural) or the center of its bowl (flat).
- Notes a second apart are drawn side by side; the left-displaced notehead belongs to the same chord.
- Hollow noteheads are half or whole notes; filled are quarters or shorter. Report what you see.
- If a clef change or new key signature appears mid-system, report it and re-read everything to its right.
- If part of a staff is cut off by the image edge, say so; do not invent notes for it.
- Only change a pitch if you are confident the image contradicts the reading. Give a confidence 0-1 per event.
- Respond with JSON only, matching the provided schema. No prose.
```

User message structure:

```
[image: system crop]
[image: zoom crop 1 (events 1-3)]   ...as needed
Here is the pipeline's reading of this system:
{ "staves": [...], "events": [...], "flags": [...] }
Return the corrected reading.
```

Response schema (also use this as the app's canonical data model):

```json
{
  "staves": [
    {"index": 0, "clef": "treble", "key_signature_fifths": -2, "cut_off": false}
  ],
  "events": [
    {
      "index": 0,
      "staff": 0,
      "x_range": [197, 232],
      "notes": [
        {"pitch": "A5", "accidental": null, "hollow": false, "y": 89, "changed": false},
        {"pitch": "F5", "accidental": null, "hollow": false, "y": 110, "changed": false}
      ],
      "duration_hint": "dotted",
      "confidence": 0.95,
      "notes_on_correction": "Top note is A5 on the first ledger line; dot placement confirms."
    }
  ],
  "system_notes": "Key signature is two flats; applied to B and E throughout."
}
```

Model: `claude-sonnet-5`. Temperature 0. Budget ~1 call per system, plus one retry with `claude-opus-5` if any event has confidence < 0.6 or the response fails schema validation. This keeps cost and latency bounded: the photo goes to an LLM exactly once in the happy path.

### Stage 6b: Claude text call for chord symbols and explanation (no image)

Cheap, fast, optional per event or batched per system. Input is the verified pitch lists; output is a player-friendly symbol and a one-line note. `claude-haiku-4-5-20251001` is enough.

```
For each event, given its pitches bottom to top and the key signature, return:
- symbol: a concise jazz/pop chord symbol (e.g. "Bbmaj9", "Eb6/9", "Gm11", "F7(b9,13)"). Prefer the reading a
  jazz pianist would write on a chart. If the notes are a single melodic line, return the note name instead.
- role: optional Roman numeral or function relative to the key signature's major key (e.g. "I", "IV", "vi").
- note: at most one sentence of insight, e.g. voice-leading between this and the previous event, or an
  enharmonic remark (F# functioning as Gb, the b9).
Respond with JSON: [{"index": 0, "symbol": "...", "role": "...", "note": "..."}].
Events: {...}
```

Batch all events of a system into one call. Also ask for the interval between consecutive top notes and consecutive bass notes if you want voice-leading arrows like Image A's "minor 2nd" label.

### Stage 7: Overlay rendering (client)

The client already has the original photo. The server returns the deskew transform (rotation angle, center) and the final event JSON with `(x, y)` in deskewed-image coordinates. Client draws on a `<canvas>` layered over the photo:

- Invert the deskew rotation to map each `(x, y)` back into original photo coordinates (or just display the deskewed image; simpler, and users do not care).
- For each note: a small pill with the pitch name (`A5`, `B♭3`) placed just right of the notehead at its `y`. Alternate left/right placement for stacked notes to avoid collisions; with unit ≈ 20 px there is room for ~11 px text if you offset every other label.
- For each event: the chord symbol above the treble staff (or below the bass staff for single-staff bass parts), centered on the event's x-range. Confidence < 0.7 gets a dashed outline; tapping it shows Claude's `notes_on_correction`.
- Toggle: names only / symbols only / both. Pinch-zoom the canvas + image together.

---

## 5. The simple v0 path (no homr): geometry + Claude does the reading

If getting homr installed is a blocker (AGPL, CPU time, dependency weight), this still gives a working demo and is close to what the chat session did:

1. Stages 1, 2 as above. You get staves, unit, notehead candidates with `(x, y)`, hollow flag, ledger line positions, accidental candidates with their reference y, and events grouped by x.
2. Compute a **provisional** step for each notehead assuming clef by staff order (top = treble, bottom = bass) and no key signature.
3. Send Claude (Sonnet 5) the crop, the zoomed crops, and this provisional JSON with the same system prompt as 6a, plus one extra instruction: *"The clef and key signature have not been read. First identify the clef and key signature for each staff from the image, then correct every pitch accordingly."* Ask it to also fill in `duration_hint` from hollow/filled and dots.
4. Stages 5, 6b, 7 unchanged.

Why this works: the hard, error-prone part for an LLM is precise vertical localization, and the geometry hands it that for free. The easy part for an LLM is reading a clef, counting flats, deciding whether a blob is a sharp or a natural, and knowing that G-B-D-F♯-A is Gmaj9. You are dividing the labor along the line where each side is strong.

Why it is not the long-term answer: no rhythm beyond hollow/filled/dotted, more tokens per image (crops are big), and every reading relies on one LLM pass instead of two independent readers agreeing. Move to the homr path once the UI is proven.

---

## 6. Full-page support later (design now, build later)

- Detect systems by finding all 5-line groups (§2.2) and clustering by vertical gap. A gap larger than ~4 units between staff groups separates systems; a brace/bracket at the left edge or a smaller gap (~2–3 units) binds staves into one grand staff.
- Crop each system with ~3 units of padding top and bottom (ledger lines, chord symbols) and run the per-system pipeline unchanged. Parallelize homr across systems.
- Keep coordinates in page space so the overlay stacks correctly.
- Claude call count = number of systems. A typical piano page has 4–6 systems. Cache aggressively (hash of the system crop).

---

## 7. Test corpus and acceptance criteria

Build a small labeled set before writing the UI. Fifteen to twenty images covering:

- Both sample images from the session (expected outputs are in §8).
- MuseScore/Lilypond-rendered PNGs of a few public-domain piano excerpts (clean baseline).
- Phone photos of the same excerpts printed on paper, at 3 distances and 2 lighting conditions.
- Cases: key signatures with 0, 2, 4 sharps/flats; chords with seconds; 4–6 note stacks; ledger lines 2 deep; a natural cancelling a key-sig flat; hollow and filled noteheads in the same event; a triplet bracket; a mid-line clef change.

Metrics: per-notehead pitch accuracy (target ≥ 97% on rendered, ≥ 93% on phone photos), event count accuracy, and chord symbol match rate against a human label (allow enharmonic equivalents). Log every Claude correction; if Claude is flipping > 10% of geometry pitches, the geometry has a bug, fix that rather than leaning harder on the LLM.

---

## 8. Reference: expected outputs for the two sample images

Use these as fixtures.

**Image A** (334×360, no key signature, grand staff, two chords, label "minor 2nd" between top voices)

| Event | Bass staff | Treble staff | Symbol |
|---|---|---|---|
| 1 | F2, E♭3 | A3, D4, F♯4 | F7(♭9,13) with F♯ read as G♭ |
| 2 | B♭2, D♭3 | G3, C4, F4 | B♭m6/9 |

Voice leading: every upper voice descends by step; top voice G♭4→F4 is the labeled minor 2nd; bass F2→B♭2 (V→i in B♭ minor).

**Image B** (2444×470, key signature 2 flats, grand staff, one system)

| Event | Bass staff | Treble staff | Symbol | Notes |
|---|---|---|---|---|
| 1 | B♭3, F4 (dotted) | C5, D5, F5, A5 (dotted) | B♭maj9 | C5 displaced left of D5 (second) |
| 2 | E♭3, B♭3 | G4, C5, F5 | E♭6/9 | eighth tied into hollow half notes (event 3 is a continuation) |
| 4 | G2, F3 (dotted) | F4, B♭4, C5 | Gm11 | B♭4 displaced left of C5; first of a 4-eighth beam |
| 5 | | D5 | | melody eighth |
| 6 | | C5 | | melody eighth |
| 7 | (16th triplet run, partly cropped: ?, B♮2, E♭3 / F♯3, G3, clef→treble) | G3, B♮3, D4, F♯4, A4 | Gmaj9 | natural on B3 cancels key sig; sharp on F4; tied into 5 whole notes at right |

Staff geometry for Image B: treble lines y = 110, 131, 152, 173, 194; bass lines y = 367, 388, 409, 430, 451; unit 21 px. First-chord noteheads: treble centers y = 89, 110, 131, 141.5 at x ≈ 197–232 (C5 at x ≈ 178–202); bass centers y = 315.5, 357 at x ≈ 201–229; ledger lines at y = 88.5 (treble) and 325, 346 (bass).

---

## 9. Appendix: the complete by-hand analysis code, cleaned up

Everything below ran as-is in the session (Python 3, numpy, scipy, Pillow). It is a starting point for `staff_geometry.py`, not the final module.

```python
from dataclasses import dataclass
import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.ndimage import binary_erosion

STEP_NAMES = "CDEFGAB"
CLEF_BOTTOM_LINE_STEP = {"treble": 4*7+2, "bass": 2*7+4}   # E4, G2

def load_mask(path, thresh=150):
    im = Image.open(path).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    g = np.array(Image.alpha_composite(bg, im).convert("L"))
    return g, g < thresh

def staff_line_rows(g, x0=None, x1=None, frac=0.6):
    """Rows that are dark across most of the chosen column band. Use a symbol-free band when possible."""
    band = g[:, x0:x1] if x0 is not None else g
    dark = (band < 200).sum(axis=1)
    return [y for y, v in enumerate(dark) if v > frac * band.shape[1]]

def group_rows(rows, max_gap=3):
    out, cur = [], [rows[0]]
    for r in rows[1:]:
        if r - cur[-1] <= max_gap: cur.append(r)
        else: out.append(float(np.mean(cur))); cur = [r]
    out.append(float(np.mean(cur)))
    return out

def cluster_staves(lines, rtol=0.25):
    staves, i = [], 0
    while i + 4 < len(lines):
        sp = np.diff(lines[i:i+5])
        if np.allclose(sp, sp.mean(), rtol=rtol):
            staves.append(lines[i:i+5]); i += 5
        else:
            i += 1
    return staves

def y_to_step(y, lines, clef):
    unit = (lines[-1] - lines[0]) / 4.0
    pos = (lines[-1] - y) / (unit / 2.0)
    k = int(round(pos))
    return CLEF_BOTTOM_LINE_STEP[clef] + k, abs(pos - k) * unit / 2.0

def step_to_name(step):
    return f"{STEP_NAMES[step % 7]}{step // 7}"

def remove_staff_lines(mask, kernel_h=7):
    return binary_erosion(mask, structure=np.ones((kernel_h, 1)))

def components(mask_no_staff, min_size=25):
    lab, n = ndimage.label(mask_no_staff)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None: continue
        ys, xs = sl
        pts = (lab[sl] == i)
        if pts.sum() < min_size: continue
        yy, xx = np.where(pts)
        out.append(dict(x0=xs.start, x1=xs.stop, y0=ys.start, y1=ys.stop,
                        cx=xs.start + xx.mean(), cy=ys.start + yy.mean(), size=int(pts.sum())))
    return out

def classify(comp, unit):
    h, w = comp["y1"] - comp["y0"], comp["x1"] - comp["x0"]
    if w <= 3 and h >= 2.5 * unit:            return "stem_or_barline"
    if 0.7*unit <= h <= 1.2*unit and 1.0*unit <= w <= 1.6*unit: return "notehead"
    if 0.7*unit <= h <= 1.2*unit and 0.3*unit <= w <= 0.6*unit: return "hollow_notehead_half"   # left/right half after erosion
    if 2.2*unit <= h <= 3.2*unit and 0.3*unit <= w <= 0.8*unit: return "accidental"
    if h > 1.2*unit and 1.0*unit <= w <= 1.6*unit:              return "fused_noteheads"        # split at unit intervals
    return "other"

def split_fused(comp, unit):
    """A tall single-width blob is k stacked noteheads; return their centers."""
    k = int(round((comp["y1"] - comp["y0"]) / unit))
    top = comp["y0"] + unit / 2
    return [top + i * unit for i in range(max(k, 1))]

def dump(mask, x0, x1, y0, y1):
    for y in range(y0, y1):
        print(y, "".join("#" if v else "." for v in mask[y, x0:x1]))

if __name__ == "__main__":
    g, mask = load_mask("photo.png")
    lines = cluster_staves(group_rows(staff_line_rows(g)))
    for i, st in enumerate(lines):
        print("staff", i, st, "unit", (st[-1]-st[0])/4)
    unit = (lines[0][-1] - lines[0][0]) / 4
    comps = components(remove_staff_lines(mask))
    for c in comps:
        kind = classify(c, unit)
        if kind in ("notehead", "fused_noteheads"):
            centers = [c["cy"]] if kind == "notehead" else split_fused(c, unit)
            for cy in centers:
                # pick nearest staff by y
                st = min(lines, key=lambda L: abs(np.mean(L) - cy))
                clef = "treble" if st is lines[0] else "bass"
                step, err = y_to_step(cy, st, clef)
                print(f"x={c['cx']:.0f} y={cy:.1f} -> {step_to_name(step)} (err {err:.1f}px)")
```

Key thresholds that worked on the samples: binarize at 150, staff-line detection at 60% row coverage, erosion kernel 7 px tall for 2–3 px lines at unit ≈ 21 (scale it as `round(unit/3)`), notehead height 0.7–1.2 unit, accidental height 2.2–3.2 unit.
