"""Silhouette diff of a built model against a reference image/drawing view.

Pure numpy (+ optional scipy / Pillow), no SolidWorks and no MCP imports, so it is unit-testable offline.
The flow the tool uses:

    model_mask = foreground_mask(model_rgb)            # standard-view capture, orthographic
    ref_mask   = foreground_mask(ref_rgb, fill=True)   # reference view, outline drawings get filled
    frame      = frame_from_bbox(model_mask, bbox_u_mm, bbox_v_mm)    # px-per-mm of the model capture
    reg        = register(ref_mask, model_mask)        # reference -> model pixel frame (scale + shift)
    result     = diff(model_mask, reg.mask, frame)     # IoU + discrepancy regions in view mm

Everything reported is an OVERLAP of two silhouettes, never a dimension: the reference's absolute scale
is whatever makes its outline fit the model's, so a uniformly wrong model size is NOT detectable here
(verify_against_interpretation's bbox row covers that when a real number exists).
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np

# View -> (screen-right axis, screen-up axis) in MODEL axes, with sign. FALLBACK ONLY: assumed from the default
# SolidWorks standard-view orientations, which the user may have redefined (IModelDoc2.GetStandardViewRotation
# remarks). export_image reports the measured axes (view_frame, from IModelView.Transform); use those.
VIEW_AXES = {
    "front": (("X", 1), ("Y", 1)),
    "back": (("X", -1), ("Y", 1)),
    "right": (("Z", -1), ("Y", 1)),
    "left": (("Z", 1), ("Y", 1)),
    "top": (("X", 1), ("Z", -1)),
    "bottom": (("X", 1), ("Z", 1)),
}

MIN_REGION_FRACTION = 0.0005      # a mismatch region smaller than this share of the silhouette area is noise
EDGE_SLOP_FRACTION = 0.005        # edge aliasing/rounding allowance, as a share of the silhouette's long side
ASPECT_TOLERANCE = 0.15           # bbox aspect-ratio difference above which registration is refused


class RefDiffError(Exception):
    """Carries a registry-style code so the tool can render FAILED | <code> | ..."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Frame:
    """Pixel <-> view-mm mapping of the MODEL capture. Origin is the silhouette bbox lower-left corner."""
    px_per_mm: float
    origin_px: tuple            # (x, y) pixel of the (u=0, v=0) corner, y down
    width_mm: float
    height_mm: float

    def to_mm(self, px: float, py: float) -> tuple:
        return ((px - self.origin_px[0]) / self.px_per_mm, (self.origin_px[1] - py) / self.px_per_mm)


@dataclass
class Registration:
    mask: np.ndarray            # the reference silhouette resampled into the model capture's pixel frame
    scale: float                # reference px -> model px
    aspect_error: float         # |ref aspect / model aspect - 1| (0 = identical proportions)


@dataclass
class DiffResult:
    iou: float
    model_only_frac: float      # share of the union where only the model has material
    ref_only_frac: float        # share of the union where only the reference has material
    regions: list = field(default_factory=list)
    xor: np.ndarray = None
    tolerance_mm: float = 0.0   # mismatch strips thinner than ~2x this are edge slop, not reported


def view_axes(view: str) -> tuple:
    try:
        return VIEW_AXES[view.strip().lower()]
    except KeyError:
        raise RefDiffError("BAD_VIEW", f"view must be one of {', '.join(VIEW_AXES)} (got {view!r}); isometric has no "
                                       "orthographic silhouette to compare") from None


def axes_from_frame(frame: dict | None) -> tuple | None:
    """((axis, sign), (axis, sign)) from export_image's measured view_frame {'right': '+X', 'up': '+Y', ...}."""
    def pick(text):
        if not isinstance(text, str) or len(text) != 2 or text[0] not in "+-" or text[1] not in ("X", "Y", "Z"):
            return None
        return text[1], 1 if text[0] == "+" else -1
    right, up = pick((frame or {}).get("right")), pick((frame or {}).get("up"))
    if right is None or up is None or right[0] == up[0]:
        return None
    return right, up


