"""sections.py -- slice a mesh into closed loops and turn each loop into sketch primitives.

    section_loops(mesh, z)            -> nested loops of the plane z = const (build frame: slice along Z)
    fit_loop(pts, tol, ...)           -> circle | ellipse | lines+arcs | closed splines (residual gated)
    resample_loop(pts, n)             -> uniform, CCW, rightmost-point-first resampling (loft alignment)

Chaining follows cad-planner/drawing/contour.py: endpoints that coincide are the same vertex (here via
a KD-tree cluster instead of a rounded key, because plane/edge intersections of neighbouring
triangles agree only to ~1e-15), a vertex of degree 2 continues the chain, anything else ends it,
and nesting is decided by containment of ONE vertex of the candidate child. Ellipses reuse
drawing/curvefit.fit_ellipse + deviation (loaded by file path, so drawing/__init__ and its heavy
imports are never triggered).

"the fitter PROPOSES, the RESIDUAL decides": a primitive is emitted only when every contour point is
within `tol` of it; otherwise the loop is carried as closed splines through resampled points.
All lengths are METERS, angles radians. 2D = the sketch plane (u, v) = world (x, y) of the slice.
"""
from __future__ import annotations

import importlib.util
import math
import os

try:
    from .geom import deps
except ImportError:
    from geom import deps

_CURVEFIT = None


