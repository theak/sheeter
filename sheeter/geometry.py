"""Deterministic reading of a page of printed music: staves, noteheads, pitches.

This is the part of the pipeline that always runs, with or without an API key, and
everything downstream trusts its coordinates.  The approach is the one worked out by
hand in docs/plan.md, with two substitutions that make it survive real photographs:

* scale is measured from vertical run-length histograms rather than assumed, so every
  threshold below is expressed in staff spaces and the code is resolution independent;
* staff lines are found by predicting the other four from any two and confirming them,
  rather than by asking which rows are dark across most of the frame.  A photo has page
  margins, so a staff almost never spans the image.

Noteheads are found by matched filtering against an ellipse rather than by classifying
connected components.  Components fuse (a stack of thirds becomes one blob) and split
(erosion cuts a hollow notehead in half); correlation peaks do neither.
"""

import numpy as np
from scipy import ndimage
from scipy.signal import fftconvolve

from . import pitches
from .preprocess import estimate_scale, run_lengths

#: Bumped whenever a change can alter what the reader says about a page.  Stored
#: analyses carry the version that produced them; the app reads one again on request,
#: and on re-upload, when its version is not this one.  Without that, an image analysed
#: before a fix keeps showing the reading from before the fix, forever, because uploads
#: are content addressed and the old reading is what the address points at.
GEOMETRY_VERSION = "7"

__all__ = ["analyze", "estimate_scale", "GEOMETRY_VERSION"]

# Every constant here is a multiple of the staff space, never a pixel count.
NOTEHEAD_W = 1.30       # notehead width in staff spaces
NOTEHEAD_H = 0.95       # notehead height
RESPONSE_MIN = 0.72     # fraction of the notehead outline that must be inked
FLANK_MIN = 0.55        # of that, how much of the left and right flanks must be inked
GRID_TOLERANCE = 0.34   # how far off the half-space grid a notehead may sit
STAFF_REACH = 6.0       # staff spaces above/below a staff that still belong to it
GAP_MARGIN = 0.5        # how close to the other staff's outer line a grand staff's
                        # search for noteheads reaches across the gap between them
LEDGER_STUB_THICKNESS = 2.0   # a ledger line is this many staff-line thicknesses
                              # tall at most; a notehead's wall is half a space

#: Staff space, in pixels, below which the reading stops being trustworthy.  Measured
#: by shrinking fixtures until they broke: at 18 and up the corpus still reads
#: perfectly, and by 15 whole chords are being invented and dropped.
RELIABLE_UNIT_PX = 18


# --------------------------------------------------------------------------- scale

# ---------------------------------------------------------------------- staff lines

def staff_line_mask(mask, thickness, unit, reach=2.0):
    """Keep only long, thin, horizontal structures.

    *reach* is the length of the horizontal opening in staff spaces.  Staff finding
    wants a strict 2.0, which admits only full staff lines.  Erasing wants about 1.2,
    which also catches ledger lines and the stubs left where a line meets a glyph;
    those are short, and leaving them behind welds neighbouring symbols together.
    """
    horizontal = max(3, int(round(unit * reach)))
    opened = ndimage.binary_opening(mask, structure=np.ones((1, horizontal)))
    thin = run_lengths(mask) <= max(2, int(round(thickness * 2.5)))
    return opened & thin


def _line_rows(lines_mask, y):
    """The rows that count as the staff line at *y*: one either side of it.

    A printed staff line is not one row of pixels across a whole page.  Deskew leaves
    a fraction of a degree behind, so a line drifts a pixel or two from one end to the
    other and no single row is all of it.  One row either side is as much of that as
    anything here tolerates, and every measurement of a line has to agree on which
    rows it is, or they disagree about where the line stops.
    """
    y = int(round(y))
    return max(0, y - 1), min(lines_mask.shape[0], y + 2)


def _row_support(lines_mask, y, x0, x1):
    """Fraction of the given x span that is inked on the line at row *y*."""
    lo, hi = _line_rows(lines_mask, y)
    if lo >= hi or x1 <= x0:
        return 0.0
    band = lines_mask[lo:hi, x0:x1]
    return float(band.any(axis=0).mean())


def find_staves(mask, thickness, unit):
    """Locate every five-line staff.

    Candidate rows come from the horizontal projection of the staff-line mask.  Each
    candidate is then treated as a possible top line and scored by how well the four
    predicted lines below it are actually inked: predict and confirm, which is what
    makes this work when the staff does not span the frame.
    """
    lines_mask = staff_line_mask(mask, thickness, unit)
    height, width = mask.shape
    projection = lines_mask.sum(axis=1).astype(np.float64)
    if projection.max() <= 0:
        return []

    # A row is a candidate if it is a local maximum carrying real horizontal ink.
    floor = max(unit * 3.0, projection.max() * 0.12)
    smoothed = ndimage.uniform_filter1d(projection, size=max(1, int(round(thickness))))
    peaks = (smoothed >= floor) & (smoothed >= ndimage.maximum_filter1d(
        smoothed, size=max(3, int(round(unit * 0.6)))))
    candidates = np.flatnonzero(peaks)
    if candidates.size < 2:
        return []

    scored = []
    for top in candidates:
        for spacing in np.arange(unit * 0.8, unit * 1.25, unit * 0.02):
            ys = [top + i * spacing for i in range(5)]
            if ys[-1] >= height:
                continue
            # Across the line's rows, not one of them.  Read off a single row, the span
            # is only as long as the stretch of the line that happens to sit on that
            # row, and on a page with any drift left in it that is a fraction of the
            # staff.  A bass staff came out 232px short at the left, which put its clef
            # outside clef_end's search, and clef_end then starts the notehead search
            # after the first chord: the whole left hand of it was never looked for.
            lo, hi = _line_rows(lines_mask, ys[0])
            columns = np.flatnonzero(lines_mask[lo:hi].any(axis=0))
            if columns.size == 0:
                continue
            x0, x1 = int(columns[0]), int(columns[-1]) + 1
            if x1 - x0 < unit * 6:
                continue
            support = [_row_support(lines_mask, y, x0, x1) for y in ys]
            if min(support) < 0.45:
                continue
            scored.append((float(np.mean(support)), float(top), float(spacing),
                           x0, x1))

    if not scored:
        return []

    # Keep the best non-overlapping candidates, strongest first.
    scored.sort(key=lambda s: (-s[0], s[1]))
    staves = []
    for score, top, spacing, x0, x1 in scored:
        ys = [top + i * spacing for i in range(5)]
        if any(not (ys[-1] < s["lines"][0] - unit * 0.5
                    or ys[0] > s["lines"][-1] + unit * 0.5) for s in staves):
            continue
        refined = [_refine_line(lines_mask, y, x0, x1, thickness) for y in ys]
        staves.append({
            "lines": refined,
            "unit": (refined[-1] - refined[0]) / 4.0,
            "x_range": [x0, x1],
            "support": score,
        })

    staves.sort(key=lambda s: s["lines"][0])
    return staves


def _refine_line(lines_mask, y, x0, x1, thickness):
    """Snap a predicted line to the inked rows around it."""
    reach = max(1, int(round(thickness * 1.5)))
    lo = max(0, int(round(y)) - reach)
    hi = min(lines_mask.shape[0], int(round(y)) + reach + 1)
    band = lines_mask[lo:hi, x0:x1]
    weights = band.sum(axis=1).astype(np.float64)
    if weights.sum() <= 0:
        return float(y)
    return float((np.arange(lo, hi) * weights).sum() / weights.sum())


# -------------------------------------------------------------------------- systems

def group_systems(staves, mask, unit):
    """Cluster staves into systems and decide which pairs are one grand staff.

    The reliable signal is whether ink actually runs from one staff to the next at the
    left edge, which is what a brace or a shared barline looks like and what a system
    break has none of.  Vertical gaps are the fallback, not the rule: on a real page the
    gap between two systems is often barely larger than the gap inside a grand staff,
    close enough that no ratio separates them, while the brace is unmistakable.
    """
    if not staves:
        return []
    if len(staves) == 1:
        return [[0]]

    gaps = [staves[i + 1]["lines"][0] - staves[i]["lines"][-1]
            for i in range(len(staves) - 1)]
    page_left = min(staff["x_range"][0] for staff in staves)
    joined = [_joined_at_left(mask, staves[i], staves[i + 1], unit, page_left)
              for i in range(len(staves) - 1)]

    if any(joined):
        breaks = [not link for link in joined]
    else:
        # Nothing was braced, so either every staff stands alone, as on a lead sheet,
        # or the brace was lost. Fall back to the shape of the gaps.
        median_gap = float(np.median(gaps))
        breaks = [gap > max(median_gap * 1.6, unit * 5.0) for gap in gaps]
        if len(staves) == 2:
            breaks = [gaps[0] > unit * 11.0]

    systems, current = [], [0]
    for index, breaking in enumerate(breaks):
        if breaking:
            systems.append(current)
            current = [index + 1]
        else:
            current.append(index + 1)
    systems.append(current)
    return systems


