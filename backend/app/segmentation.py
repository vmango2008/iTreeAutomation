"""Land-cover segmentation via SLIC superpixels + a spectral/texture/shape classifier.

This is an object-based image analysis (GEOBIA) pipeline, the classical
alternative to a trained neural segmentation model, in two passes:

  1. Per-superpixel spectral triage. SLIC partitions the RGB image into a few
     hundred to a couple thousand superpixels; each gets a feature vector
     (hue/saturation/value, an excess-green vegetation index, and local
     texture). This resolves vegetation (tree vs. grass) and confidently
     water-like pixels directly (into their own classes), and marks
     everything else as a "hardscape" candidate (pavement, roofs, bare soil).

  2. Hardscape resolution using *local paved-area density*, not brightness.
     A single superpixel can't tell a parking lot from a rooftop from a
     shadowed roof edge by its own color -- but the amount of hardscape in
     a ~20m neighborhood around it is informative: the middle of a parking
     lot is almost entirely paved for tens of meters in every direction,
     while a house roof is usually surrounded by at least some yard. This
     density is a smoothed local average (scipy.ndimage.uniform_filter over
     the hardscape mask), not a connected-component flood fill -- an early
     version of this used flood fill and it back fired: a house's driveway
     commonly touches the street with no vegetation gap, so building, its
     driveway, and the road it fronts all merged into a single component
     and got classified as one giant "road". Density is local and bounded,
     so a rooftop reads as low-density (yard on most sides) even where it's
     pixel-connected to a driveway, and a dark shadowed strip of that same
     roof still reads as "surrounded by roof", not as a separate object --
     which is also what keeps it from peeling off into road or bare soil on
     its own.

Known limitation: with only RGB (no near-infrared band), distinguishing
road / building / bare soil purely by color is inherently harder than
distinguishing vegetation from non-vegetation, and the density heuristic
assumes a suburban-style layout (buildings with some yard/setback around
them). It breaks down in a dense mixed-use block where buildings sit flush
against parking structures and each other with no yard gap at all --
measured on one such Austin site, over 90% of the hardscape there read as
"high density", including an interior rooftop that scored *denser* than an
actual parking lot next to it (the lot's edge landscaping lowered its own
density more than the rooftop's neighboring roofs lowered its). Density is
a genuinely useful signal for typical single-building-plus-yard and
strip-mall-plus-lot layouts; it is not a reliable one inside a dense,
contiguous mixed-use development.

"Impervious other" ("other" in code/wire format, for backward compatibility)
targets school-site infrastructure that would otherwise be misread as one of
the six other classes: running tracks and synthetic-turf/hard-court tennis
and sports courts so far, each pulled out at the same stage as the class it
would otherwise be confused with:

  - Running tracks are rubberized and dyed a much more vivid, uniform
    red-orange than natural bare dirt, which is duller and mottled even when
    dry -- so a high-saturation hit in the soil hue range is read as track,
    not soil. Not yet validated against a real track (none was available to
    test against) -- validated only by checking it doesn't swallow real bare
    soil, which reads well under the saturation cutoff here.
  - Synthetic turf and painted hard courts read as vegetation by color
    (green passes the excess-green test same as grass) but read as a pale,
    washed-out green -- notably *less* saturated and *brighter* than real
    grass or turf, not more vivid as might be assumed, since hard-court
    surfacing is specifically formulated to reduce glare. Confirmed against
    a real school's tennis courts (six courts, several photographed at
    different sun/shadow angles): all read 59-73% of the scene's own
    vegetation-saturation median and 112-136% of its brightness median,
    while the school's actual turf football field and mowed grass both sat
    at or below that brightness median -- so a vegetation-candidate segment
    that's both unusually pale and unusually bright is read as a court, not
    grass.

Sidewalks were attempted the obvious way -- concrete reads brighter than
the asphalt beside it, so flag elongated hardscape that's brighter than the
scene's own hardscape-brightness median -- and dropped after testing on a
real campus: line markings, glare, and sun-bleached patches are common
enough in real imagery that they, not ordinary pavement, dominate that
median (over 40% of hardscape area read above 0.9 value on one real test
image), so the "brighter than typical" signal was either never true or
true almost everywhere depending on the scene. Sidewalks remain
manually-correctable only until a more local signal (e.g. brightness
relative to the immediately adjacent street, the way hardscape density
already looks at a local neighborhood rather than the whole scene) is
tried. Known gap: blue-surfaced courts are not specifically detected and
will likely still read as water, since reliably telling a blue court apart
from a pool or retention pond by color alone risks breaking real water
detection for an uncertain win.
"""

