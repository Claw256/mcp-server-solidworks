"""Pure merge of the SolidWorks-native `extras` (analyze_slddrw_test include_extras=True) into a
DXF-derived drawing artifact. No COM, no I/O: unit-testable offline.

The DXF export of a .SLDDRW loses SolidWorks-native facts (sheet projection flag, which model/
configuration each view shows, tolerances, typed dimension text, note objects). This module ADDS them
and never removes or rewrites DXF-derived data, except filling a null `sheet.projection_detected`.

Everything is defensive: a malformed or partial extras payload is ignored piecewise, never raised.
"""
import json
import math

POS_TOL_MM = 0.01          # value match tolerance (mm, or degrees for angular dimensions)
PROJECTIONS = ("first_angle", "third_angle")


def parse_extras(response):
    """Pull the `extras` dict out of a raw analyze_slddrw_test ExecutionResponse. None if absent/bad."""
    try:
        if not isinstance(response, dict) or response.get("status") != "COMPLETED":
            return None
        feats = (response.get("cadState") or {}).get("features") or []
        for item in feats:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError:
                    continue
            if isinstance(item, dict) and isinstance(item.get("extras"), dict):
                return item["extras"]
    except Exception:  # noqa: BLE001 - extras are best-effort by contract
        return None
    return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _list(v):
    return v if isinstance(v, list) else []


def _dict(v):
    return v if isinstance(v, dict) else {}


def _sheets(extras):
    return [s for s in _list(extras.get("sheets")) if isinstance(s, dict)]


def _sw_dims(extras):
    """Flat [(sheet_name, view_name, dim dict)] of every well-formed SolidWorks dimension."""
    out = []
    for s in _sheets(extras):
        for v in _list(s.get("views")):
            if not isinstance(v, dict):
                continue
            for d in _list(v.get("dimensions")):
                if isinstance(d, dict) and _num(d.get("value_si")) is not None:
                    out.append((s.get("name"), v.get("name"), d))
    return out


def _matches(dxf_dim, sw_dim):
    val = _num(dxf_dim.get("value"))
    si = _num(sw_dim.get("value_si"))
    if val is None or si is None:
        return False
    if dxf_dim.get("kind") == "angular":
        return abs(math.degrees(si) - val) <= POS_TOL_MM
    return abs(si * 1000.0 - val) <= POS_TOL_MM


def _tol_out(tol, angular):
    if not isinstance(tol, dict):
        return None
    out = {k: tol[k] for k in ("type", "fit_type", "hole_fit", "shaft_fit") if k in tol}
    for src, dst in (("max_si", "max"), ("min_si", "min")):
        v = _num(tol.get(src))
        if v is not None:
            out[dst + ("_deg" if angular else "_mm")] = round(math.degrees(v) if angular else v * 1000.0, 4)
    return out or None


def _projection(extras):
    """The one projection every sheet that states one agrees on; None if none or they disagree."""
    seen = {s.get("projection") for s in _sheets(extras) if s.get("projection") in PROJECTIONS}
    return seen.pop() if len(seen) == 1 else None


