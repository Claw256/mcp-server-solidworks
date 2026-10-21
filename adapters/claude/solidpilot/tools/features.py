from typing import Annotated
from typing import Literal
from pydantic import Field
from solidpilot.app import mcp
from solidpilot.bridge import _call


# ---------------------------------------------------------------------------
# Tool: extrude_feature
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def extrude_feature(
    depth: Annotated[float, Field(
        ge=0, description="Extrusion depth in METERS (required > 0 for boss/cut)")] = 0.0,
    feature_type: Literal["boss", "cut", "revolve", "sweep", "loft"] = "boss",
    angle_deg: Annotated[float, Field(
        gt=0, le=360, description="Revolve angle in DEGREES")] = 360.0,
    axis_x1: float = 0.0,
    axis_y1: float = 0.0,
    axis_x2: float = 0.0,
    axis_y2: float = 0.001,
    path_sketch: str = "",
    profiles: str = "[]",
    reverse: bool = False,
    through: bool = False,
    up_to_face_index: int = -1,
    mid_plane: bool = False,
    revolve_cut: bool = False,
) -> str:
    """Extrude/feature the active sketch profile.
    feature_type='boss' (default): solid extrusion, requires depth.
    feature_type='cut': material removal, requires depth and existing solid body.
    feature_type='revolve': solid of revolution — angle_deg in DEGREES (default 360 = full revolve); axis defined by axis_x1/y1 to axis_x2/y2 (midpoint of that segment selects the centerline). revolve_cut=True makes it a revolve CUT (removes material; default False = boss).
    feature_type='sweep': sweeps profile along a path — requires path_sketch (name of the path sketch).
    feature_type='loft': lofts through multiple profiles — requires profiles (JSON array of sketch names, e.g. '[\"Sketch1\",\"Sketch2\"]').
    END CONDITION (boss/cut) — pick ONE; depth is required only for BLIND or mid_plane:
      through=True          through-all; depth ignored. Use it instead of guessing a depth.
      up_to_face_index >= 0 terminates exactly ON that model face (index from
                            analyze_model('faces'), same indexing as create_sketch face_index);
                            depth ignored. -1 (default) = off.
      mid_plane=True        SYMMETRIC about the sketch plane; depth is the TOTAL width.
    reverse (boss/cut): flip the direction — needed when a cut/boss sketched on a part FACE has its
        material on the opposite side from the default. DIRECTION IS THE SILENT ONE: a feature built
        on the wrong side leaves volume, area and topology identical (recipe R6b/R14), so bind the
        sketch plane to a face explicitly and read one coordinate back afterwards.
    Sheet metal: a 'cut' on a sheet-metal body (holes after a bend included) is handled
        automatically — Normal Cut, inserted before the Flat-Pattern; no special params.
    On COMPLETED the result carries result_geometry {volume, faces, edges} after the feature — verify
        the step from that rather than a separate analyze_model.
    Exits sketch mode automatically before executing."""
    return _call(
        "extrude_feature",
        {
            "depth": depth,
            "feature_type": feature_type,
            "angle_deg": angle_deg,
            "axis_x1": axis_x1, "axis_y1": axis_y1,
            "axis_x2": axis_x2, "axis_y2": axis_y2,
            "path_sketch": path_sketch,
            "profiles": profiles,
            "reverse": reverse,
            "through": through,
            "up_to_face_index": up_to_face_index,
            "mid_plane": mid_plane,
            "revolve_cut": revolve_cut,
        },
    )


