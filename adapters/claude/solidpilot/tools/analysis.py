from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Literal
from solidpilot import bridge
from solidpilot.app import mcp
from solidpilot.bridge import _call
from solidpilot.payloads import _analysis_items, _kv_dict


# ---------------------------------------------------------------------------
# Tool: analyze_model
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def analyze_model(
    analysis_type: Literal["mass_properties", "geometry", "bbox", "bodies", "edges", "faces", "features", "sketch", "feature_map"],
    name: str = "",
    from_feature: str = "",
    to_feature: str = "",
    near: str = "",
    k: int = 0,
    axis: str = "",
) -> str:
    """Analyze the active SolidWorks part document (read-only, does NOT change state).
    analysis_type='mass_properties': returns volume, surface_area, center of gravity (cx, cy, cz) in document units.
    analysis_type='geometry': returns bodies, faces, edges, vertices counts of all solid bodies.
    analysis_type='bbox': EXACT axis-aligned extents of all solid bodies, JSON {min,max,size} in meters (model axes).
        Use it to check overall dimensions against a drawing. Cylindrical faces in 'faces' also carry hole (true =
        concave, a hole not a boss), u_span (rad; ~6.283 = full circle) and v_span (length along the axis).
    analysis_type='bodies': PER-BODY fingerprints for multibody parts (e.g. a flattened assembly
        STEP import): volume, area, centroid, edge/vertex counts, and the body's face-index RANGE
        in the same enumeration 'faces' walks — segment the faces list per body with it.
    analysis_type='edges': returns a JSON object listing EVERY solid edge with its start/end/MIDPOINT 3D coords
        in meters (rounded to 6 decimals; closed/circular edges also report length) and a stable index `i`. Use
        the index `i` for add_edge_feature(edge_indices=...) — robust for crowded/concave edges — or a MIDPOINT
        for coordinate-based selection (e.g. edge_flange's ex/ey/ez). On a very large part (many hundreds of edges)
        this is the slowest mode; prefer 'features' for a general understanding. TARGETED (recommended when you
        already know roughly WHERE): pass near='[x,y,z]' (+ optional k, axis) to get only the k nearest edges to
        a point instead of the full dump — see near/k/axis below.
    analysis_type='faces': returns a JSON object listing EVERY solid face with a stable index `i`; planar faces
        also carry normal, area, and a representative on-plane point (meters, rounded to 6 decimals). Use the
        index `i` for create_sketch(on_face=True, face_index=i) — robust where a coordinate pick is ambiguous
        (e.g. a revolve end-cap / shaft end whose centre lies on the axis). TARGETED: pass near='[x,y,z]'
        (+ optional k, axis) for only the k nearest faces to a point — see near/k/axis below.
    analysis_type='features': the part's COMPACT RECIPE — this is the default "understand the part" read. Returns
        the ordered feature tree (name/type; suppressed shown only when true), each feature's driving dimensions
        (deduped, SI meters/radians, rounded to 6 decimals), a per-sketch SUMMARY (segment counts + an inline
        {cx,cy,r} for single-full-circle profiles), pattern semantics (instances / spacing_deg / distinct_instances
        / wraps), and equations / globals. Each sketch reports its plane as {ref, offset} where ref is the
        canonical English default plane ('Front Plane'/'Top Plane'/'Right Plane') and offset is the signed
        distance along its normal (offset 0 = that default plane itself; offset != 0 = a parallel face/plane
        at that height — sketch on the face there). Each extrude/cut reports extrude:{end (blind/through_all/
        ...), depth (blind only), reversed (present only when the direction is flipped)} so you know HOW it
        was built; for direction use the sketch's plane.offset (offset != 0 ⇒ a face, so a cut goes into the
        part). Features are listed in tree (history) ORDER — reproduce them in that exact order (order
        matters wherever features overlap). It does NOT dump every sketch segment's coordinates — that keeps the payload small
        and is enough to understand the part and build dimension/pattern variants. Use this first.
    analysis_type='sketch' (requires name=...): ONE sketch's FULL geometry — every segment's coordinates (rounded
        to 6 decimals) plus its plane. Use this only when you actually need exact sketch geometry (e.g. to reproduce
        an irregular profile); get the sketch name from the 'features' read first. name = the sketch feature name,
        e.g. 'Sketch2'.
    analysis_type='feature_map': per-feature geometry ATTRIBUTION — deterministically answers "which edges/faces
        did each feature act on?" (e.g. WHICH edge a fillet/chamfer was applied to — never guess an anchor). Walks
        the tree base→end with the rollback bar and diffs the topology between stops INSIDE the tool; returns one
        compact JSON {feature_count, map:[{feature, type, delta:{faces,edges,vertices}, consumed_edges:[{mid,len}],
        created_faces:[{point, normal?, planar, area}]}]} (6-decimal meters). consumed_edges = edges that existed
        BEFORE the feature and were consumed by it (both endpoints gone; merely trimmed neighbours excluded) — use
        a consumed edge's `mid` as the fillet/chamfer anchor `near` point (it references the PRE-feature geometry,
        exactly what the IR anchor needs). created_faces = genuinely new surfaces (trimmed existing planes excluded).
        Sketches/planes are listed without a delta; suppressed features are skipped. Optional from_feature/to_feature
        (tree names) limit the walk to a range. NON-DESTRUCTIVE: the rollback bar is restored to the end, nothing is
        saved — but it does rebuild the model feature-by-feature, so it is the slowest mode on big trees.

    near / k / axis (edges and faces modes ONLY — a TARGETED read instead of the full dump): when you already
        know approximately where the geometry is (the common build-time case — you almost always do), pass
        near='[x,y,z]' (a JSON string, meters) to return only the nearest entities to that point, each annotated
        with `dist` (meters) and still carrying its STABLE full-enumeration index `i` (so selection is unchanged).
        k (default 0 = no cap): keep only the k nearest. axis='[x,y,z]' (optional JSON string): keep only entities
        whose direction is ~parallel to axis — for faces that's the planar-face NORMAL (e.g. axis='[0,0,1]' → only
        faces on +Z planes), for edges the chord direction. The response adds total_edge_count/total_face_count so
        you know how many were filtered out. Omitting near preserves the exact historical full-dump behavior.
    """
    return _call("analyze_model", {"analysis_type": analysis_type, "name": name,
                                   "from_feature": from_feature, "to_feature": to_feature,
                                   "near": near, "k": k, "axis": axis})


