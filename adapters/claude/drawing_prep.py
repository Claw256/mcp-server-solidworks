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
import math
import os
import re

# (max long edge px, max visual tokens) -- one visual token per 28x28 px patch.
TIERS = {"high": (2576, 4784), "standard": (1568, 1568)}
DEFAULT_TIER = "high"
_PT_PER_MM = 72.0 / 25.4
_NUMBERISH = re.compile(r"^[ØøΦ⌀R∅]?\s*[+\-±]?\d+([.,]\d+)?(\s*[xX×]\s*\d+)?$|^\d+\s*[xX×]|^M\d|^\d+°|[±]")


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


def classify_page(page):
    """'vector' (text layer + drawn paths: dimensions are exact), 'scan' (a raster only: dimensions
    must be READ by vision), or 'mixed'."""
    words = len(page.get_text("words"))
    paths = len(page.get_drawings())
    images = len(page.get_images())
    if words >= 3 and paths >= 3:
        return "vector" if images == 0 else "mixed"
    if images and words < 3:
        return "scan"
    return "mixed" if (words or paths) else "scan"


def text_layer(page, limit=300):
    """Words with their position in PAGE POINTS (origin top-left, y down, 72 pt = 1 in), dimension-like
    tokens first. -> (items, total, truncated)."""
    raw = page.get_text("words")           # x0, y0, x1, y1, word, block, line, wordno
    items = [{"t": w[4], "x0": round(w[0], 1), "y0": round(w[1], 1), "x1": round(w[2], 1),
              "y1": round(w[3], 1)} for w in raw]
    items.sort(key=lambda i: (0 if _NUMBERISH.search(i["t"]) else 1, i["y0"], i["x0"]))
    return items[:limit], len(items), len(items) > limit


def plan_tiles(rect, dpi, tier=None, overlap=0.08, max_tiles=8):
    """Split a page rect (points) into a grid of regions, each rendering at `dpi` within the tier's
    pixel limits. -> [(x0, y0, x1, y1)] (points). Falls back to fewer, lower-dpi tiles past max_tiles."""
    max_edge, max_tokens = tier_limits(tier)
    w_pt, h_pt = rect[2] - rect[0], rect[3] - rect[1]
    # largest region (in points) that fits at this dpi
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
    -> (tiles, dpi_used)."""
    d = float(max(30, min(dpi, 600)))
    tiles = plan_tiles(rect, d, tier, max_tiles=max_tiles)
    while not tiles and d > 40:
        d *= 0.8
        tiles = plan_tiles(rect, d, tier, max_tiles=max_tiles)
    return tiles, d


def render_region(fitz, page, clip, tier=None, dpi=300):
    """Render `clip` (points) to PNG bytes at <= `dpi`, shrunk so the PNG ALWAYS fits the tier
    (so the client never has to resize and a tool_result is never rejected).
    -> (png_bytes, {width, height, px_per_pt, clip})."""
    max_edge, max_tokens = tier_limits(tier)
    c = fitz.Rect(*clip)
    scale = dpi / 72.0
    for _ in range(12):
        w, h = int(math.ceil(c.width * scale)), int(math.ceil(c.height * scale))
        if _fits(w, h, max_edge, max_tokens):
            break
        scale *= 0.9
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=c, alpha=False)
    return pix.tobytes("png"), {"width": pix.width, "height": pix.height,
                                "px_per_pt": round(scale, 4), "clip": [round(v, 1) for v in clip]}


def to_mm(points):
    """PDF points (1/72 in) -> millimetres of PAPER (multiply by the drawing scale for true size)."""
    return points / _PT_PER_MM


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


def annotate_region(fitz, page, marks, clip=None, tier=None, dpi=300):
    """Draw `marks` (page points) on the page IN MEMORY (the document is never saved) and render
    `clip` (default: the union of the marks plus a margin). -> (png, info, marks_with_pixels).
    Each mark gains `px` = its location in the returned image, so a claimed localisation can be
    checked by eye against what is actually there."""
    marks = parse_marks(marks) if marks and isinstance(marks[0], dict) and "kind" not in marks[0] else marks
    if clip is None:
        xs = [v for m in marks for v in (m["pts"][0::2])]
        ys = [v for m in marks for v in (m["pts"][1::2])]
        pad = 40.0
        r = page.rect
        clip = (max(r.x0, min(xs) - pad), max(r.y0, min(ys) - pad),
                min(r.x1, max(xs) + pad), min(r.y1, max(ys) + pad))
    red = (1, 0, 0)
    for m in marks:
        if m["kind"] == "box":
            x0, y0, x1, y1 = m["pts"]
            page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=red, width=1.2)
            page.insert_text(fitz.Point(x0, max(y0 - 2.0, 6.0)), m["label"], fontsize=8, color=red)
        else:
            x, y = m["pts"]
            page.draw_line(fitz.Point(x - 7, y), fitz.Point(x + 7, y), color=red, width=1.2)
            page.draw_line(fitz.Point(x, y - 7), fitz.Point(x, y + 7), color=red, width=1.2)
            page.draw_circle(fitz.Point(x, y), 4.0, color=red, width=1.0)
            page.insert_text(fitz.Point(x + 6, y - 6), m["label"], fontsize=8, color=red)
    png, info = render_region(fitz, page, clip, tier=tier, dpi=dpi)
    s = info["px_per_pt"]
    resolved = []
    for m in marks:
        px = [round((v - clip[i % 2]) * s, 1) for i, v in enumerate(m["pts"])]
        resolved.append(dict(m, px=px))
    return png, info, resolved
