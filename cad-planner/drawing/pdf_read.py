"""pdf_read.py -- a VECTOR PDF page -> the same draw dialect a DXF produces.

The trick is the seam: instead of a second reader, the page is REBUILT as an in-memory ezdxf document
(LINE / ARC / CIRCLE entities with HIDDEN / CENTER linetypes, TEXT, and DIMENSION entities) and handed to
`dxf_read.read_doc`, so frame detection, view clustering, contour chaining, scale, pairing, callouts and the
direct-build gate all apply to PDFs unchanged.

What a PDF does NOT carry, and what is therefore HEURISTIC here (and reported, never hidden):
  * dimensions: there is no DIMENSION entity, only lines, filled arrowhead triangles and text. A linear
    dimension is recognised from a PAIR of collinear, opposed arrowheads plus the numeric text beside
    their line; a diameter/radius from a `Ø`/`R` number that matches the size of exactly one nearby circle/arc.
    Numbers that fit neither stay in `sheet.pdf.unassigned_numbers` for Claude to read with vision.
  * the drawing scale: read from title-block text ("SCALE 1:2", "Maßstab 1:2"), else assumed 1:1 and SAID so.
  * line types: a dash pattern maps to hidden (2-element) or centre (4+ elements). Exporters that BAKE the
    dashes into geometry (many separate short solid segments) are recovered by `_merge_baked_dashes`: collinear
    visible segments (angle <= 0.5 deg, offset <= 0.15 mm) forming a chain of >= 4 dashes with regular gaps
    (gap 0.3-4 mm and shorter than the dash, each within 20 % of the median gap) become ONE line --
    'hidden' when the dashes are equal (CV < 0.25, mean 1.5-8 mm), 'center' when long/short alternate
    (ratio >= 2, each kind CV < 0.25). Anything irregular stays as drawn. Counted in the page report as
    `dashes_merged` (chains) and `dash_segments_merged`. NOT handled: a circle/arc dashed as many tiny arcs.
Scans / raster-only pages are refused: there is nothing to extract (use prepare_drawing + vision).
"""
import math
import re

try:
    from . import dxf_read
except ImportError:                 # run as a script
    import dxf_read

_K = 25.4 / 72.0                   # PDF points -> millimetres of PAPER
_SCALE_RE = re.compile(r"(?:SCALE|MA(?:ß|SS|SS?)STAB|ÉCHELLE|ECHELLE|\bM\b)\s*[:=]?\s*"
                       r"(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)", re.I)
_NUM_TOKEN = re.compile(r"^[±]?\d+(?:[.,]\d+)?$")
_DIA_TOKEN = re.compile(r"^[ØøΦφ⌀∅]\s*(\d+(?:[.,]\d+)?)$")
_RAD_TOKEN = re.compile(r"^R\s*(\d+(?:[.,]\d+)?)$")


def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


def _ezdxf():
    try:
        import ezdxf
        return ezdxf
    except Exception as ex:  # noqa: BLE001
        raise RuntimeError("ezdxf is required for PDF drawing import - pip install ezdxf (%s)" % ex)


