import json
import os
from solidpilot import bridge
from solidpilot.app import mcp
from solidpilot.payloads import _analysis_items, _kv_dict


# ---------------------------------------------------------------------------
# Tool: compare_parts  (adapter-only — the objective round-trip verifier, ADR-040/A0)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def compare_parts(doc_a: str, doc_b: str, detail: str = "") -> str:
    """Objectively diff two part documents — the round-trip verifier behind the artifact's
    `verified` label (and a general-purpose "are these the same part?" check).

    doc_a / doc_b: EITHER an absolute .SLDPRT path (opened/activated from disk) OR the TITLE of
        an already-open document (e.g. 'Part4' for an unsaved rebuild). doc_a is the REFERENCE
        (deltas are relative to it — normally the original; doc_b = the rebuild).

    For each doc it runs analyze_model(geometry + mass_properties) and reports topology
    (bodies/faces/edges/vertices), volume, surface area and CG side by side with deltas, plus
    the DECIDED verified-criteria verdict (analysis-artifact.schema.json): topology EXACT AND
    |ΔV| <= 1% AND |ΔA| <= 1%. The verdict is a REPORT — writing ir.verification into the
    artifact stays the caller's job. Read-only geometry-wise (activation may switch the active
    document; doc_b is left active). bbox_size A/B (exact extents) is reported beside the verdict but is not part of it.

    detail='faces' (optional): ALSO run analyze_model(faces) on both and list the surfaces that
        are EXTRA (in doc_b only) or MISSING (in doc_a only), matched by supporting plane (normal
        + offset) for planar faces and by (axis, radius) for cylinders. This is the ONE diagnosis a
        point-query can't do — it turns "which face did my rebuild add/drop?" into one call instead
        of manually pairing two full face lists. Faces are reported with their key attributes + the
        stable index `i` in their own document."""
    def _read(doc: str, label: str, want_faces: bool):
        if os.path.isfile(doc):
            r = bridge._call_raw("open_document", {"file_path": os.path.abspath(doc)})
            if r.get("status") != "COMPLETED":
                r = bridge._call_raw("activate_document", {"title": os.path.splitext(os.path.basename(doc))[0]})
        else:
            r = bridge._call_raw("activate_document", {"title": doc})
        if r.get("status") != "COMPLETED":
            err = r.get("error") or {}
            raise RuntimeError(f"DOC_{label}_UNAVAILABLE | {doc} | {err.get('code')}: {err.get('message')}")
        geo = _kv_dict(_analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "geometry", "name": ""})))
        mass = _kv_dict(_analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
        try:                                  # older execution layers lack 'bbox' — report, never fail
            bb = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "bbox", "name": ""}))
            mass["_bbox_size"] = json.loads(bb[0]).get("size") if bb else None
        except Exception:  # noqa: BLE001
            mass["_bbox_size"] = None
        faces = None
        if want_faces:
            items = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "faces", "name": ""}))
            faces = json.loads(items[0]).get("faces", []) if items else []
        return geo, mass, faces

    want_faces = detail == "faces"
    try:
        geo_a, mass_a, faces_a = _read(doc_a, "A", want_faces)
        geo_b, mass_b, faces_b = _read(doc_b, "B", want_faces)
    except RuntimeError as ex:
        return f"FAILED | {ex}"

    topo_keys = ("bodies", "faces", "edges", "vertices")
    topo_a = [geo_a.get(k) for k in topo_keys]
    topo_b = [geo_b.get(k) for k in topo_keys]
    topology_exact = topo_a == topo_b and None not in topo_a

    def _delta_pct(a, b):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or a == 0:
            return None
        return (b - a) / a * 100.0

    dv = _delta_pct(mass_a.get("volume"), mass_b.get("volume"))
    da = _delta_pct(mass_a.get("surface_area"), mass_b.get("surface_area"))
    cg_dist = None
    if all(isinstance(mass_x.get(k), (int, float)) for mass_x in (mass_a, mass_b) for k in ("cx", "cy", "cz")):
        cg_dist = ((mass_a["cx"] - mass_b["cx"]) ** 2 + (mass_a["cy"] - mass_b["cy"]) ** 2
                   + (mass_a["cz"] - mass_b["cz"]) ** 2) ** 0.5

    verified = (topology_exact and dv is not None and da is not None
                and abs(dv) <= 1.0 and abs(da) <= 1.0)

    fmt = lambda v, spec=".6g": ("?" if v is None else format(v, spec))  # noqa: E731
    line = (f"COMPLETED | verified_criteria={'PASS' if verified else 'FAIL'} | "
            f"topology A={'-'.join(str(t) for t in topo_a)} B={'-'.join(str(t) for t in topo_b)} "
            f"{'EXACT' if topology_exact else 'DIFFER'} | "
            f"volume A={fmt(mass_a.get('volume'))} B={fmt(mass_b.get('volume'))} dV={fmt(dv, '.4f')}% | "
            f"area A={fmt(mass_a.get('surface_area'))} B={fmt(mass_b.get('surface_area'))} dA={fmt(da, '.4f')}% | "
            f"cg_distance={fmt(cg_dist)} m | reference=A ({doc_a})")
    ba, bb_ = mass_a.get("_bbox_size"), mass_b.get("_bbox_size")
    if ba and bb_:                            # reported, NOT part of the verdict (extents are exact but
        line += (" | bbox_size A=" + "x".join(f"{v:.6g}" for v in ba) +        # the 1% gate is volume/area)
                 " B=" + "x".join(f"{v:.6g}" for v in bb_) + " m")

    if want_faces:
        missing, extra = _diff_faces(faces_a, faces_b)
        line += (f"\nfaces: matched={len(faces_a) - len(missing)} "
                 f"MISSING(in A only)={len(missing)} EXTRA(in B only)={len(extra)}")

        def _list(faces, cap=20):
            shown = "; ".join(_face_desc(f) for f in faces[:cap])
            if len(faces) > cap:
                shown += f"; …(+{len(faces) - cap} more)"
            return shown
        if missing:
            line += "\n  MISSING: " + _list(missing)
        if extra:
            line += "\n  EXTRA:   " + _list(extra)
    return line


