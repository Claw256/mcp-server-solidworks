from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Literal
from execution_client import ExecutionLayerError
from response_mapper import CadOperationError, map_response
from solidpilot import bridge
from solidpilot.app import mcp
from solidpilot.bridge import _call
from solidpilot.payloads import _analysis_items, _kv_dict


# ---------------------------------------------------------------------------
# Tool: analyze_model
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def analyze_model(
    analysis_type: Literal["mass_properties", "geometry", "bbox", "bodies", "edges", "faces", "features", "sketch", "feature_map", "overview", "measure"],
    name: str = "",
    from_feature: str = "",
    to_feature: str = "",
    near: str = "",
    k: int = 0,
    axis: str = "",
) -> str:
    """Analyze the active SolidWorks part (read-only, does NOT change state). Lengths are meters, angles radians
    (6-decimal rounding) unless a key says mm/deg.
    'overview': ONE read of how the part is modelled: ordered feature tree (type, suppressed, per-feature rebuild
        error/warning, driving dimensions), rollback_at_end, equations, bodies, bbox, mass (volume, area, CG, mass,
        density, principal moments), configs {names, active} + per-feature suppressed_in, file custom_properties,
        and `design` (node id -> feature names) when a design session exists. Start here.
    'mass_properties': volume, surface_area, cx/cy/cz, mass, density, pmoi_1..3 as key=value.
    'geometry': body/face/edge/vertex counts.  'bbox': exact {min,max,size} of all solid bodies.
    'bodies': per-body volume/area/centroid + its face-index range in 'faces' (multibody / flattened STEP).
    'edges' / 'faces': every solid edge (start/end/mid) or face (area; planar normal + point; cylinders kind/
        origin/axis/radius, hole = concave, u_span ~6.283 = full circle, v_span) with a stable index `i`: use it for
        add_edge_feature(edge_indices=...) and create_sketch(on_face, face_index=...). Slow on huge parts; prefer
        near/k/axis (below).
    'features': the compact recipe: ordered tree, driving dimensions, per-sketch summary (segment counts, inline
        circle, solve `status`, plane {ref, offset}), patterns (instances/spacing_deg/distinct_instances/wraps),
        equations. Feature definitions where readable: extrude {end, depth, reversed, draft_deg, direction2, thin,
        start, up_to, merge}, revolve, fillet/chamfer {radius|distance, edges}, hole_wizard, loft, sweep.
        Order matters: reproduce features in that order. No per-segment coordinates.
    'sketch' (name=sketch name): full segment coordinates + plane + `health`: status (under_defined/fully_defined/
        over_defined/no_solution/...), segment_status (indices into segments that are not fully defined), relations
        [{type, entities:[{seg: i} | {point}]}], over_defining.
    'feature_map': which edges/faces each feature acted on: {map:[{feature, type, delta, consumed_edges:[{mid,len}],
        created_faces}]}. A consumed edge's `mid` is the fillet/chamfer anchor for `near`. Rolls the bar feature by
        feature and restores it (slowest mode); from_feature/to_feature limit the range.
    'measure' (name=JSON): {"a": E, "b": E, "kind": "distance|angle|radius|diameter|length|area"}, E = {"face": i} |
        {"edge": i} | {"point": [x,y,z] meters} (indices from 'faces'/'edges'); b optional for radius/diameter/
        length/area; points support distance only. Returns one JSON object with just distance_mm | angle_deg |
        radius_mm | diameter_mm | length_mm | area_mm2. Arcs/cylinders measure centre to centre. Use it to check a
        drawing dimension.
    near / k / axis (edges, faces only): near='[x,y,z]' returns only the entities nearest that point (each with
        `dist`, same stable `i`); k caps how many (0 = all); axis='[x,y,z]' keeps entities parallel to it (faces:
        planar normal, edges: chord). Responses add total_edge_count/total_face_count.
    """
    if analysis_type == "overview":
        return _overview()
    return _call("analyze_model", {"analysis_type": analysis_type, "name": name,
                                   "from_feature": from_feature, "to_feature": to_feature,
                                   "near": near, "k": k, "axis": axis})


def _same_document(design_title, active) -> bool:
    """A design only describes THIS part when its document title matches (extension and case ignored)."""
    norm = lambda t: os.path.splitext(str(t or ""))[0].strip().lower()
    return bool(design_title) and bool(active) and norm(design_title) == norm(active)


