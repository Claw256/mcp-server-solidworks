"""Pixel projection and drawing for export_image's `grid` and `labels` overlays.

Projection, label planning and grid ticks are pure Python (no SolidWorks, no Pillow) so they are unit-testable;
only draw_overlay needs Pillow. The mapping from model mm to image pixels comes from refdiff.frame_from_bbox
(px-per-mm + origin of the model silhouette) and the MEASURED screen axes export_image reports (view_frame),
for an axis-aligned orthographic view of the whole, unsectioned model:

    proj = Projector(axes, bbox_min_mm, bbox_size_mm, frame)
    x, y, depth_mm = proj.px((x_m, y_m, z_m))        # analyze_model points are METERS

Face/edge indices are analyze_model(analysis_type='faces'|'edges')'s `i`, so "face 12" in the picture is face 12
there. Labels show front-facing planar faces, other faces and edges whose point lands on the silhouette; hidden
geometry behind another part is not tested for.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

_AXIS = {"X": 0, "Y": 1, "Z": 2}
MIN_FACING = 0.05                  # |normal . toward-viewer| below this is edge-on: not labelled
MAX_LABELS = 60                    # per kind (faces / edges): more is unreadable clutter
FACE_COLOR, EDGE_COLOR = (20, 70, 200), (200, 90, 0)


class Projector:
    """Model point (meters) -> (pixel x, pixel y, depth toward the viewer in mm) for one axis-aligned view."""

    def __init__(self, axes: tuple, bbox_min_mm: dict, bbox_size_mm: dict, frame):
        self.axes, self.frame = axes, frame
        self.bbox_min, self.bbox_size = bbox_min_mm, bbox_size_mm
        (ua, us), (va, vs) = axes
        right, up = [0.0] * 3, [0.0] * 3
        right[_AXIS[ua]], up[_AXIS[va]] = float(us), float(vs)
        # screen x right, y up, z toward the viewer (right-handed)
        self.toward = (right[1] * up[2] - right[2] * up[1], right[2] * up[0] - right[0] * up[2],
                       right[0] * up[1] - right[1] * up[0])

    def px(self, p_m) -> tuple:
        (ua, us), (va, vs) = self.axes
        mm = [float(c) * 1000.0 for c in p_m[:3]]
        u = _view_mm(mm[_AXIS[ua]], us, self.bbox_min[ua], self.bbox_size[ua])
        v = _view_mm(mm[_AXIS[va]], vs, self.bbox_min[va], self.bbox_size[va])
        s, (ox, oy) = self.frame.px_per_mm, self.frame.origin_px
        depth = sum(t * c for t, c in zip(self.toward, mm))
        return ox + u * s, oy - v * s, depth

    def facing(self, normal) -> float:
        """Cosine between a face normal and the direction toward the viewer (>0 = the face looks at the camera)."""
        return sum(t * float(c) for t, c in zip(self.toward, normal[:3]))


def _view_mm(coord_mm: float, sign: int, base: float, size: float) -> float:
    """Distance along the screen axis, from the bbox edge the axis starts at."""
    return coord_mm - base if sign > 0 else base + size - coord_mm


@dataclass
class Label:
    kind: str          # 'f' face | 'e' edge
    index: int
    x: float
    y: float
    text: str


def _on_mask(mask, x: float, y: float, slop: int = 3) -> bool:
    """True when (x, y) is on or within `slop` px of the silhouette (outer edges sit on its boundary)."""
    h, w = mask.shape
    xi, yi = int(round(x)), int(round(y))
    if not (0 <= xi < w and 0 <= yi < h):
        return False
    return bool(mask[max(0, yi - slop):yi + slop + 1, max(0, xi - slop):xi + slop + 1].any())


def _chord_mm(e: dict) -> float:
    s, t = e.get("start"), e.get("end")
    if s and t:
        return 1000.0 * math.dist(s[:3], t[:3])
    return 1000.0 * float(e.get("length") or 0.0)


def plan_labels(faces: list, edges: list, proj: Projector, mask, want: str, char_px: int = 8,
                max_per_kind: int = MAX_LABELS) -> tuple:
    """(labels, omitted): the labels to draw and the 'f12'/'e7' texts dropped for overlap or the per-kind cap.

    want: 'faces' | 'edges' | 'both'. Larger faces / longer edges win a contested spot."""
    cands = []        # (priority, kind, index, x, y)
    if want in ("faces", "both"):
        for f in faces:
            pt = f.get("point")
            if not pt or len(pt) < 3:
                continue
            n = f.get("normal")
            if n and proj.facing(n) < MIN_FACING:
                continue                                    # planar face turned away or edge-on
            x, y, _ = proj.px(pt)
            if _on_mask(mask, x, y):
                cands.append((float(f.get("area") or 0.0), "f", int(f["i"]), x, y))
    if want in ("edges", "both"):
        for e in edges:
            pt = e.get("mid")
            if not pt or len(pt) < 3:
                continue
            x, y, _ = proj.px(pt)
            if _on_mask(mask, x, y):
                cands.append((_chord_mm(e) * 1e-6, "e", int(e["i"]), x, y))     # same order of magnitude as m2
    labels, omitted, placed, count = [], [], [], {"f": 0, "e": 0}
    for _, kind, idx, x, y in sorted(cands, key=lambda c: -c[0]):
        text = f"{kind}{idx}"
        w, h = char_px * len(text) + 6, char_px * 2
        box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
        if count[kind] >= max_per_kind or any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
                                              for b in placed):
            omitted.append(text)
            continue
        placed.append(box)
        count[kind] += 1
        labels.append(Label(kind, idx, x, y, text))
    return labels, omitted


def nice_step(span_mm: float, target_lines: int = 10) -> float:
    """A 1/2/5 x 10^k step giving roughly target_lines across span_mm."""
    raw = max(span_mm, 1e-9) / target_lines
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def grid_ticks(proj: Projector, width_px: int, height_px: int, step_mm: float | None = None) -> dict:
    """Grid lines in MODEL coordinates: {'step_mm', 'u': (axis, [(value_mm, px_x)]), 'v': (axis, [(value_mm, px_y)])}.

    Values are model-axis coordinates (what analyze_model reports), at multiples of the step."""
    s, (ox, oy) = proj.frame.px_per_mm, proj.frame.origin_px
    step = step_mm or nice_step(max(width_px, height_px) / s)

    def ticks(axis_name, sign, base, size, pix_to_view, view_to_pix, n_px):
        vals = sorted(coord for coord in (_coord(pix_to_view(0), sign, base, size), _coord(pix_to_view(n_px), sign, base, size)))
        out, k = [], math.ceil(vals[0] / step - 1e-9)
        while k * step <= vals[1] + 1e-9:
            c = round(k * step, 6)
            out.append((c, view_to_pix(_view_mm(c, sign, base, size))))
            k += 1
        return axis_name, out

    (ua, us), (va, vs) = proj.axes
    u_axis = ticks(ua, us, proj.bbox_min[ua], proj.bbox_size[ua], lambda px: (px - ox) / s, lambda u: ox + u * s, width_px)
    v_axis = ticks(va, vs, proj.bbox_min[va], proj.bbox_size[va], lambda py: (oy - py) / s, lambda v: oy - v * s, height_px)
    return {"step_mm": step, "u": u_axis, "v": v_axis}


def _coord(view_mm: float, sign: int, base: float, size: float) -> float:
    """Inverse of _view_mm."""
    return base + view_mm if sign > 0 else base + size - view_mm


def font_size(width_px: int, height_px: int) -> int:
    return max(11, min(28, round(min(width_px, height_px) / 60)))


def _font(size: int):
    from PIL import ImageFont
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                          # Pillow < 10.1 has a fixed-size default font only
        return ImageFont.load_default()


def draw_overlay(png: bytes, labels: list, ticks: dict | None, legend: str = "") -> bytes:
    """Draw the grid (light lines, mm values on the top/left border) and the index labels over a PNG. Needs Pillow."""
    from PIL import Image, ImageDraw
    base = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = base.size
    size = font_size(w, h)
    font = _font(size)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if ticks:
        line, ink = (90, 90, 90, 70), (40, 40, 40, 230)
        for value, x in ticks["u"][1]:
            if 0 <= x < w:
                d.line([(x, 0), (x, h)], fill=line, width=1)
                d.text((x + 2, 2), f"{value:g}", fill=ink, font=font)
        for value, y in ticks["v"][1]:
            if 0 <= y < h:
                d.line([(0, y), (w, y)], fill=line, width=1)
                d.text((2, y + 1), f"{value:g}", fill=ink, font=font)
        if legend:
            d.text((4, h - size - 6), legend, fill=ink, font=font)
    for lb in labels:
        color = FACE_COLOR if lb.kind == "f" else EDGE_COLOR
        tw = d.textlength(lb.text, font=font)
        box = (lb.x - tw / 2 - 3, lb.y - size / 2 - 2, lb.x + tw / 2 + 3, lb.y + size / 2 + 2)
        d.rectangle(box, fill=(255, 255, 255, 215), outline=color + (255,))
        d.text((lb.x - tw / 2, lb.y - size / 2 - 1), lb.text, fill=color + (255,), font=font)
    out = Image.alpha_composite(base, layer).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
