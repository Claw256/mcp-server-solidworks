"""mesh_read.py -- STL / OBJ / PLY -> a repaired, metric, canonically aligned trimesh.

    read_mesh(path, units='mm') -> MeshData(mesh, transform, report)

* units: STL carries none, so the caller says (default 'mm'); the mesh is returned in METERS (the IR's
  unit). `transform` is the 4x4 that maps SOURCE-file coordinates (in file units) to the returned
  mesh's coordinates, so anything computed on the aligned mesh can be reported back / undone.
* repair: merge vertices, drop degenerate faces, fix winding/normals, fill small holes, flip an
  inside-out solid. The report says what was done and whether the result is watertight.
* decimation: dense meshes (> max_faces) are reduced before analysis (quadric when the optional
  fast-simplification package is installed, vertex clustering otherwise). The reported volume is
  always the ORIGINAL mesh's.
* alignment: the frame minimising the oriented bounding-box volume among {identity, principal axes,
  dominant face-normal pairs} is chosen; identity wins ties so a part already modelled in the CAD
  axes is left alone. The result is snapped so the longest extent is X, the smallest Y (up) and the
  middle Z, and the bounding box is centred on the origin.

numpy/trimesh/scipy are imported lazily (ADR-062, see geom.py). Missing -> MeshReaderUnavailable.
"""
from __future__ import annotations

import math
import os

try:
    from .geom import MeshReaderUnavailable, deps, INSTALL_HINT  # noqa: F401
except ImportError:                                     # flat import (offline gate, scripts)
    from geom import MeshReaderUnavailable, deps, INSTALL_HINT  # noqa: F401

UNIT_TO_M = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254}
SUPPORTED_EXT = (".stl", ".obj", ".ply")


class MeshReadError(ValueError):
    """The file is missing, unsupported, empty or not a triangle mesh. Wire code: MESH_READ_FAILED."""
    code = "MESH_READ_FAILED"


class MeshData:
    """Result of read_mesh. `mesh` is in meters, aligned (when align=True); `transform` (4x4,
    numpy) maps source-file coordinates to it; `report` is a small JSON-able dict."""

    def __init__(self, mesh, transform, report, scale):
        self.mesh, self.transform, self.report, self.scale = mesh, transform, report, scale

    def transform_list(self):
        return [[round(float(v), 9) for v in row] for row in self.transform]


# --------------------------------------------------------------------------- decimation
def _cluster_decimate(np, trimesh, mesh, target_faces):
    """Vertex clustering: snap vertices to a grid sized to hit ~target_faces, merge, drop degenerates.
    Cruder than quadric decimation but dependency-free; the cell size is the error bound."""
    ext = mesh.bounds[1] - mesh.bounds[0]
    area = float(mesh.area)
    cell = math.sqrt(max(area, 1e-18) / max(target_faces / 2.0, 1.0))
    cell = max(cell, float(ext.max()) / 4000.0)
    for _ in range(6):
        key = np.floor((mesh.vertices - mesh.bounds[0]) / cell).astype(np.int64)
        _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
        inv = np.asarray(inv).reshape(-1)
        newv = np.zeros((len(first), 3))
        cnt = np.bincount(inv, minlength=len(first)).astype(float)
        for k in range(3):
            newv[:, k] = np.bincount(inv, weights=mesh.vertices[:, k], minlength=len(first)) / cnt
        f = inv[mesh.faces]
        ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
        out = trimesh.Trimesh(newv, f[ok], process=True)
        if len(out.faces) <= target_faces * 1.5 or len(out.faces) == 0:
            return out
        cell *= 1.4
    return out


def _decimate(np, trimesh, mesh, max_faces):
    try:
        out = mesh.simplify_quadric_decimation(face_count=int(max_faces))
        if out is not None and len(out.faces) > 0:
            return out, "quadric"
    except Exception:                                    # noqa: BLE001 - optional dep / API drift
        pass
    return _cluster_decimate(np, trimesh, mesh, max_faces), "cluster"


