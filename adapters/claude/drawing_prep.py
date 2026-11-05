"""drawing_prep.py -- make a PDF / image drawing LEGIBLE and MEASURABLE for Claude.

Claude reads PDFs and images natively, but (platform.claude.com vision / vision-coordinates docs):
  * a PDF page is rasterised server-side at a size we do not control, so small dimension text on a
    big sheet degrades and pixel coordinates cannot be mapped back;
  * an image over the model's long-edge / visual-token limit is silently downscaled -- and a
    tool_result image over the limit is REJECTED, not downscaled;
  * spatial reasoning is approximate, while a vector PDF carries its dimension text EXACTLY.
So this module (a) renders pages/regions at a size that always fits, with a known origin and scale,
and (b) hands back the exact text layer with coordinates. Pure functions; PyMuPDF (`fitz`) is an
OPTIONAL dependency imported lazily (AGPL -- see requirements-pdf.txt); everything degrades to a
clear error, never an import-time crash.
"""
import json
import math
import os
import re

# (max long edge px, max visual tokens) -- one visual token per 28x28 px patch.
TIERS = {"high": (2576, 4784), "standard": (1568, 1568)}
DEFAULT_TIER = "high"
_PT_PER_MM = 72.0 / 25.4
_NUMBERISH = re.compile(
    r"[ØøΦ⌀∅]\s*\d|^\(?R\s*\d|^\(?[+\-±]?\d+([.,]\d+)?\)?$|^\d+\s*[xX×]|^M\d|\d\s*°|^\d+\s*:\s*\d+$"
    r"|^\d+\s*/\s*\d+$|±|\d[HhGgf]\d|^\d+([.,]\d+)?\s*[+\-]\s*\d")


def tier_limits(tier=None):
    tier = (tier or os.environ.get("SOLIDPILOT_IMAGE_TIER") or DEFAULT_TIER).lower()
    return TIERS.get(tier, TIERS[DEFAULT_TIER])


def count_tokens(w, h):
    return math.ceil(w / 28) * math.ceil(h / 28)


def _fits(w, h, max_edge, max_tokens):
    return (math.ceil(w / 28) * 28 <= max_edge and math.ceil(h / 28) * 28 <= max_edge
            and count_tokens(w, h) <= max_tokens)


def resized_size(width, height, max_edge, max_tokens):
    """The size Claude resizes an image to before padding (reference algorithm from the vision
    docs). An image that already fits is returned unchanged."""
    if _fits(width, height, max_edge, max_tokens):
        return width, height
    if height > width:
        h, w = resized_size(height, width, max_edge, max_tokens)
        return w, h
    ratio = width / height
    lo, hi = 1, width
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _fits(mid, max(round(mid / ratio), 1), max_edge, max_tokens):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / ratio), 1)


def _fitz():
    try:
        try:
            import pymupdf as fitz     # current name
        except ImportError:
            import fitz                # PyMuPDF < 1.24.3
        return fitz
    except Exception as ex:    # noqa: BLE001
        raise RuntimeError("PyMuPDF is not installed (pip install -r adapters/claude/requirements-pdf.txt): %s" % ex)


def open_source(path):
    fitz = _fitz()
    doc = fitz.open(path)      # PDFs AND png/jpg/tif/bmp (opened as a one-page document)
    return fitz, doc


class PrepError(Exception):
    """A drawing input that cannot be prepared; `code` is the FAILED | <code> the tools report."""

    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code, self.message = code, message


_RASTER_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp")
_CAD_EXT = (".dxf", ".dwg", ".slddrw")


class Source:
    """An opened drawing input: `.fitz`, `.doc`, whether it is a bare `.raster` image (no paper size or
    scale), plus what a CAD-file render hook returned (`to_source`, `warnings`, exact `text_items`).
    `close()` releases the documents and deletes the hook's temp files."""

    def __init__(self, fitz, doc, raster=False, source_kind="pdf", res=None):
        self.fitz, self.doc, self.raster, self.source_kind = fitz, doc, raster, source_kind
        res = res or {}
        self.to_source, self.warnings = res.get("to_source"), list(res.get("warnings") or [])
        self.text_items, self._temp = list(res.get("text_items") or []), list(res.get("temp_files") or [])
        self._drawable, self._path = None, None

    def drawable(self):
        """A PDF document that accepts drawing (a raster is wrapped in a one-image PDF page)."""
        if self.doc.is_pdf:
            return self.doc
        if self._drawable is None:
            self._drawable = self.fitz.open("pdf", self.doc.convert_to_pdf())
        return self._drawable

    def source_px(self, page):
        """(w, h) pixels of a raster input, else None."""
        if not self.raster:
            return None
        try:
            pm = self.fitz.Pixmap(self._path)
            return pm.width, pm.height
        except Exception:    # noqa: BLE001
            info = page.get_image_info()
            return (int(info[0]["width"]), int(info[0]["height"])) if info else None

    def close(self):
        for d in (self._drawable, self.doc):
            try:
                if d is not None:
                    d.close()
            except Exception:    # noqa: BLE001
                pass
        for t in self._temp:
            try:
                os.remove(t)
            except OSError:
                pass


