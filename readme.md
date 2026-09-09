# Sheeter: point your phone at sheet music and see what the notes are

Take a photo of a line of sheet music and Sheeter draws the note names on top of the photo,
step by step, left hand and right hand, with a chord name where there is one.

It is for people who know what a G minor 7 is but cannot read notation fast enough to find one
on the page. If you sight-read fluently you do not need this.

### How the reading works

Everything below is plain numpy and scipy. No model, no network, same answer every time.

1. **Prepare.** Decode, honour EXIF rotation, downscale so the long side is 2200px, find the
skew angle by rotating a small copy until the row-darkness profile spikes hardest, and threshold
adaptively so a lighting ramp across the page does not eat half the ink. The small copy is 900px
wide, where a whole page of music is a couple of hundred rows tall and a fifth of a degree hardly
moves the profile, so the angle gets a second look at full resolution where the same score is
sharp. That look only acts on a clear win, three times the score rather than a few percent:
asked to act on any improvement at all it moved four pages that were reading correctly, and on
one of them turned a treble clef into a bass clef, which is every pitch on the staff a twelfth
out of place with nothing about the reading looking wrong. On the page that needed it, the coarse
pass had rotated a level page 0.2 degrees off and cost it both clefs, both key signatures and its
first chord.
2. **Scale.** The commonest vertical black run on a page of music is the thickness of a staff
line and the commonest vertical white run is the gap between two of them, so the staff space
falls out of two histograms. Every threshold after this is written in staff spaces, so the
reading does not care what resolution the photo is.
3. **Staves.** Take every row that carries long horizontal ink as a hypothetical top line,
sweep the line spacing, predict the other four lines and keep the guess only if all five
predicted rows are inked across the same span. A photo
has page margins, so a staff almost never spans the frame and asking which rows are dark all the
way across does not work. That span is measured across a line's rows, one either side, and not
off a single row of pixels: deskew can leave a fraction of a degree behind, so a line drifts a
pixel or two end to end and no one row is all of it. Measured off one row, a staff came out
232 pixels short at the left, which hid its clef, its key signature and the left hand of its
first chord, all without anything about the reading looking wrong. A pixel either side is as far
as that goes, because the same rows decide whether a candidate staff is inked enough to be a
staff: a span that tolerated more drift than the score does would hand a badly drifting staff a
full width span it could not support and drop the staff altogether, which is worse. Drift past
that is the skew estimate's problem, and step 1 is where it is dealt with.
4. **Noteheads.** Correlate a notehead-shaped outline against the image with the staff lines
erased, and keep the peaks. Two things that are not notes answer to that outline and are ruled
out by what they are rather than by score, which does not separate them: a beam, which is a
notehead thick and so several spaces wide, and the thick barline that ends a piece, which is a
notehead wide and the whole staff tall. The first is caught by how far notehead-thick ink runs
sideways through the peak, the second by nothing real ever sitting at the end of a staff, because
the barline is drawn after the last note. A steeply slanted beam still gets through: it presents
too little ink on any one row to tell from a notehead. It is a small bank of outlines rather than one, because a whole note
is a different shape: wider, rounder and upright instead of leaning. Matching an outline rather
than classifying connected components is what makes dense music work, since a stack of thirds is
one blob but three separate peaks, and a hollow notehead nicked by staff-line removal is still
one peak.
5. **Pitch.** A notehead's vertical position against the five lines is a diatonic step, which
becomes a pitch once you know the clef (read from the height of the glyph at the left edge) and
the key signature (the run of accidentals right after it). The signature is fitted rather than
spelled out: it is a rigid template, so what matters is how well the run sits on the positions it
must occupy, not what letter each glyph is nearest. Asking for the letter is the fragile way
round, since on a photocopy a flat's bowl measured over half a step high and the lone flat of a
B flat signature came out as a C, which lost the page its key and read every B in it natural. Accidentals written in the bar are
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
- **An accidental touching its own notehead.** Written sharps and flats are found as connected
shapes. Two accidentals that touch each other in a cascade are cut back apart, but an accidental
fused with the notehead it belongs to, or with a stem, is one shape with a notehead in it and is
not read. Worse, a missed accidental is not just a missing sharp: noteheads are detected everywhere
except where an accidental was found, so an undetected one gets read as a notehead or two of its
own, and a three-note chord comes back with five notes in it.
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
analysis you already have instead of computing it twice. Unless the reader has changed since: every
analysis records the engine version that made it, and when that is not the version running now,
re-uploading reads the page again, keeping the title you gave it. Every reading also has a "Re-analyze"
button beside "Verify with Claude" that runs the current reader over the stored photo, and a
reading made by an older engine says so at the top of its page, so what is already in the gallery
can be brought up to date without finding the file. Otherwise a page analysed before a fix would
show the reading from before the fix for as long as it stayed in the store.

### Saying what the key is

The key signature decides the alteration of every notehead that has no accidental of its own, so
one misread glyph after the clef respells a whole page. It is the reader's worst single failure and
the one a person can see the answer to in a second, so every reading has a key signature picker: a
button showing the key it is in, which opens all fifteen drawn as little staves. Match what is
printed on your photo, tap it, and every note is spelled again and every chord named again. It is
arithmetic on the reading already stored, not a second look at the photo, so it returns at once.