from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from skimage.color import rgb2gray, rgb2hsv
from skimage.measure import regionprops
from skimage.segmentation import relabel_sequential, slic

TREE, GRASS, BUILDING, ROAD, BARE_SOIL, WATER, OTHER = range(7)
CLASS_NAMES = ["tree", "grass", "building", "road", "bare_soil", "water", "other"]

CLASS_COLORS = {
    "tree": (0, 131, 0),
    "grass": (237, 161, 0),
    "building": (232, 123, 164),
    "road": (100, 116, 139),
    "bare_soil": (168, 118, 62),
    "water": (42, 120, 214),
    "other": (27, 175, 122),
}
OVERLAY_ALPHA = 150

EXG_VEG_THRESHOLD = 0.06

# Confidently water-like (blue) pixels are pulled out before hardscape
# triage; everything else non-vegetation (grays, tans, roof colors) is
# resolved by _classify_hardscape below.
WATER_HUE_RANGE = (180.0, 250.0)
WATER_SAT_MIN = 0.25

SOIL_HUE_RANGE = (12.0, 50.0)
SOIL_SAT_RANGE = (0.15, 0.65)

# Running tracks sit in the same hue band as bare soil but are dyed far more
# vividly than natural dirt ever gets, so a high-saturation hit there is
# read as track (impervious other), not soil -- checked before the soil
# range below, and independent of its 0.65 saturation cap so a very vivid
# track isn't accidentally caught by neither rule.
TRACK_HUE_RANGE = (5.0, 35.0)
TRACK_SAT_MIN = 0.45

# Hardscape (non-vegetation, non-water) resolution. Road covers any paved
# ground surface -- streets, parking lots, driveways, plazas -- and is
# deliberately decided by shape/density rather than brightness: a bright
# concrete parking lot is still road, not building, however light it is.
#
#  - elongated (aspect ratio) -> reads like a street even if it isn't
#    surrounded by continuous pavement (e.g. a narrow residential street
#    with yards on both sides).
#  - high local paved-area density -> reads like the interior of a large
#    lot, regardless of shape.
#  - otherwise, compact and not too densely paved around it -> building.
#  - soil color is only trusted when the segment ISN'T embedded in a dense
#    paved/roofed structure, since a dark roof in shadow can drift into the
#    same brown/tan hue range as bare dirt; requiring low surrounding
#    density keeps that collision from stealing real building pixels.
ELONGATION_ROAD_THRESHOLD = 2.4
HARDSCAPE_DENSITY_RADIUS_M = 20.0
PARKING_LOT_DENSITY_MIN = 0.82
SOIL_ISOLATION_DENSITY_MAX = 0.55
SOIL_SHADOW_VALUE_MAX = 0.35

# Tree-vs-grass combines two independent signals, either of which is enough:
#
# 1. Texture (see _local_roughness): real canopy has fine leaf-cluster
#    speckle that mowing stripes, mowed-lawn mottling, and broad cloud
#    shadow all lack even though those can rival or exceed canopy in
#    whole-segment brightness/color variance. Expressed relative to this
#    image's own whole-scene median texture, since raw texture scale varies
#    a lot by source imagery (a hazier vs. a crisper capture).
#
# 2. Darkness: in some regions (tuned here for Austin, TX per user
#    feedback) trees run noticeably darker than grass even where texture
#    alone is ambiguous (they can be close to the same color/texture in
#    places). Expressed relative to this image's own vegetation-only
#    median brightness, not an absolute cutoff, since exposure varies by
#    image.
TREE_TEXTURE_RELATIVE_MULT = 1.2
TREE_DARKNESS_RELATIVE_MULT = 0.75
TEXTURE_WINDOW = 5

# Synthetic turf and painted hard courts pass the excess-green vegetation
# test the same as real grass, but hard-court surfacing is formulated to
# reduce glare, so it reads distinctly paler than living turf: less
# saturated *and* brighter, not the more-vivid look that might be assumed --
# confirmed against a real school's tennis courts (see module docstring).
# Checked before the tree/grass split above, both relative to this image's
# own vegetation-only medians for the same reason tree/grass thresholds are.
COURT_SATURATION_RELATIVE_MAX = 0.75
COURT_BRIGHTNESS_RELATIVE_MIN = 1.1


@dataclass
class SegmentationResult:
    pixel_class: np.ndarray
    mask: np.ndarray


def rasterize_polygon_mask(width: int, height: int, pixel_coords: list[tuple[float, float]]) -> np.ndarray:
    mask_img = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask_img).polygon(pixel_coords, outline=255, fill=255)
    return np.array(mask_img, dtype=bool)