def _design_map(active_document) -> dict | None:
    """node id -> feature names of the live design snapshot, or None when there is no design for this part.
    Tolerant by construction: the overview must never fail because a design record is missing or odd."""
    try:
        from solidpilot import design
        d = design.get()
        if d is None or not _same_document(getattr(d, "doc_title", ""), active_document):
            return None
        nodes = d.live().get("node_features") or {}
        return {"design_id": d.design_id, "step": d.head, "node_features": dict(nodes)} if nodes else None
    except Exception:  # noqa: BLE001
        return None


def _overview() -> str:
    """analyze_model('overview'): the C# digest plus the adapter-side design-session map."""
    try:
        response = bridge._call_raw("analyze_model", {"analysis_type": "overview", "name": "",
                                                      "from_feature": "", "to_feature": "",
                                                      "near": "", "k": 0, "axis": ""})
    except ExecutionLayerError as ex:
        raise CadOperationError(f"Execution layer unavailable | {ex}") from ex
    items = (response.get("cadState") or {}).get("features") if response.get("status") == "COMPLETED" else None
    if items:
        try:
            digest = json.loads(items[0])
            dm = _design_map((response.get("cadState") or {}).get("activeDocument"))
            if dm is not None:
                digest["design"] = dm
                response["cadState"]["features"] = [json.dumps(digest, separators=(",", ":"))] + list(items[1:])
        except (ValueError, TypeError):
            pass
    return map_response(response)


# ---------------------------------------------------------------------------
# Tool: analyze_slddrw_test  (TEST/REFERENCE ONLY — was analyze_drawing until 2026-07-27)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def analyze_slddrw_test(include_geometry: bool = False, include_relations: bool = False,
                        include_extras: bool = False) -> str:
    """TEST/REFERENCE TOOL — reads the ACTIVE native SolidWorks drawing (.SLDDRW) through COM. Read-only.

    NOT the shipping drawing->part path (use analyze_drawing for DXF/DWG/PDF). A .SLDDRW is SLDPRT-linked and
    nobody ships one; use this only to (a) check a drawing YOU produced against its model (dimension_count > 0,
    values match) and (b) get COM ground truth to compare a DXF reader against. Its COM-only fields (measures,
    section, normal_axis, relations, extent, center_marks) do not exist in a DXF.

    Returns {view_count, dimension_count, views:[...]} in METERS/radians; the FIRST view is the sheet.
    view {name, type, scale, pos[x,y], dimensions[], section?}; dimension {name, value_si, diametric?, attached?,
    anchors?} — `diametric` true means an Ø is DATA, not a guess; an anchor beyond the base body's depth marks a
    feature that extends past it. section {parent_view, cut_normal, axis?, frame{origin,xdir,ydir}, label?}:
    a 2D point maps to 3D as origin + u*xdir + v*ydir.

    include_geometry: each view's projected primitives {lines, curves, circles, frame{origin,xdir,ydir,
      normal_axis}, extent?} (heavier). Two rules that fail silently: circles in views with DIFFERENT normal_axis
      are DIFFERENT features — never chain them by radius; and read `extent` (server-computed model-space span)
      before any "no material beyond X" claim instead of re-deriving it from 2D coordinates.
    include_relations (forces include_geometry): deterministic enrichment with `source` and `residual` on every
      item — concentric / equal_diameter / tangent relations (trust the centres), `measures` (which primitive each
      dimension measures; `unattached:true` is a gap to close, never to drop), center_marks / centerlines.
    include_extras: ADDS `extras` (sheet paper size/scale/projection, per-view referenced model and notes,
      dimension tolerances/text, custom properties, errors[]); analyze_drawing(.slddrw) requests it itself."""
    return _call("analyze_slddrw_test", {"include_geometry": include_geometry,
                                         "include_relations": include_relations,
                                         "include_extras": include_extras})


# ---------------------------------------------------------------------------
# Tool: save_analysis  (Phase A analysis pipeline — ADAPTER-ONLY, no C#; ADR-040)
# ---------------------------------------------------------------------------
ANALYSIS_SCHEMA_VERSION = "0.1.0-draft"  # cad-planner/contracts/analysis-artifact.schema.json



