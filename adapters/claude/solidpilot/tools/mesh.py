import json
import os
import shutil
import sys
import tempfile

from solidpilot import bridge
from solidpilot import design as design_core
from solidpilot import expectations
from solidpilot import jobs
from solidpilot.app import mcp
from solidpilot.paths import CAD_PLANNER_DIR
from solidpilot.tools.graph import _run_graph


# ---------------------------------------------------------------------------
# Tool: analyze_mesh  (STL / OBJ / PLY -> Feature Graph IR -> an ordinary design session)
# ---------------------------------------------------------------------------
# The mesh package imports numpy/scipy/trimesh LAZILY (none at module level), so importing it here is
# free. `preload()` below pulls them in on the MAIN thread at startup: numpy's C extensions deadlock
# when first loaded from an MCP worker thread on Windows (same rule as the DXF reader in drawing_in).
try:
    if CAD_PLANNER_DIR not in sys.path:
        sys.path.insert(0, CAD_PLANNER_DIR)
    import mesh as _mesh
    _MESH_IMPORT_ERROR = None
except Exception as _exc:                          # noqa: BLE001 - startup must survive anything
    _mesh = None
    _MESH_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

INSTALL_HINT = "pip install -r adapters/claude/requirements-mesh.txt"
_MODES = ("analyze", "plan", "build", "verify")
_UNITS = ("mm", "cm", "m", "in")
_MESH_EXT = (".stl", ".obj", ".ply")

# design_id -> what verify needs (plan transform, source, units); also written beside the mesh as a sidecar
# so a restart or a later session can still verify.
_RECORDS: dict = {}


def preload() -> bool:
    """Call once from the MAIN thread at startup (inside try/except). True when mesh support is usable."""
    if _mesh is None:
        return False
    return bool(_mesh.preload())


def _fail(code, message):
    return f"FAILED | {code} | {message}"


def _unavailable(detail=""):
    return _fail("MESH_READER_UNAVAILABLE",
                 f"{detail or _MESH_IMPORT_ERROR or 'trimesh/scipy/numpy missing'} — {INSTALL_HINT}")


def _map_error(exc):
    if _mesh is not None and isinstance(exc, _mesh.MeshReaderUnavailable):
        msg = str(exc)
        return _fail("MESH_READER_UNAVAILABLE", msg if "requirements-mesh" in msg else f"{msg} — {INSTALL_HINT}")
    code = getattr(exc, "code", None)
    if code:
        return _fail(code, str(exc))
    return _fail("MESH_FAILED", f"{type(exc).__name__}: {exc}")


