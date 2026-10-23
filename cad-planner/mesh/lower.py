"""lower.py -- mesh -> Feature Graph IR, by a strategy ladder.

    plan(mesh_path, units='mm', stations=0, tolerance_mm=0.0, smooth='auto') -> dict

  1. revolve          axisymmetric part: ONE profile sketch (half section, on the axis) + revolve.
  2. extrude          constant cross-section: ONE sketch (outer loops + holes) + extrude boss.
  3. stacked_extrude  prismatic with steps/pockets: one sketch+extrude boss per constant-section slab.
  4. loft             freeform: stations along the longest axis -> offset-plane sketches -> loft.

A strategy that raises or emits IR that pycompiler.ir_schema rejects falls DOWN the ladder (the
attempts are reported). Every returned graph has been through ir_schema.validate. IR is METERS and
RADIANS. Sketches sit on the FRONT datum (x,y = world X,Y; extrusion/loft axis = +Z, offsets are
signed along Z); a revolve sketch is on FRONT too, the axis is the sketch's Y axis. `transform` in
the result is the 4x4 taking SOURCE-file coordinates (file units) to that build frame, row-major --
verify.compare() takes it to put the source mesh where the built part lives.

Smoothing (smoothing.py) is applied only to the freeform strategies (loft, revolve); the prismatic
ones use exactly fitted primitives and are never smoothed.

Large graphs are written to `<dir>/.solidpilot/<name>.mesh.graph.json` by write_graph() and built
by path -- never echoed into model context.
"""
from __future__ import annotations

import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from . import mesh_read, segment, sections, smoothing
    from .geom import deps, frame_taking_axis_to
except ImportError:
    import mesh_read
    import segment
    import sections
    import smoothing
    from geom import deps, frame_taking_axis_to

SCHEMA_VERSION = "0.7.2-draft"
STRATEGIES = ("auto", "revolve", "extrude", "stacked_extrude", "loft")
LADDER = {"revolve": ["revolve", "loft"], "extrude": ["extrude", "loft"],
          "stacked_extrude": ["stacked_extrude", "loft"], "loft": ["loft"]}


class PlanError(RuntimeError):
    """No strategy produced a valid graph. Wire code: MESH_PLAN_FAILED."""
    code = "MESH_PLAN_FAILED"


def _ir_validate(graph):
    """pycompiler.ir_schema.validate, imported via the repo's path convention (compiler/solidworks)."""
    try:
        from pycompiler import ir_schema
    except ImportError:
        root = os.path.normpath(os.path.join(_HERE, "..", "..", "compiler", "solidworks"))
        if root not in sys.path:
            sys.path.insert(0, root)
        from pycompiler import ir_schema
    return ir_schema.validate(graph)


def _sketch(nid, z, prims):
    node = {"id": nid, "type": "sketch", "ref": {"datum": "front"}, "profile": prims}
    if abs(z) > 1e-12:
        node["ref"]["offset"] = round(float(z), 9)
    return node


def _apply(np, mesh, T):
    out = mesh.copy()
    out.apply_transform(T)
    return out


# --------------------------------------------------------------------------- extrude / stacked
def _slab_signature(loops):
    outer = [l for l in loops if l["role"] == "outer"]
    return (len(loops), round(sections.net_area(loops), 12),
            tuple(sorted(round(abs(l["area"]), 12) for l in loops)))