def _local_roughness(gray: np.ndarray, window: int = TEXTURE_WINDOW) -> np.ndarray:
    """Per-pixel local std-dev over a small window: fine-grained texture that
    responds to leaf-cluster speckle but not to broad smooth gradients like
    mowing stripes or cloud shadow (see module docstring for why whole-segment
    std-dev conflates the two)."""
    local_mean = ndi.uniform_filter(gray, size=window)
    local_sqmean = ndi.uniform_filter(gray ** 2, size=window)
    return np.sqrt(np.clip(local_sqmean - local_mean ** 2, 0.0, None))


def _axis_ratio(prop) -> float:
    if prop is not None and prop.axis_minor_length > 1e-6:
        return prop.axis_major_length / prop.axis_minor_length
    return 8.0 if prop is not None else 1.0


def _classify_vegetation(
    exg: float,
    texture: float,
    sat: float,
    val: float,
    tree_texture_threshold: float,
    tree_darkness_threshold: float,
    court_sat_threshold: float,
    court_brightness_threshold: float,
) -> int | None:
    if exg <= EXG_VEG_THRESHOLD:
        return None
    # Checked before tree/grass: a segment this pale and this bright isn't
    # live vegetation at all, regardless of how dark or textured it reads.
    if sat < court_sat_threshold and val > court_brightness_threshold:
        return OTHER
    if texture > tree_texture_threshold or val < tree_darkness_threshold:
        return TREE
    return GRASS


def _is_water_like(hue_deg: float, sat: float) -> bool:
    return WATER_HUE_RANGE[0] <= hue_deg <= WATER_HUE_RANGE[1] and sat > WATER_SAT_MIN


def _classify_hardscape(hue_deg: float, sat: float, val: float, elongation: float, local_density: float) -> int:
    # Checked before the soil range: a vivid hit here is a dyed rubber
    # track, not dirt -- independent of SOIL_SAT_RANGE's own cap so a very
    # vivid track can't slip through between the two rules.
    if TRACK_HUE_RANGE[0] <= hue_deg <= TRACK_HUE_RANGE[1] and sat >= TRACK_SAT_MIN:
        return OTHER

    is_soil_hue = SOIL_HUE_RANGE[0] <= hue_deg <= SOIL_HUE_RANGE[1] and SOIL_SAT_RANGE[0] <= sat <= SOIL_SAT_RANGE[1] and val > 0.15
    # A large uniform bare-dirt field is legitimately "not isolated" (it's
    # surrounded by more of itself), so isolation only overrides the soil
    # read when the segment is also dark -- that combination is what a
    # shadowed roof section looks like; plain bright/mid-toned soil is
    # trusted regardless of how much of its surroundings are hardscape.
    looks_like_roof_shadow = val < SOIL_SHADOW_VALUE_MAX and local_density >= SOIL_ISOLATION_DENSITY_MAX
    if is_soil_hue and not looks_like_roof_shadow:
        return BARE_SOIL

    if elongation > ELONGATION_ROAD_THRESHOLD or local_density > PARKING_LOT_DENSITY_MIN:
        return ROAD
    return BUILDING