def _compact(obj, max_list=10, depth=0):
    """Shrink a nested result for the model: long lists are cut and the cut is said out loud."""
    if isinstance(obj, dict):
        return {k: _compact(v, max_list, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        if obj and all(isinstance(v, (int, float)) for v in obj):
            return list(obj)
        out = [_compact(v, max_list, depth + 1) for v in obj[:max_list]]
        if len(obj) > max_list:
            out.append(f"... +{len(obj) - max_list} more")
        return out
    return obj


def _j(obj, max_list=10):
    return json.dumps(_compact(obj, max_list), separators=(",", ":"), ensure_ascii=False, default=str)


def _stem(src):
    return os.path.splitext(os.path.basename(src))[0]


def _sidecar_path(src, out_dir=None):
    return os.path.join(out_dir or os.path.dirname(src), ".solidpilot", f"{_stem(src)}.mesh.plan.json")


def _write_graph(result, src, warnings):
    """Graph file next to the mesh; falls back to the temp dir when that folder is read-only."""
    for out_dir in (os.path.dirname(src), os.path.join(tempfile.gettempdir(), "solidpilot_mesh")):
        try:
            return _mesh.write_graph(result, out_dir, _stem(src)), out_dir
        except OSError as exc:
            warnings.append(f"graph not written to {out_dir}: {exc}")
    return None, None


def _write_sidecar(src, out_dir, record, warnings):
    try:
        path = _sidecar_path(src, out_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, separators=(",", ":"))
    except OSError as exc:
        warnings.append(f"verify record not saved: {exc}")


def _load_sidecar(src):
    for out_dir in (None, os.path.join(tempfile.gettempdir(), "solidpilot_mesh")):
        try:
            with open(_sidecar_path(src, out_dir), "r", encoding="utf-8") as fh:
                rec = json.load(fh)
            if isinstance(rec, dict) and rec.get("transform"):
                return rec
        except (OSError, ValueError):
            continue
    return None


def _smoothing_text(sm):
    if not sm:
        return "smoothing: n/a"
    return "smoothing: " + _j(sm, 6)


def _summary_text(res, graph_path):
    s = res["summary"]
    ex = res["expected"]
    lines = [
        "strategy=%s (recommended %s) | nodes %s | tolerance %s mm" % (
            res["strategy"], s["recommended"], _j(s["nodes"]), s["tolerance_mm"]),
        "expected: volume %.6g m^3 | bbox_m %s | est. volume error %s%%" % (
            ex["volume_m3"], ex["bbox_m"], s["volume_error_pct"]),
        _smoothing_text(s.get("smoothing")),
        "detail: " + _j(s.get("detail"), 6),
        "attempts: " + _j(s.get("attempts"), 6),
        "analysis: " + _j(s.get("analysis"), 6),
        "mesh: " + _j(s.get("mesh"), 6),
    ]
    if res["warnings"]:
        lines.append("WARNINGS: " + " | ".join(str(w) for w in res["warnings"]))
    if graph_path:
        lines.append("graph file: %s" % graph_path)
    return "\n".join(lines)


def _plan(src, units, stations, tolerance_mm, strategy, smooth):
    return _mesh.plan(src, units, stations, tolerance_mm, strategy=strategy, smooth=smooth)


def _do_analyze(src, units, tolerance_mm):
    rep = _mesh.analyze(src, units, tolerance_mm)
    return "\n".join([
        "MESH ANALYSIS | %s | units=%s" % (os.path.basename(src), units),
        "mesh: " + _j(rep["mesh"], 8),
        "segments: " + _j(rep["segments"], 8),
        "recommended_strategy=%s | smoothing auto level=%s" % (rep["recommended_strategy"],
                                                              rep["smoothing_auto_level"]),
        "NEXT: mode='plan' lowers it to a Feature Graph (no CAD change); mode='build' builds it as a design "
        "you refine with design_step. get_recipe('mesh') has the workflow and limits.",
    ])


def _do_plan(src, units, stations, tolerance_mm, strategy, smooth):
    res = _plan(src, units, stations, tolerance_mm, strategy, smooth)
    warnings = res["warnings"]
    path, _out = _write_graph(res, src, warnings)
    return "\n".join([
        "MESH PLAN | %s | units=%s" % (os.path.basename(src), units),
        _summary_text(res, path),
        "NEXT: mode='build' builds this graph; change smooth/stations/strategy/tolerance_mm and re-plan if the "
        "summary is not what you want.",
    ])


def _do_build(src, units, stations, tolerance_mm, strategy, smooth, reference):
    res = _plan(src, units, stations, tolerance_mm, strategy, smooth)
    warnings = res["warnings"]
    path, out_dir = _write_graph(res, src, warnings)
    text, ok, sv = _run_graph(res["graph"], fresh_document=True, kind="analyze_mesh", source=src)
    if not ok:
        return text
    exp = res["expected"]
    derived = {"volume_mm3": round(exp["volume_m3"] * 1e9, 3),
               "bbox_mm": [round(v * 1e3, 4) for v in exp["bbox_m"]]}
    expectations.remember(src, derived)
    d = design_core.get()
    record = {"source": src, "units": units, "transform": res["transform"], "expected": exp,
              "graph": path, "strategy": res["strategy"], "design_id": getattr(d, "design_id", None)}
    if d is not None:
        _RECORDS[d.design_id] = record
    _write_sidecar(src, out_dir, record, warnings)

    ref_line = ""
    if reference:
        try:
            r = bridge._call_raw("open_document", {"file_path": src, "mesh_as": "graphics", "units": units})
            if r.get("status") != "COMPLETED":
                err = r.get("error") or {}
                warnings.append("reference body NOT opened (%s: %s); the build itself succeeded"
                                % (err.get("code"), err.get("message")))
            else:
                ref_line = "reference: the source mesh is open as a display-only graphics body in its own document"
        except Exception as exc:                 # noqa: BLE001 - the reference is a convenience
            warnings.append("reference body NOT opened (%s: %s); the build itself succeeded"
                            % (type(exc).__name__, exc))
        if d is not None:                        # back to the design's own document
            try:
                bridge._call_raw("activate_document", {"title": d.doc_title})
            except Exception as exc:             # noqa: BLE001
                warnings.append("could not re-activate the design document (%s); design_step will do it" % exc)
    tail = ["", _summary_text(res, path)]
    if ref_line:
        tail.append(ref_line)
    if warnings and not res["warnings"]:
        tail.append("WARNINGS: " + " | ".join(warnings))
    elif warnings != res["warnings"]:
        tail.append("WARNINGS: " + " | ".join(str(w) for w in warnings))
    tail += ["built from %s | state_version=%s" % (os.path.basename(src), sv),
             "NEXT: analyze_mesh(file_path, mode='verify') measures the surface deviation against the mesh; "
             "verify_against_interpretation() checks volume and size; refine with design_step."]
    return text + "\n".join(tail)


def _do_verify(src, units):
    rec = _load_sidecar(src)
    d = design_core.get()
    if d is not None and d.design_id in _RECORDS and os.path.abspath(_RECORDS[d.design_id]["source"]) == src:
        rec = _RECORDS[d.design_id]
    warnings = []
    transform = None
    if rec:
        transform = rec["transform"]
        if rec.get("units") and rec["units"] != units:
            warnings.append("units=%r differs from the build's %r; using the build's" % (units, rec["units"]))
        units = rec.get("units") or units
    else:
        warnings.append("no build record for this mesh: comparing without the plan's alignment transform "
                        "(centres are matched, orientation is not) — build with mode='build' first for a "
                        "meaningful result")
    if d is not None:
        try:
            bridge._call_raw("activate_document", {"title": d.doc_title})
        except Exception as exc:                 # noqa: BLE001
            warnings.append("could not activate the design document (%s); exporting the active one" % exc)
    tmp = tempfile.mkdtemp(prefix="solidpilot_mesh_verify_")
    built = os.path.join(tmp, "built.stl")
    try:
        r = bridge._call_raw("export_document", {"format": "STL", "file_path": built})
        if r.get("status") != "COMPLETED":
            err = r.get("error") or {}
            return _fail("EXPORT_FAILED", "STL export of the active document failed: %s: %s"
                         % (err.get("code"), err.get("message")))
        if not os.path.isfile(built):
            return _fail("EXPORT_FAILED", "the STL export reported success but wrote no file")
        rep = _mesh.verify_compare(src, built, units, transform=transform)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    dist = rep["distance"]
    lines = [
        "MESH VERIFY | %s | verdict=%s" % (os.path.basename(src), rep["verdict"].upper()),
        "deviation (%% of bbox diagonal %.6g m): mean %s | p95 %s | max %s" % (
            rep["source"]["diag_m"], dist["mean_pct_diag"], dist["p95_pct_diag"], dist["max_pct_diag"]),
        "volume: source %s m^3 | built %s m^3 | error %s%%" % (
            rep["source"]["volume_m3"], rep["built"]["volume_m3"], rep["volume_error_pct"]),
        "bbox_m: source %s | built %s | error %% of diag %s" % (
            rep["source"]["bbox_m"], rep["built"]["bbox_m"], rep["bbox_error_pct_diag"]),
        "area error %s%% | built units assumed %s" % (rep["area_error_pct"], rep["built"]["units_assumed"]),
    ]
    if rep["reasons"]:
        lines.append("reasons: " + "; ".join(rep["reasons"]))
    if warnings:
        lines.append("WARNINGS: " + " | ".join(warnings))
    if rep["verdict"] == "pass":
        lines.append("NEXT: within tolerance. Export, or keep refining details with design_step.")
    else:
        lines.append("NEXT: fix local errors with design_step (design_undo to back out), or re-plan with another "
                     "smooth / stations / strategy / tolerance_mm and build again. Organic parts are an "
                     "approximation; say which deviation you accept.")
    return "\n".join(lines)


@mcp.tool(structured_output=False)
def analyze_mesh(file_path: str, mode: str = "analyze", units: str = "mm", stations: int = 0,
                 tolerance_mm: float = 0.0, smooth: str = "auto", strategy: str = "auto",
                 reference: bool = False, background: bool = False) -> str:
    """Reverse-engineer a 3D mesh (.stl/.obj/.ply) into a parametric SolidWorks part. Needs
    requirements-mesh.txt. Read get_recipe('mesh') first.

    mode: 'analyze' (stats, primitives, recommended strategy; no CAD change) | 'plan' (lower to a Feature
        Graph, save it beside the mesh; no CAD change) | 'build' (plan + build as a NEW design you refine with
        design_step/design_undo; registers expectations for verify_against_interpretation) | 'verify' (export
        the design to STL and report surface deviation, volume and bbox against the mesh).
    units: the mesh file's unit, mm|cm|m|in (STL has none). stations: loft slices, 0 = auto.
    tolerance_mm: fit tolerance, 0 = auto. smooth: auto|off|low|medium|high (noise smoothing before slicing).
    strategy: auto|revolve|extrude|stacked_extrude|loft. reference=True (build): also open the mesh as a
    display-only reference document. background=True: run as a job (build only; poll job_status).
    Freeform parts are an approximation: always verify."""
    src = os.path.abspath(file_path)
    if mode not in _MODES:
        return _fail("BAD_MODE", f"mode must be one of {', '.join(_MODES)}; got {mode!r}")
    if units not in _UNITS:
        return _fail("BAD_UNITS", f"units must be one of {', '.join(_UNITS)}; got {units!r}")
    if _mesh is None:
        return _unavailable()
    if smooth not in _mesh.SMOOTH_CHOICES:
        return _fail("BAD_SMOOTH", f"smooth must be one of {', '.join(_mesh.SMOOTH_CHOICES)}; got {smooth!r}")
    if strategy not in _mesh.STRATEGIES:
        return _fail("BAD_STRATEGY", f"strategy must be one of {', '.join(_mesh.STRATEGIES)}; got {strategy!r}")
    if stations < 0 or tolerance_mm < 0:
        return _fail("BAD_ARGUMENT", "stations and tolerance_mm must be >= 0 (0 = automatic)")
    if not os.path.isfile(src):
        return _fail("FILE_NOT_FOUND", src)
    if os.path.splitext(src)[1].lower() not in _MESH_EXT:
        return _fail("UNSUPPORTED_FORMAT", f"{os.path.basename(src)}: use .stl, .obj or .ply "
                     f"(convert other formats first)")
    if background and mode == "build":
        return jobs.launch("analyze_mesh", lambda: analyze_mesh(
            file_path, mode, units, stations, tolerance_mm, smooth, strategy, reference, False))
    try:
        if mode == "analyze":
            return _do_analyze(src, units, tolerance_mm)
        if mode == "plan":
            return _do_plan(src, units, stations, tolerance_mm, strategy, smooth)
        if mode == "build":
            return _do_build(src, units, stations, tolerance_mm, strategy, smooth, reference)
        return _do_verify(src, units)
    except jobs.JobCancelled:
        raise
    except Exception as exc:                      # noqa: BLE001 - every failure is a FAILED | CODE | text
        return _map_error(exc)