# ---------------------------------------------------------------------------
# Tool: create_rib
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def create_rib(
    thickness: Annotated[float, Field(gt=0, description="Rib thickness in METERS (e.g. 0.005 = 5mm)")],
    two_sided: bool = True,
    reverse_thickness_dir: bool = False,
    reverse_material_dir: bool = False,
    is_norm_to_sketch: bool = False,
) -> str:
    """Create a RIB from the ACTIVE sketch (consumes it, like extrude_feature). The rib profile
    is OPEN geometry — typically a single line (e.g. the diagonal between an L-bracket's legs);
    SolidWorks extends it to the surrounding walls and thickens it.
    thickness: rib thickness in METERS.
    two_sided: True (default) thickens symmetrically on both sides of the sketch; False = single
        side (then reverse_thickness_dir picks which side).
    reverse_material_dir: flip which side of the profile the rib material FILLS toward. Wrong
        direction is auto-recovered: if no feature results, the tool retries flipped (your
        explicit value is tried first).
    is_norm_to_sketch: False (default) = extrusion parallel to the sketch (the classic line-rib
        on a mid/symmetry plane); True = normal to the sketch.
    Draft is deliberately not exposed (grow on demand). On COMPLETED the result includes
    result_geometry {volume, faces, edges} — verify the step from this. Requires an existing
    solid body for the rib to attach to."""
    return _call(
        "create_rib",
        {
            "thickness": thickness,
            "two_sided": two_sided,
            "reverse_thickness_dir": reverse_thickness_dir,
            "reverse_material_dir": reverse_material_dir,
            "is_norm_to_sketch": is_norm_to_sketch,
        },
    )


# ---------------------------------------------------------------------------
# Tool: hole_wizard
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def hole_wizard(
    hole_type: Literal["counterbore", "countersink", "hole", "tap"],
    standard: Literal["ansi_inch", "ansi_metric", "iso", "din", "jis"],
    size: str,
    points: str | list[list[float]],
    face_index: int = -1,
    face_x: float | None = None,
    face_y: float | None = None,
    face_z: float | None = None,
    fastener_type: str | int = "default",
    end_condition: Literal["blind", "through_all"] = "blind",
    depth: float = 0.0,
    diameter: float = 0.0,
    cbore_diameter: float = -1.0,
    cbore_depth: float = -1.0,
    csink_diameter: float = -1.0,
    csink_angle_deg: float = -1.0,
    thread_depth: float = -1.0,
    reverse: bool = False,
) -> str:
    """Place a STANDARD SOLIDWORKS Hole Wizard hole (real counterbore / countersink / clearance / tapped
    hole with a thread callout) via IFeatureManager.HoleWizard5, at one or more points on a planar face.
    Target face: face_index (from analyze_model('faces')) OR a point face_x/face_y/face_z ON the face.
    points: array (or JSON string) of [x,y,z] METER hole centres lying ON that face, e.g. [[0.01,0.05,0]].
    hole_type: counterbore | countersink | hole (clearance) | tap (tapped).
    standard: ansi_inch | ansi_metric | iso | din | jis. fastener_type: omit for the default of that
        standard + hole_type (ISO: tap 147, clearance 144, cap-screw cbore 139, flat-head csink 140;
        ANSI metric 43/40/33/35), or pass a swWzdHoleStandardFastenerTypes_e integer. A fastener or
        size that does not belong to the standard + hole_type FAILS (HOLE_WIZARD_FAILED).
    size: e.g. 'M6' (metric) or '1/4-20' / '#10-24' (inch tap). Metric tap pitch is added automatically
        (coarse 'M6' -> 'M6x1.0'; or give 'M6x1'); for non-tap holes a pitch is stripped.
    end_condition: blind (needs depth) | through_all.
    depth/diameter in METERS (diameter 0 = the standard's size); cbore_diameter/cbore_depth METERS;
    csink_diameter METERS and csink_angle_deg DEGREES (included angle); thread_depth METERS (tap only).
    Call it AFTER the body exists (it is not part of the IR; IR hole.thread is recorded-only).
    On COMPLETED the result carries the created feature name and result_geometry."""
    params: dict = {
        "hole_type": hole_type, "standard": standard, "size": size, "points": points,
        "fastener_type": fastener_type, "end_condition": end_condition,
        "depth": depth, "diameter": diameter, "reverse": reverse,
    }
    if face_index >= 0:
        params["face_index"] = face_index
    if face_x is not None and face_y is not None and face_z is not None:
        params.update({"face_x": face_x, "face_y": face_y, "face_z": face_z})
    for k, v in (("cbore_diameter", cbore_diameter), ("cbore_depth", cbore_depth),
                 ("csink_diameter", csink_diameter), ("csink_angle_deg", csink_angle_deg),
                 ("thread_depth", thread_depth)):
        if v >= 0:
            params[k] = v
    return _call("hole_wizard", params)