_FACE_POS_TOL = 5e-5   # 50µm — supporting plane offset / point match
_FACE_RAD_TOL = 5e-5   # 50µm — cylinder radius match


def _faces_compatible(fa: dict, fb: dict) -> bool:
    """True if two faces (from analyze_model(faces) JSON) are the SAME SURFACE within tolerance —
    matched by supporting geometry, not by trim/area. Planar → same unit normal (sign-canonicalized)
    AND same signed offset from origin (±50µm). Cylinder → parallel axis AND same radius (±50µm).
    Other → same kind AND representative point within 50µm. This is deliberately looser than the
    <=1% verified gate: a face-DIFF looks for surfaces one part has and the other doesn't, so
    sub-µm/µm parameter drift on a shared surface must PAIR, leaving only true add/drops unmatched."""
    ka, kb = _face_cat(fa), _face_cat(fb)
    if ka != kb:
        return False
    if ka == "plane":
        na, oa = _plane_no(fa)
        nb, ob = _plane_no(fb)
        dot = na[0] * nb[0] + na[1] * nb[1] + na[2] * nb[2]
        return dot > 0.999 and abs(oa - ob) <= _FACE_POS_TOL
    if ka == "cyl":
        aa = [abs(float(x)) for x in fa["axis"]]
        ab = [abs(float(x)) for x in fb["axis"]]
        parallel = sum(x * y for x, y in zip(aa, ab)) > 0.999
        return parallel and abs(float(fa.get("radius", 0)) - float(fb.get("radius", 0))) <= _FACE_RAD_TOL
    pa, pb = fa.get("point"), fb.get("point")
    if isinstance(pa, list) and isinstance(pb, list):
        return all(abs(float(x) - float(y)) <= _FACE_POS_TOL for x, y in zip(pa, pb))
    return False


def _face_cat(f: dict) -> str:
    if f.get("planar") and isinstance(f.get("normal"), list) and isinstance(f.get("point"), list):
        return "plane"
    if f.get("kind") == "cylinder" and isinstance(f.get("axis"), list):
        return "cyl"
    return f.get("kind", "surf")


def _plane_no(f: dict):
    """Unit normal (sign-canonicalized so the dominant axis is positive) + signed offset from origin."""
    n = [float(x) for x in f["normal"]]
    p = [float(x) for x in f["point"]]
    offset = n[0] * p[0] + n[1] * p[1] + n[2] * p[2]
    dom = max(range(3), key=lambda i: abs(n[i]))
    if n[dom] < 0:
        n = [-x for x in n]
        offset = -offset
    return n, offset


def _diff_faces(faces_a: list, faces_b: list):
    """Greedy tolerance set-difference of two face lists. Returns (missing, extra): missing = faces
    in A that pair with no face in B; extra = faces in B that pair with none in A. Same-surface faces
    pair through sub-µm/µm parameter drift (_faces_compatible), so only genuine add/drops remain."""
    used_b = [False] * len(faces_b)
    missing = []
    for fa in faces_a:
        hit = -1
        for j, fb in enumerate(faces_b):
            if not used_b[j] and _faces_compatible(fa, fb):
                hit = j
                break
        if hit >= 0:
            used_b[hit] = True
        else:
            missing.append(fa)
    extra = [fb for j, fb in enumerate(faces_b) if not used_b[j]]
    return missing, extra


def _face_desc(f: dict) -> str:
    """Compact human description of a face for the diff report."""
    i = f.get("i")
    if f.get("planar"):
        n = f.get("normal")
        area = f.get("area")
        nstr = ("[" + ",".join(format(float(x), ".3g") for x in n) + "]") if isinstance(n, list) else "?"
        return f"i={i} plane n={nstr} area={format(float(area), '.4g') if isinstance(area, (int, float)) else '?'}"
    if f.get("kind") == "cylinder":
        return f"i={i} cyl r={format(float(f.get('radius', 0)), '.4g')}"
    return f"i={i} {f.get('kind', 'surf')}"