def _curvefit():
    global _CURVEFIT
    if _CURVEFIT is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "drawing", "curvefit.py")
        try:
            spec = importlib.util.spec_from_file_location("_mesh_curvefit", os.path.normpath(path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _CURVEFIT = mod
        except Exception:                                # noqa: BLE001
            _CURVEFIT = False
    return _CURVEFIT or None


def _nd(v, nd=9):
    return round(float(v), nd) + 0.0


# --------------------------------------------------------------------------- slicing
def _signed_area(np, p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def _pip(np, pt, poly):
    x, y = pt
    x0, y0 = poly[:, 0], poly[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    cond = (y0 > y) != (y1 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xx = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return bool(np.count_nonzero(cond & (xx > x)) % 2)


def chain_segments(segs2d, eps):
    """segs2d (n,2,2) -> list of closed loops (arrays (k,2)); open chains are dropped."""
    np, _ = deps()
    from scipy.spatial import cKDTree
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(segs2d)
    if n == 0:
        return []
    E = segs2d.reshape(-1, 2)
    tree = cKDTree(E)
    pairs = tree.query_pairs(eps, output_type="ndarray")
    m = len(E)
    if len(pairs):
        g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(m, m))
        _, lab = connected_components(g, directed=False)
    else:
        lab = np.arange(m)
    lab = np.asarray(lab)
    nv = int(lab.max()) + 1
    pos = np.zeros((nv, 2))
    cnt = np.bincount(lab, minlength=nv).astype(float)
    pos[:, 0] = np.bincount(lab, weights=E[:, 0], minlength=nv) / cnt
    pos[:, 1] = np.bincount(lab, weights=E[:, 1], minlength=nv) / cnt
    a, b = lab[0::2], lab[1::2]
    inc = {}
    for si in range(n):
        if a[si] == b[si]:
            continue
        inc.setdefault(int(a[si]), []).append(si)
        inc.setdefault(int(b[si]), []).append(si)
    used = np.zeros(n, dtype=bool)
    loops = []
    for seed in range(n):
        if used[seed] or a[seed] == b[seed]:
            continue
        used[seed] = True
        chain = [int(a[seed]), int(b[seed])]
        closed = False
        while True:
            at = chain[-1]
            if at == chain[0] and len(chain) > 2:
                closed = True
                break
            nxt = [s for s in inc.get(at, []) if not used[s]]
            if not nxt:
                break
            s = nxt[0]
            used[s] = True
            chain.append(int(b[s]) if a[s] == at else int(a[s]))
        if closed and len(chain) > 3:
            loops.append(pos[chain[:-1]])
    return loops


def section_loops(mesh, z, eps=None):
    """Slice with the plane z = `z` (mesh already in the build frame). -> list of
    {pts (k,2), area (signed, + = CCW), depth, role ('outer'|'hole'), parent}. Outer loops are CCW,
    holes CW, by containment depth (even = material boundary, odd = hole)."""
    np, trimesh = deps()
    diag = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))
    eps = eps or 2e-7 * diag
    try:
        segs = trimesh.intersections.mesh_plane(mesh, [0.0, 0.0, 1.0], [0.0, 0.0, float(z)])
    except Exception:                                    # noqa: BLE001
        segs = np.zeros((0, 2, 3))
    if len(segs) == 0:
        return []
    raw = chain_segments(np.asarray(segs)[:, :, :2], eps)
    loops = []
    for p in raw:
        keep = np.r_[True, np.linalg.norm(np.diff(p, axis=0), axis=1) > eps * 0.5]
        p = p[keep]
        if len(p) < 3:
            continue
        loops.append({"pts": p, "area": _signed_area(np, p)})
    loops = [l for l in loops if abs(l["area"]) > (diag * 1e-5) ** 2]
    order = sorted(range(len(loops)), key=lambda i: -abs(loops[i]["area"]))
    loops = [loops[i] for i in order]
    for i, lp in enumerate(loops):
        depth, parent, best = 0, None, None
        for j, other in enumerate(loops):
            if j == i or abs(other["area"]) <= abs(lp["area"]):
                continue
            if _pip(np, lp["pts"][0], other["pts"]):
                depth += 1
                if best is None or abs(other["area"]) < best:
                    best, parent = abs(other["area"]), j
        lp["depth"], lp["parent"] = depth, parent
        lp["role"] = "outer" if depth % 2 == 0 else "hole"
        want_ccw = lp["role"] == "outer"
        if (lp["area"] > 0) != want_ccw:
            lp["pts"] = lp["pts"][::-1].copy()
            lp["area"] = -lp["area"]
    return loops


def net_area(loops):
    return sum(abs(l["area"]) * (1 if l["role"] == "outer" else -1) for l in loops)


# --------------------------------------------------------------------------- polyline utilities
def _dp_mask(np, p, tol, closed=True):
    """Douglas-Peucker keep-mask for a polyline (iterative)."""
    n = len(p)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = p[i], p[j]
        d = b - a
        L = math.hypot(d[0], d[1])
        seg = p[i + 1:j]
        if L < 1e-15:
            dist = np.linalg.norm(seg - a, axis=1)
        else:
            dist = np.abs(d[0] * (seg[:, 1] - a[1]) - d[1] * (seg[:, 0] - a[0])) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            keep[i + 1 + k] = True
            stack.append((i, i + 1 + k))
            stack.append((i + 1 + k, j))
    return keep


def simplify_closed(pts, tol):
    """Douglas-Peucker on a closed loop (split at the two mutually farthest-ish points)."""
    np, _ = deps()
    n = len(pts)
    if n <= 8:
        return pts
    i0 = int(np.argmax(np.linalg.norm(pts - pts[0], axis=1)))
    a = _dp_mask(np, pts[:i0 + 1], tol)
    b = _dp_mask(np, np.vstack([pts[i0:], pts[:1]]), tol)
    keep = np.zeros(n, dtype=bool)
    keep[:i0 + 1] = a
    keep[i0:] |= b[:-1]
    keep[0] = True
    return pts[keep]


def resample_loop(pts, n, start="rightmost", ccw=True):
    """Resample a closed loop to n points uniformly by arc length, CCW, starting at the rightmost
    point (max x) so loft profiles line up. Returns (n,2) WITHOUT the repeated first point."""
    np, _ = deps()
    p = np.asarray(pts, dtype=float)
    if _signed_area(np, p) < 0 and ccw:
        p = p[::-1]
    k0 = int(np.argmax(p[:, 0]))
    p = np.roll(p, -k0, axis=0)
    q = np.vstack([p, p[:1]])
    seg = np.linalg.norm(np.diff(q, axis=0), axis=1)
    s = np.r_[0.0, np.cumsum(seg)]
    t = np.linspace(0.0, s[-1], n, endpoint=False)
    return np.c_[np.interp(t, s, q[:, 0]), np.interp(t, s, q[:, 1])]


# --------------------------------------------------------------------------- fitting
def _kasa(np, q):
    A = np.c_[2 * q, np.ones(len(q))]
    b = (q * q).sum(axis=1)
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    c = sol[:2]
    return c, math.sqrt(max(sol[2] + c @ c, 0.0))


def _arc_through(np, P, i, j):
    """Arc over P[i..j] constrained to equal radii at the ends. -> (c, r, dev, ccw) or None."""
    q = P[i:j + 1]
    if len(q) < 4:
        return None
    c, r = _kasa(np, q)
    a, b = q[0], q[-1]
    chord = b - a
    L = float(np.linalg.norm(chord))
    if L < 1e-12 or not math.isfinite(r) or r > 400.0 * L:
        return None
    mid = (a + b) / 2.0
    nrm = np.array([-chord[1], chord[0]]) / L
    # centre on the perpendicular bisector, nearest to the free fit
    c = mid + nrm * float((c - mid) @ nrm)
    r = float(np.linalg.norm(a - c))
    dev = float(np.abs(np.linalg.norm(q - c, axis=1) - r).max())
    mids = (q[1:] + q[:-1]) / 2.0                       # chord midpoints: the sag test that keeps a
    dev = max(dev, float(np.abs(np.linalg.norm(mids - c, axis=1) - r).max()))   # rectangle a rectangle
    m = q[len(q) // 2]
    cross = (m[0] - a[0]) * (b[1] - m[1]) - (m[1] - a[1]) * (b[0] - m[0])
    ccw = cross > 0
    # sweep guard: < ~330 deg
    a0 = math.atan2(a[1] - c[1], a[0] - c[0])
    a1 = math.atan2(b[1] - c[1], b[0] - c[0])
    sweep = ((a1 - a0) if ccw else (a0 - a1)) % (2 * math.pi)
    if sweep > math.radians(330) or sweep < math.radians(3):
        return None
    return c, r, dev, ccw


def _line_dev(np, P, i, j):
    a, b = P[i], P[j]
    d = b - a
    L = float(np.hypot(d[0], d[1]))
    if L < 1e-15:
        return float(np.linalg.norm(P[i:j + 1] - a, axis=1).max())
    seg = P[i + 1:j]
    if len(seg) == 0:
        return 0.0
    return float((np.abs(d[0] * (seg[:, 1] - a[1]) - d[1] * (seg[:, 0] - a[0])) / L).max())


def _greedy(np, P, tol, max_prims):
    """P: closed polyline as (n+1,2) with P[-1] == P[0]. -> list of ('line'|'arc', i, j, extra) or None."""
    n = len(P) - 1
    pieces, i = [], 0
    while i < n:
        j_line = i + 1
        while j_line + 1 <= n and _line_dev(np, P, i, j_line + 1) <= tol:
            j_line += 1
        j_arc, arc = i, None
        j = i + 3
        miss = 0
        while j <= n:
            r = _arc_through(np, P, i, j)
            if r is not None and r[2] <= tol and _line_dev(np, P, i, j) > tol:
                j_arc, arc, miss = j, r, 0
            else:
                miss += 1
                if miss >= 3:
                    break
            j += 1
        if arc is not None and j_arc > j_line + 1:
            pieces.append(("arc", i, j_arc, arc))
            i = j_arc
        else:
            pieces.append(("line", i, j_line, None))
            i = j_line
        if len(pieces) > max_prims:
            return None
    return pieces


def _sharpest_start(np, p):
    a = np.roll(p, 1, axis=0) - p
    b = np.roll(p, -1, axis=0) - p
    cosang = (a * b).sum(axis=1) / np.maximum(np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-30)
    return int(np.argmax(cosang))          # smallest interior angle = sharpest turn


def _spline_prims(np, pts, n_points):
    q = resample_loop(pts, n_points)
    half = len(q) // 2
    s1 = q[:half + 1]
    s2 = np.vstack([q[half:], q[:1]])
    out = []
    for s in (s1, s2):
        out.append({"kind": "spline", "points": [_nd(v) for v in s.reshape(-1)]})
    return out


def fit_loop(pts, tol, max_prims=16, spline_points=48, allow_conic=True, force_spline=False):
    """Closed loop (k,2) meters -> {kind, prims, deviation_m}. kind in circle|ellipse|poly|spline."""
    np, _ = deps()
    p = np.asarray(pts, dtype=float)
    far = np.r_[True, np.linalg.norm(np.diff(p, axis=0), axis=1) > 1e-9]
    p = p[far]
    if len(p) > 3 and np.linalg.norm(p[0] - p[-1]) <= 1e-9:
        p = p[:-1]
    p = simplify_closed(p, tol * 0.25) if len(p) > 24 else p
    if force_spline:
        return {"kind": "spline", "prims": _spline_prims(np, p, spline_points), "deviation_m": 0.0}
    if allow_conic and len(p) >= 8:
        c, r = _kasa(np, p)
        dev = float(np.abs(np.linalg.norm(p - c, axis=1) - r).max()) if r > 0 else 1e9
        if r > 0:                                        # chord midpoints too (a half circle + its chord is not a circle)
            mids = (p + np.roll(p, -1, axis=0)) / 2.0
            dev = max(dev, float(np.abs(np.linalg.norm(mids - c, axis=1) - r).max()))
        if dev <= tol and r > 3 * tol:
            return {"kind": "circle", "prims": [{"kind": "circle", "cx": _nd(c[0]), "cy": _nd(c[1]),
                                                 "diameter": _nd(2 * r)}], "deviation_m": dev}
        cf = _curvefit()
        if cf is not None and len(p) >= 12:
            el = cf.fit_ellipse([tuple(v) for v in p])
            if el is not None and el[3] > 3 * tol and el[2] / el[3] > 1.02:
                poly = [tuple(v) for v in p] + [tuple(p[0])]
                d2 = cf.deviation(poly, el)
                if d2 <= tol:
                    cx, cy, rx, ry, rot = el
                    th = math.radians(rot)
                    return {"kind": "ellipse", "deviation_m": d2, "prims": [{
                        "kind": "ellipse", "cx": _nd(cx), "cy": _nd(cy),
                        "x1": _nd(cx + rx * math.cos(th)), "y1": _nd(cy + rx * math.sin(th)),
                        "x2": _nd(cx - ry * math.sin(th)), "y2": _nd(cy + ry * math.cos(th))}]}
    pieces, P = None, None
    n0 = len(p)
    for k0 in dict.fromkeys([_sharpest_start(np, p), 0, n0 // 4, n0 // 2, (3 * n0) // 4]):
        q = np.roll(p, -k0, axis=0)
        Pk = np.vstack([q, q[:1]])
        pk = _greedy(np, Pk, tol, max_prims)
        if pk is not None and (pieces is None or len(pk) < len(pieces)):
            pieces, P = pk, Pk                     # the walk start is arbitrary: keep the leanest cover
    if pieces is None:
        return {"kind": "spline", "prims": _spline_prims(np, p, spline_points), "deviation_m": 0.0}
    prims, worst = [], 0.0
    # seam: the walk started at an arbitrary vertex, so a straight edge may have been cut in two
    if len(pieces) > 2 and pieces[0][0] == "line" and pieces[-1][0] == "line":
        Q = np.vstack([P[pieces[-1][1]:n_pts_of(P)], P[0:pieces[0][2] + 1]])
        a0, b0 = Q[0], Q[-1]
        d = b0 - a0
        Ld = float(np.hypot(d[0], d[1]))
        if Ld > 1e-12 and float((np.abs(d[0] * (Q[:, 1] - a0[1]) - d[1] * (Q[:, 0] - a0[0])) / Ld).max()) <= tol:
            pieces = [("wrapline", pieces[-1][1], pieces[0][2], None)] + pieces[1:-1]
    for kind, i, j, extra in pieces:
        if kind == "wrapline":
            a, b = P[i], P[j]
            kind = "line"
        else:
            a, b = P[i], P[j]
        if kind == "line":
            if np.linalg.norm(b - a) < 1e-10:
                continue
            worst = max(worst, _line_dev(np, P, i, j))
            prims.append({"kind": "line", "x1": _nd(a[0]), "y1": _nd(a[1]), "x2": _nd(b[0]), "y2": _nd(b[1])})
        else:
            c, r, dev, ccw = extra
            worst = max(worst, dev)
            prims.append({"kind": "arc", "cx": _nd(c[0]), "cy": _nd(c[1]), "x1": _nd(a[0]), "y1": _nd(a[1]),
                          "x2": _nd(b[0]), "y2": _nd(b[1]), "dir": 1 if ccw else -1})
    return {"kind": "poly", "prims": prims, "deviation_m": worst}


def n_pts_of(P):
    return len(P) - 1


def signature(fit):
    """Topology signature of a fit: used to decide whether loft profiles can keep their primitives."""
    return (fit["kind"], tuple(pr["kind"] for pr in fit["prims"]))
