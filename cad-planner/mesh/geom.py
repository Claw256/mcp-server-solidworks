"""geom.py -- shared numeric helpers for the mesh package (surface sampling, surface distance, frames).

IMPORT RULE (ADR-062): numpy's C extensions DEADLOCK when first loaded off the main thread on
Windows. This package therefore never imports numpy/scipy/trimesh at module top level; every
function calls `deps()` which imports them LAZILY. The MCP adapter calls `mesh.preload()` once,
eagerly, in the main thread at startup (inside try/except), so the heavy imports happen on the
safe thread and tool calls on worker threads only ever hit the already-populated `sys.modules`.

A missing dependency raises MeshReaderUnavailable (a clear, catchable error -- never a crash).
"""
from __future__ import annotations

import math

INSTALL_HINT = ("mesh support needs trimesh, scipy and numpy: "
                "pip install -r adapters/claude/requirements-mesh.txt")


class MeshReaderUnavailable(RuntimeError):
    """trimesh / scipy / numpy are not importable. Wire code: MESH_READER_UNAVAILABLE."""
    code = "MESH_READER_UNAVAILABLE"


def deps():
    """-> (numpy, trimesh). Imports lazily; scipy is probed too because every module needs it."""
    try:
        import numpy as np
        import scipy.spatial  # noqa: F401
        import scipy.sparse.csgraph  # noqa: F401
        import trimesh
    except ImportError as exc:
        raise MeshReaderUnavailable("%s (%s)" % (INSTALL_HINT, exc)) from exc
    return np, trimesh


def preload():
    """Import every heavy dependency in the CALLING thread. True when mesh support is usable."""
    try:
        deps()
        return True
    except MeshReaderUnavailable:
        return False


# --------------------------------------------------------------------------- surface sampling
def sample_surface(mesh, n, seed=7):
    """Area-uniform samples on a mesh -> (points (n,3), normals (n,3)). Own implementation so the
    result does not depend on trimesh's sampling signature drifting between releases."""
    np, _ = deps()
    rng = np.random.default_rng(seed)
    area = np.asarray(mesh.area_faces, dtype=float)
    tot = area.sum()
    if tot <= 0 or len(area) == 0:
        raise ValueError("mesh has no surface area")
    cdf = np.cumsum(area) / tot
    fi = np.minimum(np.searchsorted(cdf, rng.random(n)), len(area) - 1)
    tri = mesh.triangles[fi]
    r1 = np.sqrt(rng.random(n))
    r2 = rng.random(n)
    a, b, c = 1.0 - r1, r1 * (1.0 - r2), r1 * r2
    pts = tri[:, 0] * a[:, None] + tri[:, 1] * b[:, None] + tri[:, 2] * c[:, None]
    return pts, np.asarray(mesh.face_normals)[fi]


class SurfaceIndex:
    """Dense surface sample + KD-tree answering 'how far is this point from the surface'.

    Plain nearest-sample distance over-reads by up to the sample spacing. Because every sample
    carries its face normal, the distance is refined to min over the 3 nearest samples of the
    point-to-tangent-plane distance (when the tangential offset is within the sampling reach), which
    is accurate on flat and smooth surfaces; near an edge it can only UNDER-read by a distance
    bounded by the same reach, never by more."""

    def __init__(self, mesh, n=120000, seed=11):
        np, _ = deps()
        from scipy.spatial import cKDTree
        n = int(min(max(n, 4000), 400000))
        self.P, self.N = sample_surface(mesh, n, seed)
        self.tree = cKDTree(self.P)
        self.spacing = math.sqrt(float(mesh.area) / n)

    def distance(self, Q):
        np, _ = deps()
        Q = np.asarray(Q, dtype=float)
        k = 3
        d, idx = self.tree.query(Q, k=k)
        vec = Q[:, None, :] - self.P[idx]                      # (m,k,3)
        normal_d = np.abs(np.einsum("mkj,mkj->mk", vec, self.N[idx]))
        tang2 = np.maximum(d * d - normal_d * normal_d, 0.0)
        use = tang2 <= (2.5 * self.spacing) ** 2
        est = np.where(use, normal_d, d)
        return est.min(axis=1)


# --------------------------------------------------------------------------- frames
def rotation_about(axis, angle, center):
    """4x4 rotation by `angle` about the line through `center` with direction `axis`."""
    np, _ = deps()
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    R = np.eye(3) + math.sin(angle) * K + (1 - math.cos(angle)) * (K @ K)
    T = np.eye(4)
    T[:3, :3] = R
    c = np.asarray(center, dtype=float)
    T[:3, 3] = c - R @ c
    return T


def frame_taking_axis_to(axis, target):
    """4x4 pure rotation taking unit vector `axis` onto unit vector `target`. A coordinate axis maps
    through a cyclic permutation (det +1), which keeps the other two axes axis-aligned."""
    np, _ = deps()
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    t = np.asarray(target, dtype=float)
    T = np.eye(4)
    ia = int(np.argmax(np.abs(a)))
    it = int(np.argmax(np.abs(t)))
    if abs(abs(a[ia]) - 1.0) < 1e-9 and abs(abs(t[it]) - 1.0) < 1e-9:
        sa, st = math.copysign(1.0, a[ia]), math.copysign(1.0, t[it])
        # cyclic shift sending coordinate ia to it: new[(k + s) % 3] = old[k]
        shift = (it - ia) % 3
        R = np.zeros((3, 3))
        for k in range(3):
            R[(k + shift) % 3, k] = 1.0
        if sa * st < 0:                      # opposite sense: rotate 180 deg about a perpendicular axis
            flip = np.eye(3)
            perp = (it + 1) % 3
            flip[it, it] = -1.0
            flip[perp, perp] = -1.0
            R = flip @ R
        T[:3, :3] = R
        return T
    v = np.cross(a, t)
    s = np.linalg.norm(v)
    c = float(a @ t)
    if s < 1e-12:
        if c > 0:
            return T
        perp = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
        return rotation_about(perp, math.pi, [0, 0, 0])
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    T[:3, :3] = np.eye(3) + K + K @ K * ((1 - c) / (s * s))
    return T