The choice is remembered on the reading and outlives it. Re-analyze keeps it, since a better
reading of the page does not make the key a different key, and the Claude pass will not overrule it,
since somebody holding the page beats a model on what the signature is. "Use the key read from the
photo" is the way back, and that one does read the photo again, because the detected key is not kept
once a choice has replaced it.

The pictures have no clef in them. A treble clef is either a large hand-drawn path or a font glyph,
and U+1D11E renders as a missing-glyph box on plenty of systems, which is worse than nothing. The
accidentals are drawn where they are printed, which is what the eye matches on.

One limit worth knowing: re-spelling works from each notehead's stored position and its own written
accidental, so an accidental that carried across the rest of its bar is not carried again, and a
note that inherited a flat that way is spelled from the key instead. The Claude pass has the same
gap. It bites only where a bar has one accidental and a later note on the same line leans on it,
which is uncommon in the close voicings this is pointed at. Barlines are now stored on the staff,
so the missing piece is only that `restate_staff` does not read them: closing it properly means
changing what every key pick and every Claude pass produces, which is worth doing on its own rather
than as a side effect. A note corrected by hand does carry, because that path resolves the measure.

### Fixing a note or a chord by hand

Four things go wrong often enough to be worth correcting by eye rather than by re-reading the
photo: the reader invents a chord that is not there, usually out of a barline or a slanted beam; it
misses an accidental fused with the notehead it belongs to; it reads a notehead a space off; and it
misses a notehead, or a whole hand, entirely. So selecting a step shows a fix panel under the
photo, and the panel holds, for every notehead of that chord, a menu of the staff positions it
could be on, a sharp, flat and natural button, and a cross to take it out of the chord; then an
"add a note" menu for each hand, and one button to delete the whole chord.

The accidental in force is the one marked, and that is the note's alteration rather than only what
is printed in front of it: a note the key signature sharpens shows sharp, because that is what it
sounds. Asking for the one already marked does nothing and records nothing, so agreeing with the
page cannot leave a reading covered in corrections that correct nothing.

"Delete this chord" sits at the top of the panel, being the one control that is about the whole
step rather than about one notehead; last, on a panel long enough to scroll, it sat under the hand
you had just finished correcting. "Move" applies a pitch picked from the menu and is hidden until
the menu is actually changed, so it is not a button asking to be puzzled over on every notehead at
once. The overlay hides it rather than revealing it, which is what keeps the menu submittable with
no scripting.

The panel replaced two things that said what the labels on the photo already say: a step detail
that read the selected chord back in prose, and a table of the whole reading underneath it. The
staves table went too, since the key it listed is what the picker at the top of the page shows.
What went with all three, and is worth knowing: the interval list from the bass up, the roman
numeral, the duration hints, the low-confidence warning in words, and each staff's clef and
cut-off flag. The chord's common name and the Claude pass's per-event note are kept in the fix
panel's own heading, since it is per-step and there was nowhere else for them. "Copy as text" moved
into the fix section rather than going with the table it sat on. A clipped staff still says so, in
the warnings at the top, which is where it always was.

The labels over the photo are always both notes and chords now. Choosing between them was three
buttons for a decision nobody needs to make; the decision that matters on a dense page is how many
labels at once, so "One step" and "All labels" moved into the bar above the photo where the labels
they control are, and the bar below the photo is left to stepping.

An accidental written by hand behaves like a printed one: it holds at that staff position until the
next barline, so correcting the first E flat of a bar corrects the later ones that lean on it and
leaves the bar after it alone. That is the same rule `apply_alterations` applies when the page is
first read, resolved against the barlines stored on the staff, which is why it needed them stored.
A notehead that carries its own accidental is not overruled by the carry, and neither hand reaches
into the other.

Deleting a chord renumbers the ones after it, because the schema requires an event's index to be
its position and the Claude pass matches its answer up by that index. Stored corrections move along
with their chords, and a correction on the deleted chord goes with it. Taking the last notehead out
of a hand takes the hand, and the last hand takes the chord: an event with no parts is not a chord
the schema will hold, and a hand with no noteheads is not a hand playing a rest.

Only the accidentals are kept for replay, and the reason is worth stating because it decides the
whole design. Moving, adding and removing a notehead all write the geometry, and everything that
re-derives a pitch reads the geometry: `restate_staff` walks the noteheads that are there and works
each one out from its own y. So those three outlive a key pick with nothing replayed. A written
accidental is the one correction that does not, because re-deriving reads that field and resolves
it against the key instead. So the log holds accidentals, and re-applies them after anything that
re-derives a pitch: picking a key re-spells every notehead, and the Claude pass re-reads every
accidental off the image, so without the replay either one would quietly undo a person's answer.
Replaying a structural change onto a structure that already contains it is a question this never
has to answer.