def _joined_at_left(mask, upper, lower, unit, page_left=None):
    """Is there continuous vertical ink between two staves near their left edge?

    That is a brace or a barline drawn through both, and it is what makes two staves one
    grand staff.  The window reaches well left of where the staff lines were measured to
    start: the brace sits outside them, and on an indented first system the measured
    left edge can be a staff space or two right of where the joining stroke actually is.

    It reaches from the leftmost staff on the whole page, not just this pair, because
    the pair's own measured edges can both be wrong the same way.  A treble clef, a
    sharp and a 4/4 break the first hundred pixels of every staff line into runs too
    short to count as line, so on one page both staves of a grand staff measured as
    starting 130px in, the window sat clear of the barline joining them, and they were
    read as two lone staves.  Staves on a page share a left margin, so the page's
    leftmost staff says where the joining stroke can be.
    """
    left = min(upper["x_range"][0], lower["x_range"][0])
    if page_left is None:
        page_left = left
    x0 = max(0, int(min(left, page_left) - unit * 3.0))
    x1 = int(min(mask.shape[1], left + unit * 1.5))
    y0 = int(round(upper["lines"][-1]))
    y1 = int(round(lower["lines"][0]))
    if y1 <= y0 + 2 or x1 <= x0:
        return False
    band = mask[y0:y1, x0:x1]
    return bool((band.sum(axis=0) >= (y1 - y0) * 0.8).any())


# ------------------------------------------------------------------ glyph isolation

def remove_staff_lines(mask, lines_mask, thickness, unit):
    """Erase staff and ledger lines, keeping every glyph that crosses them.

    Only pixels that are both thin vertically and part of a long horizontal run are
    removed, which is what a staff line is and what a notehead is not.  The closing
    step bridges the gap a notehead punches in a line so the line is taken out whole
    rather than leaving stubs either side of every note.

    Removal is deliberately conservative.  A notehead sitting in a staff space has its
    top and bottom rim lying exactly along the two lines that bound the space, so any
    rule aggressive enough to guarantee a clean line also eats those rims.  Detection
    downstream matches a notehead outline rather than a filled shape precisely so that
    it survives the stubs this leaves behind.
    """
    runs = run_lengths(mask)
    thin = (runs > 0) & (runs <= max(2, int(round(thickness * 2.2))))
    # 1.5 staff spaces sits between the two things that matter: a notehead is 1.3 wide
    # and must survive, a ledger line is about 1.8 and must not.  Going shorter erases
    # the flat top and bottom of a hollow notehead's rim along with the lines.
    erase = staff_line_mask(mask, thickness, unit, reach=1.5)
    bridged = ndimage.binary_closing(
        erase | lines_mask, structure=np.ones((1, max(3, int(round(unit * 2.5))))))
    return mask & ~(bridged & thin)


def ellipse_kernel(unit, width=NOTEHEAD_W, height=NOTEHEAD_H, shear=0.36):
    """A filled ellipse the size of a notehead, optionally leaning like an engraved one.

    Half and quarter noteheads lean about 20 degrees, and shearing the template to
    match gains several points of response, which matters at the floor we accept.
    """
    kh = max(3, int(round(unit * height)) | 1)
    kw = max(3, int(round(unit * width)) | 1)
    yy, xx = np.mgrid[0:kh, 0:kw]
    cy, cx = (kh - 1) / 2.0, (kw - 1) / 2.0
    sheared_y = (yy - cy) + (xx - cx) * shear
    ellipse = (sheared_y / (kh / 2.0)) ** 2 + ((xx - cx) / (kw / 2.0)) ** 2 <= 1.0
    return ellipse.astype(np.float32)


def notehead_kernels(unit):
    """Templates to match noteheads against, plus the core used to tell them apart.

    Returns ``(rims, core)`` where *rims* is a small bank of outlines.  Matching the
    outline rather than the filled shape is what lets one pass find both a half note
    and a quarter note, and it degrades gracefully when staff-line removal nicks a rim:
    the score drops in proportion instead of collapsing, which is what happens if you
    fill the shape first and the fill leaks through the gap.

    The bank exists because a whole note is not the same shape as the others.  It is
    noticeably wider, rounder and upright rather than leaning, so the ordinary template
    scores it too low to keep.

    *core* is the inside of an ordinary notehead, and how much of it is inked is what
    separates a hollow notehead from a filled one.
    """
    shapes = [(NOTEHEAD_W, NOTEHEAD_H, 0.36), (1.75, 1.02, 0.0)]
    rims, sides = [], []
    for width, height, shear in shapes:
        outer = ellipse_kernel(unit, width, height, shear).astype(bool)
        rim = outer & ~_shrink(outer, max(1, int(round(unit * 0.13))))
        rims.append(rim.astype(np.float32))
        columns = np.arange(rim.shape[1])
        edge = rim.shape[1] * 0.3
        flank = (columns < edge) | (columns > rim.shape[1] - 1 - edge)
        sides.append((rim & flank[None, :]).astype(np.float32))
    ordinary = ellipse_kernel(unit, NOTEHEAD_W, NOTEHEAD_H, 0.36).astype(bool)
    core = _shrink(ordinary, max(1, int(round(unit * 0.20))))
    return rims, sides, core.astype(np.float32)


def _shrink(kernel, pixels):
    return ndimage.binary_erosion(
        kernel.astype(bool), structure=np.ones((2 * pixels + 1, 2 * pixels + 1)))


def _correlate(binary, kernel):
    total = float(kernel.sum())
    if total <= 0:
        return np.zeros(binary.shape, dtype=np.float32)
    return fftconvolve(binary.astype(np.float32), kernel[::-1, ::-1],
                       mode="same") / total


def _extent(binary, y, x, axis, limit):
    """Length of the contiguous True run through (y, x) along *axis*."""
    y, x = int(round(y)), int(round(x))
    if not (0 <= y < binary.shape[0] and 0 <= x < binary.shape[1]) or not binary[y, x]:
        return 0
    line = binary[:, x] if axis == 0 else binary[y, :]
    here = y if axis == 0 else x
    lo = hi = here
    while lo > 0 and line[lo - 1] and here - lo < limit:
        lo -= 1
    while hi + 1 < line.size and line[hi + 1] and hi - here < limit:
        hi += 1
    return hi - lo + 1


# ------------------------------------------------------------------------- glyphs

def _components(mask, min_pixels):
    """Connected components as dicts, largest first."""
    labels, count = ndimage.label(mask)
    if count == 0:
        return []
    out = []
    for index, box in enumerate(ndimage.find_objects(labels), start=1):
        if box is None:
            continue
        ys, xs = box
        patch = labels[box] == index
        size = int(patch.sum())
        if size < min_pixels:
            continue
        out.append({
            "x0": int(xs.start), "x1": int(xs.stop),
            "y0": int(ys.start), "y1": int(ys.stop),
            "w": int(xs.stop - xs.start), "h": int(ys.stop - ys.start),
            "size": size, "patch": patch,
        })
    out.sort(key=lambda c: -c["size"])
    return out


def staff_band(staves, index, unit, height):
    """The vertical slice of the page that belongs to one staff.

    Bounded by the midpoint to its neighbours so a grand staff never counts the same
    notehead twice, and by STAFF_REACH otherwise, which is about five ledger lines.
    This is the band for glyphs, stems and the signatures.  The search for noteheads
    on a grand staff reaches further, see _search_bands: which staff a notehead in the
    gap belongs to is not a question the midpoint can answer.
    """
    staff = staves[index]
    top = staff["lines"][0] - unit * STAFF_REACH
    bottom = staff["lines"][-1] + unit * STAFF_REACH
    if index > 0:
        top = max(top, (staves[index - 1]["lines"][-1] + staff["lines"][0]) / 2.0)
    if index + 1 < len(staves):
        bottom = min(bottom, (staff["lines"][-1] + staves[index + 1]["lines"][0]) / 2.0)
    return max(0, int(top)), min(height, int(bottom))


def _thin_verticals(mask, unit):
    """Barlines, braces and stems: tall, and no wider than a third of a staff space."""
    height = max(3, int(round(unit * 1.5)))
    tall = ndimage.binary_opening(mask, structure=np.ones((height, 1)))
    narrow = run_lengths(mask.T).T <= max(2, int(round(unit * 0.35)))
    return tall & narrow


def clef_end(mask_ns, staff, band, unit):
    """Where the clef at the left edge of a staff ends, so the reading can start after it.

    The clef itself is not read here; see _assign_clefs for why.  What is still needed
    from the glyph is its right edge: the key signature is looked for from there, and
    the noteheads after that.  Returning the staff's own left edge instead would put
    the clef inside the notehead search and it gets read as a note or two.

    Braces, barlines and stems are stripped first.  Left in, they join the clef into one
    component that spans the band and ends wherever they do.
    """
    x0, x1 = staff["x_range"]
    left = max(0, int(x0 - unit))
    right = min(x1, int(x0 + unit * 7))
    if right <= left:
        return x0
    search = mask_ns[band[0]:band[1], left:right]
    if not search.any():
        return x0
    solid = search & ~_thin_verticals(search, unit)

    best = None
    for comp in _components(solid, int(unit * unit * 0.2)):
        if comp["h"] < unit * 1.6 or comp["w"] < unit * 0.7:
            continue
        if comp["x0"] > unit * 4.0:
            continue        # a clef sits at the staff's edge, not out among the notes
        if best is None or comp["x0"] < best["x0"]:
            best = comp
    if best is None:
        return x0
    return left + best["x1"]