def _collect_parameters(node, out: list, feature_name: str = "") -> None:
    """Walk the features recipe and lift dimension entries into the flat named-parameter table.

    A dimension entry is a dict whose 'name' is a FULL dimension name (contains '@', e.g.
    'D1@Boss-Extrude1@Part1.Part' — the modify_dimension target) with a numeric value under
    'value_si' or 'value'. Tolerant by design: the exact recipe keys are C#-owned; anything that
    doesn't match the shape is simply not lifted."""
    if isinstance(node, dict):
        owner = node.get("name") if node.get("type") else None
        value = node.get("value_si", node.get("value"))
        name = node.get("name")
        if isinstance(name, str) and "@" in name and isinstance(value, (int, float)):
            out.append({"name": name, "value_si": value, "feature": feature_name})
            return
        for child in node.values():
            _collect_parameters(child, out, owner or feature_name)
    elif isinstance(node, list):
        for child in node:
            _collect_parameters(child, out, feature_name)


@mcp.tool(structured_output=False)
def save_analysis(file_path: str) -> str:
    """Analyze a part FILE and persist its analysis ARTIFACT — the entry tool of the analysis
    pipeline. Opens the part (activates it if already open), runs the standard reads (features
    recipe + mass_properties + geometry), computes the file's sha256, and writes
    `<folder>/.solidpilot/<filename>.analysis.json` per
    cad-planner/contracts/analysis-artifact.schema.json.

    The artifact is a CACHE of the file's state at analysis time: consumers must compare
    identity.source_hash against the current file and re-analyze on a mismatch. The `ir` block
    is left null here — the AI/IR pass fills it later: BEFORE writing an ir.graph, read the MCP
    resource `recipe://usage/index` for the mapping rules. The part is left OPEN and
    ACTIVE for follow-up work.

    file_path: absolute path of the .SLDPRT part OR .SLDASM assembly to analyze (drawing
        artifacts arrive with later pipeline steps). An assembly artifact's recipe holds the
        component tree (tree order, full transforms) + mates (creation order, enum types,
        entity anchors) + assembly mass properties.
    Returns a token-frugal summary (artifact path + counts) — the artifact JSON stays on disk;
    read it from there when the full content is needed."""
    src = os.path.abspath(file_path)
    if not os.path.isfile(src):
        return f"FAILED | FILE_NOT_FOUND | {src}"
    is_assembly = src.lower().endswith(".sldasm")
    if not src.lower().endswith(".sldprt") and not is_assembly:
        return ("FAILED | UNSUPPORTED_TYPE | save_analysis analyzes .SLDPRT parts and .SLDASM "
                "assemblies (drawing artifacts come with later pipeline steps)")
    with open(src, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()

    # Open (or activate, if it is already open under its title).
    opened = bridge._call_raw("open_document", {"file_path": src})
    if opened.get("status") == "FAILED":
        activated = bridge._call_raw("activate_document", {"title": os.path.basename(src)})
        if activated.get("status") == "FAILED":
            err = opened.get("error") or {}
            return f"FAILED | OPEN_FAILED | {err.get('code')}: {err.get('message')}"

    if is_assembly:
        return _save_assembly_analysis(src, sha)

    try:
        features_items = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "features", "name": ""}))
        mass_kv = _kv_dict(_analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
        geometry = _kv_dict(_analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "geometry", "name": ""})))
    except RuntimeError as ex:
        return f"FAILED | ANALYZE_FAILED | {ex}"

    notes = []
    if not features_items:
        return "FAILED | NO_PAYLOAD | analyze_model(features) returned an empty payload"
    try:
        features = json.loads(features_items[0])
    except (ValueError, TypeError):
        features = {"raw": features_items}
        notes.append("features payload was not parseable JSON — stored raw (reader refinement pending)")

    mass = {
        "volume_m3": mass_kv.get("volume"),
        "surface_area_m2": mass_kv.get("surface_area"),
        "cg": {"x": mass_kv.get("cx"), "y": mass_kv.get("cy"), "z": mass_kv.get("cz")},
    }

    parameters: list = []
    _collect_parameters(features, parameters)

    # V1 relationships: drawing files next to the part whose stem is the part's stem, optionally
    # followed by a suffix word (e.g. 'part-1 drawing.SLDDRW' for 'part-1.SLDPRT').
    folder = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0].lower()
    drawings = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(".slddrw")
        and (os.path.splitext(f)[0].lower() == stem
             or os.path.splitext(f)[0].lower().startswith(stem + " "))
    )

    artifact = {
        "identity": {
            "source_path": src,
            "source_filename": os.path.basename(src),
            "source_hash": "sha256:" + sha,
            "source_mtime": datetime.fromtimestamp(os.path.getmtime(src), timezone.utc).isoformat(),
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "generator": {"kind": "deterministic"},
        },
        "document_type": "part",
        "recipe": {
            "features": features,
            "mass_properties": mass,
            "geometry": geometry,
            "bbox": None,
            "material": None,
            "equations": [],
        },
        "parameters": parameters,
        "ir": None,
        "relationships": {"drawings": drawings, "category": None, "cluster_signals": {}},
        "notes": notes + ["bbox/material/equations extraction pending refinement (A2 live pass)"],
    }

    out_dir = os.path.join(folder, ".solidpilot")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.basename(src) + ".analysis.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)

    geo = geometry if isinstance(geometry, dict) else {}
    feature_count = len(features.get("features", [])) if isinstance(features, dict) else "?"
    return (f"COMPLETED | artifact={out_path} | features={feature_count} | "
            f"parameters={len(parameters)} | geometry={{bodies:{geo.get('bodies')},"
            f"faces:{geo.get('faces')},edges:{geo.get('edges')},vertices:{geo.get('vertices')}}} | "
            f"drawings_linked={len(drawings)} | hash=sha256:{sha[:12]}")