# --------------------------------------------------------------------------- repair
def _fill_holes(np, trimesh, mesh, max_edges=64):
    """Close small boundary loops by fan triangulation (trimesh.repair.fill_holes needs networkx,
    which is not a dependency). Orientation follows the neighbouring faces. -> number filled."""
    F = np.asarray(mesh.faces)
    d = np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    key = np.sort(d, axis=1)
    uniq, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = np.asarray(inv).reshape(-1)
    bnd = d[cnt[inv] == 1]
    if len(bnd) == 0:
        return 0
    nxt = {}
    for a, b in bnd:
        nxt.setdefault(int(a), []).append(int(b))
    V = [v for v in np.asarray(mesh.vertices)]
    newf, filled, seen = [], 0, set()
    for a0 in list(nxt):
        if a0 in seen:
            continue
        loop, cur, ok = [a0], a0, True
        while True:
            seen.add(cur)
            outs = nxt.get(cur)
            if not outs:
                ok = False
                break
            cur = outs[0]
            if cur == a0:
                break
            if cur in loop or len(loop) > max_edges:
                ok = False
                break
            loop.append(cur)
        if not ok or len(loop) < 3:
            continue
        c = np.mean([V[i] for i in loop], axis=0)
        ci = len(V)
        V.append(c)
        for i in range(len(loop)):
            a, b = loop[i], loop[(i + 1) % len(loop)]
            newf.append([b, a, ci])
        filled += 1
    if filled:
        mesh.vertices = np.asarray(V)
        mesh.faces = np.vstack([F, np.asarray(newf)])
    return filled


def _repair(np, trimesh, mesh, report):
    report["watertight_raw"] = bool(mesh.is_watertight)
    if not mesh.is_watertight:
        try:
            trimesh.repair.fix_winding(mesh)
            trimesh.repair.fix_normals(mesh)
            _fill_holes(np, trimesh, mesh)
            mesh.merge_vertices()
            mesh.remove_unreferenced_vertices()
        except Exception as exc:                         # noqa: BLE001 - repair is best effort
            report["warnings"].append("repair step failed: %s" % exc)
        if mesh.is_watertight:
            report["repaired"] = True
    report["watertight"] = bool(mesh.is_watertight)
    if mesh.is_watertight and mesh.volume < 0:
        mesh.invert()
        report["repaired"] = True
        report["warnings"].append("mesh was inside-out; normals flipped")
    if not mesh.is_watertight:
        report["warnings"].append("mesh is NOT watertight: volume, hole and section results are approximate")


def _body_count(np, mesh):
    try:
        import scipy.sparse as sp
        from scipy.sparse.csgraph import connected_components
        n = len(mesh.faces)
        adj = mesh.face_adjacency
        g = sp.coo_matrix((np.ones(len(adj)), (adj[:, 0], adj[:, 1])), shape=(n, n))
        return int(connected_components(g, directed=False)[0])
    except Exception:                                    # noqa: BLE001
        return None


# --------------------------------------------------------------------------- alignment
def _normal_clusters(np, mesh, top=8):
    n = np.asarray(mesh.face_normals)
    a = np.asarray(mesh.area_faces)
    sgn = np.sign(n[np.arange(len(n)), np.argmax(np.abs(n), axis=1)])
    sgn[sgn == 0] = 1.0
    n = n * sgn[:, None]
    key = np.round(n / 0.03).astype(np.int64)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    w = np.bincount(inv, weights=a, minlength=len(uniq))
    order = np.argsort(-w)[:top]
    out = []
    for i in order:
        m = inv == i
        v = (n[m] * a[m][:, None]).sum(axis=0)
        nv = np.linalg.norm(v)
        if nv > 0:
            out.append(v / nv)
    return out


def _principal_axes(np, pts):
    c = pts.mean(axis=0)
    cov = np.cov((pts - c).T)
    w, v = np.linalg.eigh(cov)
    return [v[:, i] for i in range(3)]


def _frame_from(np, u, v):
    u = u / np.linalg.norm(u)
    v = v - (v @ u) * u
    nv = np.linalg.norm(v)
    if nv < 1e-9:
        return None
    v = v / nv
    return np.vstack([u, v, np.cross(u, v)])             # rows = axes