def _glyph_columns(patch, thickness):
    """The columns of *patch* that hold glyph rather than a staff-line remnant.

    Taking the staff lines out leaves a sliver of line either side of whatever it
    crossed.  On an accidental that is a horizontal whisker sticking out of the glyph,
    and the bounding box counts it: the sharps in a close voicing measured 1.55 staff
    spaces wide against a real width of 1.00, so the width test threw them away and the
    chord lost every accidental in it.

    The cut is the staff line's own thickness, because that is what bounds the whisker:
    a column crossing nothing but a remnant cannot hold more ink than the line was
    thick.  A real stroke's column holds a good part of the glyph's height, which is
    several times that even on a thick scan, since thickness grows far more slowly than
    a glyph does.

    A fraction of the component's height would seem more scale free and is not.  Tried
    at 0.15 it read 24 pixels on a clean 51 pixel-per-space render, ate most of a glyph,
    and turned an empty key signature into one sharp.  The remnant there was still one
    pixel: it is the line that sets the scale here, not the glyph.

    Returns ``(first, last)`` inclusive, or None when nothing survives.
    """
    ink = patch.sum(axis=0)
    solid = np.flatnonzero(ink > max(1.0, thickness))
    if solid.size == 0:
        return None
    return int(solid[0]), int(solid[-1])