# ---------------------------------------------------------------------------
# Tool: add_edge_feature
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def add_edge_feature(
    feature_type: Literal["fillet", "chamfer"],
    radius_or_distance: Annotated[float, Field(gt=0, description="Fillet radius / chamfer FIRST-face setback (D1) in METERS (e.g. 0.01 = 10mm)")],
    edge_indices: str = "",
    ex: float = 0.0,
    ey: float = 0.0,
    ez: float = 0.0,
    edges_json: str = "[]",
    chamfer_type: Literal["distance_angle", "distance_distance"] = "distance_angle",
    angle_deg: Annotated[float, Field(gt=0, lt=90, description="Chamfer angle in DEGREES (distance_angle mode only; default 45)")] = 45.0,
    distance2: Annotated[float, Field(ge=0, description="Chamfer SECOND-face setback (D2) in METERS (distance_distance mode only; must be > 0 there)")] = 0.0,
    chamfer_flip: bool = False,
) -> str:
    """Apply a 3D edge modifier to solid body edges (post-extrusion). Distinct from sketch fillet/chamfer.
    feature_type='fillet': rounds selected edge(s) with given radius_or_distance.
    feature_type='chamfer': cuts a chamfer on selected edge(s). Two modes via chamfer_type:
      - 'distance_angle' (default): radius_or_distance = setback (D1), angle_deg = the chamfer angle in DEGREES
        (default 45 → the classic equal 45° chamfer). angle_deg is ignored in the other mode.
      - 'distance_distance': radius_or_distance = first-face setback (D1), distance2 = second-face setback
        (D2, METERS, required > 0). A distance-distance chamfer is DIRECTIONAL — if D1/D2 land on the wrong
        faces (the chamfer leans the wrong way), set chamfer_flip=True to swap the sides (FlipDirection).
    PREFERRED edge selection: edge_indices — a JSON array of integer indices from analyze_model(analysis_type='edges'),
    e.g. '[3,5]'. This selects edges directly (no coordinate pick), so it works on crowded or CONCAVE edges
    (inner-corner / small-radius step edges) that a coordinate pick can't disambiguate. Takes priority over
    edges_json / ex-ey-ez.
    Fallback — single edge: ex/ey/ez (3D point on the edge); multiple edges: edges_json e.g. '[{\"ex\":0.05,\"ey\":0.05,\"ez\":0.05}]'.
    All coordinates/distances in METERS. Requires an active part document with an existing solid body."""
    params = {
        "feature_type": feature_type,
        "radius_or_distance": radius_or_distance,
        "ex": ex, "ey": ey, "ez": ez,
        "edges_json": edges_json,
    }
    if edge_indices:
        params["edge_indices"] = edge_indices
    if feature_type == "chamfer":
        params["chamfer_type"] = chamfer_type
        params["angle_deg"] = angle_deg
        params["distance2"] = distance2
        params["chamfer_flip"] = chamfer_flip
    return _call("add_edge_feature", params)


# ---------------------------------------------------------------------------
# Tool: add_reference_geometry
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def add_reference_geometry(
    type: Literal["plane", "axis", "point"],
    ref_plane_name: str = "",
    offset: float = 0.0,
    entity1_name: str = "",
    entity1_type: str = "PLANE",
    entity2_name: str = "",
    entity2_type: str = "PLANE",
    px: float = 0.0,
    py: float = 0.0,
    pz: float = 0.0,
) -> str:
    """Create reference geometry (plane, axis, or point) in the active part.
    type='plane': offset plane from ref_plane_name by offset (meters). E.g. ref_plane_name='Front Plane', offset=0.05.
    type='axis': axis at intersection of two entities. Provide entity1_name, entity1_type, entity2_name, entity2_type (e.g. 'Top Plane'/'PLANE' and 'Right Plane'/'PLANE').
    type='point': reference point at vertex (px, py, pz). Coordinates must match an existing vertex exactly.
    Returns the created feature name (e.g. 'Plane1', 'Axis1', 'Point1')."""
    return _call(
        "add_reference_geometry",
        {
            "type": type,
            "ref_plane_name": ref_plane_name,
            "offset": offset,
            "entity1_name": entity1_name,
            "entity1_type": entity1_type,
            "entity2_name": entity2_name,
            "entity2_type": entity2_type,
            "px": px, "py": py, "pz": pz,
        },
    )