def canonical_frame(mesh):
    """-> (R 3x3 rows = new axes expressed in old coordinates, info). Pure rotation, det +1."""
    np, _ = deps()
    V = np.asarray(mesh.vertices)
    if len(V) > 30000:
        V = V[np.random.default_rng(3).choice(len(V), 30000, replace=False)]
    cands = [np.eye(3)[i] for i in range(3)] + _principal_axes(np, V) + _normal_clusters(np, mesh)
    frames = [np.eye(3)]
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            if abs(cands[i] @ cands[j]) < 0.05:
                f = _frame_from(np, cands[i], cands[j])
                if f is not None:
                    frames.append(f)
    pca = _principal_axes(np, V)
    f = _frame_from(np, pca[2], pca[1])
    if f is not None:
        frames.append(f)

    def vol(F):
        q = V @ F.T
        e = q.max(axis=0) - q.min(axis=0)
        return float(e[0] * e[1] * e[2]), e

    best_v, best_f, best_e = None, None, None
    for k, F in enumerate(frames):
        v, e = vol(F)
        if best_v is None or v < best_v * 0.995:
            best_v, best_f, best_e = v, F, e
    v_id, e_id = vol(np.eye(3))
    if v_id <= best_v * 1.02:                            # already CAD-aligned: leave it alone
        return np.eye(3), {"frame": "identity", "extents": e_id}
    # snap: longest -> X, smallest -> Y (up), middle -> Z; keep the chain right handed
    order = np.argsort(-best_e)
    ax_x, ax_z, ax_y = best_f[order[0]], best_f[order[1]], best_f[order[2]]
    R = np.vstack([ax_x, ax_y, ax_z])
    if np.linalg.det(R) < 0:
        R[1] = -R[1]
    return R, {"frame": "principal", "extents": best_e[order]}


# --------------------------------------------------------------------------- the reader
def read_mesh(path, units="mm", align=True, repair=True, max_faces=400000):
    """Load, repair, scale to meters, optionally decimate and align. -> MeshData."""
    np, trimesh = deps()
    if units not in UNIT_TO_M:
        raise MeshReadError("units must be one of %s (got %r)" % (sorted(UNIT_TO_M), units))
    if not os.path.isfile(path):
        raise MeshReadError("mesh file not found: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXT:
        raise MeshReadError("unsupported mesh format %r (supported: %s)" % (ext, ", ".join(SUPPORTED_EXT)))
    try:
        mesh = trimesh.load(path, force="mesh", process=True)
    except Exception as exc:                             # noqa: BLE001
        raise MeshReadError("could not parse %s: %s" % (os.path.basename(path), exc)) from exc
    if not hasattr(mesh, "faces") or mesh.faces is None or len(mesh.faces) == 0:
        raise MeshReadError("%s contains no triangle faces (point cloud or empty file)" % os.path.basename(path))

    scale = UNIT_TO_M[units]
    report = {"file": os.path.basename(path), "units": units, "faces_raw": int(len(mesh.faces)),
              "warnings": [], "repaired": False, "decimated": None}
    mesh = mesh.copy()
    mesh.apply_scale(scale)
    mesh.remove_infinite_values()
    if repair:
        _repair(np, trimesh, mesh, report)
    else:
        report["watertight"] = bool(mesh.is_watertight)
    report["bodies"] = _body_count(np, mesh)
    if report["bodies"] and report["bodies"] > 1:
        report["warnings"].append("%d disconnected bodies; sections/primitives treat them as one" % report["bodies"])
    ext_m = mesh.bounds[1] - mesh.bounds[0]
    if float(ext_m.max()) <= 0:
        raise MeshReadError("mesh has zero extent")
    report["volume_m3"] = float(abs(mesh.volume)) if mesh.is_watertight else None
    report["area_m2"] = float(mesh.area)

    if len(mesh.faces) > max_faces:
        before = len(mesh.faces)
        mesh, how = _decimate(np, trimesh, mesh, max_faces)
        report["decimated"] = {"method": how, "faces_before": int(before), "faces_after": int(len(mesh.faces))}
        report["warnings"].append("decimated %d -> %d faces (%s)" % (before, len(mesh.faces), how))
    report["faces"] = int(len(mesh.faces))

    T = np.eye(4)
    T[:3, :3] *= scale
    T4 = np.eye(4)
    if align:
        R, info = canonical_frame(mesh)
        rot = np.eye(4)
        rot[:3, :3] = R
        mesh.apply_transform(rot)
        c = (mesh.bounds[0] + mesh.bounds[1]) / 2.0
        shift = np.eye(4)
        shift[:3, 3] = -c
        mesh.apply_transform(shift)
        T4 = shift @ rot
        report["alignment"] = info["frame"]
    else:
        report["alignment"] = "none"
    full = T4 @ T
    ext_m = mesh.bounds[1] - mesh.bounds[0]
    report["bbox_m"] = [round(float(v), 9) for v in ext_m]
    report["diag_m"] = round(float(np.linalg.norm(ext_m)), 9)
    if report.get("volume_m3") is not None:
        report["volume_m3"] = round(report["volume_m3"], 12)
    return MeshData(mesh, full, report, scale)