def open_input(path, bridge=None):
    """Open a drawing input -> Source. PDFs and rasters open directly; .dxf/.dwg/.slddrw go through
    `drawing_render.resolve` (rasterised vector PDF + exact text). Raises PrepError with a precise code."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".pdf",) + _RASTER_EXT + _CAD_EXT:
        raise PrepError("UNSUPPORTED_TYPE", "prepare_drawing reads .pdf, images (%s) and .dxf/.dwg/.slddrw; got %r"
                        % ("/".join(e[1:] for e in _RASTER_EXT[:4]), ext))
    try:
        fitz = _fitz()
    except RuntimeError as ex:
        raise PrepError("PDF_READER_UNAVAILABLE", str(ex))
    res, target, kind = None, path, "image" if ext in _RASTER_EXT else "pdf"
    if ext in _CAD_EXT:
        try:
            import drawing_render
        except Exception as ex:    # noqa: BLE001
            raise PrepError("PDF_READER_UNAVAILABLE", "drawing_render is unavailable: %s" % ex)
        try:
            res = drawing_render.resolve(path, bridge=bridge)
        except getattr(drawing_render, "RenderError", ()) as ex:
            raise PrepError(ex.code, ex.message)
        target, kind = res["pdf"], res.get("source_kind") or ext[1:]
    try:
        doc = fitz.open(target)
    except Exception as ex:    # noqa: BLE001
        _cleanup(res)
        raise PrepError("UNSUPPORTED_TYPE", "cannot open %s as a drawing: %s" % (os.path.basename(path), ex))
    if doc.needs_pass:
        doc.close()
        _cleanup(res)
        raise PrepError("PDF_PASSWORD", "%s is password-protected; remove the password and retry" % os.path.basename(path))
    src = Source(fitz, doc, raster=not doc.is_pdf and ext in _RASTER_EXT, source_kind=kind, res=res)
    src._path = target
    return src


def _cleanup(res):
    for t in (res or {}).get("temp_files") or []:
        try:
            os.remove(t)
        except OSError:
            pass


def _image_coverage(page):
    area = (page.rect.width * page.rect.height) or 1.0
    cov = sum(max(0.0, (i["bbox"][2] - i["bbox"][0])) * max(0.0, (i["bbox"][3] - i["bbox"][1]))
              for i in page.get_image_info())
    return min(1.0, cov / area)


def classify_page(page, drawings=None):
    """'vector' (drawn paths: geometry is exact), 'scan' (a raster dominates: read by vision) or 'mixed'.
    A small logo on a vector sheet stays 'vector'; whether the TEXT is trustworthy is `text_reliability`."""
    words = len(page.get_text("words"))
    paths = len(page.get_drawings() if drawings is None else drawings)
    cov = _image_coverage(page)
    if cov >= 0.5:
        return "scan" if paths < 200 else "mixed"
    if paths >= 3 and (words >= 3 or paths >= 50):
        return "vector" if cov < 0.2 else "mixed"
    return "mixed" if (words or paths) else "scan"


_RELIABILITY_NOTE = {
    "exact": "text_layer is the drawing's own text: quote dimension values from it verbatim.",
    "ocr": "text_layer is OCR output from a scan and can misread digits (0/8, 1/7, Ø): verify every value in the image.",
    "outlined": "the sheet's text was converted to outlines, so text_layer is missing or partial: READ dimensions from the images.",
    "none": "there is no text layer: READ dimensions from the images (vision).",
}


def text_reliability(page, n_text, drawings=None, extra=False):
    """'exact' | 'ocr' | 'outlined' | 'none' -- what the text layer can be trusted for."""
    if extra:
        return "exact"
    paths = len(page.get_drawings() if drawings is None else drawings)
    if n_text < 3:
        return "outlined" if paths >= 50 else "none"
    try:
        trace = page.get_texttrace()
        if trace and sum(1 for t in trace if t.get("type") == 3) > len(trace) / 2:
            return "ocr"
    except Exception:    # noqa: BLE001
        pass
    if _image_coverage(page) >= 0.5:
        return "ocr"
    return "outlined" if n_text < 10 and paths >= 300 else "exact"


def _rot_rect(fitz, page, r):
    out = fitz.Rect(r) * page.rotation_matrix if page.rotation else fitz.Rect(r)
    return out.normalize()


def text_items(fitz, page, clip=None, extra=None):
    """The page's text as lines in PAGE POINTS (origin top-left, y down, page rotation applied):
    [{"t", "x0","y0","x1","y1", "rot" (deg ccw from left-to-right), "h" (font size pt)}]. Spans split where the gap
    is wider than a space so `4X Ø8` stays one item and `100   50` becomes two. `extra` = exact items from a CAD
    render hook (already in page points). `clip` keeps only items touching that region."""
    items = []
    mat = page.rotation_matrix
    for blk in page.get_text("dict").get("blocks", []):
        for ln in blk.get("lines", []):
            dx, dy = ln.get("dir", (1.0, 0.0))
            p0, p1 = fitz.Point(0, 0) * mat, fitz.Point(dx, dy) * mat
            rot = int(round(math.degrees(math.atan2(-(p1.y - p0.y), p1.x - p0.x)))) % 360
            cur = None
            for sp in ln.get("spans", []):
                txt = sp.get("text", "")
                if not txt.strip():
                    continue
                r = _rot_rect(fitz, page, sp["bbox"])
                size = float(sp.get("size") or 0)
                gap_lim = max(size, 1.0) * 0.9
                if cur is not None:
                    gap = {0: r.x0 - cur["x1"], 180: cur["x0"] - r.x1, 90: cur["y0"] - r.y1}.get(rot, r.y0 - cur["y1"])
                    if abs(gap) <= gap_lim:
                        cur["t"] += txt
                        cur.update(x0=min(cur["x0"], r.x0), y0=min(cur["y0"], r.y0),
                                   x1=max(cur["x1"], r.x1), y1=max(cur["y1"], r.y1), h=max(cur["h"], size))
                        continue
                    items.append(cur)
                cur = {"t": txt, "x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1, "rot": rot, "h": size}
            if cur is not None:
                items.append(cur)
    for e in extra or []:
        items.append({"t": str(e["t"]), "x0": float(e["x0"]), "y0": float(e["y0"]), "x1": float(e["x1"]),
                      "y1": float(e["y1"]), "rot": int(e.get("rot", 0)) % 360,
                      "h": float(e.get("h") or abs(float(e["y1"]) - float(e["y0"])))})
    for it in items:
        it["t"] = it["t"].strip()
    items = [i for i in items if i["t"]]
    if extra:      # an exact item that the PDF also carries is one item
        seen, uniq = set(), []
        for i in items:
            key = (i["t"], round((i["x0"] + i["x1"]) / 4), round((i["y0"] + i["y1"]) / 4))
            if key not in seen:
                seen.add(key)
                uniq.append(i)
        items = uniq
    if clip is not None:
        items = [i for i in items if i["x1"] >= clip[0] and i["x0"] <= clip[2] and i["y1"] >= clip[1] and i["y0"] <= clip[3]]
    return items


def text_layer(page, limit=300):
    """Text items (see text_items), dimension-like first. -> (items, total, truncated)."""
    fitz = _fitz()
    items = text_items(fitz, page)
    for i in items:
        for k in ("x0", "y0", "x1", "y1"):
            i[k] = round(i[k], 1)
    items.sort(key=lambda i: (0 if _NUMBERISH.search(i["t"]) else 1, i["y0"], i["x0"]))
    return items[:limit], len(items), len(items) > limit


def sort_text(items):
    """Dimension-like strings first, then reading order."""
    return sorted(items, key=lambda i: (0 if _NUMBERISH.search(i["t"]) else 1, round(i["y0"], 1), round(i["x0"], 1)))


def text_str(i):
    """Compact wire form: `t@x0,y0,x1,y1` (+ `,rN` when the text is rotated)."""
    return "%s@%.1f,%.1f,%.1f,%.1f%s" % (i["t"], i["x0"], i["y0"], i["x1"], i["y1"], ",r%d" % i["rot"] if i.get("rot") else "")


def min_text_px(items, clip, px_per_pt):
    """Height in image px of the smallest text inside `clip` at `px_per_pt` (None when there is none)."""
    hs = [i["h"] for i in items if i["h"] > 0 and any(c.isalnum() for c in i["t"])
          and i["x1"] >= clip[0] and i["x0"] <= clip[2] and i["y1"] >= clip[1] and i["y0"] <= clip[3]]
    return round(min(hs) * px_per_pt, 1) if hs else None


def scale_guess(items):
    """Sheet scale from the title-block text via cad-planner's detector, or None when it only assumed 1:1.
    -> {"label": "1:2", "factor": 2.0, "source": ...}"""
    try:
        import sys
        from solidpilot.paths import CAD_PLANNER_DIR
        if CAD_PLANNER_DIR not in sys.path:
            sys.path.insert(0, CAD_PLANNER_DIR)
        from drawing import pdf_read
        factor, label, source = pdf_read._detect_scale([(i["t"],) for i in items])
    except Exception:    # noqa: BLE001
        return None
    if "ASSUMED" in str(source):
        return None
    return {"label": label, "factor": round(float(factor), 4), "source": source}


def plan_tiles(rect, dpi, tier=None, overlap=0.08, max_tiles=8):
    """Split a page rect (points) into a grid of regions, each rendering at `dpi` within the tier's
    pixel limits. -> [(x0, y0, x1, y1)] (points). [] when more than `max_tiles` would be needed."""
    max_edge, max_tokens = tier_limits(tier)
    w_pt, h_pt = rect[2] - rect[0], rect[3] - rect[1]
    if w_pt <= 0 or h_pt <= 0:
        return []
    scale = dpi / 72.0
    side_px = min(max_edge - 28, int(math.sqrt(max_tokens) * 28) - 28)
    tile_w = tile_h = side_px / scale
    nx, ny = max(1, math.ceil(w_pt / (tile_w * (1 - overlap)))), max(1, math.ceil(h_pt / (tile_h * (1 - overlap))))
    if nx * ny > max_tiles:
        return []                          # caller lowers dpi
    tw, th = w_pt / nx, h_pt / ny
    pad_x, pad_y = tw * overlap / 2, th * overlap / 2
    return [(max(rect[0], rect[0] + i * tw - pad_x), max(rect[1], rect[1] + j * th - pad_y),
             min(rect[2], rect[0] + (i + 1) * tw + pad_x), min(rect[3], rect[1] + (j + 1) * th + pad_y))
            for j in range(ny) for i in range(nx)]


def plan_tiles_auto(rect, dpi=300, tier=None, max_tiles=8):
    """plan_tiles, lowering the dpi (x0.8 steps, floor 40) until the grid fits `max_tiles`.
    -> (tiles, dpi_used); tiles is [] only when even the floor dpi does not fit."""
    d = float(max(30, min(dpi, 600)))
    tiles = plan_tiles(rect, d, tier, max_tiles=max_tiles)
    while not tiles and d > 40:
        d *= 0.8
        tiles = plan_tiles(rect, d, tier, max_tiles=max_tiles)
    return tiles, d


def fit_px_per_pt(width_pt, height_pt, tier=None, dpi=300, max_scale=None):
    """Closed-form render scale: the requested dpi capped by the tier's edge and token limits (and by
    `max_scale`, the native resolution of a raster). The result always satisfies `_fits`."""
    max_edge, max_tokens = tier_limits(tier)
    w_cap = (max_edge // 28) * 28
    s = dpi / 72.0
    if max_scale:
        s = min(s, max_scale)
    s = min(s, (w_cap - 1) / width_pt, (w_cap - 1) / height_pt,
            math.sqrt(max_tokens * 784.0 / (width_pt * height_pt)))
    while s > 1e-4 and not _fits(math.ceil(width_pt * s) + 1, math.ceil(height_pt * s) + 1, max_edge, max_tokens):
        s *= 0.99
    return s


def render_region(fitz, page, clip, tier=None, dpi=300, max_bytes=None, max_scale=None):
    """Render `clip` (points) to PNG bytes at <= `dpi`, shrunk (closed form) so the PNG ALWAYS fits the
    tier and, when `max_bytes` is given, that byte budget (re-rendered smaller until it does, else
    ValueError). -> (png_bytes, {width, height, px_per_pt, dpi, clip, limited_by})."""
    c = fitz.Rect(*clip)
    if c.width <= 0 or c.height <= 0:
        raise ValueError("empty region %s" % (list(clip),))
    want = dpi / 72.0
    scale = fit_px_per_pt(c.width, c.height, tier, dpi, max_scale)
    limited = "tier" if scale < want * 0.999 and not (max_scale and scale >= max_scale * 0.999) else (
        "native" if scale < want * 0.999 else None)
    for _ in range(10):
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=c, alpha=False)
        png = pix.tobytes("png")
        if not max_bytes or len(png) <= max_bytes:
            return png, {"width": pix.width, "height": pix.height, "px_per_pt": round(scale, 4),
                         "dpi": round(scale * 72.0, 1), "clip": [round(v, 1) for v in clip],
                         "limited_by": limited, "bytes": len(png)}
        scale *= max(0.05, math.sqrt(max_bytes / len(png)) * 0.95)
        limited = "bytes"
    raise ValueError("a render of %s cannot be made to fit %d bytes" % (list(clip), max_bytes))


def image_budget(n_images, text_bytes=0):
    """Per-image PNG byte budget when `n_images` images share one result with `text_bytes` of text."""
    return max(0, (RELAY_MAX_BYTES - _JSON_HEADROOM - text_bytes) // max(1, n_images) // 4 * 3)


def _drawings_rects(fitz, page, drawings):
    out = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        r = _rot_rect(fitz, page, r)
        out.append((r.x0, r.y0, r.x1, r.y1))
    return out


def view_regions(fitz, page, drawings=None):
    """Drawing-view regions of a VECTOR page, for tiling and for WP-C's compare_to_reference to pick a
    view. Clusters the page's drawn paths (page rotation applied; the sheet frame is ignored) and returns
    [{"clip": [x0,y0,x1,y1] page points, "kind": "view"|"title"|"other", "area_pt2": float}] sorted by
    area, largest first. 'title' = the cluster in the bottom-right corner that holds the title-block text;
    'other' = small clusters (notes, symbols). [] for a page with no vector geometry (a scan)."""
    W, H = page.rect.width, page.rect.height
    rects = _drawings_rects(fitz, page, page.get_drawings() if drawings is None else drawings)
    rects = [r for r in rects if (r[2] - r[0]) * (r[3] - r[1]) < 0.5 * W * H and r[2] - r[0] < 0.9 * W + 1e-9]
    if not rects:
        return []
    gap = max(6.0, 0.012 * max(W, H))
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells = {}
    for k, (x0, y0, x1, y1) in enumerate(rects):
        for cx in range(int((x0 - gap / 2) // gap), int((x1 + gap / 2) // gap) + 1):
            for cy in range(int((y0 - gap / 2) // gap), int((y1 + gap / 2) // gap) + 1):
                j = cells.setdefault((cx, cy), k)
                if j != k:
                    parent[find(k)] = find(j)
    groups = {}
    for k, r in enumerate(rects):
        g = groups.setdefault(find(k), [r[0], r[1], r[2], r[3], 0])
        g[0], g[1], g[2], g[3], g[4] = min(g[0], r[0]), min(g[1], r[1]), max(g[2], r[2]), max(g[3], r[3]), g[4] + 1
    words = page.get_text("words")
    wr = [tuple(_rot_rect(fitz, page, w[:4])) for w in words]
    out = []
    for x0, y0, x1, y1, n in groups.values():
        area = (x1 - x0) * (y1 - y0)
        if area < 0.002 * W * H and n < 8:
            continue
        in_corner = x1 >= 0.85 * W and y1 >= 0.8 * H and (x1 - x0) < 0.6 * W and (y1 - y0) < 0.4 * H
        n_words = sum(1 for r in wr if r[0] >= x0 - 2 and r[2] <= x1 + 2 and r[1] >= y0 - 2 and r[3] <= y1 + 2)
        kind = "title" if in_corner and n_words >= 5 else ("view" if area >= 0.02 * W * H else "other")
        out.append({"clip": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)], "kind": kind,
                    "area_pt2": round(area, 1)})
    out.sort(key=lambda r: -r["area_pt2"])
    return out


def plan_page_tiles(fitz, page, dpi=300, tier=None, drawings=None, max_tiles=64):
    """The pageable tile list for a page at `dpi`: view-aligned for a vector page (each view padded; a view
    too big for one image at `dpi` is split into a grid), a plain grid otherwise. Order: views by area,
    the title block, then other clusters. -> ([{"clip", "kind", "content"}], dpi_used). [] means no tile
    fits (the caller reports an error); dpi_used < dpi only when the whole page needs more than max_tiles."""
    r = page.rect
    page_rect = (r.x0, r.y0, r.x1, r.y1)
    regions = view_regions(fitz, page, drawings)
    tiles, used = [], float(max(30, min(dpi, 600)))
    if regions:
        order = [g for g in regions if g["kind"] == "view"] + [g for g in regions if g["kind"] == "title"] \
            + [g for g in regions if g["kind"] == "other"]
        counts = {"view": 0, "title": 0, "other": 0}
        for g in order:
            counts[g["kind"]] += 1
            pad = max(8.0, 0.04 * max(g["clip"][2] - g["clip"][0], g["clip"][3] - g["clip"][1]))
            c = (max(r.x0, g["clip"][0] - pad), max(r.y0, g["clip"][1] - pad),
                 min(r.x1, g["clip"][2] + pad), min(r.y1, g["clip"][3] + pad))
            sub = plan_tiles(c, used, tier, max_tiles=max_tiles)
            name = {"view": "view %d" % counts["view"], "title": "title block", "other": "notes/other %d" % counts["other"]}[g["kind"]]
            for k, t in enumerate(sub):
                tiles.append({"clip": [round(v, 1) for v in t], "kind": g["kind"],
                              "content": name + (" (part %d/%d)" % (k + 1, len(sub)) if len(sub) > 1 else "")})
        if len(tiles) <= max_tiles:
            return tiles, used
        tiles = []
    grid, used = plan_tiles_auto(page_rect, dpi, tier, max_tiles=max_tiles)
    nx = len({round(t[0]) for t in grid})
    return [{"clip": [round(v, 1) for v in t], "kind": "page",
             "content": "page grid r%dc%d" % (k // max(1, nx) + 1, k % max(1, nx) + 1)} for k, t in enumerate(grid)], used


def to_mm(points):
    """PDF points (1/72 in) -> millimetres of PAPER (multiply by the drawing scale for true size)."""
    return points / _PT_PER_MM


def _hits(i, c):
    return i["x1"] >= c[0] and i["x0"] <= c[2] and i["y1"] >= c[1] and i["y0"] <= c[3]


def _parse_region(region, rect):
    try:
        c = [float(v) for v in json.loads(region)]
        if len(c) != 4:
            raise ValueError("need 4 numbers")
    except Exception as ex:    # noqa: BLE001
        raise PrepError("BAD_REGION", "region must be JSON [x0,y0,x1,y1] in page points: %s" % ex)
    c = (max(rect[0], min(c[0], c[2])), max(rect[1], min(c[1], c[3])),
         min(rect[2], max(c[0], c[2])), min(rect[3], max(c[1], c[3])))
    if c[2] - c[0] < 1 or c[3] - c[1] < 1:
        raise PrepError("BAD_REGION", "region lies outside the page %s" % [round(v, 1) for v in rect])
    return c


def _parse_tiles(tiles, n):
    try:
        got = json.loads(tiles) if tiles.strip().startswith("[") else [v for v in tiles.split(",") if v.strip()]
        got = [int(v) for v in got]
    except Exception as ex:    # noqa: BLE001
        raise PrepError("BAD_TILES", "tiles must be a JSON list of tile numbers from tile_index (0 = overview): %s" % ex)
    bad = [v for v in got if not 0 <= v <= n]
    if bad:
        raise PrepError("BAD_TILES", "tile(s) %s do not exist; tile_index has 1..%d (0 = overview)" % (bad, n))
    return list(dict.fromkeys(got))


def _check_page(src, page_no):
    if not 1 <= page_no <= src.doc.page_count:
        raise PrepError("BAD_PAGE", "%s has %d page(s); asked for %d" % (os.path.basename(src._path), src.doc.page_count, page_no))
    return src.doc[page_no - 1]


def _native(src, page):
    """(native px per page point, (w, h) px) of a raster input, else (None, None)."""
    px = src.source_px(page)
    return ((px[0] / page.rect.width if px else None), px) if src.raster else (None, None)


def prepare_page(src, page_no=1, region="", dpi=300, tiles="", max_images=4, text_offset=0, text_limit=300, tier=None):
    """prepare_drawing's engine. -> (meta dict, [png bytes]); raises PrepError. See the tool docstring."""
    fitz = src.fitz
    pg = _check_page(src, page_no)
    dpi = max(30, min(int(dpi), 600))
    max_images = max(1, min(int(max_images), 8))
    r = pg.rect
    rect = (r.x0, r.y0, r.x1, r.y1)
    drawings = pg.get_drawings()
    items = text_items(fitz, pg, extra=src.text_items)
    reliability = text_reliability(pg, len(items), drawings, extra=bool(src.text_items))
    native, source_px = _native(src, pg)
    warnings = list(src.warnings)
    meta = {"file": os.path.basename(src._path), "page": page_no, "pages": src.doc.page_count,
            "kind": classify_page(pg, drawings), "page_pt": [round(rect[2], 1), round(rect[3], 1)]}
    if pg.rotation:
        meta["rotation"] = pg.rotation
    if src.raster:
        meta["source_px"] = list(source_px) if source_px else None
    else:
        meta["page_mm_on_paper"] = [round(to_mm(rect[2]), 1), round(to_mm(rect[3]), 1)]
    plan, used, index = [], float(dpi), []
    if region:
        plan, sel = [{"clip": list(_parse_region(region, rect)), "kind": "region", "content": "zoom region"}], [1]
    else:
        ov = fit_px_per_pt(r.width, r.height, tier, dpi, native)
        if ov * 72.0 >= 0.85 * dpi or (native and ov >= native * 0.999):
            sel = [0]                                  # the whole page is legible in one image
        else:
            plan, used = plan_page_tiles(fitz, pg, dpi, tier, drawings)
            if not plan:
                raise PrepError("NO_TILE_FITS", "no tile of this page fits the image limits; pass a smaller `region`")
            if used < dpi - 1:
                warnings.append("page needs more than 64 tiles at %d dpi: tiles planned at %d dpi; request a `region` "
                                "for full dpi" % (dpi, round(used)))
            sel = _parse_tiles(tiles, len(plan)) if tiles.strip() else [0] + list(range(1, max_images))
            sel = [i for i in sel if i <= len(plan)]
            if len(sel) > max_images:
                warnings.append("max_images=%d: returned %d of the %d requested images" % (max_images, max_images, len(sel)))
                sel = sel[:max_images]
            index = [{"i": k + 1, "clip": t["clip"], "kind": t["kind"], "content": t["content"], "dpi": round(used, 1),
                      "w_px": int((t["clip"][2] - t["clip"][0]) * used / 72), "h_px": int((t["clip"][3] - t["clip"][1]) * used / 72),
                      "min_text_px": min_text_px(items, t["clip"], used / 72.0)} for k, t in enumerate(plan)]
            if not src.raster:
                for t in index:
                    t["px_per_mm_paper"] = round(used / 25.4, 2)
            rest = [k for k in range(1, len(plan) + 1) if k not in sel]
            if rest:
                warnings.append("%d more tile(s) not returned (see tile_index): request tiles=%s next"
                                % (len(rest), json.dumps(rest[:max_images])))
    ordered = sort_text([i for i in items if not region or _hits(i, plan[0]["clip"])])
    off, lim = max(0, int(text_offset)), max(0, int(text_limit))
    shown = ordered[off:off + lim]
    strs = [text_str(i) for i in shown]
    budget = image_budget(len(sel), sum(len(x) + 4 for x in strs) + 24 * 1024)
    pngs, images = [], []
    for n, i in enumerate(sel, 1):
        clip, label = (rect, "overview") if i == 0 else (tuple(plan[i - 1]["clip"]), "tile %d" % i)
        png, info = render_region(fitz, pg, clip, tier=tier, dpi=dpi, max_bytes=budget, max_scale=native)
        s = info["px_per_pt"]
        img = {"n": n, "tile": i, "clip": info["clip"], "px_per_pt": s, "width": info["width"], "height": info["height"],
               "bytes": info["bytes"], "min_text_px": min_text_px(items, clip, s)}
        if i == 0 and not region:
            img["overview"] = True
        if src.raster:
            img["vs_source_px"] = round(s / native, 3) if native else None
        else:
            img["dpi"], img["px_per_mm_paper"] = info["dpi"], round(s * _PT_PER_MM, 2)
        if info["limited_by"] == "bytes":
            warnings.append("%s re-rendered at %d dpi to fit the %d MB relay cap; request a smaller `region` for full dpi"
                            % (label, round(info["dpi"]), RELAY_MAX_BYTES // 1048576))
        elif info["limited_by"] == "tier" and (i != 0 or region):
            warnings.append("%s rendered at %d dpi (asked %d): too large for one image; request a smaller `region`"
                            % (label, round(info["dpi"]), dpi))
        pngs.append(png)
        images.append(img)
    body = [m for m in images if not m.get("overview")] or images
    meta["images"] = images
    if not src.raster:
        meta["dpi_used"] = min(m["dpi"] for m in body)
        meta["px_per_mm_paper"] = min(m["px_per_mm_paper"] for m in body)
    mt = [m["min_text_px"] for m in body if m["min_text_px"] is not None]
    meta["min_text_px"] = min(mt) if mt else None
    if index:
        meta["tile_index"] = index
    meta["text_layer_reliability"] = reliability
    meta["text_layer"] = {"total": len(ordered), "offset": off, "shown": len(shown),
                          "truncated": off + len(shown) < len(ordered), "items": strs}
    if off + len(shown) < len(ordered):
        meta["text_layer"]["next_offset"] = off + len(shown)
    guess = scale_guess(items)
    if guess:
        meta["scale_guess"] = guess
    if src.to_source:
        meta["to_source"] = src.to_source
    if warnings:
        meta["warnings"] = warnings
    meta["note"] = ("Coordinates are PAGE POINTS (72/in, origin top-left, y down); pixel (px,py) in image n = "
                    "(clip_x0 + px/px_per_pt, clip_y0 + py/px_per_pt). " + _RELIABILITY_NOTE[reliability] + " " +
                    ("Raster input: no paper size or drawing scale is known (points = source pixels at 72/in)."
                     if src.raster else "True size = paper size x the drawing scale (scale_guess / title block)."))
    return meta, pngs


def annotate_page(src, marks, page_no=1, clip=None, dpi=300, tier=None):
    """annotate_regions' engine. -> (meta dict, png bytes); raises PrepError."""
    pg = _check_page(src, page_no)
    dpi = max(30, min(int(dpi), 600))
    native, _px = _native(src, pg)
    if clip is not None:
        clip = _parse_region(json.dumps(list(clip)), (pg.rect.x0, pg.rect.y0, pg.rect.x1, pg.rect.y1))
    png, info, resolved = annotate_region(src.fitz, src.drawable()[page_no - 1], marks, clip=clip, tier=tier, dpi=dpi,
                                          max_bytes=image_budget(1, 16 * 1024), max_scale=native)
    if not src.raster:
        info["px_per_mm_paper"] = round(info["px_per_pt"] * _PT_PER_MM, 2)
    meta = {"file": os.path.basename(src._path), "page": page_no, "image": info,
            "marks": [{"label": m["label"], "kind": m["kind"], "page_pt": m["pts"], "px": m["px"]} for m in resolved]}
    if src.to_source:
        meta["to_source"] = src.to_source
    if src.warnings:
        meta["warnings"] = list(src.warnings)
    return meta, png


def bmp_size(data):
    """(width, height) in pixels from a BMP header (height is always positive)."""
    import struct
    if data[:2] != b"BM" or len(data) < 26:
        raise ValueError("not a BMP file")
    w, h = struct.unpack_from("<ii", data, 18)
    return w, abs(h)


# The relay (gateway host) rejects request bodies over ~4.5 MB; agent.MAX_RESULT_BYTES mirrors this value.
RELAY_MAX_BYTES = 4 * 1024 * 1024
_JSON_HEADROOM = 64 * 1024          # envelope, notes and the text parts around the base64 image data


def max_png_bytes(n_images=1):
    """Largest PNG (raw bytes) that still fits the relay cap once base64-encoded, per image when
    `n_images` images share one result."""
    return max(0, (RELAY_MAX_BYTES - _JSON_HEADROOM) // max(1, n_images) // 4 * 3)


def fit_scale(png_len, n_images=1):
    """Linear scale (<= 1) to apply to a render's width/height so its PNG should fit `max_png_bytes`.
    PNG size grows roughly with pixel count; the 0.95 factor leaves slack for content differences."""
    budget = max_png_bytes(n_images)
    if png_len <= budget:
        return 1.0
    return max(0.05, math.sqrt(budget / png_len) * 0.95)


def _rep(pattern, n):
    return int.from_bytes(bytes(pattern) * n, "little")


def _swar_consts(n):
    """Per-row-length constants for the big-int (SWAR) PNG filters (n = bytes per row)."""
    return {
        "H8": _rep([0x80], n), "L8": _rep([0x7F], n), "M8": (1 << (8 * n)) - 1,
        "H16": _rep([0x00, 0x80], n), "L16": _rep([0xFF, 0x7F], n), "B16": _rep([0xFF, 0x00], n), "K16": _rep([0x00, 0x01], n),
        "F16": (1 << (16 * n)) - 1,
    }


def _sub8(x, y, k):
    """Byte-wise (x - y) mod 256 on big ints, no borrow between bytes."""
    return ((x | k["H8"]) - (y & k["L8"])) ^ ((x ^ ~y) & k["H8"])


def _absdiff16(x, y, k):
    """Lane-wise |x - y| for 16-bit lanes holding values < 0x8000."""
    t = (x | k["H16"]) - y                       # (x - y) + 0x8000 per lane, never borrows across lanes
    m = ((t & k["H16"]) >> 15) * 0xFFFF          # full-lane mask where x >= y
    lt = k["F16"] ^ m
    return (t & m & k["L16"]) | ((k["H16"] - (t & lt)) & lt)


def _le16(x, y, k):
    """Full-lane mask where x <= y (16-bit lanes, values < 0x8000)."""
    return ((((y | k["H16"]) - x) & k["H16"]) >> 15) * 0xFFFF


def _widen(b, n):
    buf = bytearray(2 * n)
    buf[0::2] = b
    return int.from_bytes(buf, "little")


def _paeth_residual(x16, a16, b16, c16, k, n):
    """Paeth-filtered row bytes. a/b/c are the left / up / up-left ORIGINAL pixels, so every byte
    of the row can be computed at once."""
    pa = _absdiff16(b16, c16, k)
    pb = _absdiff16(a16, c16, k)
    pc = _absdiff16(a16 + b16, c16 << 1, k)
    m_a = _le16(pa, pb, k) & _le16(pa, pc, k)
    m_b = _le16(pb, pc, k) & (k["F16"] ^ m_a)
    pred = (a16 & m_a) | (b16 & m_b) | (c16 & (k["F16"] ^ (m_a | m_b)))
    res = ((x16 + k["K16"]) - pred) & k["B16"]      # (x - pred) mod 256 per lane
    return res.to_bytes(2 * n, "little")[0::2]


_MIN_FILTER_WIDTH = 8


def bmp_to_png(data):
    """Uncompressed 24/32-bit BMP bytes -> PNG bytes (stdlib only). SolidWorks' SaveBMP writes BMP,
    but Claude accepts only JPEG/PNG/GIF/WebP, and a Pillow dependency just for this is not worth it.

    Each row gets whichever PNG filter (None/Sub/Up/Paeth) has the smallest sum of absolute
    residuals, then zlib level 9. The filters run as big-int (SWAR) byte arithmetic on whole rows --
    no per-pixel Python loops -- so a 4096x3072 view converts in seconds."""
    import struct
    import zlib
    if data[:2] != b"BM":
        raise ValueError("not a BMP file")
    off = struct.unpack_from("<I", data, 10)[0]
    hdr, w, h, planes, bpp, comp = struct.unpack_from("<IiiHHI", data, 14)
    if comp not in (0, 3) or bpp not in (24, 32):
        raise ValueError("unsupported BMP (bpp=%s, compression=%s); need uncompressed 24/32-bit" % (bpp, comp))
    top_down = h < 0
    h = abs(h)
    if w <= 0 or h <= 0:
        raise ValueError("empty BMP")
    stride = ((w * bpp + 31) // 32) * 4
    step = bpp // 8
    if len(data) < off + stride * h:
        raise ValueError("truncated BMP (%d bytes, need %d)" % (len(data), off + stride * h))
    n = w * 3
    k = _swar_consts(n)
    cost = bytes(v if v < 128 else 256 - v for v in range(256))     # |signed byte|
    z = zlib.compressobj(9)
    out = []
    prev_i, prev16 = 0, 0
    for y in range(h):
        src = off + (y if top_down else h - 1 - y) * stride
        px = data[src:src + w * step]
        row = bytearray(n)
        row[0::3] = px[2::step]      # R (BMP is BGR)
        row[1::3] = px[1::step]      # G
        row[2::3] = px[0::step]      # B
        row = bytes(row)
        if w < _MIN_FILTER_WIDTH:        # thumbnails gain nothing from filtering; stay plain (filter 0)
            out.append(z.compress(b"\x00" + row))
            continue
        cur = int.from_bytes(row, "little")
        cur16 = _widen(row, n)
        sub =_sub8(cur, (cur << 24) & k["M8"], k).to_bytes(n, "little")     # left pixel is 3 bytes back
        cands = [(sum(row.translate(cost)), 0, row), (sum(sub.translate(cost)), 1, sub)]
        if y:
            up = _sub8(cur, prev_i, k).to_bytes(n, "little")
            cands.append((sum(up.translate(cost)), 2, up))
            pa = _paeth_residual(cur16, (cur16 << 48) & k["F16"], prev16, (prev16 << 48) & k["F16"], k, n)
            cands.append((sum(pa.translate(cost)), 4, pa))
        _score, ftype, body = min(cands, key=lambda c: c[0])
        out.append(z.compress(bytes((ftype,)) + body))
        prev_i, prev16 = cur, cur16
    out.append(z.flush())

    def chunk(tag, body):
        c = struct.pack(">I", len(body)) + tag + body
        return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", b"".join(out)) + chunk(b"IEND", b""))


def parse_marks(marks):
    """Validate/normalise `marks` -> [{kind: 'box'|'point', pts: [...], label: str}] in PAGE POINTS.
    Accepts {box:[x0,y0,x1,y1]} or {point:[x,y]} (+ optional label). Raises ValueError, never guesses."""
    if not isinstance(marks, list) or not marks:
        raise ValueError("marks must be a non-empty JSON list of {box:[x0,y0,x1,y1]|point:[x,y], label?}")
    out = []
    for i, m in enumerate(marks[:60]):
        if not isinstance(m, dict):
            raise ValueError("marks[%d] must be an object" % i)
        label = str(m.get("label", i + 1))[:24]
        if "box" in m:
            b = [float(v) for v in m["box"]]
            if len(b) != 4:
                raise ValueError("marks[%d].box needs 4 numbers [x0,y0,x1,y1]" % i)
            out.append({"kind": "box", "pts": [min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])],
                        "label": label})
        elif "point" in m:
            p = [float(v) for v in m["point"]]
            if len(p) != 2:
                raise ValueError("marks[%d].point needs 2 numbers [x,y]" % i)
            out.append({"kind": "point", "pts": p, "label": label})
        else:
            raise ValueError("marks[%d] needs 'box' or 'point'" % i)
    return out


def annotate_region(fitz, page, marks, clip=None, tier=None, dpi=300, max_bytes=None, max_scale=None):
    """Draw `marks` (page points) on the page IN MEMORY (the document is never saved) and render
    `clip` (default: the union of the marks plus a margin). -> (png, info, marks_with_pixels).
    Each mark gains `px` = its location in the returned image, so a claimed localisation can be
    checked by eye against what is actually there. `page` must accept drawing (Source.drawable())."""
    marks = parse_marks(marks) if marks and isinstance(marks[0], dict) and "kind" not in marks[0] else marks
    if clip is None:
        xs = [v for m in marks for v in (m["pts"][0::2])]
        ys = [v for m in marks for v in (m["pts"][1::2])]
        pad = 40.0
        r = page.rect
        clip = (max(r.x0, min(xs) - pad), max(r.y0, min(ys) - pad),
                min(r.x1, max(xs) + pad), min(r.y1, max(ys) + pad))
    red = (1, 0, 0)
    inv = page.derotation_matrix if page.rotation else None      # drawing happens in UNROTATED page space

    def P(x, y):
        p = fitz.Point(x, y)
        return p * inv if inv is not None else p

    for m in marks:
        if m["kind"] == "box":
            x0, y0, x1, y1 = m["pts"]
            q = fitz.Quad(P(x0, y0), P(x1, y0), P(x0, y1), P(x1, y1))
            page.draw_rect(q.rect, color=red, width=1.2)
            page.insert_text(P(x0, max(y0 - 2.0, 6.0)), m["label"], fontsize=8, color=red)
        else:
            x, y = m["pts"]
            page.draw_line(P(x - 7, y), P(x + 7, y), color=red, width=1.2)
            page.draw_line(P(x, y - 7), P(x, y + 7), color=red, width=1.2)
            page.draw_circle(P(x, y), 4.0, color=red, width=1.0)
            page.insert_text(P(x + 6, y - 6), m["label"], fontsize=8, color=red)
    png, info = render_region(fitz, page, clip, tier=tier, dpi=dpi, max_bytes=max_bytes, max_scale=max_scale)
    s = info["px_per_pt"]
    resolved = []
    for m in marks:
        px = [round((v - clip[i % 2]) * s, 1) for i, v in enumerate(m["pts"])]
        resolved.append(dict(m, px=px))
    return png, info, resolved