# --------------------------------------------------------------------------- geometry helpers
def _circle3(a, b, c):
    """Circle through three points -> (cx, cy, r) or None when (nearly) collinear."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _bez(p0, p1, p2, p3, t):
    u = 1.0 - t
    return tuple(u ** 3 * p0[i] + 3 * u * u * t * p1[i] + 3 * u * t * t * p2[i] + t ** 3 * p3[i] for i in (0, 1))


def _bezier_arc(p0, p1, p2, p3):
    """A cubic that is (nearly) a circular arc -> (cx, cy, r, ccw) else None. Checked at t=.25/.75."""
    fit = _circle3(p0, _bez(p0, p1, p2, p3, 0.5), p3)
    if not fit:
        return None
    cx, cy, r = fit
    if r > 5000.0 or r < 0.05:
        return None
    for t in (0.25, 0.75):
        q = _bez(p0, p1, p2, p3, t)
        if abs(math.hypot(q[0] - cx, q[1] - cy) - r) > max(0.02, 0.01 * r):
            return None
    m = _bez(p0, p1, p2, p3, 0.5)
    cross = (m[0] - p0[0]) * (p3[1] - m[1]) - (m[1] - p0[1]) * (p3[0] - m[0])
    return cx, cy, r, cross > 0


def _dash_class(dashes):
    nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", (dashes or "").split("]")[0])]
    if not nums:
        return "visible"
    return "hidden" if len(nums) <= 2 else "center"


def _cv(v):
    m = sum(v) / len(v)
    if m <= 0:
        return 9.0
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) / m


def _classify_chain(segs):
    """segs: [(s0, s1)] sorted along the line -> 'hidden' | 'center' | None (not a regular dash chain)."""
    n = len(segs)
    if n < 4:
        return None
    L = [b - a for a, b in segs]
    g = [segs[i + 1][0] - segs[i][1] for i in range(n - 1)]
    gm = sorted(g)[len(g) // 2]
    if not 0.3 <= gm <= 4.0 or any(abs(x - gm) > 0.2 * gm + 0.02 for x in g):
        return None
    longs, shorts = (L[0::2], L[1::2]) if L[0] >= L[1] else (L[1::2], L[0::2])
    ml, ms = sum(longs) / len(longs), sum(shorts) / len(shorts)
    if ms > 0.1 and ml / ms >= 2.0 and _cv(longs) < 0.25 and _cv(shorts) < 0.25             and ml <= 40.0 and 0.3 <= ms <= 6.0 and gm < ml:
        return "center"
    mean = sum(L) / n
    if _cv(L) < 0.25 and 1.5 <= mean <= 8.0 and gm < mean:
        return "hidden"
    return None


def _merge_baked_dashes(prims):
    """Replace chains of short collinear solid segments with regular gaps by one hidden/center line.
    -> (new prims, chains merged, segments consumed). Buckets by angle then offset, so ~O(n log n)."""
    cand = []                                                  # (angle, i, ux, uy, a, b)
    for i, p in enumerate(prims):
        if p[0] != "line" or p[3] != "visible":
            continue
        a, b = p[1], p[2]
        ln = math.hypot(b[0] - a[0], b[1] - a[1])
        if not 0.3 <= ln <= 40.0:
            continue
        ux, uy = (b[0] - a[0]) / ln, (b[1] - a[1]) / ln
        if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
            ux, uy = -ux, -uy
        ang = math.degrees(math.atan2(uy, ux))
        if ang < -89.5:
            ang += 180.0
        cand.append((ang, i, ux, uy, a, b))
    cand.sort()
    drop, added, chains = set(), [], 0
    k = 0
    while k < len(cand):
        e = k + 1                                              # angle cluster: span <= 0.5 deg
        while e < len(cand) and cand[e][0] - cand[k][0] <= 0.5:
            e += 1
        cl = cand[k:e]
        k = e
        if len(cl) < 4:
            continue
        ux, uy = cl[0][2], cl[0][3]
        nx, ny = -uy, ux
        rows = []                                              # (offset, s0, s1, idx)
        for _ang, i, _x, _y, a, b in cl:
            sa, sb = a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy
            rows.append(((a[0] + b[0]) * 0.5 * nx + (a[1] + b[1]) * 0.5 * ny, min(sa, sb), max(sa, sb), i))
        rows.sort()
        j = 0
        while j < len(rows):
            m = j + 1                                          # offset group: neighbours <= 0.15, span <= 0.3 mm
            while m < len(rows) and rows[m][0] - rows[m - 1][0] <= 0.15 and rows[m][0] - rows[j][0] <= 0.3:
                m += 1
            grp = sorted(rows[j:m], key=lambda r: r[1])
            j = m
            if len(grp) < 4:
                continue
            q = 0
            while q < len(grp):                                # runs: consecutive with a plausible gap
                r = q + 1
                while r < len(grp) and -0.1 <= grp[r][1] - grp[r - 1][2] <= 4.0:
                    r += 1
                run = grp[q:r]
                q = r
                x = 0
                while len(run) - x >= 4:
                    best = None
                    for y in range(x + 4, min(len(run), x + 400) + 1):
                        c = _classify_chain([(t[1], t[2]) for t in run[x:y]])
                        if c:
                            best = (y, c)
                    if not best:
                        x += 1
                        continue
                    y, c = best
                    ch = run[x:y]
                    off = sum(t[0] for t in ch) / len(ch)
                    s0, s1 = ch[0][1], ch[-1][2]
                    added.append(("line", (s0 * ux + off * nx, s0 * uy + off * ny),
                                  (s1 * ux + off * nx, s1 * uy + off * ny), c))
                    drop.update(t[3] for t in ch)
                    chains += 1
                    x = y
    if not chains:
        return prims, 0, 0
    return [p for i, p in enumerate(prims) if i not in drop] + added, chains, len(drop)


def _polyline_len(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1))


# --------------------------------------------------------------------------- the page -> primitives
def _extract(page):
    """-> (prims, arrows, texts) in PAPER mm, y UP.  prims: [('line', a, b, cls) | ('arc', c, r, a0, a1, cls)
    | ('circle', c, r, cls)]; arrows: [(tip, axis_unit, length)]; texts: [(string, x, y, height, rot_deg)]."""
    H = page.rect.height

    def P(p):
        return (p[0] * _K, (H - p[1]) * _K)

    prims, arrows, pens = [], [], []
    for path in page.get_drawings():
        items = path.get("items") or []
        kind = path.get("type") or "s"
        n_before = len(prims)
        if kind == "f":                                    # pure fill: an arrowhead triangle, or glyph ink
            pts = []
            for it in items:
                if it[0] == "l":
                    pts += [P(it[1]), P(it[2])]
                elif it[0] == "qu":
                    pts = []
                    break
            uniq = []
            for p in pts:
                if not any(math.hypot(p[0] - q[0], p[1] - q[1]) < 1e-3 for q in uniq):
                    uniq.append(p)
            if len(uniq) == 3:
                edges = sorted(((math.hypot(uniq[i][0] - uniq[(i + 1) % 3][0], uniq[i][1] - uniq[(i + 1) % 3][1]), i)
                                for i in range(3)))
                base_len, bi = edges[0]
                tip = uniq[(bi + 2) % 3]
                bm = ((uniq[bi][0] + uniq[(bi + 1) % 3][0]) / 2.0, (uniq[bi][1] + uniq[(bi + 1) % 3][1]) / 2.0)
                length = math.hypot(tip[0] - bm[0], tip[1] - bm[1])
                if 1.0 <= length <= 9.0 and length >= 1.5 * base_len and base_len > 0.05:
                    arrows.append((tip, ((tip[0] - bm[0]) / length, (tip[1] - bm[1]) / length), length))
            continue
        cls = _dash_class(path.get("dashes"))
        arc_run = []                                       # consecutive bezier arcs of one circle

        def flush():
            if not arc_run:
                return
            cx = sum(a[0] for a in arc_run) / len(arc_run)
            cy = sum(a[1] for a in arc_run) / len(arc_run)
            r = sum(a[2] for a in arc_run) / len(arc_run)
            sweep = sum(a[6] for a in arc_run)
            if sweep >= 2 * math.pi - 0.05:
                prims.append(("circle", (cx, cy), r, cls))
            else:
                first, last = arc_run[0], arc_run[-1]
                a_s = math.atan2(first[4][1] - cy, first[4][0] - cx)
                a_e = math.atan2(last[5][1] - cy, last[5][0] - cx)
                if first[3]:                               # CCW as drawn
                    prims.append(("arc", (cx, cy), r, math.degrees(a_s), math.degrees(a_e), cls))
                else:                                      # drawn CW -> the same arc runs CCW end -> start
                    prims.append(("arc", (cx, cy), r, math.degrees(a_e), math.degrees(a_s), cls))
            arc_run.clear()

        for it in items:
            op = it[0]
            if op == "l":
                flush()
                a, b = P(it[1]), P(it[2])
                if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-4:
                    prims.append(("line", a, b, cls))
            elif op == "re" or op == "qu":
                flush()
                if op == "re":
                    r = it[1]
                    c = [P((r.x0, r.y0)), P((r.x1, r.y0)), P((r.x1, r.y1)), P((r.x0, r.y1))]
                else:
                    q = it[1]
                    c = [P(q.ul), P(q.ur), P(q.lr), P(q.ll)]
                for i in range(4):
                    prims.append(("line", c[i], c[(i + 1) % 4], cls))
            elif op == "c":
                p0, p1, p2, p3 = (P(it[i]) for i in range(1, 5))
                fit = _bezier_arc(p0, p1, p2, p3)
                if fit:
                    cx, cy, r, ccw = fit
                    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
                    a1 = math.atan2(p3[1] - cy, p3[0] - cx)
                    span = (a1 - a0) % (2 * math.pi) if ccw else (a0 - a1) % (2 * math.pi)
                    if arc_run and (abs(arc_run[-1][0] - cx) > 0.03 or abs(arc_run[-1][1] - cy) > 0.03
                                    or abs(arc_run[-1][2] - r) > 0.03 or arc_run[-1][3] != ccw):
                        flush()
                    arc_run.append((cx, cy, r, ccw, p0, p3, span))
                else:                                      # a free curve -> 8 chords (the reader refits ellipses)
                    flush()
                    pts = [_bez(p0, p1, p2, p3, i / 8.0) for i in range(9)]
                    for i in range(8):
                        prims.append(("line", pts[i], pts[i + 1], cls))
        flush()
        pens.extend([round((path.get("width") or 0.0) * _K, 3)] * (len(prims) - n_before))

    texts = []
    for blk in page.get_text("dict").get("blocks", []):
        for ln in blk.get("lines", []):
            dx, dy = ln.get("dir", (1.0, 0.0))
            rot = math.degrees(math.atan2(-dy, dx))
            for sp in ln.get("spans", []):
                s = (sp.get("text") or "").strip()
                if not s:
                    continue
                ox, oy = sp["origin"]
                texts.append((s, ox * _K, (H - oy) * _K, max(sp.get("size", 8.0) * _K, 0.5), rot))
    return prims, arrows, texts, pens


def _pen_stats(prims, pens):
    """[{width_mm, primitives, curves}] heaviest-population first, plus the SUGGESTED part pen: CAD PDFs draw the
    part with one pen and everything else (dimensions, outlined text, centre lines, the title-block grid) with
    others, and the part pen is the one rich in arcs/circles. A suggestion only: ties and tiny drawings are
    ambiguous, so the caller can pass pen_mm."""
    by = {}
    for pr, w in zip(prims, pens):
        d = by.setdefault(w, {"width_mm": w, "primitives": 0, "curves": 0})
        d["primitives"] += 1
        if pr[0] in ("arc", "circle"):
            d["curves"] += 1
    rows = sorted(by.values(), key=lambda d: -d["primitives"])
    best = max(rows, key=lambda d: (d["curves"], d["primitives"]), default=None)
    suggested = best["width_mm"] if best and best["curves"] >= 3 and len(rows) > 1 else None
    return rows, suggested


def _pair_arrows(arrows):
    """Greedy mutual pairing of opposed, collinear arrowheads -> [(tipA, tipB)] by ascending distance."""
    cands = []
    for i in range(len(arrows)):
        for j in range(i + 1, len(arrows)):
            (ta, aa, _la), (tb, ab, _lb) = arrows[i], arrows[j]
            if aa[0] * ab[0] + aa[1] * ab[1] > -0.95:
                continue
            d = math.hypot(ta[0] - tb[0], ta[1] - tb[1])
            if d < 3.0:
                continue
            ux, uy = (tb[0] - ta[0]) / d, (tb[1] - ta[1]) / d
            if abs(ux * aa[1] - uy * aa[0]) > 0.05:         # the arrow axis must lie along the tip-to-tip line
                continue
            cands.append((d, i, j))
    cands.sort()
    used, out = set(), []
    for d, i, j in cands:
        if i in used or j in used:
            continue
        used.update((i, j))
        out.append((arrows[i][0], arrows[j][0]))
    return out


def _num(s):
    return float(s.replace(",", "."))


def _detect_scale(texts, override=""):
    if override:
        m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)\s*$", override)
        if not m:
            raise ValueError("scale must look like '1:2' (paper:true), got %r" % override)
        return _num(m.group(2)) / _num(m.group(1)), override.strip(), "argument"
    blob = "  ".join(t[0] for t in texts)
    m = _SCALE_RE.search(blob)
    if m and _num(m.group(1)) > 0 and _num(m.group(2)) > 0:
        return _num(m.group(2)) / _num(m.group(1)), "%s:%s" % (m.group(1), m.group(2)), "title block text"
    return 1.0, "1:1", "ASSUMED (no scale text found) -- check the title block"


# --------------------------------------------------------------------------- the build
def _seg_point_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _is_dimension_furniture(a, b, dims):
    """A line that is a recognised dimension's own DIMENSION LINE (collinear, between/around the arrow tips)
    or one of its EXTENSION LINES (perpendicular, passing through an arrow tip). Left in, they cluster into
    bogus 'views' -- exactly as they would on any PDF."""
    la = math.hypot(b[0] - a[0], b[1] - a[1])
    if la < 1e-6:
        return False
    for ta, tb in dims:
        d = math.hypot(tb[0] - ta[0], tb[1] - ta[1])
        ux, uy = (tb[0] - ta[0]) / d, (tb[1] - ta[1]) / d
        lx, ly = (b[0] - a[0]) / la, (b[1] - a[1]) / la
        along = abs(lx * ux + ly * uy)
        if along > 0.995:                                        # parallel: the dimension line itself
            off = abs(-(a[0] - ta[0]) * uy + (a[1] - ta[1]) * ux)
            pa = (a[0] - ta[0]) * ux + (a[1] - ta[1]) * uy
            pb = (b[0] - ta[0]) * ux + (b[1] - ta[1]) * uy
            if off <= 0.4 and max(pa, pb) >= -12.0 and min(pa, pb) <= d + 12.0 and la <= d + 30.0:
                return True
        elif along < 0.05 and la <= 80.0:                        # perpendicular through a tip: extension line
            if min(_seg_point_dist(ta, a, b), _seg_point_dist(tb, a, b)) <= 0.5:
                return True
    return False


def build_dxf_doc(page, scale_override="", pen_mm=0.0):
    """-> (ezdxf doc, report). The document is what `dxf_read.read_doc` consumes.
    pen_mm > 0 keeps ONLY primitives stroked with that pen width as part geometry (the rest -- dimension lines,
    outlined text, centre lines, title-block grid -- are dropped; arrowheads and text still feed dimension
    recognition). 0 = every pen (fine for a drawing made with ONE pen; CAD PDFs usually are not)."""
    ezdxf = _ezdxf()
    prims, arrows, texts, pens = _extract(page)
    pen_rows, pen_suggested = _pen_stats(prims, pens)
    pen_used = None
    if pen_mm and pen_mm > 0:
        # the SHEET BORDER must survive the filter whatever its pen: the reader recognises the frame as the cluster that
        # encloses everything else, and without a border the part itself (a plate enclosing its holes) would be taken for it
        span = 0.85 * min(page.rect.width, page.rect.height) * _K

        def _border(pr):
            return pr[0] == "line" and math.hypot(pr[1][0] - pr[2][0], pr[1][1] - pr[2][1]) >= span

        keep = [i for i, w in enumerate(pens) if abs(w - pen_mm) <= 0.006 or _border(prims[i])]
        prims = [prims[i] for i in keep]
        pen_used = pen_mm
    prims, n_chains, n_segs = _merge_baked_dashes(prims)
    if len(prims) < 5:
        raise ValueError("this page has almost no vector geometry (%d paths) -- it is a scan or an image; "
                         "use prepare_drawing and read it with vision" % len(prims))
    factor, scale_label, scale_src = _detect_scale(texts, scale_override)

    def center(t):
        return (t[1] + len(t[0]) * t[3] * 0.3, t[2] + t[3] * 0.4)

    # ---- 1. recognise dimensions (pure geometry + text; nothing is drawn yet) ----------------------
    num_tokens = [(i, t) for i, t in enumerate(texts) if _NUM_TOKEN.match(t[0].replace(" ", ""))]
    dia_tokens = [(i, t) for i, t in enumerate(texts) if _DIA_TOKEN.match(t[0].strip())]
    rad_tokens = [(i, t) for i, t in enumerate(texts) if _RAD_TOKEN.match(t[0].strip())]
    used_tokens, made = set(), {"linear": 0, "diameter": 0, "radius": 0}
    linear, rounds = [], []                                      # (ta, tb, text) / (kind, c, r, text)

    for ta, tb in _pair_arrows(arrows):
        d = math.hypot(ta[0] - tb[0], ta[1] - tb[1])
        ux, uy = (tb[0] - ta[0]) / d, (tb[1] - ta[1]) / d
        best = None
        for i, t in num_tokens:
            if i in used_tokens:
                continue
            cx, cy = center(t)
            along = (cx - ta[0]) * ux + (cy - ta[1]) * uy
            perp = abs(-(cx - ta[0]) * uy + (cy - ta[1]) * ux)
            if -8.0 <= along <= d + 8.0 and perp <= 8.0:
                score = perp + abs(along - d / 2.0) * 0.2
                if best is None or score < best[0]:
                    best = (score, i, t)
        if best:
            used_tokens.add(best[1])
            linear.append((ta, tb, best[2][0].strip()))
            made["linear"] += 1

    circles = [(p[1], p[2]) for p in prims if p[0] == "circle"]
    arcs = [(p[1], p[2]) for p in prims if p[0] in ("circle", "arc")]

    def match_round(tokens, table, kind):
        """A `Ø8` / `R5` token -> the ONE nearest circle/arc whose size agrees (true = paper x factor)."""
        for i, t in tokens:
            if i in used_tokens:
                continue
            m = (_DIA_TOKEN if kind == "diameter" else _RAD_TOKEN).match(t[0].strip())
            paper = _num(m.group(1)) / factor                    # the size the PAPER geometry must have
            cx, cy = center(t)
            span = 2.0 if kind == "diameter" else 1.0
            hits = sorted((math.hypot(cx - c[0][0], cy - c[0][1]), c) for c in table
                          if abs(c[1] * span - paper) <= max(0.3, 0.02 * paper))
            if not hits:
                continue
            c, r = hits[0][1]
            used_tokens.add(i)
            rounds.append((kind, c, r, t[0].strip()))
            made[kind] += 1

    match_round(dia_tokens, circles, "diameter")
    match_round(rad_tokens, arcs, "radius")

    # ---- 2. build the DXF ---------------------------------------------------------------------------
    doc = ezdxf.new("R2010", setup=True)
    doc.units = 4                                               # PAPER millimetres
    if "HIDDEN" not in doc.linetypes:
        doc.linetypes.add("HIDDEN", pattern=[9.525, 6.35, -3.175], description="Hidden __ __ __")
    # the SLD* dimstyle name is how read_doc finds the SHEET scale; DIMLFAC = true/paper
    ds = doc.dimstyles.duplicate_entry("Standard", "SLD_PDF")
    ds.dxf.dimlfac = factor
    msp = doc.modelspace()
    ltype = {"visible": "Continuous", "hidden": "HIDDEN", "center": "CENTER"}
    dim_pairs = [(ta, tb) for ta, tb, _t in linear]
    dropped = 0
    for pr in prims:
        attr = {"linetype": ltype.get(pr[-1], "Continuous")}
        if pr[0] == "line":
            if dim_pairs and _is_dimension_furniture(pr[1], pr[2], dim_pairs):
                dropped += 1
                continue
            msp.add_line(pr[1], pr[2], dxfattribs=attr)
        elif pr[0] == "circle":
            msp.add_circle(pr[1], pr[2], dxfattribs=attr)
        else:
            msp.add_arc(pr[1], pr[2], pr[3], pr[4], dxfattribs=attr)

    for s, x, y, h, rot in texts:                               # every string, so notes/callouts/scale survive
        msp.add_text(s, dxfattribs={"insert": (x, y), "height": h, "rotation": rot})
    for ta, tb, txt in linear:
        msp.add_aligned_dim(p1=ta, p2=tb, distance=0.0, dimstyle="SLD_PDF", text=txt).render()
    for kind, c, r, txt in rounds:
        if kind == "diameter":
            msp.add_diameter_dim(center=c, radius=r, angle=45.0, dimstyle="SLD_PDF", text=txt).render()
        else:
            msp.add_radius_dim(center=c, radius=r, angle=45.0, dimstyle="SLD_PDF", text=txt).render()

    unassigned = [{"text": t[0].strip(), "at": [round(center(t)[0], 1), round(center(t)[1], 1)]}
                  for i, t in num_tokens + dia_tokens + rad_tokens if i not in used_tokens]
    report = {"page_size_mm": [round(page.rect.width * _K, 1), round(page.rect.height * _K, 1)],
              "scale": scale_label, "scale_factor": factor, "scale_source": scale_src,
              "paths": len(prims), "dashes_merged": n_chains, "dash_segments_merged": n_segs,
              "arrowheads": len(arrows), "dimensions_made": made,
              "dimension_lines_dropped": dropped, "unassigned_numbers": unassigned[:80],
              "pens": pen_rows[:8], "pen_used_mm": pen_used, "pen_suggested_mm": pen_suggested}
    return doc, report


def read_pdf(path, cfg, page=1, scale="", pen_mm=0.0):
    """Read page `page` (1-based) of a VECTOR PDF -> the draw dialect (+ sheet.pdf report).
    pen_mm: keep only that stroke width as part geometry (see build_dxf_doc); sheet.pdf.pens / pen_suggested_mm
    say which pens the page uses."""
    fitz = _fitz()
    pdf = fitz.open(path)
    try:
        if not 1 <= page <= pdf.page_count:
            raise ValueError("%s has %d page(s); asked for page %d" % (path, pdf.page_count, page))
        doc, report = build_dxf_doc(pdf[page - 1], scale, pen_mm)
    finally:
        pdf.close()
    auditor = doc.audit()
    art = dxf_read.read_doc(doc, auditor, path, cfg, source_kind="pdf")
    art["sheet"]["pdf"] = dict(report, page=page)
    return art
