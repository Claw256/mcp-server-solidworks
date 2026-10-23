"""smoothing.py -- turn a faceted low/medium-poly mesh into a smooth, high-resolution surface.

Two cooperating stages, both feature preserving (sharp edges stay crisp) and NEVER plain Laplacian:

  1. smooth_mesh(mesh, level)      Phong midpoint subdivision + Taubin lambda|mu smoothing
                                   (volume-preserving, non-shrinking) with every vertex on a sharp edge
                                   (dihedral angle > sharp_deg, default 40) or a boundary PINNED, so
                                   chamfers/corners are not rounded; planar regions stay planar because
                                   the average of coplanar neighbours is coplanar. Subdivision is Phong
                                   (tangent-plane) midpoint lifting: interpolating -- original vertices
                                   never move -- so a faceted sphere converges to the sphere instead of
                                   shrinking the way Loop/Catmull-Clark approximation does.
  2. smooth_polyline(pts, tol)     per-loop periodic smoothing spline (scipy splprep) for a slice
                                   contour; vertices whose turn angle exceeds `corner_deg` are corners:
                                   the spline is cut there and each piece is smoothed with its ends held.

Levels: 'off' | 'low' | 'medium' | 'high'. 'auto' resolves from the triangle count (and how coarse
the faces are relative to the bbox diagonal): few/large facets get more smoothing.
apply() wraps stage 1 with the safety net: after smoothing, volume change and max deviation from the
original are measured and the level is stepped down while volume SHRINKS by more than
`max_shrink` (default 0.5 %) or the surface moves farther than `max_dev_frac` of the diagonal.
Growth is allowed (smoothing a faceted sphere legitimately gains volume) and reported.
"""
from __future__ import annotations

import math

try:
    from .geom import deps, SurfaceIndex
except ImportError:
    from geom import deps, SurfaceIndex

LEVELS = ("off", "low", "medium", "high")
SMOOTH_CHOICES = ("auto",) + LEVELS
# level -> (subdivision iterations, Taubin iterations)
_PLAN = {"low": (1, 3), "medium": (2, 5), "high": (2, 8)}
MAX_FACES_OUT = 250000


def choose_level(faces, diag, mean_edge=None):
    """Auto level from triangle count, bumped one step when the facets are visibly coarse."""
    if faces <= 0:
        return "off"
    if faces < 1500:
        lvl = 3
    elif faces < 6000:
        lvl = 2
    elif faces < 20000:
        lvl = 1
    else:
        lvl = 0
    if mean_edge and diag and lvl and mean_edge / diag > 0.06:
        lvl = min(3, lvl + 1)
    return LEVELS[lvl]


def station_multiplier(level):
    return {"off": 1.0, "low": 1.4, "medium": 1.8, "high": 2.2}.get(level, 1.0)


# --------------------------------------------------------------------------- mesh smoothing
def sharp_vertices(mesh, sharp_deg=40.0):
    """Boolean mask of vertices on a sharp edge (dihedral > sharp_deg) or a mesh boundary."""
    np, _ = deps()
    mask = np.zeros(len(mesh.vertices), dtype=bool)
    adj = mesh.face_adjacency
    if len(adj):
        ang = np.asarray(mesh.face_adjacency_angles)
        sharp = ang > math.radians(sharp_deg)
        mask[np.asarray(mesh.face_adjacency_edges)[sharp].reshape(-1)] = True
    try:
        edges = mesh.edges_sorted
        uniq, inv, cnt = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
        mask[uniq[cnt == 1].reshape(-1)] = True
    except Exception:                                    # noqa: BLE001
        pass
    return mask


def _taubin(np, V, F, pinned, iters, lam=0.5, mu=-0.53):
    import scipy.sparse as sp
    n = len(V)
    e = np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    A = sp.coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(n, n))
    A = ((A + A.T) > 0).astype(float).tocsr()
    deg = np.asarray(A.sum(axis=1)).reshape(-1)
    deg[deg == 0] = 1.0
    W = sp.diags(1.0 / deg) @ A
    free = (~pinned)[:, None]
    V = V.copy()
    for _ in range(iters):
        for f in (lam, mu):
            V = V + free * (f * (W @ V - V))
    return V