# ---------------------------------------------------------------------------
# Tool: create_pattern
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def create_pattern(
    pattern_type: Literal["linear", "circular", "mirror"],
    feature_name: str = "",
    spacing: Annotated[float, Field(
        gt=0, description="Linear pattern spacing in METERS")] = 0.01,
    count: Annotated[int, Field(
        ge=2, description="Total instances incl. seed (>= 2)")] = 2,
    direction: Literal["X", "Y", "Z"] = "X",
    count2: Annotated[int, Field(
        ge=1, description="Second-direction instances (1 = off)")] = 1,
    spacing2: Annotated[float, Field(
        gt=0, description="Second-direction spacing in METERS")] = 0.01,
    flip: bool = False,
    axis_name: str = "",
    angle_deg: Annotated[float, Field(
        gt=0, le=360, description="Circular pattern angle in DEGREES")] = 90.0,
    equal_spacing: bool = True,
    features_json: str = "[]",
    plane: str = "",
    geometry_pattern: bool = False,
) -> str:
    """Create a linear, circular, or mirror feature pattern.
    pattern_type='linear': repeats feature_name along direction ('X', 'Y', or 'Z') with spacing (meters) and count instances.
        flip=True patterns toward the NEGATIVE axis (default False = positive).
        Direction maps to default planes: X→Right Plane normal, Y→Top Plane normal, Z→Front Plane normal.
        KNOWN LIMIT: count2/spacing2 are accepted but the second direction has no D2 entity wired —
        two-direction grids do NOT work in one call; compose a grid as a pattern OF a pattern instead.
    pattern_type='circular': repeats feature_name around axis_name (reference axis, e.g. 'Axis1') with count instances.
        Create the axis first with add_reference_geometry(type='axis', ...).
        equal_spacing=True (default): angle_deg is the TOTAL spread (use 360 for a full ring); count instances are
            evenly divided across it and NEVER overlap (count distinct == count).
        equal_spacing=False: angle_deg is the spacing BETWEEN adjacent instances. count*angle_deg can exceed 360, in which
            case later instances WRAP and overlap earlier ones, so the number of DISTINCT instances < count.
        Round-trip: analyze_model reports a CirPattern's instances, equal_spacing, spacing_deg plus
        the EFFECTIVE distinct_instances and a `wraps` flag. Either replay the stored form verbatim,
        or — when it wraps — use the equivalent full ring (count=distinct_instances, angle_deg=360,
        equal_spacing=True). Both give the same geometry.
    pattern_type='mirror': mirrors one or more FEATURES about a plane. features_json = a JSON array of
        feature tree names, e.g. '["Edge-Flange1","Sketched Bend2"]' (feature_name works for a single
        feature). plane = the mirror plane name ('Right Plane' default; canonical English default-plane
        names work on any localization, or a created reference plane's name). geometry_pattern=True
        mirrors the geometry without solving each feature (faster; default False = SW default).
        Round-trip: analyze_model(features) reports a MirrorPattern's mirror:{plane, features} — replay
        those values verbatim. feature_name/spacing/count/direction/axis params are ignored for mirror.
    On COMPLETED the result includes result_geometry {volume, faces, edges} after the pattern — verify from
        this instead of a separate analyze_model.
    Returns the created pattern feature name."""
    return _call(
        "create_pattern",
        {
            "pattern_type": pattern_type,
            "feature_name": feature_name,
            "spacing": spacing,
            "count": count,
            "direction": direction,
            "count2": count2,
            "spacing2": spacing2,
            "flip": flip,
            "axis_name": axis_name,
            "angle_deg": angle_deg,
            "equal_spacing": equal_spacing,
            "features_json": features_json,
            "plane": plane,
            "geometry_pattern": geometry_pattern,
        },
    )


