# Sheeter: point your phone at sheet music and see what the notes are

Take a photo of a line of sheet music and Sheeter draws the note names on top of the photo,
step by step, left hand and right hand, with a chord name where there is one.

It is for people who know what a G minor 7 is but cannot read notation fast enough to find one
on the page. If you sight-read fluently you do not need this.

### How the reading works

Everything below is plain numpy and scipy. No model, no network, same answer every time.

1. **Prepare.** Decode, honour EXIF rotation, downscale so the long side is 2200px, find the
skew angle by rotating a small copy until the row-darkness profile spikes hardest, and threshold
adaptively so a lighting ramp across the page does not eat half the ink.
2. **Scale.** The commonest vertical black run on a page of music is the thickness of a staff
line and the commonest vertical white run is the gap between two of them, so the staff space
falls out of two histograms. Every threshold after this is written in staff spaces, so the
reading does not care what resolution the photo is.
3. **Staves.** Take every row that carries long horizontal ink as a hypothetical top line,
sweep the line spacing, predict the other four lines and keep the guess only if all five
predicted rows are inked across the same span. A photo
has page margins, so a staff almost never spans the frame and asking which rows are dark all the
way across does not work.
4. **Noteheads.** Correlate a notehead-shaped outline against the image with the staff lines
erased, and keep the peaks. It is a small bank of outlines rather than one, because a whole note
is a different shape: wider, rounder and upright instead of leaning. Matching an outline rather
than classifying connected components is what makes dense music work, since a stack of thirds is
one blob but three separate peaks, and a hollow notehead nicked by staff-line removal is still
one peak.
5. **Pitch.** A notehead's vertical position against the five lines is a diatonic step, which
becomes a pitch once you know the clef (read from the height of the glyph at the left edge) and
the key signature (the run of accidentals right after it). Accidentals written in the bar are
applied to the notes after them, ledger lines are confirmed before a note outside the staff is
believed, and stems group noteheads into chords.
6. **Naming.** Every pitch class present is tried as the root of the chord, each reading is
charged for what makes it unlikely, and the cheapest wins. See below.

The only runtime dependencies are numpy, scipy, Pillow, pillow-heif, bottle and waitress.
Two libraries were in there and are not any more: music21, for one field, and the anthropic SDK,
which `sheeter/claude.py` replaces with about a hundred lines of `urllib`. See below for both.

### The Claude pass

There is an optional second pass that sends the processed image, zoomed crops of each system, and
the current reading to Claude, and asks for a corrected reading back. It does not run on every
upload. `SHEETER_VERIFY` is `manual` by default, which puts a "Verify with Claude" button on the
analysis page; `auto` runs it on upload, `off` hides it. It needs `ANTHROPIC_API_KEY` set and
nothing else installed, and it adds five to twenty-five seconds.

What comes back is also merged narrowly. A system is only overruled where the geometry admitted
doubt: the system is cut off by the frame, or a clef or key signature was read with confidence
under 0.9, or a notehead came in under 0.88. Everywhere else the pitches, clef and key go back to
what the geometry measured and only the model's commentary is kept.

That is not hedging, it is what the measurement said. On the fixture corpus at the time the two
were compared, the geometry read 157 of 157 noteheads and the vision pass read 151. All six of
its losses were notes on ledger lines, where counting staff positions by eye is exactly what a
language model is worst at. So the model arbitrates where the geometry is unsure and comments
where it is not.

### How accurate it is

The corpus is 15 cases, 273 noteheads, built by `tools/make_fixtures.py`: a music21 score with
known pitches, engraved through Verovio, plus a synthesized phone-camera version of the same
page. The pipeline reads 273/273 on the clean renders and 273/273 on the photos, with no spurious
noteheads and the right number of events in both.

Three more suites sit alongside it, each written because the corpus could not say anything about
what it covers:

- `tests/test_fonts.py` renders the same music in all five music fonts Verovio ships, Petaluma's
handwritten-style face included, and expects the same answer from each. It reads all five
exactly. This is the one that says the reader has not simply memorised one engraver.
- `tests/test_page.py` reads a ten bar page across several systems, which is what someone
photographing a book actually points the camera at.
- `tests/test_annotations.py` reads a jazz-textbook style page: whole note voicings under chord
symbols with three lines of analysis printed between the staves. None of the text becomes a note.