def _phong_subdivide(np, trimesh, V, F, feature, alpha=0.5):
    """One 1->4 subdivision whose new edge midpoints are lifted onto the tangent-plane (Phong)
    surface of their end vertices. Original vertices do not move (interpolating), flat regions stay
    flat (the projection is the identity in a plane), and a FEATURE vertex contributes no lift, so
    sharp edges stay exactly where they were. alpha 0.5 reproduces a circular arc to first order."""
    N = np.asarray(trimesh.Trimesh(V, F, process=False).vertex_normals)
    e = np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    es = np.sort(e, axis=1)
    uniq, inv = np.unique(es, axis=0, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    a, b = uniq[:, 0], uniq[:, 1]
    m = (V[a] + V[b]) / 2.0
    offa = -np.einsum("ij,ij->i", m - V[a], N[a])[:, None] * N[a]
    offb = -np.einsum("ij,ij->i", m - V[b], N[b])[:, None] * N[b]
    offa[feature[a]] = 0.0
    offb[feature[b]] = 0.0
    both = (~feature[a]) & (~feature[b])
    off = (offa + offb) * 0.5
    # one end pinned: its half of the lift is lost, so scale the other half back up to keep arcs round
    off[~both] *= 2.0
    off[feature[a] & feature[b]] = 0.0
    newV = np.vstack([V, m + alpha * off])
    nf = len(F)
    m01, m12, m20 = (len(V) + inv[:nf], len(V) + inv[nf:2 * nf], len(V) + inv[2 * nf:])
    v0, v1, v2 = F[:, 0], F[:, 1], F[:, 2]
    newF = np.vstack([np.c_[v0, m01, m20], np.c_[v1, m12, m01], np.c_[v2, m20, m12], np.c_[m01, m12, m20]])
    return newV, newF


def smooth_mesh(mesh, level, sharp_deg=40.0):
    """-> (new mesh, info). Level 'off' returns the input untouched."""
    np, trimesh = deps()
    if level == "off":
        return mesh, {"level": "off"}
    sub, it = _PLAN[level]
    V, F = np.asarray(mesh.vertices, float), np.asarray(mesh.faces)
    feature = sharp_vertices(mesh, sharp_deg)
    has_sharp = bool(feature.any())
    while sub > 0 and len(F) * (4 ** sub) > MAX_FACES_OUT:
        sub -= 1
    for _ in range(sub):
        V, F = _phong_subdivide(np, trimesh, V, F, feature)
        # the midpoint of a SHARP edge lies on that edge: recompute so it is pinned too
        feature = sharp_vertices(trimesh.Trimesh(V, F, process=False), sharp_deg)
    out = trimesh.Trimesh(V, F, process=False)
    out.vertices = _taubin(np, np.asarray(out.vertices), np.asarray(out.faces), feature, it)
    out.fix_normals()
    return out, {"level": level, "subdivision": "phong-midpoint", "subdivide_iterations": sub,
                 "taubin_iterations": it, "sharp_edges_preserved": has_sharp,
                 "faces_before": int(len(mesh.faces)), "faces_after": int(len(out.faces))}


def apply(mesh, level, tol, sharp_deg=40.0, max_shrink=0.005, max_dev_frac=0.02):
    """Smooth with the safety net. -> (mesh, report). report always says what was applied."""
    np, _ = deps()
    diag = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))
    if level not in LEVELS:
        raise ValueError("smooth level must be one of %s" % (SMOOTH_CHOICES,))
    rep = {"requested": level, "applied": "off", "fell_back": []}
    if level == "off":
        return mesh, rep
    v0 = abs(float(mesh.volume)) if mesh.is_watertight else None
    ref = None
    cur = LEVELS.index(level)
    while cur > 0:
        lv = LEVELS[cur]
        sm, info = smooth_mesh(mesh, lv, sharp_deg)
        v1 = abs(float(sm.volume)) if sm.is_watertight else None
        drift = ((v1 - v0) / v0) if (v0 and v1) else None
        if ref is None:
            ref = SurfaceIndex(mesh, n=60000)
        samp = sm.vertices[np.random.default_rng(1).choice(len(sm.vertices), min(6000, len(sm.vertices)), replace=False)]
        maxdev = float(ref.distance(samp).max())
        bad = (drift is not None and drift < -max_shrink) or maxdev > max_dev_frac * diag
        if not bad:
            rep.update({"applied": lv, "volume_change_pct": None if drift is None else round(100 * drift, 3),
                        "max_deviation_m": round(maxdev, 9),
                        "max_deviation_pct_diag": round(100 * maxdev / diag, 3), **info})
            return sm, rep
        rep["fell_back"].append({"level": lv, "volume_change_pct": None if drift is None else round(100 * drift, 3),
                                 "max_deviation_pct_diag": round(100 * maxdev / diag, 3)})
        cur -= 1
    return mesh, rep


