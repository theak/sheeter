"""Photo in, deskewed grayscale and a clean binary mask out.

Deliberately numpy + scipy + Pillow only.  OpenCV would pull in a 60MB wheel that
has no musl build, and everything here is a dozen lines of scipy.
"""

import io
import math

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

try:
    # iPhones shoot HEIC by default.  Safari converts on upload, but a file picked out
    # of Files does not, and Pillow cannot read it on its own.  Optional, because it is
    # the only dependency here that is not pure numerics.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:                                   # pragma: no cover
    HEIC_SUPPORTED = False

#: Long side of the image the pipeline actually works on.  Big enough that a
#: photo of one system lands at a staff space of 20px or more (which is what the
#: geometry needs), small enough to keep analysis under a couple of seconds.
MAX_DIM = 2200

#: Deskew search range.  Beyond this the photo is not "reasonably flat" and the
#: user is better served by a retake than by a heroic correction.
MAX_SKEW_DEG = 6.0


def load_gray(raw_bytes):
    """Decode upload bytes to a grayscale numpy array, honouring EXIF rotation.

    Returns ``(gray, orig_width, orig_height)``.  Transparent PNGs (common from
    scanning apps) are composited onto white first, otherwise alpha reads as black.
    """
    im = Image.open(io.BytesIO(raw_bytes))
    im = ImageOps.exif_transpose(im)
    orig_w, orig_h = im.size
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    return np.asarray(im.convert("L")), orig_w, orig_h