Read the numbers with their limits attached:

- The photos are synthesized, not real. They are the clean render rotated, keystoned, lit with a
diagonal ramp, blurred, noised and saved as JPEG. That covers a lot of what a phone does to a
page, but it is not a camera.
- Every image is machine engraved, from one renderer, even across the five fonts.
- So real photographs of real print will be harder, and the number to expect from them is not
this one. If you want to know how it does on your music, try it on your music.

Below about 18 pixels between staff lines the reading starts to miss notes, and the app says so
on the page rather than quietly handing you a worse answer.

To reproduce:

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python tools/make_fixtures.py
.venv/bin/python -m pytest tests/test_accuracy.py
```

### On chord names

The symbol is worked out here, not looked up. Every pitch class present is tried as the root,
each reading is charged for what makes it unlikely (no third, a tension that only makes sense
over a dominant, a rare quality), and the cheapest wins, with a discount for the bass note. That
is what makes the same five notes read as Cm11 over C and Eb6/9 over Eb.

music21 used to supply the plain-English description beside the symbol and no longer does. Asked
about the chords above it answers "C-major pentatonic" for E-flat 6/9, "G-quartal tetramirror"
for G minor 11 and "D-major-minor-diminished pentachord" for F7(b9,13). Those are set-theory
names, they are no help to someone who cannot read notation, and printing one next to a symbol
that disagrees with it is worse than printing nothing. The description is now the symbol said in
words, so the two always agree, and music21 and matplotlib behind it, about 45% of the installed
footprint, are out of what the app needs to run. They are still in `requirements-dev.txt`,
because the fixtures are built with music21.

An earlier version also asked Claude to improve the symbols, following the plan. Given the
pitches of four voicings, both model tiers tried named a chord containing a note that is not on
the page in three of them. That pass was removed; `tests/test_naming.py` records the cases.

### Known limits

Things it genuinely cannot do, as opposed to does badly:

- **Handwritten music.** Every shape it looks for is an engraved one.
- **Rests.** They are not detected at all. A bar of rests in the left hand simply reads as no
notes there, with nothing marking the silence.
- **Rhythm.** A notehead gets `whole`, `half` or `quarter-or-shorter` from its shape and its
stem, plus a dot if there is one. It stops there because telling a quarter from an eighth means
finding the flag or the beam, and across the corpus the ink beside a stem tip is 0.00 to 0.12 of
the box for a flag and 0.00 to 0.17 for a plain quarter: they do not separate, so any threshold
labels some notes wrongly. There are no ties, no tuplets, and nothing that adds up to a bar.
- **Two voices in one staff.** Noteheads sharing an x position in a staff become one chord, so
independent voices collapse into each other.
- **A clef change mid-staff.** The clef is read once, from the left edge of each staff, and holds
for the whole line.
- **Music cropped at the frame edge.** It notices (`cut_off`, and a warning on the page) but it
cannot recover what is not in the photo.

It also expects one staff or one grand staff per system, treble and bass clefs, and a page
photographed within about 6 degrees of level. A system of three or more staves, an organ score
or a song with a piano part, is still read but gets no left and right hand labels. Treat the
whole thing as a reading aid and check it against the page.

### Option 1: Run as docker image

```
docker run -d -p 5000:5000 -v sheeter-data:/data --restart unless-stopped akshaykannan/sheeter
```

Add `-e ANTHROPIC_API_KEY=sk-ant-...` if you want the Claude pass. The volume is worth keeping:
that is where your previous analyses live.

Or with docker compose:
```yaml
services:
  sheeter:
    image: akshaykannan/sheeter
    ports:
      - "5000:5000"
    volumes:
      - sheeter-data:/data
    environment:
      # optional, enables the Claude verify button
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    restart: unless-stopped

volumes:
  sheeter-data:
```

### Option 2: Run directly

1. Clone the repo: ```git clone https://github.com/theak/sheeter```
2. Make a virtual env: ```python -m venv .venv```
3. Install the deps: ```.venv/bin/pip install -r requirements.txt```
4. Start it: ```tools/serve.sh```

### Usage

Open http://localhost:5000 on your phone, take or pick a photo of one line of music, and wait a
second or two. You get the photo back with a label on each notehead and the chord spelled out
under each step. Everything you have analysed before is on the home page, so you can jump back to
it.