def _save_assembly_analysis(src: str, sha: str) -> str:
    """The .SLDASM branch of save_analysis (Phase B): components (tree order, full transforms)
    + mates (creation order, enum types, entity anchors) + assembly mass properties. Component
    source files get their own sha256 in relationships so staleness is loud per part
    (ADR-047b: the assembly references part FILES — their artifacts live separately)."""
    try:
        comp_items = _analysis_items(bridge._call_raw("analyze_assembly", {"analysis_type": "components"}))
        mate_items = _analysis_items(bridge._call_raw("analyze_assembly", {"analysis_type": "mates"}))
        mass_kv = _kv_dict(_analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
    except RuntimeError as ex:
        return f"FAILED | ANALYZE_FAILED | {ex}"
    try:
        components = json.loads(comp_items[0]) if comp_items else {}
        mates = json.loads(mate_items[0]) if mate_items else {}
    except (ValueError, TypeError) as ex:
        return f"FAILED | PAYLOAD_UNPARSEABLE | {ex}"

    part_files = []
    seen_paths = set()
    for c in components.get("components", []):
        p = c.get("path")
        if not p or p in seen_paths:
            continue
        seen_paths.add(p)
        entry = {"path": p}
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                entry["hash"] = "sha256:" + hashlib.sha256(fh.read()).hexdigest()
        else:
            entry["missing"] = True
        part_files.append(entry)

    artifact = {
        "identity": {
            "source_path": src,
            "source_filename": os.path.basename(src),
            "source_hash": "sha256:" + sha,
            "source_mtime": datetime.fromtimestamp(os.path.getmtime(src), timezone.utc).isoformat(),
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "generator": {"kind": "deterministic"},
        },
        "document_type": "assembly",
        "recipe": {
            "components": components,
            "mates": mates,
            "mass_properties": {
                "volume_m3": mass_kv.get("volume"),
                "surface_area_m2": mass_kv.get("surface_area"),
                "cg": {"x": mass_kv.get("cx"), "y": mass_kv.get("cy"), "z": mass_kv.get("cz")},
            },
        },
        "parameters": [],
        "ir": None,
        "relationships": {"part_files": part_files, "category": None, "cluster_signals": {}},
        "notes": [],
    }

    out_dir = os.path.join(os.path.dirname(src), ".solidpilot")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.basename(src) + ".analysis.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)

    return (f"COMPLETED | artifact={out_path} | components={components.get('component_count')} | "
            f"mates={mates.get('mate_count')} | part_files={len(part_files)} | hash=sha256:{sha[:12]}")
