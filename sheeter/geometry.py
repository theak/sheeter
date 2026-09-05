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

GEOMETRY_VERSION = "1"

__all__ = ["analyze", "estimate_scale", "GEOMETRY_VERSION"]

# Every constant here is a multiple of the staff space, never a pixel count.
NOTEHEAD_W = 1.30       # notehead width in staff spaces
NOTEHEAD_H = 0.95       # notehead height
RESPONSE_MIN = 0.72     # fraction of the ellipse template that must be inked
GRID_TOLERANCE = 0.34   # how far off the half-space grid a notehead may sit
STAFF_REACH = 6.0       # staff spaces above/below a staff that still belong to it

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


def _row_support(lines_mask, y, x0, x1):
    """Fraction of the given x span that is inked on row *y* (or either neighbour)."""
    y = int(round(y))
    lo, hi = max(0, y - 1), min(lines_mask.shape[0], y + 2)
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
            columns = np.flatnonzero(lines_mask[int(round(ys[0]))])
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

    Two signals: the vertical gap relative to the other gaps on the page, and whether
    ink actually runs continuously between the two staves at the left edge, which is
    what a brace or a shared barline looks like.
    """
    if not staves:
        return []
    if len(staves) == 1:
        return [[0]]

    gaps = [staves[i + 1]["lines"][0] - staves[i]["lines"][-1]
            for i in range(len(staves) - 1)]
    joined = [_joined_at_left(mask, staves[i], staves[i + 1], unit)
              for i in range(len(staves) - 1)]

    median_gap = float(np.median(gaps))
    systems, current = [], [0]
    for i, gap in enumerate(gaps):
        # A break is a gap clearly larger than the typical one, with no connecting ink.
        breaks = gap > max(median_gap * 1.6, unit * 5.0) and not joined[i]
        if len(staves) == 2:
            breaks = gap > unit * 11.0 and not joined[i]
        if breaks:
            systems.append(current)
            current = [i + 1]
        else:
            current.append(i + 1)
    systems.append(current)
    return systems


def _joined_at_left(mask, upper, lower, unit):
    """Is there continuous vertical ink between two staves near their left edge?"""
    x0 = max(0, int(min(upper["x_range"][0], lower["x_range"][0]) - unit * 1.5))
    x1 = int(min(mask.shape[1], x0 + unit * 3.0))
    y0 = int(round(upper["lines"][-1]))
    y1 = int(round(lower["lines"][0]))
    if y1 <= y0 or x1 <= x0:
        return False
    band = mask[y0:y1, x0:x1]
    # A brace or a through barline inks every row of the gap in at least one column.
    return bool((band.sum(axis=0) >= (y1 - y0) * 0.85).any())


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
    rims = []
    for width, height, shear in shapes:
        outer = ellipse_kernel(unit, width, height, shear).astype(bool)
        rim = outer & ~_shrink(outer, max(1, int(round(unit * 0.13))))
        rims.append(rim.astype(np.float32))
    ordinary = ellipse_kernel(unit, NOTEHEAD_W, NOTEHEAD_H, 0.36).astype(bool)
    core = _shrink(ordinary, max(1, int(round(unit * 0.20))))
    return rims, core.astype(np.float32)


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
    """
    staff = staves[index]
    top = staff["lines"][0] - unit * STAFF_REACH
    bottom = staff["lines"][-1] + unit * STAFF_REACH
    if index > 0:
        top = max(top, (staves[index - 1]["lines"][-1] + staff["lines"][0]) / 2.0)
    if index + 1 < len(staves):
        bottom = min(bottom, (staff["lines"][-1] + staves[index + 1]["lines"][0]) / 2.0)
    return max(0, int(top)), min(height, int(bottom))