def view_extents_mm(view: str, bbox_size_mm: dict, axes: tuple | None = None) -> tuple:
    """(width_mm, height_mm) of the model in this view from {'X':sx,'Y':sy,'Z':sz}."""
    (ua, _), (va, _) = axes or view_axes(view)
    return float(bbox_size_mm[ua]), float(bbox_size_mm[va])


def _decode(png_or_bmp: bytes) -> np.ndarray:
    try:
        from PIL import Image
    except Exception as ex:  # noqa: BLE001
        raise RefDiffError("COMPARE_UNAVAILABLE", f"Pillow is required for image comparison ({ex})") from ex
    try:
        return np.asarray(Image.open(io.BytesIO(png_or_bmp)).convert("RGB"), dtype=np.int16)
    except Exception as ex:  # noqa: BLE001
        raise RefDiffError("REFERENCE_UNREADABLE", f"could not decode the image: {ex}") from ex


def foreground_mask(image: bytes | np.ndarray, tol: int = 28, fill: bool = False, clean_px: int = 0,
                    clean: bool = False, filtered: list | None = None) -> np.ndarray:
    """Boolean mask of what is NOT background.

    Background is estimated per row from the left/right image edge (handles SolidWorks' vertical gradient
    as well as a plain white page), so the object must not touch the left/right border. fill=True fills
    closed outlines (a line drawing is a silhouette only once its interior is filled). clean_px > 0 erodes
    then dilates that many pixels to drop thin leaders/dimension lines — also thins real slender features,
    so it is off unless asked for.

    clean=True is for a reference SHEET (scan/photo/raster drawing): the background comes from a wide, smoothed
    border strip on all four edges (a dark sheet frame cannot become the background), the threshold adapts to the
    paper noise so pale or coloured lines survive, and detached thin lines (dimension/extension lines, leaders),
    the sheet frame and specks are dropped before the fill. What was dropped is appended to `filtered`.
    """
    rgb = _decode(image) if isinstance(image, (bytes, bytearray)) else image.astype(np.int16)
    if rgb.ndim != 3 or rgb.shape[0] < 8 or rgb.shape[1] < 8:
        raise RefDiffError("REFERENCE_UNREADABLE", "image is too small or not a colour/greyscale raster")
    if clean:
        mask = _sheet_ink(rgb, tol, filtered if filtered is not None else [])
    else:
        edge = np.concatenate([rgb[:, :3], rgb[:, -3:]], axis=1)             # rows x 6 x 3
        bg = np.median(edge, axis=1)[:, None, :]                              # rows x 1 x 3
        mask = np.abs(rgb - bg).max(axis=2) > tol
    if fill:
        mask = _fill_holes(mask)
    if clean_px > 0:
        mask = _open(mask, clean_px)
    if mask.sum() < 0.0005 * mask.size:
        raise RefDiffError("REFERENCE_UNREADABLE", "found (almost) nothing but background — wrong crop, or the "
                           "object is lighter than the tolerance; pass a region around the view")
    return mask