def _build_prismatic(np, mb, levels, tol, stacked):
    notes, nodes, cnt = [], [], 0
    diag = float(np.linalg.norm(mb.bounds[1] - mb.bounds[0]))
    slabs = []
    for z0, z1 in zip(levels[:-1], levels[1:]):
        if z1 - z0 <= tol * 0.5:
            continue
        loops = sections.section_loops(mb, (z0 + z1) / 2.0)
        if not loops:
            continue
        sig = _slab_signature(loops)
        if slabs and slabs[-1]["sig"][0] == sig[0] and \
                abs(slabs[-1]["sig"][1] - sig[1]) <= 1e-3 * max(abs(sig[1]), 1e-18) and \
                all(abs(a - b) <= 2e-3 * max(a, 1e-18) for a, b in zip(slabs[-1]["sig"][2], sig[2])):
            slabs[-1]["z1"] = z1
            continue
        slabs.append({"z0": z0, "z1": z1, "loops": loops, "sig": sig})
    if not slabs:
        raise PlanError("no cross-section found along the extrusion axis")
    if not stacked and len(slabs) > 1:
        raise PlanError("section is not constant")
    vol = 0.0
    prims_total = {"line": 0, "arc": 0, "circle": 0, "ellipse": 0, "spline": 0}
    for k, s in enumerate(slabs):
        prims = []
        for lp in s["loops"]:
            fit = sections.fit_loop(lp["pts"], tol)
            prims += fit["prims"]
            for p in fit["prims"]:
                prims_total[p["kind"]] += 1
        nodes.append(_sketch("sk%d" % k, s["z0"], prims))
        nodes.append({"id": "ex%d" % k, "type": "extrude", "sketch": "sk%d" % k, "operation": "boss",
                      "depth": round(s["z1"] - s["z0"], 9)})
        vol += sections.net_area(s["loops"]) * (s["z1"] - s["z0"])
        cnt += 1
    return nodes, vol, {"slabs": cnt, "primitives": prims_total}, notes


# --------------------------------------------------------------------------- revolve
def _clip_x_positive(np, p):
    """Sutherland-Hodgman against x >= 0; crossing points land exactly on x = 0."""
    out = []
    n = len(p)
    for i in range(n):
        a, b = p[i], p[(i + 1) % n]
        ina, inb = a[0] >= 0.0, b[0] >= 0.0
        if ina:
            out.append((a[0], a[1]))
        if ina != inb:
            t = a[0] / (a[0] - b[0])
            out.append((0.0, a[1] + t * (b[1] - a[1])))
    if len(out) < 3:
        return None
    arr = np.array(out)
    # drop consecutive duplicates
    keep = np.r_[True, np.linalg.norm(np.diff(arr, axis=0), axis=1) > 1e-12]
    arr = arr[keep]
    return arr if len(arr) >= 3 else None


def _build_revolve(np, mb, tol, level):
    diag = float(np.linalg.norm(mb.bounds[1] - mb.bounds[0]))
    loops = sections.section_loops(mb, 3e-5 * diag)
    if not loops:
        raise PlanError("no section through the revolve axis")
    prof, vol = [], 0.0
    for lp in loops:
        c = _clip_x_positive(np, lp["pts"])
        if c is None:
            continue
        if level != "off" and len(c) >= 6:
            c = np.asarray(smoothing.smooth_polyline(c, tol))
            c[np.abs(c[:, 0]) < 1e-9, 0] = 0.0
            c[:, 0] = np.maximum(c[:, 0], 0.0)
        a = sections._signed_area(np, c)
        if abs(a) < (diag * 1e-5) ** 2:
            continue
        cx = float(np.mean(c[:, 0]))
        # exact polygon centroid x
        x, y = c[:, 0], c[:, 1]
        x1, y1 = np.roll(x, -1), np.roll(y, -1)
        cr = x * y1 - x1 * y
        cxs = float(((x + x1) * cr).sum() / (6 * a)) if abs(a) > 0 else cx
        role_sign = 1.0 if lp["role"] == "outer" else -1.0
        vol += 2 * math.pi * role_sign * abs(a) * cxs
        prof.append(c)
    if not prof:
        raise PlanError("empty half-section")
    prims, tot = [], {"line": 0, "arc": 0, "circle": 0, "ellipse": 0, "spline": 0}
    for c in prof:
        fit = sections.fit_loop(c, tol, allow_conic=True)
        prims += fit["prims"]
    axis = None
    for p in prims:
        if p["kind"] == "line" and abs(p["x1"]) < 1e-9 and abs(p["x2"]) < 1e-9:
            L = abs(p["y2"] - p["y1"])
            if axis is None or L > axis[0]:
                axis = (L, p)
    if axis is not None:
        p = axis[1]
        axis_d = {"x1": 0.0, "y1": p["y1"], "x2": 0.0, "y2": p["y2"]}
        p["x1"] = p["x2"] = 0.0
    else:
        ys = np.concatenate([c[:, 1] for c in prof])
        axis_d = {"x1": 0.0, "y1": round(float(ys.min()), 9), "x2": 0.0, "y2": round(float(ys.max()), 9)}
        prims.append({"kind": "line", "construction": True, **{k: v for k, v in axis_d.items()}})
    for p in prims:
        tot[p["kind"]] += 1
    nodes = [_sketch("profile", 0.0, prims),
             {"id": "rev", "type": "revolve", "sketch": "profile", "axis": axis_d}]
    return nodes, vol, {"profile_loops": len(prof), "primitives": tot,
                        "axis_on_profile_edge": axis is not None}, []


