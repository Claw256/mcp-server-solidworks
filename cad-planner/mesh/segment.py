"""segment.py -- primitive recognition on an (aligned, metric) mesh -> a COMPACT summary dict.

RANSAC planes and cylinders (numpy/scipy) on an area-uniform surface sample, through-holes vs
pockets, an axisymmetry test (revolve) and a constant-section / stepped-prismatic test (extrude).
The output is counts, dimensions and axes -- never raw arrays -- and ends in a recommended strategy:

    revolve | extrude | stacked_extrude | loft

The mesh is expected in the canonical frame produced by mesh_read (axes ~ X/Y/Z), but nothing here
requires it: axes are reported as unit vectors plus the nearest coordinate letter when aligned.
"""
from __future__ import annotations

import math

try:
    from .geom import deps, sample_surface, SurfaceIndex, rotation_about
except ImportError:
    from geom import deps, sample_surface, SurfaceIndex, rotation_about

AXIS_NAMES = "xyz"


def _axis_letter(np, v, tol=0.9995):
    i = int(np.argmax(np.abs(v)))
    return AXIS_NAMES[i] if abs(v[i]) >= tol else None


def _r(v, nd=6):
    return round(float(v), nd)


def _rl(vs, nd=6):
    return [round(float(x), nd) for x in vs]


# --------------------------------------------------------------------------- planes
def _find_planes(np, P, N, tol, rng, max_planes=12, min_count=40, iters=250):
    alive = np.ones(len(P), dtype=bool)
    planes = []
    cos_a = math.cos(math.radians(8.0))
    for _ in range(max_planes):
        ridx = np.flatnonzero(alive)
        if len(ridx) < min_count:
            break
        Pr, Nr = P[ridx], N[ridx]
        best, best_m = 0, None
        for i in rng.choice(len(ridx), min(iters, len(ridx)), replace=False):
            n = Nr[i]
            m = (np.abs((Pr - Pr[i]) @ n) < tol) & (np.abs(Nr @ n) > cos_a)
            c = int(m.sum())
            if c > best:
                best, best_m = c, m
        if best < min_count:
            break
        pts = Pr[best_m]
        c0 = pts.mean(axis=0)
        n = np.linalg.svd(pts - c0, full_matrices=False)[2][2]
        if (Nr[best_m] @ n).mean() < 0:
            n = -n
        m = (np.abs((Pr - c0) @ n) < tol) & (np.abs(Nr @ n) > cos_a)
        if int(m.sum()) < min_count:
            break
        planes.append({"normal": n, "d": float(n @ c0), "center": Pr[m].mean(axis=0), "count": int(m.sum())})
        alive[ridx[m]] = False
    return planes, alive


# --------------------------------------------------------------------------- cylinders
def _circle_fit_2d(np, q):
    A = np.c_[2 * q, np.ones(len(q))]
    b = (q * q).sum(axis=1)
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    c = sol[:2]
    r2 = sol[2] + c @ c
    return c, math.sqrt(max(r2, 0.0))


def _cyl_inliers(np, P, N, c, a, r, tol):
    q = P - c
    qp = q - np.outer(q @ a, a)
    rd = np.linalg.norm(qp, axis=1)
    rd_safe = np.maximum(rd, 1e-12)
    radial = qp / rd_safe[:, None]
    return (np.abs(rd - r) < tol) & (np.abs(N @ a) < 0.1) & (np.abs((N * radial).sum(axis=1)) > 0.95)