A correction is anchored by the height it was made at and not by the notehead's place in the chord,
so adding or removing a notehead cannot silently re-point the corrections after it. The slack is a
quarter of a step, which scales with the photo: a fixed pixel count would drop every correction on
a page the next reading nudged by a pixel, or slide one onto the staff position next door. Re-analyze is the exception. A fresh reading of the photo renumbers everything and
may not contain the notehead at all, so there is no anchor left that means anything, and the
corrections are cleared rather than moved onto whatever now sits at that index. The page says how
many it holds and warns before the button.

Every control is a submit button, so correcting a reading works with scripting off, like the key
picker: a panel per step is rendered for every step and all of them are visible, and it is the
overlay that hides all but the selected one. One form per notehead rather than one per button,
each button pointing itself at its route with `formaction`, because five forms a notehead repeated
the same four indices five times and put 36KB of it on a thirteen-step page. The whole page is
164KB there, 9KB gzipped, being almost entirely repeated pitch menus.

### Hearing the chords

Selecting a step plays it, which for someone who knows what a chord sounds like but not what it
looks like is the whole point. Tap a numbered dot, a label, or a row in the table, or walk through
with the arrow keys.

The chord is strummed rather than struck: one note every 120ms, low to high, left hand before
right, so a six-note voicing rolls out over 600ms. That is slow enough to hear the notes arrive
one at a time and still fast enough that they ring together as a chord. Ordering is by hand first
and pitch second, which is not the same as sorting every note by pitch, because the hands can
overlap and a left-hand note above a right-hand one should still come first.

The synthesis is deliberately plain: one triangle oscillator per note through a struck envelope,
so it lands somewhere near a music box. The decay is 2.6s, which is set by the strum rather than
by taste: at the 1.15s it started out with, the bottom note was down to 2% of its peak by the time
the top one arrived, and six notes in a row is not a chord. That is about a hundred lines of Web Audio in
`static/js/audio.js` and needs no samples, no library and no network. It is not trying to sound
like a piano, only to be recognisable.

A "Sound on" button next to the zoom controls turns it off, and the choice is remembered. Playback
is on by default, and nothing sounds until you actually select something: the audio context is
built inside the first click, which is both what the autoplay rules want and why the page is silent
while it loads. Each chord cuts the one before it, so holding an arrow key steps rather than
smears. Where there is no Web Audio the button is hidden rather than left there doing nothing.


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

There is a second, smaller set that is not generated: `tests/pages/` holds real pages, photocopied
jazz voicings, with what a person said is on them in `manifest.json`. That is where the reader has
actually failed, and every fix to the geometry since was made against one of them. Its test is
strict in both directions: a step the manifest says is right must read right, and a step listed in
`known_wrong` must still read wrong, so a fix that lands by accident gets noticed and promoted
rather than quietly relied on. Adding a page is a PNG, its ground truth, and a note saying which
values a person checked and which were inferred.

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

A wheel that installs is not a wheel that loads, and musl is where that bites: `libstdc++` is on the
manylinux allowlist but not the musllinux one. Reading the `DT_NEEDED` entries of all 166 shared
objects in the musl wheels says they are self-contained, because auditwheel bundled exactly the
libraries that are not allowlisted: `libstdc++`, `libgcc_s`, `libgfortran`, `libquadmath` and
openblas all ship inside the wheels. The only external name left is `libz.so.1`, which alpine has
anyway since apk links it. The Dockerfile does not take that on faith: it imports the compiled
modules in their own layer, so a base image that stops satisfying them fails the build rather than
the first upload.

### CI

Two workflows, and one of them calls the other.

`tests.yml` runs on every pull request against main. It installs `libcairo2`, engraves the
fixture corpus from scratch, and runs the suite. Two of its steps exist because of specific ways
this repo can go quietly wrong:

- **Corpus drift.** The fixture images are gitignored and rebuilt on each run, which is only safe
because every case is seeded from its own name. The job diffs the regenerated manifest against the
committed one, so a change that alters what gets engraved fails loudly instead of moving the
accuracy baseline underneath you. This is not hypothetical: the generator sat broken for several
commits because the images on disk predated the break.
- **Silent skips.** The accuracy tests skip rather than fail when the corpus is missing, so the
suite can go green having never read a note. Everything they need is installed on the runner, so
the job fails on any skip at all and names which ones.

It also builds the docker image, runs the container, and polls `/health`. That is the end-to-end
check that the Alpine base actually works, which the musl notes above can only argue statically.

`docker-build.yml` runs on push to main, which is what merging a pull request does. It calls
`tests.yml` first and pushes `akshaykannan/sheeter:latest` for amd64 and arm64 only if every job
passes. It needs `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` as repository secrets.

Note that a workflow running on a pull request does not by itself stop anyone merging it. To make
these a real gate, require `pytest` and `docker image` as status checks in the branch protection
rule for main.

### Credits

- [Pico CSS](https://picocss.com) for the stylesheet, vendored in `static/css/`.
- [music21](https://www.music21.org) for engraving the test corpus. It used to name the chords
too; `sheeter/naming.py` does that now, so it is a dev dependency only.
- [Verovio](https://www.verovio.org) for engraving the test fixtures.
- The geometry and the split between the deterministic pass and the model pass follow a written
plan rather than my own guessing, which is most of why it works at all.
