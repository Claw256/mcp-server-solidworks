"""silhouette.py -- the OUTLINE of a part view in a vector PDF, by raster flood-fill instead of contour chaining.

WHY: real CAD drawings draw tangent edges (fillet boundaries, chamfer lines) as extra lines hugging the outline, so
the outline is full of degree-3/4 junctions and strict contour chaining (contour.py) cannot close it. The outside of
a view, however, is unambiguous: draw ONLY the part pen, flood-fill from the border, and whatever the fill cannot
reach is the part's silhouette. Interior detail (windows, hidden lines, bores) is deliberately included -- two
orthogonal silhouettes give the visual hull, and the holes are added from their own circles.

Pure numpy + PyMuPDF (imported lazily by the caller). The polygon is a POLYLINE approximation (default tolerance
0.06 mm), not arcs: faces made from it are faceted. Coordinates: millimetres of PAPER, origin = lower-left of the
clip, y UP.
"""
import math
from collections import deque

_K = 25.4 / 72.0


def _pen_paths(page, pen_mm, clip):
    out = []
    for p in page.get_drawings():
        if p.get("type") == "f":
            continue
        if abs((p.get("width") or 0.0) * _K - pen_mm) > 0.006:
            continue
        r = p.get("rect")
        if r is not None and not (r.x1 >= clip[0] and r.x0 <= clip[2] and r.y1 >= clip[1] and r.y0 <= clip[3]):
            continue
        out.append(p)
    return out


def _mask(fitz, page, clip, pen_mm, px_per_mm):
    """Dark-pixel mask (numpy bool, rows top->bottom) of the part pen inside `clip` (page points, y down)."""
    import numpy as np
    paths = _pen_paths(page, pen_mm, clip)
    tmp = fitz.open()
    pg = tmp.new_page(width=page.rect.width, height=page.rect.height)
    stroke_pt = 1.6 / (px_per_mm * _K)                               # points that render ~1.6 px wide
    for p in paths:
        shape = pg.new_shape()
        for it in p["items"]:
            if it[0] == "l":
                shape.draw_line(it[1], it[2])
            elif it[0] == "c":
                shape.draw_bezier(it[1], it[2], it[3], it[4])
            elif it[0] == "re":
                shape.draw_rect(it[1])
            elif it[0] == "qu":
                shape.draw_quad(it[1])
        shape.finish(color=(0, 0, 0), width=stroke_pt, closePath=False)
        shape.commit()
    s = px_per_mm * _K                                               # pixels per point
    pix = pg.get_pixmap(matrix=fitz.Matrix(s, s), clip=fitz.Rect(*clip), colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    tmp.close()
    return arr < 140, len(paths)


def _outside(dark):
    """4-connected flood fill of the NOT-dark pixels reachable from the border. -> bool array (True = outside)."""
    import numpy as np
    h, w = dark.shape
    pad = np.zeros((h + 2, w + 2), dtype=bool)
    pad[1:-1, 1:-1] = dark
    hh, ww = pad.shape
    flat_dark = bytearray(pad.astype(np.uint8).tobytes())
    seen = bytearray(hh * ww)
    q = deque([0])
    seen[0] = 1
    while q:
        i = q.popleft()
        y, x = divmod(i, ww)
        if x > 0:
            j = i - 1
            if not seen[j] and not flat_dark[j]:
                seen[j] = 1
                q.append(j)
        if x < ww - 1:
            j = i + 1
            if not seen[j] and not flat_dark[j]:
                seen[j] = 1
                q.append(j)
        if y > 0:
            j = i - ww
            if not seen[j] and not flat_dark[j]:
                seen[j] = 1
                q.append(j)
        if y < hh - 1:
            j = i + ww
            if not seen[j] and not flat_dark[j]:
                seen[j] = 1
                q.append(j)
    out = np.frombuffer(bytes(seen), dtype=np.uint8).reshape(hh, ww).astype(bool)
    return out[1:-1, 1:-1]


_N8 = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))   # clockwise from north (dy, dx)


def _trace(sil):
    """Outer boundary of the LARGEST region of `sil` (bool array): Moore-neighbour tracing with Jacob's stop.
    -> [(row, col)] boundary pixel centres, clockwise."""
    import numpy as np
    h, w = sil.shape
    pad = np.zeros((h + 2, w + 2), dtype=bool)
    pad[1:-1, 1:-1] = sil
    rows = np.nonzero(pad.any(axis=1))[0]
    if len(rows) == 0:
        return []
    r0 = int(rows[0])
    c0 = int(np.nonzero(pad[r0])[0][0])
    start = (r0, c0)
    cur, back = start, 7                                               # we arrived from the west (looking from N-W)
    pts = [start]
    first_move = None
    for _ in range(h * w * 4):
        found = False
        for k in range(8):
            d = (back + 1 + k) % 8
            ny, nx = cur[0] + _N8[d][0], cur[1] + _N8[d][1]
            if pad[ny, nx]:
                nxt, back_dir = (ny, nx), (d + 4) % 8
                found = True
                break
        if not found:
            break                                                      # an isolated pixel
        if cur == start and first_move is not None and (nxt == first_move):
            break
        if first_move is None:
            first_move = nxt
        cur, back = nxt, back_dir
        if cur == start and len(pts) > 2:
            break
        pts.append(cur)
    return [(r - 1, c - 1) for r, c in pts]


def _rdp(pts, eps):
    """Douglas-Peucker on a CLOSED ring: split at the point farthest from the first, simplify both halves."""
    n = len(pts)
    if n < 4:
        return list(pts)
    a = 0
    b = max(range(n), key=lambda i: (pts[i][0] - pts[a][0]) ** 2 + (pts[i][1] - pts[a][1]) ** 2)
    half1 = _rdp_open(pts[a:b + 1], eps)
    half2 = _rdp_open(pts[b:] + [pts[a]], eps)
    out = half1[:-1] + half2[:-1]
    return out if len(out) >= 3 else list(pts)


def _rdp_open(pts, eps):
    if len(pts) < 3:
        return list(pts)
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1e-12
    best, bi = -1.0, 0
    for i in range(1, len(pts) - 1):
        d = abs(dy * (pts[i][0] - x1) - dx * (pts[i][1] - y1)) / L
        if d > best:
            best, bi = d, i
    if best <= eps:
        return [pts[0], pts[-1]]
    return _rdp_open(pts[:bi + 1], eps)[:-1] + _rdp_open(pts[bi:], eps)


def view_silhouette(fitz, page, clip_pt, pen_mm, px_per_mm=16.0, eps_mm=0.06):
    """Silhouette polygon of the part-pen geometry inside `clip_pt` (page points, y down).
    -> {"polygon": [(x_mm, y_mm)...] (paper mm, origin lower-left of the clip, y up), "size_mm": (w, h),
        "paths": n, "pixels": (w, h)}  or None when the clip holds no such geometry."""
    dark, n_paths = _mask(fitz, page, clip_pt, pen_mm, px_per_mm)
    if not n_paths or not dark.any():
        return None
    h, w = dark.shape
    sil = ~_outside(dark)
    ring = _trace(sil)
    if len(ring) < 8:
        return None
    ring_xy = [(c + 0.5, r + 0.5) for r, c in ring]                    # px, y down
    simp = _rdp(ring_xy, eps_mm * px_per_mm)
    poly = [(x / px_per_mm, (h - y) / px_per_mm) for x, y in simp]     # mm, y up
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return {"polygon": poly, "size_mm": (round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)),
            "origin_mm": (round(min(xs), 3), round(min(ys), 3)), "paths": n_paths, "pixels": (w, h)}