def detect_clef(mask_ns, staff, band, unit):
    """Identify the clef at the left edge of a staff.

    A treble clef is enormous, spanning well past both outer lines; a bass clef is
    about two and a half spaces and hangs from the top line.  Height alone separates
    them cleanly, and there are only two clefs in scope.
    """
    x0, x1 = staff["x_range"]
    search = mask_ns[band[0]:band[1], x0:min(x1, int(x0 + unit * 7))]
    if not search.any():
        return None, 0.0, x0
    best = None
    for comp in _components(search, int(unit * unit * 0.25)):
        if comp["h"] < unit * 1.8:
            continue
        # A brace and the opening barline are taller than a bass clef and sit further
        # left, so height and position alone pick the wrong glyph on a grand staff.
        # Both are hairline thin; a clef of either kind is over two spaces wide.
        if comp["w"] < unit * 0.8:
            continue
        if best is None or comp["x0"] < best["x0"]:
            best = comp
    if best is None:
        return None, 0.0, x0
    ratio = best["h"] / unit
    if ratio >= 4.0:
        clef, confidence = "treble", min(1.0, 0.6 + (ratio - 4.0) * 0.15)
    elif ratio >= 1.9:
        clef, confidence = "bass", min(1.0, 0.55 + (3.2 - abs(ratio - 2.8)) * 0.12)
    else:
        return None, 0.0, x0
    return clef, round(float(confidence), 2), x0 + best["x1"]


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