# --------------------------------------------------------------------------- loft
def _build_loft(np, mb, tol, stations, level):
    zmin, zmax = float(mb.bounds[0][2]), float(mb.bounds[1][2])
    L = zmax - zmin
    if L <= 0:
        raise PlanError("zero extent along the loft axis")
    n = int(stations) if stations else int(round(15 * smoothing.station_multiplier(level)))
    n = max(4, min(n, 48))
    notes = []
    fits, zs, areas = [], [], []
    for k in range(n):
        z = zmin + L * (1.0 - math.cos(math.pi * (k + 0.5) / n)) / 2.0
        loops = sections.section_loops(mb, z)
        outers = [l for l in loops if l["role"] == "outer"]
        if not outers:
            continue
        big = max(outers, key=lambda l: abs(l["area"]))
        others = [l for l in outers if l is not big and abs(l["area"]) > 0.05 * abs(big["area"])]
        if others:
            notes.append("station z=%.4f m has %d extra regions; only the largest is lofted" % (z, len(others)))
        pts = big["pts"]
        if level != "off" and len(pts) >= 6:
            pts = smoothing.smooth_polyline(pts, tol)
        fits.append(sections.fit_loop(pts, tol))
        fits[-1]["_pts"] = pts
        zs.append(z)
        areas.append(abs(big["area"]))
    if len(zs) < 2:
        raise PlanError("fewer than 2 usable sections for a loft")
    sigs = {sections.signature(f) for f in fits}
    keep = len(sigs) == 1 and fits[0]["kind"] in ("circle", "ellipse", "poly")
    if len(sigs) > 1 and {f["kind"] for f in fits} <= {"circle"}:
        keep = True
    npts = int(max(32, min(80, 40 * smoothing.station_multiplier(level) / 1.4)))
    npts += npts % 2
    nodes, ids = [], []
    for k, (z, f) in enumerate(zip(zs, fits)):
        prims = f["prims"] if keep else sections._spline_prims(np, f["_pts"], npts)
        nodes.append(_sketch("st%d" % k, z, prims))
        ids.append("st%d" % k)
    nodes.append({"id": "loft", "type": "loft", "profiles": ids})
    za, aa = np.array(zs), np.array(areas)
    vol = float(np.sum((aa[1:] + aa[:-1]) / 2.0 * np.diff(za)))
    kinds = {}
    for p in [p for n_ in nodes if n_["type"] == "sketch" for p in n_["profile"]]:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    return nodes, vol, {"stations": len(zs), "profile_kind": "kept primitives" if keep else "closed splines",
                        "primitives": kinds}, notes


# --------------------------------------------------------------------------- the planner
def _mean_edge(np, mesh):
    try:
        return float(np.mean(mesh.edges_unique_length))
    except Exception:                                    # noqa: BLE001
        return None