def _sheet_background(rgb: np.ndarray) -> np.ndarray:
    """rows x 1 x 3 background: a flat page gets one colour (median of all four edge strips), a gradient gets a
    per-row median of wide left/right strips smoothed over rows (so a horizontal frame line cannot become it)."""
    h, w = rgb.shape[:2]
    kw, kh = max(8, w // 16), max(8, h // 16)
    sides = np.concatenate([rgb[:, :kw].reshape(-1, 3), rgb[:, -kw:].reshape(-1, 3),
                            rgb[:kh].reshape(-1, 3), rgb[-kh:].reshape(-1, 3)])
    flat = np.median(sides, axis=0)
    rows = np.median(np.concatenate([rgb[:, :kw], rgb[:, -kw:]], axis=1), axis=1)       # rows x 3
    win = max(9, (h // 10) | 1)                                  # wider than twice a frame line; truncated at the ends
    pad = np.pad(rows, ((win // 2, win // 2), (0, 0)), mode="constant", constant_values=np.nan)
    rows = np.nanmedian(np.stack([pad[i:i + h] for i in range(win)]), axis=0)
    if np.abs(rows - flat).max() <= 24:
        return np.broadcast_to(flat, (h, 1, 3)).astype(np.float64)
    return rows[:, None, :]


def _sheet_ink(rgb: np.ndarray, tol: int, filtered: list) -> np.ndarray:
    bg = _sheet_background(rgb)
    dist = np.abs(rgb - bg).max(axis=2)
    quiet = dist[dist <= tol]
    noise = float(np.percentile(quiet, 90)) if quiet.size else 0.0
    mask = dist > int(min(tol, max(10.0, 2.0 * noise + 4.0)))                  # clean paper: 10; noisy scan: up to tol
    try:
        from scipy import ndimage
    except Exception:  # noqa: BLE001
        filtered.append("component filter skipped (scipy missing)")
        return mask
    lab, n = ndimage.label(mask)
    if n < 2:
        return mask
    h, w = mask.shape
    counts = np.bincount(lab.ravel(), minlength=n + 1)
    thin = frame = specks = 0
    keep = np.ones(n + 1, dtype=bool)
    keep[0] = False
    lines = np.zeros(n + 1, dtype=bool)
    for i, slc in enumerate(ndimage.find_objects(lab), start=1):
        ys, xs = slc
        bh, bw = ys.stop - ys.start, xs.stop - xs.start
        area = float(bh * bw)
        if bw >= 0.9 * w and bh >= 0.9 * h and counts[i] / area < 0.15:
            keep[i], frame = False, frame + 1                                  # the sheet border: a hollow ring
        elif max(bh, bw) >= 0.04 * max(h, w) and counts[i] / area < 0.25:
            comp = lab[slc] == i                                               # strokes that enclose nothing:
            if ndimage.binary_fill_holes(comp).sum() - counts[i] <= 0.02 * area:      # dimension chains, leaders
                lines[i] = True
    if lines.any() and (keep & ~lines)[1:].any():
        keep &= ~lines
        thin = int(lines.sum())
    if keep.any():
        floor, small = 0.002 * int(counts[keep].max()), 0.025 * max(h, w)
        for i, (ys, xs) in enumerate(ndimage.find_objects(lab), start=1):
            if keep[i] and (counts[i] < floor or max(ys.stop - ys.start, xs.stop - xs.start) < small):
                keep[i], specks = False, specks + 1                            # text glyphs, dust
    for label, count in (("line work", thin), ("frame", frame), ("specks/text", specks)):
        if count:
            filtered.append(f"{label} x{count}" if label != "frame" else label)
    return keep[lab]


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    try:
        from scipy import ndimage
        return ndimage.binary_fill_holes(mask)
    except Exception:  # noqa: BLE001  (no scipy: flood the background from the border instead)
        bgm = ~mask
        reach = np.zeros_like(mask)
        reach[0, :], reach[-1, :], reach[:, 0], reach[:, -1] = bgm[0, :], bgm[-1, :], bgm[:, 0], bgm[:, -1]
        while True:
            grown = reach.copy()
            grown[1:, :] |= reach[:-1, :]
            grown[:-1, :] |= reach[1:, :]
            grown[:, 1:] |= reach[:, :-1]
            grown[:, :-1] |= reach[:, 1:]
            grown &= bgm
            if (grown == reach).all():
                return ~reach
            reach = grown


def _open(mask: np.ndarray, n: int) -> np.ndarray:
    try:
        from scipy import ndimage
        return ndimage.binary_opening(mask, iterations=n)
    except Exception:  # noqa: BLE001
        m = mask.copy()
        for _ in range(n):                                               # erode
            e = m.copy()
            e[1:, :] &= m[:-1, :]; e[:-1, :] &= m[1:, :]; e[:, 1:] &= m[:, :-1]; e[:, :-1] &= m[:, 1:]
            m = e
        for _ in range(n):                                               # dilate
            d = m.copy()
            d[1:, :] |= m[:-1, :]; d[:-1, :] |= m[1:, :]; d[:, 1:] |= m[:, :-1]; d[:, :-1] |= m[:, 1:]
            m = d
        return m


def bbox_px(mask: np.ndarray) -> tuple:
    """(x0, y0, x1, y1) inclusive pixel bounds of the True region."""
    ys, xs = np.nonzero(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def frame_from_bbox(model_mask: np.ndarray, width_mm: float, height_mm: float) -> Frame:
    """Scale the model capture from its known size: its silhouette bbox spans width_mm x height_mm."""
    if width_mm <= 0 or height_mm <= 0:
        raise RefDiffError("BAD_BBOX", f"model extents in this view are {width_mm} x {height_mm} mm")
    x0, y0, x1, y1 = bbox_px(model_mask)
    wpx, hpx = x1 - x0 + 1, y1 - y0 + 1
    sx, sy = wpx / width_mm, hpx / height_mm
    if abs(sx / sy - 1) > 0.08:
        raise RefDiffError("REGISTRATION_WEAK", f"the capture is not orthographic or not this view: silhouette "
                           f"{wpx}x{hpx}px vs model {width_mm:.1f}x{height_mm:.1f}mm (scale differs {abs(sx / sy - 1):.0%} "
                           "between axes)")
    s = (sx + sy) / 2
    return Frame(px_per_mm=s, origin_px=(x0, y1 + 1), width_mm=width_mm, height_mm=height_mm)


def _resample(mask: np.ndarray, scale: float, out_shape: tuple, shift: tuple) -> np.ndarray:
    """Nearest-neighbour: out[y, x] = mask[(y - sy)/scale, (x - sx)/scale]."""
    h, w = out_shape
    src_y = np.floor((np.arange(h) - shift[1]) / scale).astype(np.int64)
    src_x = np.floor((np.arange(w) - shift[0]) / scale).astype(np.int64)
    oky = (src_y >= 0) & (src_y < mask.shape[0])
    okx = (src_x >= 0) & (src_x < mask.shape[1])
    out = np.zeros(out_shape, dtype=bool)
    out[np.ix_(oky, okx)] = mask[np.ix_(src_y[oky], src_x[okx])]
    return out


def register(ref_mask: np.ndarray, model_mask: np.ndarray, aspect_tol: float = ASPECT_TOLERANCE) -> Registration:
    """Map the reference silhouette into the model capture's pixel frame by matching their bounding boxes.

    A uniform scale (fit the larger dimension) and a bbox-corner shift. Refuses (REGISTRATION_WEAK) when the
    two bounding boxes have different proportions: that is a wrong crop, wrong view or wrong overall size, and
    overlaying them would score the alignment, not the model.
    """
    rx0, ry0, rx1, ry1 = bbox_px(ref_mask)
    mx0, my0, mx1, my1 = bbox_px(model_mask)
    rw, rh = rx1 - rx0 + 1, ry1 - ry0 + 1
    mw, mh = mx1 - mx0 + 1, my1 - my0 + 1
    aspect_error = abs((rw / rh) / (mw / mh) - 1)
    if aspect_error > aspect_tol:
        raise RefDiffError(
            "REGISTRATION_WEAK",
            f"the reference outline is {rw}x{rh}px (aspect {rw / rh:.2f}) but the model view is {mw}x{mh}px "
            f"(aspect {mw / mh:.2f}): {aspect_error:.0%} apart. Crop the reference to the one view, check the "
            "view name, or the model's overall proportions are wrong — compare bbox first.")
    scale = (mw / rw + mh / rh) / 2
    shift = (mx0 - rx0 * scale, my0 - ry0 * scale)
    return Registration(mask=_resample(ref_mask, scale, model_mask.shape, shift), scale=scale, aspect_error=aspect_error)


def _label(mask: np.ndarray) -> tuple:
    try:
        from scipy import ndimage
        return ndimage.label(mask)
    except Exception:  # noqa: BLE001
        return _label_py(mask)


def _label_py(mask: np.ndarray) -> tuple:
    """4-connected labelling without scipy (iterative min-label propagation over the True pixels only)."""
    labels = np.zeros(mask.shape, dtype=np.int32)
    ys, xs = np.nonzero(mask)
    labels[ys, xs] = np.arange(1, len(ys) + 1, dtype=np.int32)
    while True:
        prev = labels.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.zeros_like(labels)
            sy = slice(max(dy, 0), labels.shape[0] + min(dy, 0))
            sx = slice(max(dx, 0), labels.shape[1] + min(dx, 0))
            ty = slice(max(-dy, 0), labels.shape[0] + min(-dy, 0))
            tx = slice(max(-dx, 0), labels.shape[1] + min(-dx, 0))
            shifted[sy, sx] = labels[ty, tx]
            better = mask & (shifted > 0) & ((labels == 0) | (shifted < labels))
            labels = np.where(better, shifted, labels)
        if (labels == prev).all():
            break
    uniq = np.unique(labels[labels > 0])
    remap = np.zeros(labels.max() + 1, dtype=np.int32)
    remap[uniq] = np.arange(1, len(uniq) + 1)
    return remap[labels], len(uniq)


def diff(model_mask: np.ndarray, ref_registered: np.ndarray, frame: Frame, view: str = "front",
         max_regions: int = 8, min_region_fraction: float = MIN_REGION_FRACTION, axes: tuple | None = None) -> DiffResult:
    """IoU and the largest mismatch regions, in view millimetres and in model axes."""
    inter = model_mask & ref_registered
    union = model_mask | ref_registered
    uni = int(union.sum()) or 1
    xor = model_mask ^ ref_registered
    out = DiffResult(iou=float(inter.sum()) / uni,
                     model_only_frac=float((model_mask & ~ref_registered).sum()) / uni,
                     ref_only_frac=float((ref_registered & ~model_mask).sum()) / uni, xor=xor)
    (ua, usign), (va, vsign) = axes or view_axes(view)
    floor_px = max(4, int(min_region_fraction * int(model_mask.sum())))
    x0, y0, x1, y1 = bbox_px(model_mask)
    slop = max(1, int(round(EDGE_SLOP_FRACTION * max(x1 - x0 + 1, y1 - y0 + 1))))
    out.tolerance_mm = round(2 * slop / frame.px_per_mm, 2)
    for kind, part in (("model_has_extra", model_mask & ~ref_registered), ("model_missing", ref_registered & ~model_mask)):
        lab, n = _label(_open(part, slop))
        if not n:
            continue
        counts = np.bincount(lab.ravel())[1:]
        for idx in np.nonzero(counts >= floor_px)[0]:
            ys, xs = np.nonzero(lab == idx + 1)
            rx0, rx1, ry0, ry1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
            (u0, v1), (u1, v0) = frame.to_mm(rx0, ry0), frame.to_mm(rx1, ry1)        # y down: top-left is max v
            out.regions.append({
                "kind": kind,
                "area_mm2": round(float(counts[idx]) / frame.px_per_mm ** 2, 2),
                "share_of_silhouette": round(float(counts[idx]) / max(int(model_mask.sum()), 1), 4),
                "view_mm": {"u": [round(u0, 2), round(u1, 2)], "v": [round(v0, 2), round(v1, 2)]},
                "size_mm": [round(u1 - u0, 2), round(v1 - v0, 2)],
                "touches_edge_of": _edges(u0, u1, v0, v1, frame),
                "axes": {"u": ("+" if usign > 0 else "-") + ua, "v": ("+" if vsign > 0 else "-") + va},
            })
    out.regions.sort(key=lambda r: -r["area_mm2"])
    out.regions = out.regions[:max_regions]
    return out


def _edges(u0: float, u1: float, v0: float, v1: float, f: Frame, slack: float = 0.02) -> list:
    """Which side(s) of the model's view bbox a region touches: tells 'the whole right edge is off' from 'a notch'."""
    tw, th = f.width_mm * slack, f.height_mm * slack
    sides = []
    if u0 <= tw:
        sides.append("left")
    if u1 >= f.width_mm - tw:
        sides.append("right")
    if v0 <= th:
        sides.append("bottom")
    if v1 >= f.height_mm - th:
        sides.append("top")
    return sides


def region_model_ranges(region: dict, view: str, bbox_min_mm: dict, bbox_size_mm: dict, axes: tuple | None = None) -> dict:
    """The model-axis interval (mm) a view-mm region covers on the two axes this view shows; the depth axis is free."""
    (ua, usign), (va, vsign) = axes or view_axes(view)
    out = {}
    for axis, sign, (lo, hi) in ((ua, usign, region["view_mm"]["u"]), (va, vsign, region["view_mm"]["v"])):
        base, size = bbox_min_mm[axis], bbox_size_mm[axis]
        a, b = (base + lo, base + hi) if sign > 0 else (base + size - hi, base + size - lo)
        out[axis] = [round(a, 3), round(b, 3)]
    return out


def suspect_features(ranges: dict, feature_map: list, margin_mm: float = 2.0, limit: int = 3) -> list:
    """Features of analyze_model(feature_map) whose created faces / consumed edges lie inside a region.

    Points are METERS (the analyze_model unit); ranges are mm. Only the two axes the view shows are tested.
    Ranked by hit count; a feature with no hits is never returned (an empty list = localisation unknown,
    which the caller must say rather than guess).
    """
    axes = {"X": 0, "Y": 1, "Z": 2}
    scored = []
    for entry in feature_map:
        pts = [f.get("point") for f in entry.get("created_faces", [])] + [e.get("mid") for e in entry.get("consumed_edges", [])]
        hits = 0
        for p in pts:
            if not p or len(p) < 3:
                continue
            if all(lo - margin_mm <= p[axes[a]] * 1000.0 <= hi + margin_mm for a, (lo, hi) in ranges.items()):
                hits += 1
        if hits:
            scored.append((hits, entry.get("feature"), entry.get("type")))
    scored.sort(key=lambda s: -s[0])
    return [{"feature": n, "type": t, "hits": h} for h, n, t in scored[:limit]]


_DATUM_NORMAL = {"front": "Z", "top": "Y", "right": "X"}        # the +normal of the standard datums (feature-graph schema)


def design_step_hint(region: dict, nodes: list) -> dict | None:
    """A SUGGESTED design_step patch for one region, or None unless the mapping is unambiguous.

    Only a blind boss extrude (or box) whose depth axis is shown by the view, that is the region's ONLY suspect
    design node, and whose END face the region touches the bbox edge of. The region's size along that axis is
    the depth error: model_has_extra shortens the depth, model_missing lengthens it. Lengths are METERS (the
    graph's unit). Never applied here."""
    nodes_by_id = {n.get("id"): n for n in nodes or [] if isinstance(n, dict)}
    suspects = region.get("suspect_features") or []
    owners = {s.get("design_node") for s in suspects}
    if len(owners) != 1 or None in owners or next(iter(owners)) not in nodes_by_id:
        return None
    node = nodes_by_id[next(iter(owners))]
    if node.get("type") == "box":
        datum, key, reverse = (node.get("ref") or {}).get("datum", "top"), "height", False
    elif node.get("type") == "extrude" and node.get("operation", "boss") == "boss" and node.get("end", "blind") == "blind":
        sketch = nodes_by_id.get(node.get("sketch")) or {}
        ref = sketch.get("ref") or {}
        if ref.get("face") or ref.get("offset"):
            return None
        datum, key, reverse = ref.get("datum"), "depth", bool(node.get("reversed"))
    else:
        return None
    axis = _DATUM_NORMAL.get(datum)
    depth = node.get(key)
    if axis is None or isinstance(depth, bool) or not isinstance(depth, (int, float)) or depth <= 0:
        return None
    axes = region.get("axes") or {}
    shown = {name[-1]: (idx, name[0] == "+") for idx, name in enumerate((axes.get("u", ""), axes.get("v", ""))) if len(name) == 2}
    touched = region.get("touches_edge_of") or []
    if axis not in shown:
        return None
    idx, positive = shown[axis]
    sides = [t for t in touched if t in (("left", "right") if idx == 0 else ("bottom", "top"))]
    if len(sides) != 1:
        return None
    high_in_view = sides[0] in ("right", "top")
    at_high_end = high_in_view == positive                         # the touched bbox end of the model axis
    if at_high_end == reverse:                                     # the end face grows toward +normal (-normal if reversed)
        return None                                                # the region is at the SKETCH end, not the end face
    delta_mm = (region.get("size_mm") or [0, 0])[idx]
    kind = region.get("kind")
    sign = {"model_has_extra": -1, "model_missing": 1}.get(kind)
    proposed = depth + sign * delta_mm / 1000.0 if sign and delta_mm else None
    if proposed is None or proposed <= 0:
        return None
    proposed = round(proposed, 6)
    return {"suggestion_only": True,
            "patch": [{"op": "set", "id": node["id"], "params": {key: proposed}}],
            "reasoning": (f"{kind}: a {delta_mm:g} mm strip at the {'+' if at_high_end else '-'}{axis} end of the model, "
                          f"where {node['id']} ({node['type']}) ends along {axis}; {key} {depth:g} -> {proposed:g} m "
                          f"({'shorten' if sign < 0 else 'lengthen'} by {delta_mm:g} mm). Reference reads are 'vision': "
                          "check the strip against the drawing before applying.")}


def overlay_png(model_mask: np.ndarray, ref_registered: np.ndarray, title: str = "", max_edge: int = 1280,
                max_bytes: int = 0) -> bytes:
    """Blue = model only, red = reference only, grey = both. White background. The masks may be larger than
    the picture the caller relays: it is decimated to `max_edge` and, when `max_bytes` is set, shrunk further
    until the PNG fits (so the result can never trip the relay cap)."""
    from PIL import Image, ImageDraw
    step = max(1, -(-max(model_mask.shape) // max(64, max_edge)))
    for _ in range(12):
        m, r = model_mask[::step, ::step], ref_registered[::step, ::step]
        img = np.full(m.shape + (3,), 255, dtype=np.uint8)
        img[m & r] = (150, 150, 150)
        img[m & ~r] = (40, 90, 220)
        img[r & ~m] = (225, 50, 40)
        pic = Image.fromarray(img)
        if title:
            ImageDraw.Draw(pic).text((6, 4), title, fill=(0, 0, 0))
        buf = io.BytesIO()
        pic.save(buf, format="PNG", optimize=True)
        png = buf.getvalue()
        if not max_bytes or len(png) <= max_bytes:
            return png
        step += max(1, step // 2)
    return png


# ---------------------------------------------------------------------------------------------
# vector references (PDF page, or a DXF/DWG/SLDDRW rendered to one): the part pen only
# ---------------------------------------------------------------------------------------------
VECTOR_PX = 1000                  # longest side of a vector silhouette raster, px
FRAME_PAGE_FRACTION = 0.9         # a path spanning this much of the page in both directions is the sheet frame


def _cad_drawing():
    """(silhouette, pdf_read) of cad-planner/drawing, via the path the other tools use."""
    import sys
    from solidpilot.paths import CAD_PLANNER_DIR
    if CAD_PLANNER_DIR not in sys.path:
        sys.path.insert(0, CAD_PLANNER_DIR)
    from drawing import pdf_read, silhouette
    return silhouette, pdf_read


class _Paths:
    """A page stand-in whose get_drawings() is a pre-filtered list (what silhouette._mask reads)."""

    def __init__(self, page, drawings):
        self.rect, self._drawings = page.rect, drawings

    def get_drawings(self):
        return self._drawings


def page_pens(page) -> tuple:
    """([{width_mm, primitives, curves}] heaviest first, suggested part pen or None) for a vector page; ([], None)
    when it has no vector pens (a scan) or the cad-planner reader is unavailable."""
    try:
        _sil, pdf_read = _cad_drawing()
        prims, _arrows, _texts, pens = pdf_read._extract(page)
        return pdf_read._pen_stats(prims, pens)
    except Exception:  # noqa: BLE001
        return [], None


def vector_mask(fitz, page, clip: tuple, pen_mm: float, fill: bool = True) -> tuple:
    """Silhouette mask of the `pen_mm` strokes inside `clip` (page points, y down), flood-filled from the border.
    -> (mask | None, info{paths, ignored_paths, frame, px_per_mm}); None when the clip has no such geometry.
    Paths that span the whole sheet (the frame) are left out."""
    sil, _pdf = _cad_drawing()
    W, H = page.rect.width, page.rect.height
    pad = 2.0 / (25.4 / 72.0)                                      # 2 mm so an outline on the clip edge still closes
    box = (clip[0] - pad, clip[1] - pad, clip[2] + pad, clip[3] + pad)
    drawings = page.get_drawings()
    frame = 0
    kept = []
    for d in drawings:
        r = d.get("rect")
        if r is not None and (r.x1 - r.x0) >= FRAME_PAGE_FRACTION * W and (r.y1 - r.y0) >= FRAME_PAGE_FRACTION * H:
            frame += 1
            continue
        kept.append(d)
    on_pen = sil._pen_paths(_Paths(page, kept), pen_mm, box)
    if not on_pen:
        return None, {"paths": 0, "ignored_paths": 0, "frame": frame, "px_per_mm": 0.0}
    total = sum(1 for d in kept if d.get("type") != "f" and (d.get("rect") is None or (
        d["rect"].x1 >= box[0] and d["rect"].x0 <= box[2] and d["rect"].y1 >= box[1] and d["rect"].y0 <= box[3])))
    px_per_mm = max(1.0, min(16.0, VECTOR_PX / (max(box[2] - box[0], box[3] - box[1]) * 25.4 / 72.0)))
    dark, n = sil._mask(fitz, _Paths(page, on_pen), box, pen_mm, px_per_mm)
    if not dark.any():
        return None, {"paths": 0, "ignored_paths": 0, "frame": frame, "px_per_mm": px_per_mm}
    mask = _fill_holes(dark) if fill else dark
    return mask, {"paths": n, "ignored_paths": max(0, total - n), "frame": frame, "px_per_mm": px_per_mm}


def pick_pen(fitz, page, clip: tuple, rows: list, suggested: float | None) -> float | None:
    """The part pen for `clip`: _pen_stats' suggestion (arc-rich pen), else the pen whose filled silhouette inside
    the clip is the largest (dimension/extension/hatch pens enclose nothing)."""
    if suggested:
        return suggested
    best, best_area = None, 0
    for row in rows[:5]:
        mask, _info = vector_mask(fitz, page, clip, row["width_mm"])
        if mask is not None and int(mask.sum()) > best_area:
            best, best_area = row["width_mm"], int(mask.sum())
    return best