def _find_cylinders(np, P, N, alive, tol, diag, rng, max_cyl=10, min_count=60, iters=160):
    from scipy.spatial import cKDTree
    out = []
    tree = cKDTree(P)
    alive = alive.copy()
    for _ in range(max_cyl):
        ridx = np.flatnonzero(alive)
        if len(ridx) < min_count:
            break
        best, best_h = 0, None
        for i in rng.choice(ridx, min(iters, len(ridx)), replace=False):
            nb = np.asarray(tree.query_ball_point(P[i], r=0.35 * diag), dtype=int)
            nb = nb[alive[nb] & (nb != i)]
            if len(nb) < 5:
                continue
            for j in rng.choice(nb, min(2, len(nb)), replace=False):
                a = np.cross(N[i], N[j])
                s = np.linalg.norm(a)
                if s < 0.25:
                    continue
                a = a / s
                # centre = intersection of the two normal lines in the plane perpendicular to a
                u = N[i] - (N[i] @ a) * a
                v = N[j] - (N[j] @ a) * a
                M = np.c_[u, -v]
                rhs = (P[j] - P[i])
                sol = np.linalg.lstsq(M, rhs, rcond=None)[0]
                c = P[i] + sol[0] * u
                r = abs(sol[0]) * np.linalg.norm(u)
                if not (tol * 3 < r < 2.0 * diag):
                    continue
                m = _cyl_inliers(np, P[ridx], N[ridx], c, a, r, tol)
                cnt = int(m.sum())
                if cnt > best:
                    best, best_h = cnt, (c, a, r, m)
        if best < min_count:
            break
        c, a, r, m = best_h
        pts, nrm = P[ridx][m], N[ridx][m]
        # refine: axis = least-significant direction of the normals, centre/radius = 2D circle fit
        for _k in range(2):
            a = np.linalg.svd(nrm, full_matrices=False)[2][2]
            e1 = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
            e1 /= np.linalg.norm(e1)
            e2 = np.cross(a, e1)
            q = np.c_[(pts - pts.mean(axis=0)) @ e1, (pts - pts.mean(axis=0)) @ e2]
            c2, r = _circle_fit_2d(np, q)
            c = pts.mean(axis=0) + c2[0] * e1 + c2[1] * e2
            m2 = _cyl_inliers(np, P[ridx], N[ridx], c, a, r, tol)
            if int(m2.sum()) < min_count:
                break
            m, pts, nrm = m2, P[ridx][m2], N[ridx][m2]
        if int(m.sum()) < min_count or r <= 0:
            break
        q = pts - c
        t = q @ a
        qp = q - np.outer(t, a)
        rd = np.maximum(np.linalg.norm(qp, axis=1), 1e-12)
        concave = float(((nrm * (qp / rd[:, None])).sum(axis=1)).mean()) < 0
        # angular coverage (how much of the circumference is real surface)
        e1 = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(a, e1)
        ang = np.arctan2(qp @ e2, qp @ e1)
        cover = len(np.unique(np.floor((ang + math.pi) / (2 * math.pi) * 24).astype(int))) / 24.0
        out.append({"axis": a, "center": c, "radius": float(r), "concave": concave,
                    "t0": float(t.min()), "t1": float(t.max()), "count": int(m.sum()), "cover": cover})
        alive[ridx[m]] = False
    return out


def _merge_cylinders(np, cyls, tol):
    merged = []
    for c in cyls:
        for m in merged:
            if (abs(m["axis"] @ c["axis"]) > 0.998 and abs(m["radius"] - c["radius"]) < 2 * tol
                    and m["concave"] == c["concave"]):
                d = c["center"] - m["center"]
                perp = d - (d @ m["axis"]) * m["axis"]
                if np.linalg.norm(perp) < 2 * tol:
                    tm = m["center"] @ m["axis"]
                    lo = min(m["t0"] + tm, c["t0"] + c["center"] @ m["axis"])
                    hi = max(m["t1"] + tm, c["t1"] + c["center"] @ m["axis"])
                    m["t0"], m["t1"] = lo - tm, hi - tm
                    m["count"] += c["count"]
                    m["cover"] = max(m["cover"], c["cover"])
                    break
        else:
            merged.append(dict(c))
    return merged


# --------------------------------------------------------------------------- symmetry / prism
def _axis_candidates(np, P, cyls):
    c = P.mean(axis=0)
    cov = np.cov((P - c).T)
    w, v = np.linalg.eigh(cov)
    cands = [(v[:, i], c) for i in range(3)] + [(np.eye(3)[i], c) for i in range(3)]
    for cy in cyls:
        cands.append((cy["axis"], cy["center"]))
    return cands


def _axisymmetry(np, mesh, idx, P, cands, tol):
    """Rotate a sample about each candidate axis by two incommensurate angles; symmetric iff the
    rotated points stay on the surface. Returns the best (axis, point, p99 deviation) or None."""
    rng = np.random.default_rng(5)
    Q = P[rng.choice(len(P), min(3000, len(P)), replace=False)]
    best = None
    tol_s = max(tol, 0.6 * idx.spacing)
    for a, c in cands:
        a = a / np.linalg.norm(a)
        worst = 0.0
        for ang in (math.radians(37.0), math.radians(113.0)):
            T = rotation_about(a, ang, c)
            R = Q @ T[:3, :3].T + T[:3, 3]
            d = idx.distance(R)
            worst = max(worst, float(np.percentile(d, 99.5)))
            if worst > tol_s * 3:
                break
        if worst <= tol_s and (best is None or worst < best[2]):
            best = (a, c, worst)
    return best


def _prismatic(np, mesh, axis_i, tol):
    n = np.asarray(mesh.face_normals)
    A = np.asarray(mesh.area_faces)
    comp = np.abs(n[:, axis_i])
    side = comp < 0.03
    cap = comp > 0.999
    bad = float(A[~(side | cap)].sum() / A.sum())
    cen = np.asarray(mesh.triangles_center)[:, axis_i]
    lo, hi = float(mesh.bounds[0][axis_i]), float(mesh.bounds[1][axis_i])
    levels = sorted(set([lo, hi] + [float(x) for x in cen[cap]]))
    merged = [levels[0]]
    for z in levels[1:]:
        if z - merged[-1] > max(tol, 1e-9):
            merged.append(z)
    if hi - merged[-1] > max(tol, 1e-9):
        merged.append(hi)
    merged[0], merged[-1] = lo, hi
    if len(merged) < 2:
        merged = [lo, hi]
    return {"axis_i": axis_i, "nonconforming_area_frac": bad, "levels": merged,
            "intervals": len(merged) - 1}