def analyze(image: Image.Image, pixel_polygon: list[tuple[float, float]], meters_per_pixel: float) -> SegmentationResult:
    rgb = np.asarray(image.convert("RGB"), dtype=np.float64) / 255.0
    h, w, _ = rgb.shape

    mask = rasterize_polygon_mask(w, h, pixel_polygon)

    n_segments = int(np.clip((w * h) / 1800, 120, 2500))
    raw_labels = slic(rgb, n_segments=n_segments, compactness=8, sigma=1, start_label=1, channel_axis=-1)
    labels, _, _ = relabel_sequential(raw_labels)
    seg_ids = np.unique(labels)

    gray = rgb2gray(rgb)
    hsv = rgb2hsv(rgb)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    exg = 2 * g - r - b
    roughness = _local_roughness(gray)

    mean_hue = ndi.mean(hsv[..., 0], labels, seg_ids) * 360.0
    mean_sat = ndi.mean(hsv[..., 1], labels, seg_ids)
    mean_val = ndi.mean(hsv[..., 2], labels, seg_ids)
    mean_exg = ndi.mean(exg, labels, seg_ids)
    texture = ndi.mean(roughness, labels, seg_ids)

    props_by_label = {p.label: p for p in regionprops(labels)}
    elongation = np.array([_axis_ratio(props_by_label.get(int(sid))) for sid in seg_ids])

    veg_mask = mean_exg > EXG_VEG_THRESHOLD
    scene_texture_median = float(np.median(texture))
    tree_texture_threshold = scene_texture_median * TREE_TEXTURE_RELATIVE_MULT
    veg_value_median = float(np.median(mean_val[veg_mask])) if veg_mask.any() else float(np.median(mean_val))
    tree_darkness_threshold = veg_value_median * TREE_DARKNESS_RELATIVE_MULT
    veg_sat_median = float(np.median(mean_sat[veg_mask])) if veg_mask.any() else float(np.median(mean_sat))
    court_sat_threshold = veg_sat_median * COURT_SATURATION_RELATIVE_MAX
    court_brightness_threshold = veg_value_median * COURT_BRIGHTNESS_RELATIVE_MIN

    # Pass 1: vegetation / water / hardscape-candidate triage.
    class_ids = np.empty(len(seg_ids), dtype=np.uint8)
    is_hardscape = np.zeros(len(seg_ids), dtype=bool)
    for i in range(len(seg_ids)):
        veg_class = _classify_vegetation(
            mean_exg[i], texture[i], mean_sat[i], mean_val[i], tree_texture_threshold, tree_darkness_threshold, court_sat_threshold, court_brightness_threshold
        )
        if veg_class is not None:
            class_ids[i] = veg_class
        elif _is_water_like(mean_hue[i], mean_sat[i]):
            class_ids[i] = WATER
        else:
            is_hardscape[i] = True  # resolved in pass 2

    hardscape_lut = np.zeros(int(seg_ids.max()) + 1, dtype=bool)
    hardscape_lut[seg_ids] = is_hardscape
    hardscape_pixels = hardscape_lut[labels]

    window_px = max(3, round(2 * HARDSCAPE_DENSITY_RADIUS_M / meters_per_pixel))
    local_density_px = ndi.uniform_filter(hardscape_pixels.astype(np.float64), size=window_px)
    mean_density = ndi.mean(local_density_px, labels, seg_ids)

    # Pass 2: resolve hardscape segments into building / road / bare soil.
    for i in range(len(seg_ids)):
        if is_hardscape[i]:
            class_ids[i] = _classify_hardscape(mean_hue[i], mean_sat[i], mean_val[i], elongation[i], mean_density[i])

    lut = np.zeros(int(seg_ids.max()) + 1, dtype=np.uint8)
    lut[seg_ids] = class_ids
    pixel_class = lut[labels]

    return SegmentationResult(pixel_class=pixel_class, mask=mask)


def apply_overrides(
    base_pixel_class: np.ndarray,
    mask: np.ndarray,
    overrides: list[tuple[str, list[tuple[float, float]]]],
    to_px: Callable[[float, float], tuple[float, float]],
) -> SegmentationResult:
    """Paint manually-labeled regions on top of an automatic classification.

    `overrides` is an ordered list of (class_name, [(lon, lat), ...]) rings;
    later entries win over earlier ones (and all of them win over the
    automatic base) wherever they overlap, matching normal "layers" drawing
    semantics. The base classification is never mutated -- each call starts
    from it fresh, so re-sending an edited override list (add/remove/
    relabel a region) always recomputes cleanly rather than compounding.
    """
    height, width = base_pixel_class.shape
    pixel_class = base_pixel_class.copy()
    for class_name, polygon in overrides:
        class_idx = CLASS_NAMES.index(class_name)
        pixel_coords = [to_px(lon, lat) for lon, lat in polygon]
        override_mask = rasterize_polygon_mask(width, height, pixel_coords)
        pixel_class[override_mask] = class_idx
    return SegmentationResult(pixel_class=pixel_class, mask=mask)


def compute_stats(result: SegmentationResult) -> tuple[dict, int]:
    total = int(result.mask.sum())
    stats = {}
    for idx, name in enumerate(CLASS_NAMES):
        pixels = int(((result.pixel_class == idx) & result.mask).sum())
        percent = round(100.0 * pixels / total, 2) if total else 0.0
        stats[name] = {"percent": percent, "pixels": pixels}
    return stats, total


def render_overlay(result: SegmentationResult) -> Image.Image:
    h, w = result.pixel_class.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    for idx, name in enumerate(CLASS_NAMES):
        color = CLASS_COLORS[name]
        sel = (result.pixel_class == idx) & result.mask
        rgba[sel, 0] = color[0]
        rgba[sel, 1] = color[1]
        rgba[sel, 2] = color[2]
        rgba[sel, 3] = OVERLAY_ALPHA
    return Image.fromarray(rgba, mode="RGBA")
