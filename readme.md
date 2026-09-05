
# Sheeter: point your phone at sheet music and see what the notes are

Take a photo of a line of sheet music and Sheeter draws the note names on top of the photo,
step by step, left hand and right hand, with a chord name where there is one.

It is for people who know what a G minor 7 is but cannot read notation fast enough to find one
on the page. If you sight-read fluently you do not need this.

### How it works

Two passes, and the first one always runs:

1. **Geometry.** Plain numpy and scipy. Find the staff lines from a row-darkness profile, measure
the staff space, remove the lines, find the noteheads as connected components, and turn each
notehead's vertical position into a diatonic step. Group noteheads that share an x position into
an event. music21 names the chord. No model involved, no network, deterministic.
2. **Claude.** Optional, and only runs if `ANTHROPIC_API_KEY` is set. The geometry reading plus the
processed image go to Claude, which fixes the cases geometry is bad at: noteheads fused into one
blob, deciding which note an accidental belongs to, and reading the clef and key signature. It
corrects pitches, it does not invent notes at new positions.

The division of labour is the point. Precise vertical localization is easy for geometry and hard
for a model; counting flats in a key signature is the other way around.

It gets things wrong. Bad lighting, heavy perspective, handwritten music, and anything denser than
one system in the frame will all confuse it. Treat it as a reading aid and check it against the page.
It expects printed music, one system at a time (one staff or one grand staff pair), treble and bass
clefs, photographed reasonably flat.

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
      # optional, enables the Claude correction pass
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

Open http://localhost:5000 on your phone, take or pick a photo of one line of music, and wait a few
seconds. You get the photo back with a label on each notehead and the chord spelled out under each
step. Everything you have analysed before is on the home page, so you can jump back to it.

Uploads are content addressed by sha256, so re-uploading the same photo returns the analysis you
already have instead of paying for it twice.

### Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `SHEETER_DATA_DIR` | `<repo>/data`, and `/data` in the docker image | Where analyses are stored |
| `ANTHROPIC_API_KEY` | unset | Enables the Claude correction pass. Without it you get the geometry reading only |
| `SHEETER_VERIFY_MODEL` | `claude-sonnet-5` | Model for the vision pass that corrects pitches |
| `SHEETER_SYMBOL_MODEL` | `claude-haiku-4-5` | Model for the text pass that names chords and writes the one-line explanation |
| `PORT` | `5000` | Port for `tools/serve.sh`. The docker image always listens on 5000, remap it with `-p` |

### Where things are stored

One directory per analysis, named after the first 12 hex characters of the sha256 of the upload:

```
<data dir>/<id>/original.<ext>   the bytes exactly as uploaded
<data dir>/<id>/processed.png    the deskewed grayscale image the overlay sits on
<data dir>/<id>/thumb.jpg        preview for the home page
<data dir>/<id>/analysis.json    the reading itself, schema in docs/schema.md
```

There is no database and no index file. The home page is built by scanning directories, and
`analysis.json` is written last, so a half-written analysis is simply skipped. Deleting an analysis
is `rm -rf` on its directory. The JSON is also served at `/a/<id>/analysis.json` if you want to do
something else with it.

### Development

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The test fixtures are engraved from a table of pitches written out once in `tools/make_fixtures.py`,
so the expected reading in `manifest.json` cannot drift from the image it describes. The images are
gitignored and the manifest is not, so regenerate them after a clean checkout:

```
.venv/bin/python tools/make_fixtures.py
```

That needs verovio and cairosvg, which are in `requirements-dev.txt`. cairosvg loads cairo itself
through cffi, so install that too. On a mac:
```brew install cairo```
On Ubuntu:
```sudo apt install libcairo2```

Every fixture is seeded from its case name, so two runs produce byte-identical output.

### Credits

- [Pico CSS](https://picocss.com) for the stylesheet, vendored in `static/css/`.
- [music21](https://www.music21.org) for chord naming and pitch spelling.
- The geometry and the split between the deterministic pass and the model pass follow a written
plan rather than my own guessing, which is most of why it works at all.