# ---------------------------------------------------------------------------
# Tool: set_part_material
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def set_part_material(
    material_name: str,
    library: str = "SolidWorks Materials",
) -> str:
    """Assign a material to the active part document. Applied to all configurations.
    material_name: exact library name (e.g. '1060 Alloy', 'AISI 1020', 'ABS') — must match the SOLIDWORKS Materials database exactly (e.g. '1060 Alloy', NOT 'Aluminum 1060 Alloy').
    library: material library name (default 'SolidWorks Materials').
    The applied material is visible in Mass Properties and the feature tree.
    Requires an active part document — not a drawing."""
    return _call("set_part_material", {"material_name": material_name, "library": library})


# ---------------------------------------------------------------------------
# Tool: sheet_metal_feature
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def sheet_metal_feature(
    feature_type: Literal["base_flange", "edge_flange", "edge_flange_sketch", "edge_flange_finish",
                          "flat_pattern", "sketched_bend"],
    thickness: Annotated[float, Field(
        gt=0, description="Sheet thickness in METERS")] = 0.001,
    bend_radius: Annotated[float, Field(
        gt=0, description="Bend radius in METERS")] = 0.001,
    k_factor: Annotated[float, Field(
        gt=0, lt=1, description="Neutral-axis K-factor (0..1)")] = 0.5,
    ex: float = 0.0,
    ey: float = 0.0,
    ez: float = 0.0,
    flange_length: Annotated[float, Field(
        gt=0, description="Edge flange length in METERS; use >= 2*thickness+1mm")] = 0.02,
    angle_deg: Annotated[float, Field(
        gt=0, le=180, description="Bend angle in DEGREES (edge_flange and sketched_bend; default 90)")] = 90.0,
    use_default_radius: bool = False,
    flip: bool = False,
    bend_position: Literal["centerline", "material_inside", "material_outside", "bend_outside"] = "centerline",
    fixed_face_index: int = -1,
    fixed_x: float = 0.0,
    fixed_y: float = 0.0,
    fixed_z: float = 0.0,
    reverse_thickness: bool = False,
    symmetric_thickness: bool = False,
    clear_profile: bool = True,
    edge_index: int = -1,
) -> str:
    """Create sheet metal features on the active part. Lengths METERS, angles DEGREES.

    'base_flange' — a sheet metal base from the ACTIVE sketch profile; exits sketch mode.
        thickness · bend_radius (default = thickness) · k_factor (0.5).
        THICKENING SIDE IS A BUILD DECISION, and it is silent when wrong: `reverse_thickness`
        thickens to the OPPOSITE side of the sketch plane, `symmetric_thickness` splits ±t/2.
        Rebuilding an analyzed part, replay the original's own flags (analyze_model(features)
        reports them on the SMBaseFlange) — they also set the sheet orientation later bends fold
        against, so a wrong side fails downstream and less clearly.
    'edge_flange' — a DEFAULT-profile (full-edge-width) flange on an existing sheet metal edge.
        Select by edge_index (from analyze_model(edges), PREFERRED — a coordinate pick can miss a
        real edge) or a point ex/ey/ez on it. flange_length · angle_deg (90).
    'edge_flange_sketch' + 'edge_flange_finish' — the CUSTOM-profile flange, in two calls. The
        sketch call takes the attach edge (edge_index preferred, else ex/ey/ez), generates the
        edge-linked profile sketch (the API accepts ONLY a sketch it generated), clears it
        (clear_profile) and leaves it ACTIVE, echoing the sketch's MEASURED frame — express your
        profile in THAT frame. Draw with add_sketch_entity, then call the finish with the SAME
        edge (+ angle, bend_radius or use_default_radius, bend_position). The profile defines the
        outline, so flange_length is ignored.
    'sketched_bend' — bends the sheet about the bend LINE(S) in the ACTIVE sketch (draw them on a
        sheet face with create_sketch + add_sketch_entity first; the sketch must still be active).
        The side that stays PUT is the fixed face: fixed_face_index (from analyze_model(faces),
        PREFERRED) or fixed_x/y/z, a 3D point ON that face. angle_deg (90) · bend_radius, or
        use_default_radius=True to take the sheet's default and ignore it · flip reverses the fold
        direction · bend_position 'centerline' (default) | 'material_inside' | 'material_outside' |
        'bend_outside', matching the `position` analyze_model(features) reports on an SM3dBend, so a
        recipe value replays as-is. ONE sketch with SEVERAL bend lines makes one feature with one
        bend per line.
    'flat_pattern' — unfolds every bend. No extra parameters; needs an existing base_flange.

    A wrong fold is expensive: there is no sketch-entity move/delete tool, so repairing one costs
    deleting the feature and re-creating it. And a mirrored fold is invisible to volume, area AND
    topology alike — read one bend face back after building (recipe R14)."""
    params = {
        "feature_type": feature_type,
        "thickness": thickness,
        "bend_radius": bend_radius,
        "k_factor": k_factor,
        "ex": ex, "ey": ey, "ez": ez,
        "flange_length": flange_length,
        "angle_deg": angle_deg,
        "use_default_radius": use_default_radius,
        "flip": flip,
        "bend_position": bend_position,
        "fixed_x": fixed_x, "fixed_y": fixed_y, "fixed_z": fixed_z,
        "reverse_thickness": reverse_thickness,
        "symmetric_thickness": symmetric_thickness,
        "clear_profile": clear_profile,
    }
    if fixed_face_index >= 0:
        params["fixed_face_index"] = fixed_face_index
    if edge_index >= 0:
        params["edge_index"] = edge_index
    return _call("sheet_metal_feature", params)