def classify_accidental(comp):
    """Tell a flat from a sharp from a natural, and say where its pitch reference is.

    Returns ``(kind, reference_y_offset_within_bbox)`` or ``(None, None)``.
    The reference differs per glyph: a flat is pitched at the centre of its bowl, a
    sharp and a natural at the midpoint of their two crossbars.  Using the bounding
    box centre instead is wrong by most of a staff space for a flat.
    """
    patch = comp["patch"]
    height, width = patch.shape
    if height < 4 or width < 2:
        return None, None
    rows = patch.sum(axis=1).astype(np.float64)
    top_share = rows[: height // 2].sum() / max(1.0, rows.sum())

    if top_share < 0.36:
        # Flat: a thin ascender over a round bowl, so nearly all ink is low.
        lower = rows.copy()
        lower[: int(height * 0.35)] = 0.0
        centre = (np.arange(height) * lower).sum() / max(1.0, lower.sum())
        return "flat", float(centre)

    # Sharp and natural both have two crossbars, so the widest rows locate the pitch.
    bars = rows >= rows.max() * 0.75
    centre = float(np.flatnonzero(bars).mean()) if bars.any() else height / 2.0

    # A natural's strokes are staggered (left one high, right one low); a sharp's two
    # verticals both run the full height.  Comparing the half-column centroids
    # separates them without needing to find the strokes themselves.
    left, right = patch[:, : width // 2], patch[:, -(width // 2) or width:]
    ys = np.arange(height)
    left_c = (ys * left.sum(axis=1)).sum() / max(1.0, left.sum())
    right_c = (ys * right.sum(axis=1)).sum() / max(1.0, right.sum())
    stagger = (right_c - left_c) / height
    return ("natural" if stagger > 0.075 else "sharp"), centre


#: How far notehead-thick ink may run horizontally through a notehead, in staff spaces.
#: Past this it is a beam, which is the same thickness as a notehead and the reason the
#: correlator answers on one at all.  Thin ink is eroded away before measuring, so a
#: ledger line or a staff line remnant through a notehead cannot lengthen the run.
#:
#: Measured over every image here, the widest run through a real notehead is 1.27
#: spaces, and the beams read as notes ran 3.45 and 3.77.  This catches a level beam.
#: A steeply slanted one presents little ink on any one row, measures 1.36, and is not
#: separable from a notehead this way, so two of those still get through on the page
#: this was written for.
BEAM_RUN_MAX = 1.7

#: How close to the end of its staff a notehead may sit, in staff spaces.  Everything
#: at or past the edge was the final barline; the nearest real notehead was 1.2 spaces
#: inside it, so this sits between them with room either way.
BARLINE_MARGIN = 0.35

#: An accidental's height and width in staff spaces, wide enough for every font.
ACCIDENTAL_H = (1.85, 3.7)
ACCIDENTAL_W = (0.22, 1.25)
#: A half of a split cascade has to be a whole accidental, not a stroke of one.  The
#: thinnest column of a lone sharp is the gap between its two strokes, and cutting there
#: gives two "sharps" 0.4 and 0.6 wide that both classify.  Real accidentals measure
#: 0.77 (natural) to 1.0 (sharp), so this is clear of both.
CASCADE_HALF_W_MIN = 0.65


def _accidental_sized(comp, unit, narrowest=ACCIDENTAL_W[0]):
    h, w = comp["h"] / unit, comp["w"] / unit
    return ACCIDENTAL_H[0] <= h <= ACCIDENTAL_H[1] and narrowest <= w <= ACCIDENTAL_W[1]


def _holes(patch):
    """How many regions of background *patch* encloses."""
    _labels, count = ndimage.label(ndimage.binary_fill_holes(patch) & ~patch)
    return count


def _whole_accidental(comp, unit, narrowest=ACCIDENTAL_W[0]):
    """Accidental sized, and closed: every accidental encloses some background.

    The square of a sharp, the parallelogram of a natural, the bowl of a flat.  Nothing
    else this size and shape does.  A stem with a notehead's edge on it is the right
    height and width and has no hole, and classify_accidental, which only ever chooses
    between the three kinds, called two of them sharps on one page: one sat first after
    the bass clef, its letter happened to be F, and the whole page was read in G major.
    """
    return _accidental_sized(comp, unit, narrowest) and _holes(comp["patch"]) > 0


def _trim_whiskers(comp, thickness):
    """*comp* cut down to its glyph columns, or None when nothing is left.

    The trimmed box matters beyond the width test: detect_noteheads drops noteheads
    that overlap an accidental, so an inflated box reaching half a staff space past the
    ink could swallow the very note the accidental belongs to.
    """
    span = _glyph_columns(comp["patch"], thickness)
    if span is None:
        return None
    left, right = span
    return dict(comp, patch=comp["patch"][:, left:right + 1],
                x0=comp["x0"] + left, x1=comp["x0"] + right + 1, w=right - left + 1)


def _crop_to_ink(patch):
    """*patch* cut to its inked rows and columns: ``(sub, row_offset, col_offset)``."""
    rows = np.flatnonzero(patch.any(axis=1))
    cols = np.flatnonzero(patch.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return None
    return (patch[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1], int(rows[0]), int(cols[0]))


def _halves_at(comp, cut, unit, thickness):
    """*comp* cut at column *cut*, if both sides are whole accidentals.  Else None.

    Whole is the operative word.  A hollow notehead cut down the middle is a C with no
    enclosed background, so the closed-shape test in _whole_accidental keeps a chord of
    whole notes from passing as two accidentals, which a size test alone let through.
    It also grades the cut itself: a cut through a sharp opens the square, so only a
    cut between two glyphs leaves both halves closed.
    """
    patch = comp["patch"]
    halves = []
    for c0, c1 in ((0, cut), (cut, patch.shape[1])):
        cropped = _crop_to_ink(patch[:, c0:c1])
        if cropped is None:
            return None
        sub, dy, dx = cropped
        half = _trim_whiskers({
            "x0": comp["x0"] + c0 + dx, "x1": comp["x0"] + c0 + dx + sub.shape[1],
            "y0": comp["y0"] + dy, "y1": comp["y0"] + dy + sub.shape[0],
            "w": sub.shape[1], "h": sub.shape[0], "patch": sub,
        }, thickness)
        if half is None or not _whole_accidental(half, unit, CASCADE_HALF_W_MIN):
            return None
        half["cascade"] = True
        halves.append(half)
    return halves


def _split_cascade(comp, unit, thickness):
    """Two accidentals that touch, cut back into two.  None if that is not what this is.

    A close voicing writes its accidentals in a diagonal cascade, and two of them that
    touch are one connected component: about twice the ink of one, wider than one, and
    taller than one because they are staggered.  Every such component used to fail the
    size test and take both accidentals with it, and on a page of jazz voicings that was
    most of the accidentals on the page.

    The parent has to be at least one accidental tall, because two staggered accidentals
    cannot be shorter than one: that alone rejects a 2.1 space tall smudge that
    otherwise cut into two plausible "sharps".

    Every cut across the middle is tried and the thinnest one that leaves two whole
    accidentals wins.  Not just the thinnest column: the gap between a sharp's own two
    strokes is as thin as the gap between two sharps, and on the first page this met,
    argmin picked the former and cut a sharp in half.
    """
    patch = comp["patch"]
    h, w = patch.shape
    if not (2.6 <= h / unit <= 5.2 and 1.5 <= w / unit <= 2.5):
        return None
    cols = patch.sum(axis=0)
    best = None
    for cut in range(int(w * 0.3), int(w * 0.7)):
        halves = _halves_at(comp, cut, unit, thickness)
        if halves and (best is None or cols[cut] < cols[best[0]]):
            best = (cut, halves)
    return best[1] if best else None


def detect_accidentals(mask_ns, band, unit, x_from=0, thickness=1):
    """Every flat, sharp and natural in a staff's band, left to right."""
    region = mask_ns[band[0]:band[1], x_from:]
    found = []
    for comp in _components(region, int(unit * unit * 0.12)):
        # Measure, classify and place each glyph on its own ink.  Whiskers first, so a
        # lone sharp that only looked wide is accepted as one and never offered to the
        # split, whose one false positive is exactly a lone sharp cut down the middle.
        comp = _trim_whiskers(comp, thickness)
        if comp is None:
            continue
        if _whole_accidental(comp, unit):
            candidates = [comp]
        else:
            candidates = _split_cascade(comp, unit, thickness) or []
        # A pair stands or falls together: one half that does not classify means the
        # cut was wrong, not that the other half is an accidental.
        kinds = [classify_accidental(c) for c in candidates]
        if not kinds or any(kind is None for kind, _ in kinds):
            continue
        for cand, (kind, offset) in zip(candidates, kinds):
            found.append({
                "kind": kind,
                "x": x_from + (cand["x0"] + cand["x1"]) / 2.0,
                "x0": x_from + cand["x0"], "x1": x_from + cand["x1"],
                "y": band[0] + cand["y0"] + offset,
                "y0": band[0] + cand["y0"], "y1": band[0] + cand["y1"],
                "h": cand["h"],
                "cascade": cand.get("cascade", False),
            })
    found.sort(key=lambda a: a["x"])
    return found


def detect_stems(mask_ns, band, unit):
    """Tall thin verticals: note stems and barlines.

    Found by eroding vertically rather than by classifying components, because a stem
    is joined to the noteheads it carries and the three are one connected component.
    Anything that survives an erosion two staff spaces tall is a stem or a barline;
    a notehead is one space tall and disappears.
    """
    region = mask_ns[band[0]:band[1], :]
    height = max(3, int(round(unit * 1.7)))
    cores = ndimage.binary_erosion(region, structure=np.ones((height, 1)))
    if not cores.any():
        return []
    labels, count = ndimage.label(cores)
    stems = []
    for index, box in enumerate(ndimage.find_objects(labels), start=1):
        if box is None:
            continue
        ys, xs = box
        if xs.stop - xs.start > unit * 0.55:
            continue
        span = (ys.stop - ys.start) + height        # erosion trims half at each end
        stems.append({
            "x": (xs.start + xs.stop - 1) / 2.0,
            "y0": band[0] + ys.start - height / 2.0,
            "y1": band[0] + ys.stop + height / 2.0,
            "h": span,
            "kind": "barline",     # decided once the noteheads are known
            "barline": True,
        })
    stems.sort(key=lambda s: s["x"])
    return stems


# ---------------------------------------------------------------------- noteheads

def _ledger_ok(mask, x, staff, unit, thickness, k):
    """Does the ledger line a note outside the staff needs actually exist?

    Notes on the staff, and in the single space just outside it, need no ledger.
    Anything further out does, and requiring it is what stops chord symbols, lyrics
    and fingerings from being read as notes.

    The check samples the original mask just clear of the notehead on either side,
    because the notehead itself interrupts the ledger line: measuring the ledger as a
    connected horizontal run finds two short stubs, not a line.
    """
    if -1 <= k <= 9:
        return True
    nearest = k if k % 2 == 0 else (k - 1 if k > 0 else k + 1)
    # Ledger lines come in an unbroken run out from the staff, so check the whole run.
    # A single dark smudge at the right height, which is what a photo's edge or a stray
    # mark looks like, cannot fake five of them.
    rungs = range(10, nearest + 1, 2) if nearest > 0 else range(-2, nearest - 1, -2)
    return all(_ledger_row_ok(mask, x, staff, unit, thickness, rung) for rung in rungs)


def _ledger_row_ok(mask, x, staff, unit, thickness, k):
    y = pitches.step_to_y(
        pitches.CLEF_BOTTOM_LINE_STEP["treble"] + k, staff["lines"], "treble")
    reach = max(1, int(round(thickness * 1.4)))
    lo, hi = int(round(y)) - reach, int(round(y)) + reach + 1
    if lo < 0 or hi > mask.shape[0]:
        return False
    band = mask[lo:hi, :].any(axis=0)

    # A ledger line is a short horizontal run through the notehead's column.  Its
    # length is engraver's choice, a little wider than the notehead, so measure the
    # run rather than probing fixed offsets that may fall off either end of it.
    column = int(round(x))
    if not (0 <= column < band.size) or not band[column]:
        return False
    left = right = column
    span = int(unit * 8)
    while left > 0 and band[left - 1] and column - left < span:
        left -= 1
    while right + 1 < band.size and band[right + 1] and right - column < span:
        right += 1
    if right - left + 1 > unit * 8.0:
        return False
    # A ledger line always sticks out past the notehead it carries.  Measuring only the
    # total run would let a notehead's own body, which is 1.3 spaces wide, vouch for a
    # ledger that was never drawn.
    reach_out = max(column - left, right - column)
    if reach_out < unit * 0.78:
        return False
    # And the part that sticks out has to be a line.  A whole note is wider than the
    # 1.3 spaces the test above allows for, so when a rung falls on its rim its own
    # walls reach far enough to pass: that is how a right-hand whole note two ledgers
    # below the treble was claimed by the bass as a note one ledger above it, with
    # the note's own bottom as the ledger.  A ledger line is as thick as a staff line
    # and a notehead's wall is half a space tall, so somewhere in the stub, past where
    # any notehead reaches, the ink has to be thin.  Either side will do: the other
    # may have a displaced second or an accidental sitting on it.
    limit = max(3, int(round(thickness * LEDGER_STUB_THICKNESS)))
    footprint = int(round(unit * 0.7))
    for columns in (range(left, column - footprint + 1),
                    range(column + footprint, right + 1)):
        if any(0 < _ink_height(mask, lo, hi, c) <= limit for c in columns):
            return True
    return False


def _ink_height(mask, lo, hi, column):
    """Height of the ink that passes through rows *lo*..*hi* at *column*, 0 if none."""
    col = mask[:, column]
    rows = np.flatnonzero(col[lo:hi])
    if rows.size == 0:
        return 0
    top, bottom = lo + int(rows[0]), lo + int(rows[-1])
    while top > 0 and col[top - 1]:
        top -= 1
    while bottom + 1 < col.size and col[bottom + 1]:
        bottom += 1
    return bottom - top + 1


def _thick_run(row, x):
    """Width of the unbroken ink containing *x* in *row*, in pixels."""
    if not row[x]:
        return 0
    left = right = x
    while left > 0 and row[left - 1]:
        left -= 1
    while right + 1 < len(row) and row[right + 1]:
        right += 1
    return right - left + 1


def _longest_run(column, tolerate):
    """Longest run of ink down *column*, treating gaps of *tolerate* or fewer as ink.

    Staff line removal can nick a stem where a line crossed it; a nick is never wider
    than the line was thick, and must not read as the stem ending.
    """
    edges = np.diff(np.r_[0, column.astype(int), 0])
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    best = 0
    for i, start in enumerate(starts):
        end = ends[i]
        j = i
        while j + 1 < len(starts) and starts[j + 1] - ends[j] <= tolerate:
            j += 1
            end = ends[j]
        best = max(best, end - start)
    return best


def _has_stem(patch, thickness):
    """Is there a thin stroke running unbroken down at least 80% of *patch*'s height?

    Unbroken is the point.  A fused 4/4 has its two uprights in the same columns, and
    summed they hold as much ink as a stem does; what they never make is one run.  A
    test with exactly that glyph is what caught the version that only counted ink.
    """
    height = patch.shape[0]
    stemlike = np.zeros(patch.shape[1], dtype=bool)
    for x in range(patch.shape[1]):
        column = patch[:, x]
        if column.sum() >= 0.8 * height:
            stemlike[x] = _longest_run(column, thickness) >= 0.8 * height
    edges = np.diff(np.r_[0, stemlike.astype(int), 0])
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return any(end - start <= 2 * thickness for start, end in zip(starts, ends))


def time_signature_edge(mask_ns, staff, band, unit, x_from, thickness=1):
    """Right edge of the time signature, or *x_from* if there is none.

    Digits score well enough against a notehead outline to be read as notes, so they
    have to be excluded by position rather than left to the shape test.  What identifies
    them is structure, not size: two glyphs of the same size at the same x, straddling
    the middle line symmetrically, in the slot right after the key signature.  Their
    proportions and ink density vary a lot between music fonts and under a camera, so
    leaning on those instead makes this work for one font and fail for the next.

    Engravers also fuse the two digits into one blob at small sizes, so a single tall
    component centred on the middle line counts as well.
    """
    x_to = int(min(mask_ns.shape[1], x_from + unit * 8))
    if x_to <= x_from + unit:
        return x_from, None
    middle = (staff["lines"][0] + staff["lines"][-1]) / 2.0

    glyphs = []
    for comp in _components(mask_ns[band[0]:band[1], x_from:x_to], int(unit * unit * 0.10)):
        if not (unit * 1.3 <= comp["h"] <= unit * 5.2):
            continue
        if not (unit * 0.5 <= comp["w"] <= unit * 2.8):
            continue
        if comp["h"] / float(comp["w"]) > 3.2:
            continue        # taller and narrower than this is an accidental
        if comp["x0"] > unit * 3.5:
            continue        # a time signature follows the key signature immediately
        centre = band[0] + (comp["y0"] + comp["y1"]) / 2.0
        glyphs.append({
            "x0": comp["x0"], "x1": comp["x1"], "h": comp["h"],
            "cx": (comp["x0"] + comp["x1"]) / 2.0,
            "offset": (centre - middle) / unit,
            "patch": comp["patch"],
        })

    best = None
    for upper in glyphs:
        if upper["offset"] > -0.5:
            continue
        for lower in glyphs:
            if lower is upper or lower["offset"] < 0.5:
                continue
            if abs(upper["cx"] - lower["cx"]) > unit * 0.9:
                continue
            if abs(upper["offset"] + lower["offset"]) > unit * 0.02 + 0.9:
                continue        # the two must straddle the middle line evenly
            edge = max(upper["x1"], lower["x1"])
            start = min(upper["x0"], lower["x0"])
            if best is None or start < best[1]:
                best = (edge, start)
    if best is None:
        for glyph in glyphs:
            if abs(glyph["offset"]) <= 0.9 and glyph["h"] >= unit * 2.6:
                # A note on the middle line with its stem up is also a single tall
                # component centred on the middle line, and on one page it was the
                # first left-hand note, which this then walked the notehead search
                # straight past.  Digits have no stroke running most of their height:
                # the upright of a 4 is under half of a fused pair's height.
                if _has_stem(glyph["patch"], thickness):
                    continue
                if best is None or glyph["x0"] < best[1]:
                    best = (glyph["x1"], glyph["x0"])
    if best is None:
        return x_from, None
    return int(x_from + best[0] + unit * 0.3), float(x_from + best[1])


def _outline_response(region, erased, rims):
    """How much of a notehead outline is inked at every point, ignoring erased lines.

    A notehead centred in a staff space has its top and bottom rim lying along the two
    lines that bound the space, so removing those lines takes a slice out of the rim and
    the plain score drops about a tenth against a notehead sitting on a line.  Nobody
    knows what was under the line, so it is not counted either way: the erased pixels
    come out of the denominator instead of counting as blank.
    """
    best = None
    for rim in rims:
        total = float(rim.sum())
        inked = _correlate(region, rim)
        unknown = _correlate(erased, rim)
        # Never let the denominator collapse: a template sitting mostly on erased ink
        # would otherwise score on a sliver.
        usable = np.maximum(1.0 - unknown, 0.6)
        score = inked / usable
        best = score if best is None else np.maximum(best, score)
    return np.minimum(best, 1.0)


def detect_noteheads(mask, mask_ns, staff, band, unit, thickness, x_from,
                     accidentals=()):
    """Find noteheads by correlating a notehead outline against the cleaned mask.

    Matching an outline rather than classifying connected components means fused and
    fragmented noteheads both come out right: a stack of thirds is one blob but three
    correlation peaks, and a half note split by staff-line removal is still one peak.

    *accidentals* are excluded by position.  A sharp is two vertical strokes crossed by
    two horizontal ones, which is a notehead outline in every respect the template can
    measure, and no shape test is going to separate them.  An accidental is always clear
    of the notehead it belongs to, so ruling out its box costs nothing.
    """
    # Correlate over a little more than the band, then keep only peaks whose centre is
    # inside it.  On a grand staff the band handed in reaches across the gap to the
    # other staff, and _settle_gap sorts out what both staves find.  But a notehead
    # centred half a space inside the band's edge has its template hanging off the
    # end of the region, scores low, and is lost: on two pages that was a right-hand
    # note reaching down into the gap, the lowest note of its chord each time.
    # A staff ends with a barline, and the thick one that ends a piece is a notehead's
    # width across and the whole staff tall, so the correlator finds a ladder of notes
    # down it: seven on one page, and an extra event at the end of four others.  Nothing
    # real is ever there, because the barline is drawn after the last note.  Measured
    # over every image here, every notehead that lands within 1.2 spaces of the edge is
    # one of those, and the nearest real one is further in than that.
    #
    # Unless the frame cut the staff off, in which case its last ink is wherever the
    # photo stopped and a real notehead can sit right at it.
    edge = staff["x_range"][1]
    if edge < mask_ns.shape[1] - 2:
        edge -= unit * BARLINE_MARGIN
    else:
        edge = float("inf")

    pad = int(round(unit * 1.2))
    lo, hi = max(0, band[0] - pad), min(mask_ns.shape[0], band[1] + pad)
    region = mask_ns[lo:hi, :]
    if not region.any():
        return []

    # Ink a third of a space thick, which is what a notehead and a beam have and a
    # staff line or a ledger line does not.  Eroding harder than this is worse, not
    # better: a beam is only two thirds of a space thick on a photocopy, so taking half
    # a space off it leaves a two pixel sliver and the test lands between rows.
    thick_run = max(3, int(round(unit * 0.35)) | 1)
    thick = ndimage.binary_erosion(region, structure=np.ones((thick_run, 1)))

    rims, sides, core = notehead_kernels(unit)
    erased = mask[lo:hi, :] & ~region
    response = _outline_response(region, erased, rims)
    # The flanks of the outline are what separate a notehead from the gap between two
    # stacked a third apart.  That gap has the bottom of one notehead above it and the
    # top of the next below it, so the top and bottom arcs of the template are inked and
    # the whole thing scores as well as a real note; what it never has is ink out to
    # either side, because there is nothing there.
    flanks = _outline_response(region, erased, sides)
    inside = _correlate(region, core)

    # The vertical suppression radius has to sit between half a staff space and a whole
    # one.  Noteheads a third apart, the closest two ever stack, are a space apart and
    # must both survive; the midpoint between them must not, because the outline
    # template lands its top arc on one and its bottom arc on the other and scores well
    # on a gap with no note in it.  A window of 1.2 spaces puts the radius at 0.6, clear
    # of both.  Suppression needs the horizontal test too, or a notehead displaced
    # sideways for a second, half a space up and over a space across, would be lost.
    nms_h = max(3, int(round(unit * 1.2)) | 1)
    nms_w = max(3, int(round(unit * 0.85)) | 1)
    peaks = (response >= RESPONSE_MIN) & (
        response >= ndimage.maximum_filter(response, size=(nms_h, nms_w)) - 1e-6)
    labels, count = ndimage.label(peaks)
    if count == 0:
        return []

    centres = ndimage.center_of_mass(peaks, labels, range(1, count + 1))
    out = []
    for cy, cx in centres:
        if cx < x_from or cx > edge:
            continue
        y = lo + cy
        if not band[0] <= y < band[1]:
            continue        # the neighbouring staff's, and it will find it
        step, err = pitches.y_to_step(y, staff["lines"], "treble")  # clef applied later
        if err > unit * GRID_TOLERANCE:
            continue
        k = step - pitches.CLEF_BOTTOM_LINE_STEP["treble"]
        if not _ledger_ok(mask, cx, staff, unit, thickness, k):
            continue

        if any(box["x0"] - unit * 0.15 <= cx <= box["x1"] + unit * 0.15
               and box["y0"] - unit * 0.15 <= y <= box["y1"] + unit * 0.15
               for box in accidentals):
            continue
        iy, ix = int(round(cy)), int(round(cx))
        if flanks[iy, ix] < FLANK_MIN:
            continue
        # Measured on the row the notehead is placed on, not on the row the
        # correlation peaked: the peak lands anywhere within the blob, and on a beam
        # only two thirds of a space thick that is often a row where the eroded ink
        # has already run out.
        placed = int(round(pitches.step_to_y(step, staff["lines"], "treble"))) - lo
        if 0 <= placed < thick.shape[0]:
            if _thick_run(thick[placed], ix) > unit * BEAM_RUN_MAX:
                continue    # a beam, not a note: notehead-thick ink that runs on
        score = float(response[iy, ix])
        filled_share = float(inside[iy, ix])
        # Snap to the grid: the pitch is what the staff says, not where the blob's
        # centroid landed, and a half-space error is a whole scale degree.
        out.append({
            "x": float(cx),
            "y": float(pitches.step_to_y(step, staff["lines"], "treble")),
            "raw_y": float(y),
            "k": int(k),
            "step_err_px": round(float(err), 2),
            "w": unit * NOTEHEAD_W,
            "h": unit * NOTEHEAD_H,
            "hollow": bool(filled_share < 0.55),
            "filled_share": round(filled_share, 3),
            "response": round(score, 3),
            "confidence": round(min(0.99, 0.5 + score * 0.5), 2),
        })
    out.sort(key=lambda n: (n["x"], n["y"]))
    return _drop_overlaps(out, unit)


def _drop_overlaps(notes, unit):
    """Two peaks on one notehead: keep the stronger.  Real neighbours never collide."""
    kept = []
    for note in sorted(notes, key=lambda n: -n["response"]):
        if any(abs(note["x"] - k["x"]) < unit * 0.55
               and abs(note["raw_y"] - k["raw_y"]) < unit * 0.45 for k in kept):
            continue
        kept.append(note)
    kept.sort(key=lambda n: (n["x"], n["y"]))
    return kept


# ------------------------------------------------------------------- key signature

def key_signature_run(accidentals, clef_end, unit):
    """The leading run of accidentals after the clef: the key signature.

    Taken as a run rather than by an x cutoff because the thing that follows a key
    signature is a time signature, whose position depends on how many accidentals came
    before it.  Walk right from the clef while the glyphs keep coming at signature
    spacing and keep being the same kind.
    """
    run, cursor, spacing = [], clef_end, unit * 2.6
    for acc in accidentals:
        if acc["x0"] - cursor > spacing:
            break
        # Key signatures are engraved with clear space between the glyphs.  Two that
        # touched came from a chord, and a chord's accidentals are the end of the run.
        if acc.get("cascade"):
            break
        spacing = unit * 1.8      # the first sits clear of the clef, the rest are tight
        if run and acc["kind"] != run[0]["kind"]:
            break
        if acc["kind"] == "natural" and not run:
            break        # a lone natural cancels a key, it does not declare one
        run.append(acc)
        cursor = acc["x1"]
        if len(run) == 7:
            break
    return run


#: How far a signature's accidental may sit from the position it must be at, in staff
#: spaces, before the run is not a signature.
#:
#: A key signature is a rigid template.  Given the clef and whether the run is sharps or
#: flats, every accidental's position is fixed, so the question worth asking is how well
#: the run fits that template, not what letter each glyph is nearest.  Asking for the
#: letter is the fragile way round: on a photocopied page a flat's bowl measured 0.28
#: spaces high, which is over half a step, so the lone flat of a B flat signature came
#: out as a C, the run was thrown away, and the page read in C major with every B in it
#: natural and its E flats read as E.
#:
#: Measured over every image in this repo, a real signature fits to within 0.22 spaces,
#: and the one run that is not a signature misses by 1.70: a first chord's own
#: accidental standing where a signature would be, on a page in C.
KEY_SIGNATURE_FIT = 0.4


def _signature_miss(acc, letter, staff, clef):
    """Distance from *acc* to the nearest staff position carrying *letter*, in pixels."""
    step, _err = pitches.y_to_step(acc["y"], staff["lines"], clef)
    best = None
    for candidate in range(step - 4, step + 5):
        if pitches.step_letter(candidate) != letter:
            continue
        miss = abs(acc["y"] - pitches.step_to_y(candidate, staff["lines"], clef))
        if best is None or miss < best:
            best = miss
    return best if best is not None else float("inf")


def read_key_signature(run, staff, clef):
    """Turn the key signature's accidentals into a number of fifths.

    The run is fitted against the one pattern it could be, which catches a stray glyph
    swept in after the clef: two flats that are nowhere near B and E are not a key
    signature, and reporting low confidence is better than inventing one.
    """
    if not run:
        return 0, 0.9
    kinds = set(a["kind"] for a in run)
    if kinds not in ({"flat"}, {"sharp"}):
        return 0, 0.4
    order = pitches.FLAT_ORDER if "flat" in kinds else pitches.SHARP_ORDER
    count = min(len(run), 7)
    fifths = -count if "flat" in kinds else count
    allowed = staff["unit"] * KEY_SIGNATURE_FIT
    for acc, letter in zip(run[:count], order):
        if _signature_miss(acc, letter, staff, clef) > allowed:
            return fifths, 0.5
    return fifths, 0.92


def attach_accidentals(notes, accidentals, unit, staff, clef):
    """Give each notehead the accidental written for it, if any.

    Matching is by vertical position, never by left-to-right order: several
    accidentals before one chord are staggered horizontally, and the leftmost belongs
    to whichever note shares its line or space, not to the lowest note.
    """
    for acc in accidentals:
        step, _ = pitches.y_to_step(acc["y"], staff["lines"], clef)
        acc["step"] = step
    for note in notes:
        best, best_dx = None, None
        for acc in accidentals:
            dx = note["x"] - acc["x"]
            if not (unit * 0.35 <= dx <= unit * 3.6):
                continue
            if abs(acc["step"] - note["step"]) > 0:
                continue
            if best_dx is None or dx < best_dx:
                best, best_dx = acc, dx
        if best is not None:
            note["accidental"] = best["kind"]
            best["claimed"] = True


def apply_alterations(notes, fifths, barlines):
    """Resolve every notehead's alteration from the key and any written accidental.

    An accidental holds for the rest of its measure at that staff position, which is
    what the notation means and what a reader would do.  A natural cancels the key
    signature for the same span.
    """
    key = pitches.key_alterations(fifths)
    active = {}
    edges = sorted(barlines)
    measure = 0
    for note in sorted(notes, key=lambda n: n["x"]):
        while measure < len(edges) and note["x"] > edges[measure]:
            measure += 1
            active = {}
        written = note.get("accidental")
        if written == "flat":
            note["alter"] = -1
        elif written == "sharp":
            note["alter"] = 1
        elif written == "natural":
            note["alter"] = 0
        elif written == "double-flat":
            note["alter"] = -2
        elif written == "double-sharp":
            note["alter"] = 2
        elif note["step"] in active:
            note["alter"] = active[note["step"]]
        else:
            note["alter"] = key.get(pitches.step_letter(note["step"]), 0)
            note["from_key"] = note["alter"] != 0
        if written:
            active[note["step"]] = note["alter"]


# -------------------------------------------------------------------------- events

def group_by_stem(notes, stems, unit):
    """Cluster noteheads into chords.

    A stem is the notation's own statement of which noteheads sound together, so use
    it when there is one.  It also solves displaced seconds for free: the shunted
    notehead hangs off the same stem, and no amount of x clustering gets that right,
    because the displacement is exactly the gap between two separate notes.
    """
    playing = [s for s in stems if s.get("kind") == "stem"]
    groups = {}
    loose = []
    for note in notes:
        chosen = None
        for index, stem in enumerate(playing):
            dx = abs(note["x"] - stem["x"])
            if not (unit * 0.30 <= dx <= unit * 0.95):
                continue
            if not (stem["y0"] - unit * 0.6 <= note["y"] <= stem["y1"] + unit * 0.6):
                continue
            if chosen is None or dx < chosen[1]:
                chosen = (index, dx)
        if chosen is None:
            loose.append(note)
        else:
            groups.setdefault(chosen[0], []).append(note)

    clusters = list(groups.values())
    # Whole notes carry no stem, so fall back to proximity for those.
    loose.sort(key=lambda n: n["x"])
    for note in loose:
        for cluster in clusters:
            if any(abs(note["x"] - other["x"]) <= unit * 1.45
                   and abs(note["y"] - other["y"]) <= unit * 4.5 for other in cluster):
                cluster.append(note)
                break
        else:
            clusters.append([note])

    for cluster in clusters:
        cluster.sort(key=lambda n: -n["y"])
    clusters.sort(key=lambda c: min(n["x"] for n in c))
    return clusters


TIME_SIGNATURE_STEPS = (2, 6)   # the two staff positions digits are centred on


def drop_time_signature(per_staff, key_ends, unit):
    """Remove a leading cluster that is a time signature read as noteheads.

    The shape test on the glyphs themselves is the first line of defence, and on a
    clean render it is enough.  A blurred photo fuses the two digits into a blob that
    is neither one digit nor two, the shape test lets it through, and its outline
    scores well enough against a notehead to be read as one or two notes.

    Their position does not blur.  Time signature digits are centred on the two staff
    positions below and above the middle line, they sit in the slot immediately after
    the key signature, and they are never the only thing on the staff.  A cluster that
    is all three of those is a time signature.
    """
    for position, clusters in per_staff.items():
        if len(clusters) < 2:
            continue            # never delete the only thing found
        cluster = clusters[0]
        if not set(note["k"] for note in cluster) <= set(TIME_SIGNATURE_STEPS):
            continue
        x = float(np.mean([note["x"] for note in cluster]))
        if x - key_ends.get(position, 0.0) > unit * 4.0:
            continue            # too far right to be in the time signature's slot
        if x > min(float(np.mean([n["x"] for n in c])) for c in clusters[1:]) - unit:
            continue
        per_staff[position] = clusters[1:]


def align_across_staves(per_staff, unit):
    """Merge each staff's chords into shared events, left to right.

    A grand staff is two staves of one instrument: what the right hand plays at an x
    and what the left hand plays at the same x are one thing the player reads, and the
    whole product is built on saying what that thing is.
    """
    entries = []
    for staff_index, clusters in per_staff.items():
        for cluster in clusters:
            entries.append({
                "staff": staff_index,
                "notes": cluster,
                "x": float(np.mean([n["x"] for n in cluster])),
                "x0": float(min(n["x"] for n in cluster)),
                "x1": float(max(n["x"] for n in cluster)),
            })
    entries.sort(key=lambda e: e["x"])

    events, current = [], []
    for entry in entries:
        if current and entry["x"] - current[-1]["x"] > unit * 2.0:
            events.append(_one_part_per_staff(current))
            current = []
        current.append(entry)
    if current:
        events.append(_one_part_per_staff(current))
    return events


def _one_part_per_staff(entries):
    """Fold entries from the same staff together.

    Two chords from one staff landing in the same event happens with wide intervals,
    where the two noteheads are far enough apart vertically to be clustered separately
    and close enough in x to be simultaneous.  They are simultaneous, so they are one
    chord.  Leaving them as two parts breaks an invariant everything downstream leans
    on: a correction is resolved onto the one part a staff has in an event, and a
    second part would take a corrected pitch onto the wrong notehead.
    """
    merged = {}
    for entry in entries:
        held = merged.get(entry["staff"])
        if held is None:
            merged[entry["staff"]] = dict(entry)
            continue
        held["notes"] = sorted(held["notes"] + entry["notes"], key=lambda n: -n["y"])
        held["x0"] = min(held["x0"], entry["x0"])
        held["x1"] = max(held["x1"], entry["x1"])
        held["x"] = float(np.mean([n["x"] for n in held["notes"]]))
    return [merged[key] for key in sorted(merged)]


def mark_barlines(stems, notes, unit):
    """Sort the verticals into stems, barlines and neither.

    Length does not separate a stem from a barline: the stem of a five note chord is
    longer than a barline.  Two things do.  A barline has no notehead beside it.  And a
    stem sticks out past the noteheads it carries, which is what tells it apart from the
    thick left and right walls of a chord of whole notes, since those survive the same
    vertical erosion and are otherwise the right size and shape.
    """
    for stem in stems:
        attached = [note for note in notes
                    if abs(note["x"] - stem["x"]) <= unit * 0.95
                    and stem["y0"] - unit <= note["y"] <= stem["y1"] + unit]
        if not attached:
            stem["kind"] = "barline"
        else:
            lowest = max(note["y"] for note in attached)
            highest = min(note["y"] for note in attached)
            reaches = (stem["y1"] - lowest >= unit * 1.2
                       or highest - stem["y0"] >= unit * 1.2)
            stem["kind"] = "stem" if reaches else "other"
        stem["barline"] = stem["kind"] == "barline"
    return stems


def duration_hint(notes, stems, unit):
    """What the notehead and its stem say about how long the note is.

    Deliberately stops at "quarter or shorter".  Telling a quarter from an eighth means
    finding the flag or the beam, and measured across the corpus the ink beside a stem
    tip is 0.00 to 0.12 of the box for a flag and 0.00 to 0.17 for a plain quarter: the
    two do not separate, so any threshold labels some notes wrongly.  Saying "quarter or
    shorter" is true of every one of them, and the product promise is which notes these
    are, not how long they last.
    """
    hollow = all(note["hollow"] for note in notes)
    xs = [note["x"] for note in notes]
    stem = None
    for candidate in stems:
        if candidate.get("kind") != "stem":
            continue
        if min(abs(candidate["x"] - x) for x in xs) <= unit * 0.95:
            stem = candidate
            break
    if hollow:
        return ("half" if stem else "whole"), stem
    return "quarter-or-shorter", stem


def detect_dots(mask_ns, band, unit, notes):
    """An augmentation dot sits just right of its notehead, in the nearest space."""
    region = mask_ns[band[0]:band[1], :]
    blobs = []
    for comp in _components(region, max(3, int(unit * unit * 0.02))):
        if comp["w"] > unit * 0.55 or comp["h"] > unit * 0.55:
            continue
        if comp["w"] < unit * 0.12 or comp["h"] < unit * 0.12:
            continue
        blobs.append(((comp["x0"] + comp["x1"]) / 2.0,
                      band[0] + (comp["y0"] + comp["y1"]) / 2.0))
    for note in notes:
        for bx, by in blobs:
            if unit * 0.55 <= bx - note["x"] <= unit * 1.9 and \
                    abs(by - note["y"]) <= unit * 0.75:
                note["dotted"] = True
                break
    return any(n.get("dotted") for n in notes)


# ----------------------------------------------------------------------- top level

def analyze(mask):
    """Read a whole page.  Returns ``(systems, warnings)`` in schema shape minus naming.

    Chord naming and the analysis document wrapper are the pipeline's job; everything
    here is measurement, so that this module can be tested against images alone.
    """
    warnings = []
    height, width = mask.shape
    thickness, unit = estimate_scale(mask)
    if not unit:
        return [], ["Could not find any staff lines. Try a closer, sharper photo."]

    strong_lines = staff_line_mask(mask, thickness, unit, reach=2.0)
    staves = find_staves(mask, thickness, unit)
    if not staves:
        return [], ["Could not find any staff lines. Try a closer, sharper photo."]
    if unit < RELIABLE_UNIT_PX:
        warnings.append(
            "The staves are small in this photo, about %d pixels between the lines. "
            "Below about %d the reading starts to miss notes, so move closer or fill "
            "the frame with one line of music and try again."
            % (round(unit), RELIABLE_UNIT_PX))

    cleaned = remove_staff_lines(mask, strong_lines, thickness, unit)
    grouping = group_systems(staves, mask, unit)
    for staff_indices in grouping:
        _line_up_left_edges([staves[i] for i in staff_indices])

    measured = []
    for index, staff in enumerate(staves):
        band = staff_band(staves, index, unit, height)
        measured.append({
            "staff": staff, "band": band,
            "clef_end": clef_end(cleaned, staff, band, unit),
        })

    systems = []
    for system_index, staff_indices in enumerate(grouping):
        members = [measured[i] for i in staff_indices]
        _assign_clefs(members)
        system = _read_system(mask, cleaned, strong_lines, members, staff_indices,
                              unit, thickness, system_index, width, height, warnings)
        if system["events"] or system["staves"]:
            systems.append(system)

    if not any(system["events"] for system in systems):
        warnings.append(
            "Found the staves but no notes. If the photo is blurry or angled, "
            "retake it flat on and filling the frame.")
    return systems, warnings


def _line_up_left_edges(staves):
    """The staves of one system start where the leftmost of them starts.

    They are drawn from a shared margin, so a measured edge further right than a
    sibling's is the measurement coming up short, not the staff.  It comes up short
    where the front matter is dense: a treble clef, a sharp and a 4/4 cut the staff
    lines into runs too short to count, and the span was measured as beginning after
    them, about a hundred pixels in.  clef_end searches the first seven spaces after
    this edge, so measured that way the clef was out of its window and the key
    signature was never looked for: three of four systems on the page read as no key.
    The bass staff beside it, whose clef leaves the top and bottom lines whole, had
    the edge right, so it says where the treble staff starts too.
    """
    if len(staves) < 2:
        return
    left = min(staff["x_range"][0] for staff in staves)
    for staff in staves:
        staff["x_range"] = [left, staff["x_range"][1]]


def _assign_clefs(members):
    """Name the clefs from the staves' positions, and the hands with them.

    A two-staff system is a piano grand staff: treble on top, bass below, right hand
    and left.  A lone staff is a treble staff, which is what a lead sheet is.  The
    clef is not read off the glyph, and it used to be.  The glyph test was right on
    clean pages and wrong on the pages that matter: the search window was anchored on
    a staff edge that measured a hundred pixels late, so it never held the clef, and
    whatever tall mark it did hold, the 4 of a time signature or the first chord, was
    measured as a clef and generally came out bass.  Two bass clefs on a grand staff
    were then settled by which was the less confident misreading, and one page swapped
    both hands on one build and neither on another.  A misread clef is the worst
    failure the reader has, since every pitch on the staff is then a twelfth out and
    nothing about the result looks wrong.  What is given up is the non-standard case,
    a bass clef on a lone staff or a treble-treble pair, which the app was never for.

    The confidence is 1.0 because it is a rule and not a measurement; the field is
    kept because stored readings and the schema have it.
    """
    for position, member in enumerate(members):
        member["clef"] = "bass" if len(members) == 2 and position == 1 else "treble"
        member["clef_conf"] = 1.0
    if len(members) == 2:
        members[0]["hand"], members[1]["hand"] = "right", "left"
    else:
        for member in members:
            member["hand"] = None


def _search_bands(members, unit):
    """Where each staff of a system looks for noteheads.

    A lone staff looks in its own band.  The two staves of a grand staff each look
    across the whole gap between them, to GAP_MARGIN short of the other's outer line
    and never past STAFF_REACH, and a notehead both find goes to the nearer one.

    The midpoint alone gets the gap wrong.  It is right for a note one ledger out, but
    a right hand's note two ledgers below the treble is past the middle of a gap under
    four spaces wide, and the bass either claims it or throws it away: on one page
    that was every note the right hand had down there, six chords of fifty.  Each
    staff already demands the ledger lines a note that far out would need, at its own
    spacing, and only the staff the note belongs to has them.
    """
    if len(members) != 2:
        return [member["band"] for member in members]
    upper, lower = members[0]["staff"], members[1]["staff"]
    return [
        (members[0]["band"][0],
         int(min(lower["lines"][0] - unit * GAP_MARGIN,
                 upper["lines"][-1] + unit * STAFF_REACH))),
        (int(max(upper["lines"][-1] + unit * GAP_MARGIN,
                 lower["lines"][0] - unit * STAFF_REACH)),
         members[1]["band"][1]),
    ]


def _settle_gap(members, unit):
    """A notehead both staves of a grand staff found belongs to the nearer one.

    The two searches overlap across the gap, so a note either staff can account for,
    with the ledger lines it would need, is found twice.  That happens where the two
    staves' ledger rows coincide, which the midpoint has always settled and still does.
    A note only one staff can account for is that staff's, whichever side it is on.
    """
    if len(members) != 2:
        return
    upper, lower = members
    divide = upper["band"][1]
    for note in list(upper["notes"]):
        twin = next((other for other in lower["notes"]
                     if abs(other["x"] - note["x"]) < unit * 0.55
                     and abs(other["raw_y"] - note["raw_y"]) < unit * 0.45), None)
        if twin is None:
            continue
        if note["raw_y"] < divide:
            lower["notes"].remove(twin)
        else:
            upper["notes"].remove(note)


def _outside_signature(accidentals, key_used):
    """The accidentals that are not the key signature, matched by where they are.

    By position rather than identity because the accidentals a grand staff attaches
    come from a search wider than the band the signature was read in, and the same
    glyph found twice is two dicts.
    """
    used = set((round(a["x0"]), round(a["x1"])) for a in key_used)
    return [a for a in accidentals if (round(a["x0"]), round(a["x1"])) not in used]


def _read_system(mask, cleaned, strong_lines, members, staff_indices, unit, thickness,
                 system_index, width, height, warnings):
    per_staff, staff_docs, key_ends = {}, [], {}

    # Accidentals and leading runs for every staff first, because whether a run is a
    # signature is partly a question about the other staff.  Both staves of a grand
    # staff carry the same signature, so a sharp run on one and a flat run on the other
    # means neither is one: they are the first chord's own accidentals.  Nothing about
    # the run itself can tell.  On the page this came from, the treble's first chord had
    # a written F sharp 1.35 spaces after the clef, which is exactly where a G major
    # signature sits and reads as one at full confidence; only the bass's E flat at the
    # same x said otherwise, and the whole page was in G major until it was asked.
    found = []
    for member in members:
        accidentals = detect_accidentals(cleaned, member["band"], unit,
                                         member["clef_end"], thickness)
        found.append((accidentals, key_signature_run(accidentals, member["clef_end"],
                                                     unit)))
    kinds = set(run[0]["kind"] for _accidentals, run in found if run)
    signatures_disagree = len(kinds) > 1

    # Every staff reads its signatures and finds its noteheads before anything is made
    # of them, because on a grand staff which staff a notehead in the gap belongs to is
    # settled between the two, once both have looked.  The accidentals each staff
    # excludes from its search are everyone's: a flat in the gap is in one staff's band
    # and may be cut in half by the edge of the other's, and a glyph cut in half is not
    # an accidental to the classifier but is still a notehead outline to the correlator.
    searches = _search_bands(members, unit)
    for position, member in enumerate(members):
        member["search"] = searches[position]
        member["accidentals"] = (
            found[position][0] if member["search"] == member["band"]
            else detect_accidentals(cleaned, member["search"], unit, member["clef_end"],
                                    thickness))
    boxes = [acc for member in members for acc in member["accidentals"]]

    for position, member in enumerate(members):
        staff, band, clef = member["staff"], member["band"], member["clef"]
        _accidentals, key_used = found[position]
        if signatures_disagree:
            key_used = []

        # Order matters: the key signature has to be read before the time signature can
        # be looked for, because it is what decides where the time signature starts.
        fifths, key_conf = read_key_signature(key_used, staff, clef)
        if key_used and key_conf < 0.9:
            # The letters did not follow the fixed order, which read_key_signature
            # takes to mean a stray glyph was swept into the run.  On a page with no
            # signature and no time signature the stray glyph is the first chord's own
            # accidental, and consuming it lost the flat off an E flat.  Hand the run
            # back to the chord; there is no signature here.
            key_used, fifths, key_conf = [], 0, 0.9
        key_end = key_used[-1]["x1"] if key_used else member["clef_end"]
        time_end, _time_start = time_signature_edge(cleaned, staff, band, unit, key_end,
                                                    thickness)
        member["notes"] = detect_noteheads(mask, cleaned, staff, member["search"], unit,
                                           thickness, time_end, boxes)
        member["signature"] = (fifths, key_conf, key_used, key_end)

    _settle_gap(members, unit)

    for position, member in enumerate(members):
        staff, band, clef = member["staff"], member["band"], member["clef"]
        fifths, key_conf, key_used, key_end = member["signature"]
        notes = member["notes"]
        stems = detect_stems(cleaned, band, unit)

        bottom = pitches.CLEF_BOTTOM_LINE_STEP[clef]
        for note in notes:
            note["step"] = bottom + note["k"]
            note["alter"] = 0
            note["from_key"] = False
            note["accidental"] = None
            note["dotted"] = False

        mark_barlines(stems, notes, unit)
        free = _outside_signature(member["accidentals"], key_used)
        attach_accidentals(notes, free, unit, staff, clef)
        barlines = [s["x"] for s in stems if s["barline"]]
        apply_alterations(notes, fifths, barlines)
        detect_dots(cleaned, member["search"], unit, notes)

        cut_off = (staff["lines"][0] < unit * 1.2
                   or staff["lines"][-1] > height - unit * 1.2
                   or staff["x_range"][1] > width - 2)
        if cut_off:
            note = ("A staff runs off the edge of the photo, so notes there may be "
                    "missing. Retake it with a little margin around the music.")
            if note not in warnings:
                warnings.append(note)     # once, however many staves are clipped

        per_staff[position] = group_by_stem(notes, stems, unit)
        key_ends[position] = key_end
        member["stems"] = stems
        staff_docs.append({
            "index": position,
            "hand": member["hand"],
            "clef": clef,
            "clef_confidence": member["clef_conf"],
            "lines": [round(float(y), 2) for y in staff["lines"]],
            "unit": round(float(staff["unit"]), 2),
            "x_range": [int(staff["x_range"][0]), int(staff["x_range"][1])],
            "key_fifths": int(fifths),
            "key_confidence": round(float(key_conf), 2),
            "cut_off": bool(cut_off),
            # Kept because a written accidental holds only to the end of its measure,
            # so anything that re-derives a pitch later needs to know where the
            # measures are.  apply_alterations has them here and nothing downstream
            # could work them out again without the photo.
            "barlines": [round(float(x), 1) for x in barlines],
        })

    drop_time_signature(per_staff, key_ends, unit)

    _reconcile_keys(staff_docs, per_staff, members)

    events = []
    for index, entries in enumerate(align_across_staves(per_staff, unit)):
        parts = []
        for entry in sorted(entries, key=lambda e: e["staff"]):
            member = members[entry["staff"]]
            hint, _stem = duration_hint(entry["notes"], member["stems"], unit)
            parts.append({
                "staff": entry["staff"],
                "hand": member["hand"],
                "duration_hint": hint,
                "dotted": any(n.get("dotted") for n in entry["notes"]),
                "notes": [_note_doc(n) for n in
                          sorted(_one_per_step(entry["notes"]), key=lambda n: -n["y"])],
                "chord": None,
            })
        if not parts:
            continue
        xs = [n["x"] for part in parts for n in part["notes"]]
        events.append({
            "index": index,
            "x": round(float(np.mean(xs)), 1),
            "x_range": [int(min(xs) - unit), int(max(xs) + unit)],
            "confidence": round(float(np.mean(
                [n["confidence"] for part in parts for n in part["notes"]])), 2),
            "note": None,
            "parts": parts,
            "combined": None,
        })

    tops = [m["staff"]["lines"][0] for m in members]
    bottoms = [m["staff"]["lines"][-1] for m in members]
    return {
        "index": system_index,
        "y_range": [int(min(tops) - unit * 4), int(min(height, max(bottoms) + unit * 4))],
        "x_range": [int(min(m["staff"]["x_range"][0] for m in members)),
                    int(max(m["staff"]["x_range"][1] for m in members))],
        "grand": len(members) == 2,
        "cut_off": any(doc["cut_off"] for doc in staff_docs),
        "staves": staff_docs,
        "events": events,
    }


def _reconcile_keys(staff_docs, per_staff, members):
    """Make the staves of one system agree on the key signature.

    Both staves of a grand staff always carry the same key, so a disagreement means one
    of them lost an accidental to blur or to a glyph running into the clef.  Trusting
    the staff that read its signature most confidently recovers the other one, and this
    is the single cheapest accuracy win available on a photograph.
    """
    if len(staff_docs) < 2:
        return
    best = max(staff_docs, key=lambda d: (d["key_confidence"], abs(d["key_fifths"])))
    if best["key_confidence"] < 0.9:
        return
    for position, doc in enumerate(staff_docs):
        if doc["key_fifths"] == best["key_fifths"]:
            continue
        doc["key_fifths"] = best["key_fifths"]
        doc["key_confidence"] = round(best["key_confidence"] - 0.15, 2)
        notes = [note for cluster in per_staff.get(position, []) for note in cluster]
        apply_alterations(notes, best["key_fifths"],
                          [s["x"] for s in members[position]["stems"] if s["barline"]])


def _one_per_step(notes):
    """A chord has one notehead per line or space.  Keep the strongest of any doubles.

    Two noteheads at one step in one part is never what is on the page, it is one head
    read twice, or an accidental that went undetected and was read as a head in its own
    right.  Better detection removes most of these at the source; this is the guarantee
    that none of them reach the reader.  Across the whole corpus no part repeats a
    pitch, so nothing real is lost to it.
    """
    best = {}
    for note in sorted(notes, key=lambda n: -n.get("response", 0.0)):
        best.setdefault(note["step"], note)
    return list(best.values())


def _note_doc(note):
    """One measured notehead, in the schema's shape."""
    return pitches.make_note(
        note["step"], note["alter"], note["x"], note["y"],
        w=round(float(note["w"]), 1), h=round(float(note["h"]), 1),
        hollow=bool(note["hollow"]),
        accidental=note.get("accidental"),
        from_key=bool(note.get("from_key")),
        ledger=pitches.ledger_count(
            pitches.CLEF_BOTTOM_LINE_STEP["treble"] + note["k"], "treble"),
        step_err_px=float(note["step_err_px"]),
        confidence=float(note["confidence"]),
    )
