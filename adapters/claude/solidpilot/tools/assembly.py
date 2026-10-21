from typing import Literal
from solidpilot.app import mcp
from solidpilot.bridge import _call


# ---------------------------------------------------------------------------
# Tool: open_new_assembly  (Phase B, ADR-047)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def open_new_assembly(template_path: str = "") -> str:
    """Open a new blank SolidWorks ASSEMBLY document (the twin of open_new_part).
    Use before insert_component / add_mate. template_path: optional .asmdot template
    (default: the user-configured assembly template)."""
    params = {}
    if template_path:
        params["template_path"] = template_path
    return _call("open_new_assembly", params)


# ---------------------------------------------------------------------------
# Tool: analyze_assembly  (Phase B read tool, ADR-047 B1)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def analyze_assembly(
    analysis_type: Literal["components", "components_flat", "mates", "faces", "edges"],
    component: str = "",
) -> str:
    """Analyze the active ASSEMBLY document (read-only, does NOT change state).
    analysis_type='components': top-level component occurrences in FEATURE-TREE order — per
        instance: name (instance-numbered, e.g. 'Shaft-1'), source file path, config, fixed /
        suppressed flags, children count (a subassembly — out of scope, recorded as a gap), and
        the FULL transform (13 numbers: 3x3 rotation row-major + translation in meters + scale)
        — the position ground truth for round-trip verification.
    analysis_type='components_flat': LEAF occurrences across ALL nesting levels (depth-first
        tree order, transforms in ROOT space, each with its parent's name) — flattens a
        wrapper/subassembly level for ground-truth acquisition; the IR itself stays flat.
    analysis_type='mates': all mates in CREATION order — type and alignment from the SolidWorks
        ENUMS (locale-proof), the SI value for distance/angle mates, and per mate entity: owning
        component, entity kind (plane/cylinder/circle/...), and its geometric params in ASSEMBLY
        space (location + direction + radii).
    analysis_type='faces' / 'edges': ONE component's face/edge list with stable indices — feeds
        add_mate's index-based entity selection. Requires `component` (the Name2 from
        'components'). Coordinates are COMPONENT-LOCAL (the payload carries the transform)."""
    params = {"analysis_type": analysis_type}
    if component:
        params["component"] = component
    return _call("analyze_assembly", params)


# ---------------------------------------------------------------------------
# Tool: insert_component  (Phase B build tool, ADR-047 B3)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def insert_component(
    file_path: str,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    config: str = "",
    fixed: bool = False,
    transform_json: str = "[]",
) -> str:
    """Insert a part file as a component of the ACTIVE assembly (open_new_assembly first).
    file_path: the .SLDPRT to insert (meters for all coordinates).
    x/y/z: insertion point — used when transform_json is not given.
    transform_json: OPTIONAL full placement as a JSON array STRING of 13 numbers
        (3x3 rotation row-major + translation xyz + scale — exactly the layout
        analyze_assembly(components) reports). Needed for rotated placements.
    fixed: True fixes the occurrence (its transform becomes authoritative); False leaves it
        floating for mates to position. The state is applied explicitly either way.
    Returns the runtime component name (e.g. 'Shaft-2') + fixed/transform readbacks."""
    params = {"file_path": file_path, "x": x, "y": y, "z": z, "fixed": fixed}
    if config:
        params["config"] = config
    if transform_json and transform_json != "[]":
        params["transform_json"] = transform_json
    return _call("insert_component", params)


# ---------------------------------------------------------------------------
# Tool: add_mate  (Phase B build tool, ADR-047 B3)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def add_mate(
    mate_type: Literal["coincident", "concentric", "perpendicular", "parallel",
                       "tangent", "distance", "angle", "lock"],
    a_component: str,
    a_index: int,
    b_component: str,
    b_index: int,
    a_kind: Literal["face", "edge"] = "face",
    b_kind: Literal["face", "edge"] = "face",
    alignment: Literal["aligned", "anti_aligned", "closest"] = "closest",
    flip: bool = False,
    value: float = 0.0,
) -> str:
    """Add a mate between two component entities in the ACTIVE assembly.
    Entity selection is INDEX-based (robust — coordinate picks miss real geometry): each side is
    (component name from analyze_assembly(components), kind 'face'|'edge', index from
    analyze_assembly(faces|edges, component=...)).
    value: METERS for distance mates, DEGREES for angle mates (ignored otherwise).
    alignment 'closest' lets SolidWorks pick the nearer solution; the mates reader reports the
    RESOLVED alignment afterwards.
    Returns the mate name + both components' post-rebuild transforms — compare against the
    original's readback after EVERY mate (the stop-check discipline)."""
    params = {"mate_type": mate_type, "alignment": alignment,
              "a_component": a_component, "a_kind": a_kind, "a_index": a_index,
              "b_component": b_component, "b_kind": b_kind, "b_index": b_index}
    if flip:
        params["flip"] = True
    if mate_type in ("distance", "angle"):
        params["value"] = value
    return _call("add_mate", params)


# ---------------------------------------------------------------------------
# Tool: save_body_as_part  (Phase B — flattened-assembly reconstruction, ADR-048)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def save_body_as_part(body_index: int, file_path: str) -> str:
    """Extract ONE solid body of the ACTIVE multibody part into its own .SLDPRT file.
    body_index: from analyze_model(analysis_type='bodies'). The body's geometry stays in the
    SOURCE coordinates (recover the instance transform separately); the new part is saved to
    file_path and closed, and the source document is re-activated. Use for flattened assembly
    imports whose per-part files don't exist: one file per DISTINCT body (fingerprint-deduped),
    then instances reference it with recovered transforms."""
    return _call("save_body_as_part", {"body_index": body_index, "file_path": file_path})