# ---------------------------------------------------------------------------
# Tool: analyze_slddrw_test  (TEST/REFERENCE ONLY — was analyze_drawing until 2026-07-27)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def analyze_slddrw_test(include_geometry: bool = False, include_relations: bool = False,
                        include_extras: bool = False) -> str:
    """TEST/REFERENCE TOOL — reads a NATIVE SolidWorks drawing (.SLDDRW) through COM.

    ⚠ NOT the shipping drawing→part path. A .SLDDRW is SLDPRT-linked (its views go blank if the part
      is gone) and nobody ships one — real inputs are DXF/DWG (and PDF). This tool survives for two
      jobs ONLY: (a) checking a drawing YOU produced (do its dimensions match the model?), and
      (b) generating COM ground truth to compare a DXF reader against. For a DXF/DWG job do NOT use
      this tool and do NOT apply its discipline — the fields below (measures, section, normal_axis,
      relations, extent, center_marks) are COM constructs that DO NOT EXIST in a DXF.

    ⮕ Reconstructing a part from a NATIVE SLDDRW (test/comparison work only)? The FROZEN
      reverse-reading discipline is deliberately NOT on the MCP surface — it is the repo
      file `cad-planner/slddrw-testing-recipe-usage.md`. Read it from disk when this tool is the
      job; port a rule out of it into the active recipe only as a conscious decision.

    Read-only, does NOT change state. Requires an active NATIVE drawing document. All lengths are
    METERS (angles radians), 6 decimals. Returns {view_count, dimension_count, views:[…]}, and the
    FIRST view is the drawing SHEET.

    view — {name, type (swDrawingViewTypes_e int), scale, pos [x,y], dimensions[], section?}
    dimension — {name, value_si, diametric?, attached?, anchors?}. `diametric` true means Ø is DATA,
      not a guess. `anchors` are the dim's reference points in MODEL space; an anchor beyond the base
      body's depth signals a feature that extends past it (offset-plane boss/loft), not bad data.
    section (SECTION views only) — {parent_view, cut_normal (the cutting plane's MODEL-space normal,
      i.e. the viewing direction), axis? (that normal snapped to X/Y/Z when axis-aligned), frame
      {origin, xdir, ydir}, label?}. A 2D coord maps to 3D as p = origin + u*xdir + v*ydir.

    include_geometry (default False) — each view's projected 2D geometry as clean primitives:
      {lines[{x1,y1,x2,y2}], curves[{n,x1,y1,xm,ym,x2,y2,cx?,cy?,r?}] (partial arcs as start/mid/end
      + fitted centre), circles[{cx,cy,r}] (FULL circles, true centre+radius), frame{origin,xdir,ydir,
      normal_axis}, extent? }. Source IView.GetPolylines7; heavier payload, so use it when you need
      the SHAPE, not for a dimension check. Two rules that fail silently if broken:
      · `normal_axis` is the view's viewing direction as a SIGNED principal axis. A circle is the
        cross-section of a feature whose axis runs along it, so circles in views with DIFFERENT
        normal_axis are DIFFERENT features — never chain them into one by radius alone.
      · `extent` {axis:[min,max]} is the SERVER-computed model-space span, frame signs applied. Read
        it before any "no material beyond X" claim; never re-derive a span from raw 2D coordinates,
        because a frame's direction components can be negative.

    include_relations (default False; forces include_geometry) — additive deterministic enrichment;
      pass BOTH for a reconstruction. Ids are positional per read: vid 'v<i>', and within a view
      c<i>/a<i>/l<i> for circles/curves/lines. Every relation carries `source` (closed enum) and
      `residual` (max deviation), so "why is this concentric?" is answerable by reading the field.
      · relations — concentric {members,center,radii} (TRUST these centres, never re-derive),
        equal_diameter {members,r,centers} (same Ø at distinct centres = twin bores), tangent.
      · measures — per dimension, the primitive id(s) it measures. `unattached:true` means it
        resolved to NO primitive: a loud gap to close from geometry, never to drop.
      · center_marks / centerlines — which circles carry marks (on:[ids]), which segments run
        through which (through:[ids]). A view that cannot expose its centerlines says so
        ({centerlines_reported, unreadable:true}) instead of guessing.

    include_extras (default False) — ADDS `extras` (default output unchanged): per sheet {name, paper_size,
      scale_num/scale_den, projection first_angle|third_angle, width_m/height_m, template, notes[]}, per view
      {name, referenced_model, referenced_configuration, type, scale, notes[], dimensions[{name, value_si,
      tol?, text?{prefix,suffix,above,below}}]}, custom_properties, and errors[] for sub-reads that failed.
      analyze_drawing(.slddrw) requests this itself and merges it into its artifact."""
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