# --------------------------------------------------------------------------- contour smoothing
def _turn_angles(np, p):
    a = p - np.roll(p, 1, axis=0)
    b = np.roll(p, -1, axis=0) - p
    cr = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    dt = (a * b).sum(axis=1)
    return np.abs(np.arctan2(cr, dt))


def smooth_polyline(pts, tol, corner_deg=35.0, density=None):
    """Smooth a closed contour (k,2) with periodic / piecewise smoothing splines, corners kept.
    Returns a denser closed polyline (n,2), no repeated first point. The smoothing factor is tied
    to `tol`: s = m * (0.35 tol)^2, i.e. the spline may wander ~a third of the tolerance."""
    np, _ = deps()
    from scipy.interpolate import splprep, splev
    p = np.asarray(pts, dtype=float)
    keep = np.r_[True, np.linalg.norm(np.diff(p, axis=0), axis=1) > 1e-12]
    p = p[keep]
    if len(p) > 1 and np.linalg.norm(p[0] - p[-1]) < 1e-12:
        p = p[:-1]
    n = len(p)
    if n < 6:
        return p
    per = np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum()
    dens = density or max(per / max(8 * n, 96), tol * 0.5)
    turns = _turn_angles(np, p)
    med = float(np.median(turns))
    # a turn is a CORNER when it is large in absolute terms, or moderately large AND an outlier next to
    # the typical facet turn (a 10-gon's 36 deg vertices are facets of a circle, not corners)
    corners = np.flatnonzero((turns > math.radians(70.0))
                             | ((turns > math.radians(corner_deg)) & (turns > 2.2 * med)))
    sfac = (0.35 * tol) ** 2

    def run(q, periodic):
        m = len(q)
        if periodic:
            q = np.vstack([q, q[:1]])
        w = np.ones(len(q))
        if not periodic:
            w[0] = w[-1] = 1e3
        try:
            tck, _u = splprep([q[:, 0], q[:, 1]], w=w, s=sfac * len(q), per=1 if periodic else 0,
                              k=3 if len(q) > 4 else 2)
        except Exception:                                # noqa: BLE001
            return q if not periodic else q[:-1]
        seg = np.linalg.norm(np.diff(q, axis=0), axis=1).sum()
        cnt = int(min(max(seg / dens, 6 * m if periodic else 3 * m, 12), 4000))
        u = np.linspace(0.0, 1.0, cnt, endpoint=not periodic)
        x, y = splev(u, tck)
        return np.c_[x, y]

    if len(corners) == 0:
        return run(p, True)
    out = []
    k = len(corners)
    for ci in range(k):
        i, j = corners[ci], corners[(ci + 1) % k]
        idx = list(range(i, j + 1)) if j > i else list(range(i, n)) + list(range(0, j + 1))
        q = p[idx]
        if len(q) < 3:
            out.append(q[:-1])
            continue
        out.append(run(q, False)[:-1])
    return np.vstack(out)