def plan(mesh_path, units="mm", stations=0, tolerance_mm=0.0, *, strategy="auto", smooth="auto",
         sharp_deg=40.0, align=True, max_faces=400000, mesh_data=None):
    """Mesh file -> {strategy, graph, summary, expected, transform, warnings}. See module docstring."""
    np, _ = deps()
    if strategy not in STRATEGIES:
        raise PlanError("strategy must be one of %s" % (STRATEGIES,))
    if smooth not in smoothing.SMOOTH_CHOICES:
        raise PlanError("smooth must be one of %s" % (smoothing.SMOOTH_CHOICES,))
    md = mesh_data or mesh_read.read_mesh(mesh_path, units, align=align, max_faces=max_faces)
    mesh = md.mesh
    ext = mesh.bounds[1] - mesh.bounds[0]
    diag = float(np.linalg.norm(ext))
    tol = float(tolerance_mm) * 1e-3 if tolerance_mm and tolerance_mm > 0 else max(0.0015 * diag, 1e-5)
    seg = segment.analyze(mesh, tol)
    chosen = seg["recommended_strategy"] if strategy == "auto" else strategy
    ladder = LADDER[chosen]
    warnings = list(md.report.get("warnings", []))
    attempts, result = [], None
    for strat in ladder:
        try:
            result = _attempt(np, strat, mesh, seg, md, tol, stations, smooth, sharp_deg, diag)
            attempts.append({"strategy": strat, "ok": True})
            break
        except Exception as exc:                          # noqa: BLE001 - fall down the ladder
            attempts.append({"strategy": strat, "ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)})
    if result is None:
        raise PlanError("no strategy produced a valid graph: %s" % attempts)
    graph, est_vol, detail, notes, T_total, smooth_rep, mb = result
    strat = attempts[-1]["strategy"]
    warnings += notes
    mesh_vol = md.report.get("volume_m3")
    vol_ref = mesh_vol if mesh_vol else None
    summary = {
        "strategy": strat, "recommended": seg["recommended_strategy"], "attempts": attempts,
        "nodes": {"total": len(graph["nodes"]),
                  **{t: sum(1 for n in graph["nodes"] if n["type"] == t) for t in sorted({n["type"] for n in graph["nodes"]})}},
        "detail": detail, "tolerance_mm": round(tol * 1e3, 4), "smoothing": smooth_rep,
        "estimated_volume_m3": round(est_vol, 12),
        "volume_error_pct": None if not vol_ref else round(100.0 * (est_vol - vol_ref) / vol_ref, 3),
        "analysis": {k: seg[k] for k in ("planes", "cylinders", "holes", "unmodelled_holes", "axisymmetric",
                                        "prismatic", "reasons")},
        "mesh": {k: md.report.get(k) for k in ("file", "faces", "faces_raw", "watertight", "repaired", "bodies",
                                               "alignment", "decimated")},
        "warnings": warnings,
    }
    expected = {"volume_m3": vol_ref if vol_ref else round(est_vol, 12),
                "bbox_m": [round(float(v), 9) for v in (mb.bounds[1] - mb.bounds[0])],
                "bbox_min_m": [round(float(v), 9) for v in mb.bounds[0]],
                "bbox_max_m": [round(float(v), 9) for v in mb.bounds[1]]}
    return {"strategy": strat, "graph": graph, "summary": summary, "expected": expected,
            "transform": [[round(float(v), 12) for v in row] for row in T_total], "warnings": warnings}


def _attempt(np, strat, mesh, seg, md, tol, stations, smooth, sharp_deg, diag):
    # 1. build frame
    if strat == "revolve":
        ax = seg["axisymmetric"]
        if ax is None:
            ax = _forced_axis(np, mesh)
        a, pt = np.array(ax["axis"]), np.array(ax.get("point", [0, 0, 0]))
        Tb = frame_taking_axis_to(a, [0.0, 1.0, 0.0])
        c2 = Tb[:3, :3] @ pt
        shift = np.eye(4)
        shift[0, 3], shift[2, 3] = -c2[0], -c2[2]
        Tb = shift @ Tb
    elif strat in ("extrude", "stacked_extrude"):
        pr = seg["_prism"]
        if pr is None:
            raise PlanError("part is not prismatic")
        e = np.zeros(3)
        e[pr["axis_i"]] = 1.0
        Tb = frame_taking_axis_to(e, [0.0, 0.0, 1.0])
    else:
        e = np.zeros(3)
        e[int(np.argmax(mesh.bounds[1] - mesh.bounds[0]))] = 1.0
        Tb = frame_taking_axis_to(e, [0.0, 0.0, 1.0])
    mb = _apply(np, mesh, Tb)

    # 2. smoothing (freeform strategies only)
    smooth_rep = {"requested": smooth, "applied": "off"}
    level = "off"
    if strat in ("extrude", "stacked_extrude"):
        smooth_rep["note"] = "skipped: prismatic parts use exactly fitted primitives"
    elif smooth != "off":
        level = smooth if smooth != "auto" else smoothing.choose_level(len(mesh.faces), diag, _mean_edge(np, mesh))
        if level != "off":
            mb, smooth_rep = smoothing.apply(mb, level, tol, sharp_deg)
            smooth_rep["requested"] = smooth
            level = smooth_rep["applied"]
    smooth_rep["contour_spline_smoothing"] = level != "off"

    # 3. IR
    if strat == "revolve":
        nodes, vol, detail, notes = _build_revolve(np, mb, tol, level)
    elif strat in ("extrude", "stacked_extrude"):
        nodes, vol, detail, notes = _build_prismatic(np, mb, seg["_prism"]["levels"], tol, strat == "stacked_extrude")
    else:
        nodes, vol, detail, notes = _build_loft(np, mb, tol, stations, level)
    graph = {"_intent": "Reverse-engineered from %s by strategy '%s' (tolerance %.3g mm)."
                        % (md.report.get("file"), strat, tol * 1e3),
             "schema_version": SCHEMA_VERSION, "units": "meters", "nodes": nodes}
    errs = _ir_validate(graph)
    if errs:
        raise PlanError("IR rejected by ir_schema: %s" % errs[:3])
    return graph, vol, detail, notes, Tb @ md.transform, smooth_rep, mb


def _forced_axis(np, mesh):
    ext = mesh.bounds[1] - mesh.bounds[0]
    i = int(np.argmax(ext))
    a = np.zeros(3)
    a[i] = 1.0
    return {"axis": list(a), "point": [0, 0, 0]}


def analyze(mesh_path, units="mm", tolerance_mm=0.0, align=True, max_faces=400000):
    """Stats + recognised primitives + recommended strategy, no graph. -> compact dict."""
    np, _ = deps()
    md = mesh_read.read_mesh(mesh_path, units, align=align, max_faces=max_faces)
    diag = float(np.linalg.norm(md.mesh.bounds[1] - md.mesh.bounds[0]))
    tol = float(tolerance_mm) * 1e-3 if tolerance_mm and tolerance_mm > 0 else None
    seg = segment.analyze(md.mesh, tol)
    return {"mesh": {k: v for k, v in md.report.items()}, "segments": segment.public(seg),
            "recommended_strategy": seg["recommended_strategy"],
            "smoothing_auto_level": smoothing.choose_level(len(md.mesh.faces), diag, _mean_edge(np, md.mesh))}


def write_graph(result, out_dir, name=None):
    """Save a plan()'s graph to <out_dir>/.solidpilot/<name>.mesh.graph.json -> path."""
    folder = os.path.join(out_dir, ".solidpilot")
    os.makedirs(folder, exist_ok=True)
    name = name or "part"
    path = os.path.join(folder, "%s.mesh.graph.json" % name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result["graph"], fh, separators=(",", ":"))
    return path