# ---------------------------------------------------------------------------
# Tool: modify_dimension
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def modify_dimension(
    name: str,
    value: float,
) -> str:
    """Change a named display dimension's value — the UNIVERSAL parametric edit (variant keystone).
    name: the dimension full-name exactly as analyze_model(features) reports it, e.g.
          'D1@Boss-Extrude1@Part.Part' or 'D1@Sketch1'.
    value: the new value in SI units — METERS for a length/distance, RADIANS for an angle
          (e.g. 0.036 = 36mm; 0.5236 = 30°). NOT mm/degrees.
    Workflow: analyze_model(analysis_type='features') → read a feature's dimension name + value (SI) →
          modify_dimension(name, new_value) → analyze again to confirm. (Changes geometry,
          unlike analyze.) On COMPLETED the result carries result_geometry
          {dimension, requested, effective}: the value read back from SolidWorks AFTER rebuild — verify
          it matches your intent (an equation/relation may have driven it elsewhere). This is how you
          build variants (e.g. resize a boss, widen a slot, change a gear's tooth-spacing)."""
    return _call("modify_dimension", {"name": name, "value": value})


# ---------------------------------------------------------------------------
# Tool: edit_feature
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def edit_feature(
    feature_name: str,
    action: Literal["suppress", "unsuppress", "delete", "rename"],
    new_name: str = "",
) -> str:
    """Structurally edit an existing feature by name (suppress / unsuppress / delete / rename).
    feature_name: exact feature-tree name, e.g. 'Boss-Extrude1', 'Cut-Extrude1', 'CirPattern1'
          (get the names from analyze_model(analysis_type='features')).
    action='suppress':   removes the feature (and anything dependent on it) from the build WITHOUT deleting it.
    action='unsuppress': brings a suppressed feature back into the build.
    action='delete':     permanently removes the feature (works for ANY feature type, incl. sketches and reference geometry).
    action='rename':     renames the feature; requires new_name.
    new_name: required only for action='rename' (ignored otherwise).
    WARNING: suppress and delete CHANGE the model topology, so any coordinate /
    edge / face selection captured earlier may no longer resolve — re-run analyze_model
    (analysis_type='edges' or 'features') after a structural edit before selecting geometry again."""
    return _call(
        "edit_feature",
        {"feature_name": feature_name, "action": action, "new_name": new_name},
    )