def detect_accidentals(mask_ns, band, unit, x_from=0):
    """Every flat, sharp and natural in a staff's band, left to right."""
    region = mask_ns[band[0]:band[1], x_from:]
    found = []
    for comp in _components(region, int(unit * unit * 0.12)):
        ratio_h, ratio_w = comp["h"] / unit, comp["w"] / unit
        if not (1.85 <= ratio_h <= 3.7 and 0.22 <= ratio_w <= 1.25):
            continue
        kind, offset = classify_accidental(comp)
        if kind is None:
            continue
        found.append({
            "kind": kind,
            "x": x_from + (comp["x0"] + comp["x1"]) / 2.0,
            "x0": x_from + comp["x0"], "x1": x_from + comp["x1"],
            "y": band[0] + comp["y0"] + offset,
            "h": comp["h"],
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
    return reach_out >= unit * 0.78


def time_signature_edge(mask_ns, staff, band, unit, x_from):
    """Right edge of the time signature, or *x_from* if there is none.

    Digits score well enough against a notehead outline to be read as notes, so they
    have to be excluded by position rather than by shape.  A time signature is the
    first solid glyph after the key signature, it is centred on the middle line, and
    its two digits are as often fused into one blob as they are separate, so accept
    either a single tall component or a pair.
    """
    x_to = int(min(mask_ns.shape[1], x_from + unit * 8))
    if x_to <= x_from + unit:
        return x_from, None
    middle = (staff["lines"][0] + staff["lines"][-1]) / 2.0
    best = None
    for comp in _components(mask_ns[band[0]:band[1], x_from:x_to], int(unit * unit * 0.12)):
        if not (unit * 1.4 <= comp["h"] <= unit * 5.0):
            continue
        if not (unit * 0.75 <= comp["w"] <= unit * 2.4):
            continue
        # A sharp is about three and a half times as tall as it is wide, a flat and a
        # natural more; a digit, or two fused into one blob, is under three.
        if comp["h"] / float(comp["w"]) > 3.0:
            continue
        if comp["size"] / float(comp["w"] * comp["h"]) < 0.40:
            continue        # digits are solid; a stack of noteheads is mostly air
        centre = band[0] + (comp["y0"] + comp["y1"]) / 2.0
        if abs(centre - middle) > unit * 1.1:
            continue
        if comp["x0"] > unit * 3.5:
            continue        # a time signature follows the key signature immediately
        if best is None or comp["x0"] < best["x0"]:
            best = comp
    if best is None:
        return x_from, None
    return int(x_from + best["x1"] + unit * 0.3), float(x_from + best["x0"])


def detect_noteheads(mask, mask_ns, staff, band, unit, thickness, x_from):
    """Find noteheads by correlating a notehead outline against the cleaned mask.

    Matching an outline rather than classifying connected components means fused and
    fragmented noteheads both come out right: a stack of thirds is one blob but three
    correlation peaks, and a half note split by staff-line removal is still one peak.
    """
    region = mask_ns[band[0]:band[1], :]
    if not region.any():
        return []

    rims, core = notehead_kernels(unit)
    response = _correlate(region, rims[0])
    for extra in rims[1:]:
        response = np.maximum(response, _correlate(region, extra))
    inside = _correlate(region, core)

    nms_h = max(3, int(round(unit * 0.75)) | 1)
    nms_w = max(3, int(round(unit * 0.85)) | 1)
    peaks = (response >= RESPONSE_MIN) & (
        response >= ndimage.maximum_filter(response, size=(nms_h, nms_w)) - 1e-6)
    labels, count = ndimage.label(peaks)
    if count == 0:
        return []

    centres = ndimage.center_of_mass(peaks, labels, range(1, count + 1))
    out = []
    for cy, cx in centres:
        if cx < x_from:
            continue
        y = band[0] + cy
        step, err = pitches.y_to_step(y, staff["lines"], "treble")  # clef applied later
        if err > unit * GRID_TOLERANCE:
            continue
        k = step - pitches.CLEF_BOTTOM_LINE_STEP["treble"]
        if not _ledger_ok(mask, cx, staff, unit, thickness, k):
            continue

        iy, ix = int(round(cy)), int(round(cx))
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


def read_key_signature(run, staff, clef):
    """Turn the key signature's accidentals into a number of fifths.

    The letters are checked against the fixed order accidentals are written in, which
    catches a stray glyph swept into the run: two flats that are not B and E are not a
    key signature, and reporting low confidence is better than inventing one.
    """
    if not run:
        return 0, 0.9
    kinds = set(a["kind"] for a in run)
    if kinds not in ({"flat"}, {"sharp"}):
        return 0, 0.4
    letters = [pitches.step_letter(pitches.y_to_step(a["y"], staff["lines"], clef)[0])
               for a in run]
    count = min(len(letters), 7)
    expected = (pitches.FLAT_ORDER if "flat" in kinds else pitches.SHARP_ORDER)[:count]
    fifths = -count if "flat" in kinds else count
    if letters[:count] != expected:
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
            events.append(current)
            current = []
        # One staff never contributes twice to the same event.
        if any(other["staff"] == entry["staff"] for other in current) and \
                entry["x"] - current[-1]["x"] > unit * 0.9:
            events.append(current)
            current = []
        current.append(entry)
    if current:
        events.append(current)
    return events


def duration_hint(notes, stems, beams, unit):
    """Whole, half, quarter or eighth, from the notehead and its stem.

    Rhythm is secondary to the product promise, so this reports what can be seen
    without parsing beam groups: hollow or filled, stemmed or not, beamed or not.
    """
    hollow = all(n["hollow"] for n in notes)
    xs = [n["x"] for n in notes]
    stem = None
    for candidate in stems:
        if candidate.get("kind") != "stem":
            continue
        if min(abs(candidate["x"] - x) for x in xs) <= unit * 0.95:
            stem = candidate
            break
    if hollow:
        return ("half" if stem else "whole"), stem
    if stem is None:
        return "quarter", None
    for beam in beams:
        if abs(beam["x"] - stem["x"]) <= unit * 0.9:
            return "eighth", stem
    return "quarter", stem


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


def detect_beams(mask_ns, band, unit):
    """Thick horizontal bars: beams and flags, which mark an eighth or shorter."""
    region = mask_ns[band[0]:band[1], :]
    found = []
    for comp in _components(region, int(unit * unit * 0.2)):
        if comp["w"] < unit * 0.8 or comp["h"] > unit * 2.2:
            continue
        if comp["h"] < unit * 0.25:
            continue
        if comp["w"] / float(comp["h"]) < 1.4:
            continue
        found.append({"x": comp["x0"], "x1": comp["x1"],
                      "y": band[0] + (comp["y0"] + comp["y1"]) / 2.0})
    return found


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

    measured = []
    for index, staff in enumerate(staves):
        band = staff_band(staves, index, unit, height)
        clef, clef_conf, clef_end = detect_clef(cleaned, staff, band, unit)
        measured.append({
            "staff": staff, "band": band, "clef": clef, "clef_conf": clef_conf,
            "clef_end": clef_end,
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


def _assign_clefs(members):
    """Fill in any clef the glyph test could not call, and name the hands.

    A two-staff system is a piano grand staff: the top staff is the right hand and the
    bottom is the left.  That is the vocabulary the user asked for, so it is what the
    document carries; a lone staff has no hand and falls back to its clef.
    """
    for position, member in enumerate(members):
        if member["clef"] is None:
            member["clef"] = "treble" if position == 0 else "bass"
            member["clef_conf"] = 0.4
    if len(members) == 2:
        members[0]["hand"], members[1]["hand"] = "right", "left"
    else:
        for member in members:
            member["hand"] = None


def _read_system(mask, cleaned, strong_lines, members, staff_indices, unit, thickness,
                 system_index, width, height, warnings):
    per_staff, staff_docs, key_ends = {}, [], {}
    for position, member in enumerate(members):
        staff, band, clef = member["staff"], member["band"], member["clef"]
        stems = detect_stems(cleaned, band, unit)
        beams = detect_beams(cleaned, band, unit)
        accidentals = detect_accidentals(cleaned, band, unit, member["clef_end"])

        # Order matters: the key signature has to be read before the time signature can
        # be looked for, because it is what decides where the time signature starts.
        key_used = key_signature_run(accidentals, member["clef_end"], unit)
        fifths, key_conf = read_key_signature(key_used, staff, clef)
        key_end = key_used[-1]["x1"] if key_used else member["clef_end"]
        time_end, _time_start = time_signature_edge(cleaned, staff, band, unit, key_end)
        notes = detect_noteheads(mask, cleaned, staff, band, unit, thickness, time_end)

        bottom = pitches.CLEF_BOTTOM_LINE_STEP[clef]
        for note in notes:
            note["step"] = bottom + note["k"]
            note["alter"] = 0
            note["from_key"] = False
            note["accidental"] = None
            note["dotted"] = False

        mark_barlines(stems, notes, unit)
        free = [a for a in accidentals if a not in key_used]
        attach_accidentals(notes, free, unit, staff, clef)
        barlines = [s["x"] for s in stems if s["barline"]]
        apply_alterations(notes, fifths, barlines)
        detect_dots(cleaned, band, unit, notes)

        cut_off = (staff["lines"][0] < unit * 1.2
                   or staff["lines"][-1] > height - unit * 1.2
                   or staff["x_range"][1] > width - 2)
        if cut_off:
            warnings.append(
                "A staff runs off the edge of the photo, so notes there may be "
                "missing. Retake it with a little margin around the music.")

        per_staff[position] = group_by_stem(notes, stems, unit)
        key_ends[position] = key_end
        member["stems"], member["beams"] = stems, beams
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
        })

    drop_time_signature(per_staff, key_ends, unit)

    _reconcile_keys(staff_docs, per_staff, members)

    events = []
    for index, entries in enumerate(align_across_staves(per_staff, unit)):
        parts = []
        for entry in sorted(entries, key=lambda e: e["staff"]):
            member = members[entry["staff"]]
            hint, _stem = duration_hint(entry["notes"], member["stems"],
                                        member["beams"], unit)
            parts.append({
                "staff": entry["staff"],
                "hand": member["hand"],
                "duration_hint": hint,
                "dotted": any(n.get("dotted") for n in entry["notes"]),
                "notes": [_note_doc(n) for n in
                          sorted(entry["notes"], key=lambda n: -n["y"])],
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