# --------------------------------------------------------------------------- the entry point
def analyze(mesh, tolerance_m=None, seed=7, samples=9000):
    """-> compact summary dict (JSON-able). `tolerance_m` None = auto (0.15% of the bbox diagonal)."""
    np, _ = deps()
    ext = mesh.bounds[1] - mesh.bounds[0]
    diag = float(np.linalg.norm(ext))
    tol = float(tolerance_m) if tolerance_m else max(0.0015 * diag, 1e-5)
    rng = np.random.default_rng(seed)
    P, N = sample_surface(mesh, samples, seed)
    ptol = max(tol, 0.0008 * diag)
    planes, alive = _find_planes(np, P, N, ptol, rng)
    cyls = _merge_cylinders(np, _find_cylinders(np, P, N, alive, ptol, diag, rng), ptol)
    cyls = [c for c in cyls if c["cover"] >= 0.2]

    out_cyl, holes = [], []
    for c in sorted(cyls, key=lambda k: -k["count"])[:10]:
        a = c["axis"]
        proj = np.asarray(mesh.vertices) @ a
        mesh_len = float(proj.max() - proj.min())
        span = c["t1"] - c["t0"]
        thru = bool(c["concave"] and span >= 0.9 * mesh_len)
        rec = {"axis": _rl(a), "axis_letter": _axis_letter(np, a), "center": _rl(c["center"]),
               "radius_m": _r(c["radius"]), "length_m": _r(span), "concave": bool(c["concave"]),
               "through": thru, "coverage": round(c["cover"], 2)}
        out_cyl.append(rec)
        if c["concave"]:
            holes.append({"axis_letter": rec["axis_letter"], "center": rec["center"],
                          "diameter_m": _r(2 * c["radius"]), "through": thru,
                          "depth_m": _r(span)})

    idx = SurfaceIndex(mesh, n=70000, seed=seed)
    sym = _axisymmetry(np, mesh, idx, idx.P, _axis_candidates(np, idx.P, cyls), tol)
    axisym = None
    if sym is not None:
        a, c, dev = sym
        axisym = {"axis": _rl(a), "axis_letter": _axis_letter(np, a), "point": _rl(c),
                  "deviation_m": _r(dev)}

    prisms = [_prismatic(np, mesh, i, tol) for i in range(3)]
    ok = [p for p in prisms if p["nonconforming_area_frac"] <= 0.01 and p["intervals"] <= 24]
    prism = None
    if ok:
        prism = min(ok, key=lambda p: (p["intervals"], -float(ext[p["axis_i"]])))
    prism_out = None
    if prism:
        prism_out = {"axis_letter": AXIS_NAMES[prism["axis_i"]], "intervals": prism["intervals"],
                     "length_m": _r(ext[prism["axis_i"]]),
                     "nonconforming_area_frac": _r(prism["nonconforming_area_frac"], 5)}

    reasons = []
    if prism and prism["intervals"] == 1 and (axisym is None or (axisym["axis_letter"] == AXIS_NAMES[prism["axis_i"]])):
        strategy = "extrude"
        reasons.append("constant cross-section along %s" % AXIS_NAMES[prism["axis_i"]])
    elif axisym:
        strategy = "revolve"
        reasons.append("rotationally symmetric about %s (p99 deviation %.3g mm)"
                       % (axisym["axis_letter"] or "an off-axis line", axisym["deviation_m"] * 1e3))
    elif prism:
        strategy = "stacked_extrude"
        reasons.append("prismatic with %d constant-section steps along %s"
                       % (prism["intervals"], AXIS_NAMES[prism["axis_i"]]))
    else:
        strategy = "loft"
        reasons.append("no exact primitive structure: freeform, approximated by section lofts")

    other_holes = [h for h in holes if not prism or h["axis_letter"] != AXIS_NAMES[prism["axis_i"]]]
    return {
        "bbox_m": _rl(ext), "diag_m": _r(diag),
        "volume_m3": _r(abs(mesh.volume), 12) if mesh.is_watertight else None,
        "area_m2": _r(mesh.area, 9), "watertight": bool(mesh.is_watertight),
        "faces": int(len(mesh.faces)), "tolerance_m": _r(tol, 9),
        "planes": {"count": len(planes),
                   "axis_aligned": sum(1 for p in planes if _axis_letter(np, p["normal"]) is not None),
                   "area_coverage": round(float(sum(p["count"] for p in planes)) / len(P), 3)},
        "cylinders": out_cyl,
        "holes": holes,
        "unmodelled_holes": other_holes if strategy in ("extrude", "stacked_extrude") else [],
        "axisymmetric": axisym, "prismatic": prism_out,
        "recommended_strategy": strategy, "reasons": reasons,
        "_prism": prism,           # internal: levels for the lowering; stripped by public wrappers
    }


def public(summary):
    """The summary without the internal keys (what the adapter returns to the model)."""
    return {k: v for k, v in summary.items() if not k.startswith("_")}
