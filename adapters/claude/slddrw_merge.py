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


def _payload(sw_dim, angular):
    text = sw_dim.get("text") if isinstance(sw_dim.get("text"), dict) else None
    return json.dumps([_tol_out(sw_dim.get("tol"), angular), text], sort_keys=True)


def _pos_mm(v):
    """A SolidWorks dimension's sheet position ('pos' / 'text_pos', metres) -> [x, y] mm, or None."""
    for key in ("pos", "text_pos"):
        p = v.get(key)
        if isinstance(p, list) and len(p) >= 2 and _num(p[0]) is not None and _num(p[1]) is not None:
            return [p[0] * 1000.0, p[1] * 1000.0]
    return None


def _resolve_group(ds, ss, sw, sheet):
    """k DXF dimensions and k SolidWorks dimensions that all share one value -> (pairs, named) or (None, _).

    Only a NON-guess resolves: (1) every SolidWorks dim has the same tolerance/text -> any assignment is
    right, so each DXF dim gets it (no sw_name: which one it was is unknowable); (2) every SolidWorks dim
    has a position and the DXF dims' own points pair up MUTUALLY-nearest with a clear margin (assumes the
    DXF sheet origin is the SolidWorks sheet origin, as in SolidWorks' own export)."""
    angular = ds[0].get("kind") == "angular"
    if len({_payload(sw[i][2], angular) for i in ss}) == 1:
        return [(d, ss[0]) for d in ds], False
    pos = {i: _pos_mm(sw[i][2]) for i in ss}
    pts = {id(d): (d.get("defpts") or [None])[-1] for d in ds}
    if any(p is None for p in pos.values()) or any(not isinstance(p, list) or len(p) < 2 for p in pts.values()):
        return None, True

    def dist(d, i):
        return math.hypot(pts[id(d)][0] - pos[i][0], pts[id(d)][1] - pos[i][1])

    best_sw = {id(d): sorted(ss, key=lambda i: dist(d, i)) for d in ds}
    best_d = {i: sorted(ds, key=lambda d: dist(d, i)) for i in ss}
    pairs = []
    for d in ds:
        i = best_sw[id(d)][0]
        second = dist(d, best_sw[id(d)][1]) if len(ss) > 1 else float("inf")
        if best_d[i][0] is not d or dist(d, i) > 0.5 * second:
            return None, True                      # not mutual / no clear margin: refuse to guess
        pairs.append((d, i))
    return pairs, True


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
    matched_idx = set()

    def attach(d, i, named=True):
        _, vname, s = sw[i]
        angular = d.get("kind") == "angular"
        tol = _tol_out(s.get("tol"), angular)
        text = s.get("text") if isinstance(s.get("text"), dict) else None
        if tol:
            d["sw_tol"] = tol
        if text:
            d["sw_text"] = text
        if tol or text:
            if named:                     # an anonymous group assignment must not invent a name/view
                d["sw_name"] = s.get("name")
                d["sw_view"] = vname
            matched_idx.add(i)
            summary["dimensions_matched"] += 1

    # Group dimensions that can only be told apart TOGETHER: connected components of the value-match graph.
    # 1 DXF dim <-> 1 SW dim is the plain case. Several of each (the same value used twice) are resolved only
    # when that is NOT a guess: every SW dim carries the same tolerance/text (any assignment is then right),
    # or each has a position and the mutually-nearest pairing is clear. Anything else stays ambiguous.
    parent = {}

    def find(k):
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for d in dxf_dims:
        for i in cand[id(d)]:
            parent[find(("d", id(d)))] = find(("s", i))
    comps = {}
    for d in dxf_dims:
        if cand[id(d)]:
            comps.setdefault(find(("d", id(d))), ([], set()))[0].append(d)
            comps[find(("d", id(d)))][1].update(cand[id(d)])
    for ds, ss in comps.values():
        ss = sorted(ss)
        pairs, named = None, True
        if len(ds) == 1 and len(ss) == 1:
            pairs = [(ds[0], ss[0])]
        elif len(ds) == len(ss) and len({d.get("kind") == "angular" for d in ds}) == 1:
            pairs, named = _resolve_group(ds, ss, sw, sheet)
        if pairs is None:
            summary["dimensions_ambiguous"] += len(ds)
            continue
        for d, i in pairs:
            attach(d, i, named)

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