def downscale(gray, max_dim=MAX_DIM):
    """Shrink so the long side is at most *max_dim*.  Returns ``(gray, scale)``."""
    h, w = gray.shape
    longest = max(h, w)
    if longest <= max_dim:
        return gray, 1.0
    scale = max_dim / float(longest)
    im = Image.fromarray(gray).resize(
        (max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.LANCZOS
    )
    return np.asarray(im), scale


def otsu_threshold(gray):
    """Global threshold by Otsu's method.  Used for the skew search only."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    omega = np.cumsum(hist) / total
    mu = np.cumsum(hist * np.arange(256)) / total
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = np.where(denom > 0, (mu_t * omega - mu) ** 2 / denom, 0.0)
    return int(np.argmax(sigma_b))


def _skew_score(binary):
    """How sharply horizontal the dark content is.

    Staff lines are the only full-width dark structure in a page of music, so when
    the image is level they pile into a few rows and the row-sum profile spikes.
    Variance of that profile is therefore maximised at the correct angle.
    """
    profile = binary.sum(axis=1).astype(np.float64)
    return float(profile.var())


def estimate_skew(gray, max_deg=MAX_SKEW_DEG):
    """Best rotation in degrees to level the staff lines (counter-clockwise positive)."""
    thresh = otsu_threshold(gray)
    small, _ = downscale(gray, 900)          # the angle does not need full resolution
    binary = (small < thresh).astype(np.float32)
    if binary.sum() == 0:
        return 0.0

    def score_at(angle):
        if angle == 0.0:
            rotated = binary
        else:
            rotated = ndimage.rotate(binary, angle, reshape=False, order=0, cval=0.0)
        return _skew_score(rotated)

    # Coarse sweep then two refinement passes, so we look at ~35 angles instead of 480.
    best = max(np.arange(-max_deg, max_deg + 0.01, 1.0), key=score_at)
    for step in (0.25, 0.05):
        window = np.arange(best - step * 4, best + step * 4 + 1e-9, step)
        best = max(window, key=score_at)
    return float(round(best, 2))


def rotate(gray, angle):
    """Rotate a grayscale image, filling the corners with page white."""
    if abs(angle) < 0.01:
        return gray
    return ndimage.rotate(gray, angle, reshape=False, order=1, cval=255, mode="constant")


def run_lengths(mask):
    """For every True pixel, the length of the vertical run it belongs to."""
    height = mask.shape[0]
    depth = np.zeros(mask.shape, dtype=np.int32)
    depth[0] = mask[0]
    for y in range(1, height):
        depth[y] = np.where(mask[y], depth[y - 1] + 1, 0)
    runs = np.zeros(mask.shape, dtype=np.int32)
    runs[-1] = depth[-1]
    for y in range(height - 2, -1, -1):
        runs[y] = np.where(mask[y] & mask[y + 1], runs[y + 1], depth[y])
    return runs


def _run_start_lengths(mask):
    """Lengths of vertical runs, counted once each (at the run's top pixel)."""
    runs = run_lengths(mask)
    starts = mask.copy()
    starts[1:] &= ~mask[:-1]
    return runs[starts]


def estimate_scale(mask):
    """Measure ``(line_thickness, unit)`` in pixels from the image itself.

    The most common vertical black run on a page of music is the thickness of a staff
    line; the most common vertical white run is the staff space.  This is the standard
    OMR scale estimator and it does not care about resolution, cropping or margins.
    """
    black = _run_start_lengths(mask)
    white = _run_start_lengths(~mask)
    if black.size == 0 or white.size == 0:
        return None, None

    def mode(values, lo, hi):
        values = values[(values >= lo) & (values <= hi)]
        if values.size == 0:
            return None
        counts = np.bincount(values)
        return int(np.argmax(counts))

    limit = max(4, mask.shape[0] // 4)
    thickness = mode(black, 1, max(2, limit // 8))
    gap = mode(white, 3, limit)
    if not thickness or not gap or gap < 4:
        return None, None
    # The commonest white run is the gap *between* two staff lines, so the staff
    # space, measured line centre to line centre, is that gap plus one line.
    return float(thickness), float(gap + thickness)


def binarize(gray, unit_hint=None):
    """Adaptive threshold for the uneven lighting of a phone photo.

    Local mean minus a constant, which is Sauvola's cheap cousin and plenty for
    printed music.  Two details matter more than the method:

    The window must be several times a notehead.  A filled notehead is a solid block
    of ink wider than a small window, so a window that fits inside one sees a local
    mean of nearly zero, decides the middle of the notehead is background, and punches
    a hole through it.  That is why the staff space is measured first and fed back in.

    Ink darker than about half the global threshold is taken as ink whatever the local
    mean says, which is the same failure caught from the other side and costs nothing.
    """
    window = 61 if unit_hint is None else max(31, int(round(unit_hint * 5)) | 1)
    g = gray.astype(np.float32)
    local_mean = ndimage.uniform_filter(g, size=window, mode="nearest")
    spread = float(g.std())
    offset = max(6.0, min(25.0, spread * 0.35))
    global_threshold = otsu_threshold(gray)
    adaptive = g < (local_mean - offset)
    definite = gray < global_threshold * 0.55

    # On a clean render the page is uniform, the local mean sits right on the paper
    # value, and adaptive thresholding alone picks up sensor noise.  Intersecting with
    # a global threshold costs nothing and removes that.
    return (adaptive & (gray < global_threshold + 40)) | definite


def prepare(raw_bytes, max_dim=MAX_DIM):
    """Full front end: bytes in, ``(gray, mask, info)`` out.

    *gray* and *mask* share the same shape and are what every later stage sees.
    *info* records what was done so the analysis document can report it.
    """
    gray, orig_w, orig_h = load_gray(raw_bytes)
    gray, scale = downscale(gray, max_dim)
    angle = estimate_skew(gray)
    gray = rotate(gray, angle)
    mask = binarize(gray)
    _thickness, unit = estimate_scale(mask)
    if unit:
        mask = binarize(gray, unit_hint=unit)
    info = {
        "width": int(gray.shape[1]),
        "height": int(gray.shape[0]),
        "scale": float(scale),
        "deskew_deg": float(angle),
        "orig_width": int(orig_w),
        "orig_height": int(orig_h),
    }
    return gray, mask, info


def to_png_bytes(gray):
    """Encode the processed grayscale image for storage and for the overlay."""
    buf = io.BytesIO()
    Image.fromarray(gray).save(buf, format="PNG", optimize=True)
    return buf.getvalue()