Four ways in: the camera button, a file from the device, drag and drop, or paste. Pasting is
usually the fastest on a desktop, since a screenshot of a PDF or a scan goes straight from the
clipboard to a reading with no file to save first. A pasted image is titled by the moment you
pasted it, because the clipboard does not carry a useful name.

Readings are titled by whatever the file was called, which is rarely what you want to see in a
list a week later. Every card in the gallery has a "Rename or delete" row: rename it to something
you will recognise, or throw it away. Both are also on the reading itself. Deleting is permanent
and says so before it does it, and the whole thing is a details element and two forms, so it works
with JavaScript off.

Uploads are content addressed by sha256, so re-uploading or re-pasting the same image returns the
analysis you already have instead of computing it twice.

### Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `SHEETER_DATA_DIR` | `<repo>/data`, and `/data` in the docker image | Where analyses are stored |
| `SHEETER_VERIFY` | `manual` | `manual` puts the verify button on the analysis page, `auto` runs the pass on every upload, `off` hides it. Anything else behaves like `manual` |
| `ANTHROPIC_API_KEY` | unset | Required for the Claude pass. Without it there is no button and no pass, whatever `SHEETER_VERIFY` says |
| `SHEETER_VERIFY_MODEL` | `claude-sonnet-5` | Model for the vision pass |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Point the Claude pass at a gateway or a proxy |
| `PORT` | `5000` | Port to listen on, both for `tools/serve.sh` and in the docker image |

### Where things are stored

One directory per analysis, named after the first 12 hex characters of the sha256 of the upload:

```
<data dir>/<id>/original.<ext>   the bytes exactly as uploaded
<data dir>/<id>/processed.png    the deskewed grayscale image the overlay sits on
<data dir>/<id>/thumb.jpg        preview for the home page
<data dir>/<id>/analysis.json    the reading itself, schema in docs/schema.md
```

There is no database and no index file. The home page is built by scanning directories, and
`analysis.json` is written last, so a half-written analysis is simply skipped. Deleting an
analysis is `rm -rf` on its directory. The JSON is also served at `/a/<id>/analysis.json` if you
want to do something else with it.

### Development

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The test fixtures are engraved from a table of pitches written out once in
`tools/make_fixtures.py`, so the expected reading in `manifest.json` cannot drift from the image
it describes. The images are gitignored and the manifest is not, so regenerate them after a clean
checkout:

```
.venv/bin/python tools/make_fixtures.py
```

That needs verovio and cairosvg, which are in `requirements-dev.txt`. cairosvg loads cairo itself
through cffi, so install that too. On a mac:
```brew install cairo```
On Ubuntu:
```sudo apt install libcairo2```

Every fixture is seeded from its case name, so two runs produce byte-identical output. Without
them the accuracy tests skip rather than fail.

### Why there is no SDK

The docker image is Alpine, and every requirement has a musllinux wheel on both amd64 and arm64,
so nothing is compiled during the build. That is only true because the anthropic SDK is not one of
them. It pulls in `jiter`, which publishes no musl wheel at all, and pip does not fail on that: it
quietly resolves back to an SDK too old to send the strict tool schemas in `sheeter/verify.py`.
Building `jiter` from source instead means a Rust toolchain in the image.

Switching vendors does not help. The `openai` SDK requires `jiter` as well, and `litellm` depends
on `openai` and adds `tiktoken`, `tokenizers` and `fastuuid` on top, all Rust extensions with the
same problem. Both are strictly worse.

So there is no SDK. `sheeter/claude.py` is a `urllib` POST to `/v1/messages` with the retries kept,
exposing `client.messages.create(...)` so the calling code reads the same either way. It honours
`ANTHROPIC_BASE_URL`, which is also how `tests/test_claude.py` points it at a loopback server. What
is given up is streaming and typed response models, neither of which this app used: the reply was
walked as plain JSON before and still is.

### Credits

- [Pico CSS](https://picocss.com) for the stylesheet, vendored in `static/css/`.
- [music21](https://www.music21.org) for chord naming and pitch spelling.
- [Verovio](https://www.verovio.org) for engraving the test fixtures.
- The geometry and the split between the deterministic pass and the model pass follow a written
plan rather than my own guessing, which is most of why it works at all.
