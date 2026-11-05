"""dxf_entities.py -- entity NORMALISATION for dxf_read: everything the reader can turn into its
LINE / ARC / CIRCLE / ELLIPSE primitives without changing what it already emits.

Why a separate module: dxf_read's geometry loop only understands four entity types, and a DXF from
AutoCAD / BricsCAD / LibreCAD draws its part outline with LWPOLYLINE (or POLYLINE / SPLINE), mirrors
arcs through a negative extrusion (OCS), and often wraps the whole drawing in INSERT blocks. Each of
those used to vanish without a trace. Here they are converted to VIRTUAL entities (never added to the
document) that carry the source's layer / linetype / lineweight, so `dxf_read.edge_class` and the
emit loop treat them exactly like native ones.

    explode_poly(e)      LWPOLYLINE / POLYLINE -> LINE + ARC (bulges exact), SPLINE -> flattened LINEs
    round_to_wcs(e)      ARC / CIRCLE with a non-default extrusion -> the same curve in WCS
    block_entities(ins)  every entity inside an INSERT (nested + MINSERT), in WCS
    normalise(e)         -> (list of entities to read, reason-it-was-not-read | None)

Pure geometry over ezdxf entities; ezdxf itself is imported lazily (the CI that runs the contract
tests has no ezdxf).
"""
from __future__ import annotations

import math

SPLINE_SAG_MM = 0.01        # flatten a SPLINE until the chord sag is below this (true mm)
MAX_SPLINE_SEGMENTS = 4000  # a runaway spline is cut off (and counted) rather than flooding the payload

NATIVE = ("LINE", "ARC", "CIRCLE", "ELLIPSE")
POLY = ("LWPOLYLINE", "POLYLINE", "SPLINE")


def _props(e):
    """The graphic properties a virtual replacement must inherit (layer, linetype, lineweight, ...)."""
    try:
        return dict(e.graphic_properties())
    except Exception:  # noqa: BLE001
        return {"layer": getattr(e.dxf, "layer", "0")}


def _line(a, b, props):
    from ezdxf.entities import Line
    d = dict(props)
    d["start"] = (float(a[0]), float(a[1]), 0.0)
    d["end"] = (float(b[0]), float(b[1]), 0.0)
    return Line.new(dxfattribs=d)


def _arc(c, r, a1, a2, props):
    from ezdxf.entities import Arc
    d = dict(props)
    d.update(center=(float(c[0]), float(c[1]), 0.0), radius=float(r),
             start_angle=float(a1), end_angle=float(a2))
    return Arc.new(dxfattribs=d)


def _circle(c, r, props):
    from ezdxf.entities import Circle
    d = dict(props)
    d.update(center=(float(c[0]), float(c[1]), 0.0), radius=float(r))
    return Circle.new(dxfattribs=d)


def _ext(e):
    try:
        v = e.dxf.extrusion
        return float(v.x), float(v.y), float(v.z)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 1.0


def round_to_wcs(e):
    """ARC / CIRCLE -> an equivalent entity in WCS, or None when its plane is TILTED (a 3-D curve the
    2-D reader cannot place).

    A DXF ARC/CIRCLE stores its centre and angles in the OCS defined by its extrusion. With the
    default (0,0,1) that IS the WCS. With extrusion (0,0,-1) -- how a mirrored drawing's arcs arrive --
    the x axis is flipped: the centre's x changes sign and the arc, still CCW in the OCS, runs CLOCKWISE
    in the WCS, i.e. CCW from its WCS end point to its WCS start point. Reading the raw numbers would
    mirror every such arc."""
    ex, ey, ez = _ext(e)
    if abs(ex) < 1e-9 and abs(ey) < 1e-9 and ez > 0:
        return e
    if abs(ex) > 1e-6 or abs(ey) > 1e-6:
        return None
    props = _props(e)
    c = e.ocs().to_wcs(e.dxf.center)
    if e.dxftype() == "CIRCLE":
        return _circle(c, e.dxf.radius, props)
    sp, ep = e.start_point, e.end_point                       # both in WCS
    a_s = math.degrees(math.atan2(ep.y - c.y, ep.x - c.x))    # WCS start of the CCW sweep = OCS END
    a_e = math.degrees(math.atan2(sp.y - c.y, sp.x - c.x))
    return _arc(c, e.dxf.radius, a_s % 360.0, a_e % 360.0, props)


def explode_poly(e):
    """LWPOLYLINE / POLYLINE / SPLINE -> [virtual LINE/ARC]. Bulge segments become exact ARCs
    (ezdxf's own conversion, extrusion included); a SPLINE is flattened to chords within
    SPLINE_SAG_MM, which the reader's curve fitter then collapses where a run IS an ellipse.
    Returns None when the entity cannot be exploded (a polyface / polymesh)."""
    t = e.dxftype()
    props = _props(e)
    try:
        if t == "SPLINE":
            from ezdxf import path
            pts = list(path.make_path(e).flattening(SPLINE_SAG_MM))
            if len(pts) > MAX_SPLINE_SEGMENTS:
                pts = list(path.make_path(e).flattening(SPLINE_SAG_MM * 10))
            return [_line(a, b, props) for a, b in zip(pts, pts[1:])
                    if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-9]
        if t == "POLYLINE" and (e.is_polygon_mesh or e.is_poly_face_mesh):
            return None
        return [v for v in e.virtual_entities() if v.dxftype() in ("LINE", "ARC")]
    except Exception:  # noqa: BLE001 -- a malformed entity must not sink the read
        return None


def block_entities(ins):
    """Every entity inside an INSERT in WCS, nested blocks and MINSERT arrays included."""
    try:
        parts = list(ins.multi_insert()) if (getattr(ins.dxf, "row_count", 1) or 1) > 1 \
            or (getattr(ins.dxf, "column_count", 1) or 1) > 1 else [ins]
    except Exception:  # noqa: BLE001
        parts = [ins]
    for part in parts:
        try:
            for ve in part.virtual_entities():
                if ve.dxftype() == "INSERT":
                    yield from block_entities(ve)
                else:
                    yield ve
        except Exception:  # noqa: BLE001
            continue


def normalise(e):
    """One layout entity -> ([entities the reader should consume], unread_label | None).

    LINE / ELLIPSE / DIMENSION / text pass through. ARC / CIRCLE are moved to WCS. Polylines and
    splines are exploded. `unread_label` names an entity that WAS seen and could not be used, so it
    lands in sheet.not_read instead of vanishing."""
    t = e.dxftype()
    if t in ("ARC", "CIRCLE"):
        w = round_to_wcs(e)
        return ([w], None) if w is not None else ([], t + " (tilted OCS)")
    if t in POLY:
        parts = explode_poly(e)
        return (parts, None) if parts is not None else ([], t)
    return [e], None