def merge_extras(art, extras):
    """Merge `extras` into `art` IN PLACE. Returns a small summary dict (never raises).

    Adds: art['sheet']['slddrw'], art['slddrw_notes'], `sw_tol`/`sw_text`/`sw_name` on DXF dimensions that
    match exactly one SolidWorks dimension (and vice versa), and fills a null sheet.projection_detected.
    """
    summary = {"merged": False, "dimensions_matched": 0, "dimensions_ambiguous": 0}
    if not isinstance(art, dict) or not isinstance(extras, dict) or not _sheets(extras):
        return summary
    sheet = art.setdefault("sheet", {})
    if not isinstance(sheet, dict):
        return summary

    sw = _sw_dims(extras)
    dxf_dims = [d for d in _list(art.get("dimensions")) if isinstance(d, dict)]
    cand = {id(d): [i for i, (_, _, s) in enumerate(sw) if _matches(d, s)] for d in dxf_dims}
    claimed = {}
    for d in dxf_dims:
        for i in cand[id(d)]:
            claimed.setdefault(i, []).append(d)
    matched_idx = set()
    for d in dxf_dims:
        c = cand[id(d)]
        if not c:
            continue
        if len(c) == 1 and len(claimed[c[0]]) == 1:
            i = c[0]
            _, vname, s = sw[i]
            angular = d.get("kind") == "angular"
            tol = _tol_out(s.get("tol"), angular)
            text = s.get("text") if isinstance(s.get("text"), dict) else None
            if tol:
                d["sw_tol"] = tol
            if text:
                d["sw_text"] = text
            if tol or text:
                d["sw_name"] = s.get("name")
                d["sw_view"] = vname
                matched_idx.add(i)
                summary["dimensions_matched"] += 1
        else:
            summary["dimensions_ambiguous"] += 1

    # Sheet block: SolidWorks units kept as reported; matched dims flagged, the rest stay listed.
    flat = 0
    sheets_out = []
    for s in _sheets(extras):
        so = {k: s[k] for k in ("name", "paper_size", "scale_num", "scale_den", "projection",
                                "width_m", "height_m", "template", "skipped") if k in s}
        views_out = []
        for v in _list(s.get("views")):
            if not isinstance(v, dict):
                continue
            vo = {k: v[k] for k in ("name", "type", "scale", "referenced_model",
                                    "referenced_configuration") if k in v}
            dims = []
            for d in _list(v.get("dimensions")):
                if isinstance(d, dict) and _num(d.get("value_si")) is not None:
                    do = dict(d)
                    if flat in matched_idx:
                        do["matched"] = True
                    dims.append(do)
                    flat += 1
            vo["dimensions"] = dims
            views_out.append(vo)
        so["views"] = views_out
        sheets_out.append(so)
    block = {"sheets": sheets_out, "errors": [e for e in _list(extras.get("errors")) if isinstance(e, str)],
             "dimensions_matched": summary["dimensions_matched"]}
    props = extras.get("custom_properties")
    if isinstance(props, dict) and props:
        block["custom_properties"] = props
    sheet["slddrw"] = block

    notes = []
    for s in _sheets(extras):
        for n in _list(s.get("notes")):
            if isinstance(n, dict) and isinstance(n.get("text"), str) and n["text"].strip():
                e = {"text": n["text"], "sheet": s.get("name")}
                if isinstance(n.get("pos"), list):
                    e["pos"] = n["pos"]
                notes.append(e)
        for v in _list(s.get("views")):
            if not isinstance(v, dict):
                continue
            for n in _list(v.get("notes")):
                if isinstance(n, dict) and isinstance(n.get("text"), str) and n["text"].strip():
                    e = {"text": n["text"], "sheet": s.get("name"), "view": v.get("name")}
                    if isinstance(n.get("pos"), list):
                        e["pos"] = n["pos"]
                    notes.append(e)
    if notes:
        art["slddrw_notes"] = notes

    proj = _projection(extras)
    if proj and not sheet.get("projection_detected"):
        sheet["projection_detected"] = proj
        sheet["projection_source"] = "slddrw sheet"

    summary["merged"] = True
    return summary


def advisory(art):
    """One advisory line naming the referenced models and any extras errors; '' when nothing to say."""
    block = ((art or {}).get("sheet") or {}).get("slddrw")
    if not isinstance(block, dict):
        return ""
    models = []
    for s in _list(block.get("sheets")):
        for v in _list(_dict(s).get("views")):
            m = _dict(v).get("referenced_model")
            if isinstance(m, str) and m and m not in models:
                models.append(m)
    parts = []
    if models:
        parts.append("views reference %s" % ", ".join(models))
    errs = _list(block.get("errors"))
    if errs:
        parts.append("%d native sub-read(s) failed (%s)" % (len(errs), "; ".join(str(e) for e in errs[:3])))
    if not parts:
        return ""
    return "slddrw extras (sheet.slddrw) — " + "; ".join(parts) + ". Native facts supplement, never replace, the DXF reading."