# ---------------------------------------------------------------------------
# Tool: compare_assemblies  (adapter-only — the assembly round-trip verifier, ADR-047 B4)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def compare_assemblies(doc_a: str, doc_b: str) -> str:
    """Objectively diff two ASSEMBLY documents against the RATIFIED verified criteria
    (ADR-047): component set EXACT (source file + config + instance counts) AND every
    component transform within tolerance (position <= 1µm, rotation <= 1e-6) AND mate
    count + types match AND mass properties within the V1 thresholds (|dV| <= 1%, |dA| <= 1%).

    doc_a / doc_b: EITHER an absolute .SLDASM path OR the TITLE of an already-open document
        (e.g. 'Assem2' for an unsaved rebuild). doc_a is the REFERENCE (the original).

    Components are paired by (source basename, instance ordinal in tree order) — instance
    numbering may legitimately differ between original and rebuild. The verdict is a REPORT;
    writing ir.verification into the artifact stays the caller's job. doc_b is left active."""
    def _read(doc: str, label: str):
        if os.path.isfile(doc):
            r = bridge._call_raw("open_document", {"file_path": os.path.abspath(doc)})
            if r.get("status") != "COMPLETED":
                r = bridge._call_raw("activate_document", {"title": os.path.splitext(os.path.basename(doc))[0]})
        else:
            r = bridge._call_raw("activate_document", {"title": doc})
        if r.get("status") != "COMPLETED":
            err = r.get("error") or {}
            raise RuntimeError(f"DOC_{label}_UNAVAILABLE | {doc} | {err.get('code')}: {err.get('message')}")
        comps = json.loads(_analysis_items(bridge._call_raw("analyze_assembly", {"analysis_type": "components"}))[0])
        mates = json.loads(_analysis_items(bridge._call_raw("analyze_assembly", {"analysis_type": "mates"}))[0])
        mass = _kv_dict(_analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
        return comps.get("components", []), mates.get("mates", []), mass

    try:
        comps_a, mates_a, mass_a = _read(doc_a, "A")
        comps_b, mates_b, mass_b = _read(doc_b, "B")
    except (RuntimeError, ValueError, IndexError) as ex:
        return f"FAILED | {ex}"

    def _key_seq(comps):
        counters: dict = {}
        out = []
        for c in comps:
            base = os.path.basename(c.get("path") or "").lower()
            cfg = c.get("config") or ""
            n = counters.get((base, cfg), 0) + 1
            counters[(base, cfg)] = n
            out.append(((base, cfg, n), c))
        return dict(out), sorted(k for k, _c in out)

    map_a, keys_a = _key_seq(comps_a)
    map_b, keys_b = _key_seq(comps_b)
    set_exact = keys_a == keys_b

    # Transform comparison over the paired components (rotation elements vs 1e-6; translation
    # meters vs 1µm). Only meaningful when the sets pair up.
    max_rot = max_pos = 0.0
    transforms_ok = set_exact
    if set_exact:
        for k in keys_a:
            ta, tb = map_a[k].get("transform"), map_b[k].get("transform")
            if not (isinstance(ta, list) and isinstance(tb, list) and len(ta) >= 12 and len(tb) >= 12):
                transforms_ok = False
                break
            max_rot = max(max_rot, max(abs(ta[i] - tb[i]) for i in range(9)))
            max_pos = max(max_pos, max(abs(ta[i] - tb[i]) for i in range(9, 12)))
        transforms_ok = transforms_ok and max_rot <= 1e-6 and max_pos <= 1e-6

    types_a = sorted(m.get("type") or "" for m in mates_a)
    types_b = sorted(m.get("type") or "" for m in mates_b)
    mates_ok = len(mates_a) == len(mates_b) and types_a == types_b

    def _delta_pct(a, b):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or a == 0:
            return None
        return (b - a) / a * 100.0

    dv = _delta_pct(mass_a.get("volume"), mass_b.get("volume"))
    da = _delta_pct(mass_a.get("surface_area"), mass_b.get("surface_area"))
    mass_ok = dv is not None and da is not None and abs(dv) <= 1.0 and abs(da) <= 1.0

    verified = set_exact and transforms_ok and mates_ok and mass_ok
    fmt = lambda v, spec=".6g": ("?" if v is None else format(v, spec))  # noqa: E731
    return (f"COMPLETED | verified_criteria={'PASS' if verified else 'FAIL'} | "
            f"components A={len(comps_a)} B={len(comps_b)} set={'EXACT' if set_exact else 'DIFFER'} | "
            f"transforms max_rot={fmt(max_rot, '.3g')} max_pos={fmt(max_pos, '.3g')} m "
            f"{'OK' if transforms_ok else 'FAIL'} | "
            f"mates A={len(mates_a)} B={len(mates_b)} types={'MATCH' if types_a == types_b else 'DIFFER'} | "
            f"volume dV={fmt(dv, '.4f')}% area dA={fmt(da, '.4f')}% | reference=A ({doc_a})")
