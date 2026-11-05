import json
import os
import tempfile
import uuid

from solidpilot import bridge, design
from solidpilot.app import mcp
from solidpilot.payloads import _analysis_items


def _model_bbox():
    items = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "bbox", "name": ""}))
    if not items:
        raise RuntimeError("analyze_model(bbox) returned nothing — is there a solid body in the active document?")
    b = json.loads(items[0])
    mm = lambda d: {k.upper(): float(v) * 1000.0 for k, v in (d.items() if isinstance(d, dict) else zip("xyz", d))}  # noqa: E731
    return mm(b["min"]), mm(b["size"])


def _capture(view: str, width: int, height: int) -> tuple:
    """(BMP bytes, view_frame dict). The frame is {} when the execution layer predates view_frame."""
    path = os.path.join(tempfile.gettempdir(), f"solidpilot_cmp_{uuid.uuid4().hex[:8]}.bmp")
    try:
        r = bridge._call_raw("export_image", {"view": view, "width": width, "height": height, "file_path": path})
        if r.get("status") != "COMPLETED":
            err = r.get("error") or {}
            raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
        frame = {}
        for item in (r.get("cadState") or {}).get("features") or []:
            if isinstance(item, str) and item.startswith("view_frame="):
                try:
                    frame = json.loads(item[len("view_frame="):])
                except ValueError:
                    frame = {}
        with open(path, "rb") as fh:
            return fh.read(), frame
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


CAPTURE_PX = (2560, 1920)        # model capture; the execution layer accepts up to 4096 per side
AMBIGUOUS_ERR = 0.03             # two view candidates this close in aspect error cannot be told apart


def _parse_region(region: str) -> tuple:
    clip = tuple(float(v) for v in json.loads(region))
    if len(clip) != 4:
        raise ValueError("region needs 4 numbers [x0,y0,x1,y1]")
    return clip


def _aspect_error(mask, aspect: float) -> float:
    import refdiff
    x0, y0, x1, y1 = refdiff.bbox_px(mask)
    return abs(((x1 - x0 + 1) / (y1 - y0 + 1)) / aspect - 1)


def _pick_view(fitz, pg, views: list, pen: float, aspect: float, fill: bool) -> tuple:
    """Choose the view cluster whose part silhouette has the model view's proportions -> (clip, candidates).
    AMBIGUOUS_VIEW when two fit equally; REGISTRATION_WEAK when none fits."""
    import refdiff
    scored = []
    for v in views[:8]:
        mask, _info = refdiff.vector_mask(fitz, pg, tuple(v["clip"]), pen, fill)
        if mask is not None:
            scored.append((_aspect_error(mask, aspect), v["clip"]))
    scored.sort(key=lambda t: t[0])
    cands = [{"clip": c, "aspect_error": round(e, 3)} for e, c in scored]
    if not scored:
        return None, cands
    listing = "; ".join(f"{c['clip']} (aspect off {c['aspect_error']:.0%})" for c in cands)
    if scored[0][0] > refdiff.ASPECT_TOLERANCE:
        raise CompareError("REGISTRATION_WEAK", f"no view on the sheet has this model view's proportions: {listing}. "
                           "Check the view name or pass region=[x0,y0,x1,y1] around the right view.")
    if len(scored) > 1 and scored[1][0] <= refdiff.ASPECT_TOLERANCE and scored[1][0] - scored[0][0] < AMBIGUOUS_ERR:
        raise CompareError("AMBIGUOUS_VIEW", f"{len(scored)} views fit this model view equally ({listing}); "
                           "pass region=[x0,y0,x1,y1] (page points) for the one to compare.")
    return tuple(scored[0][1]), cands


def _reference(src_path: str, page: int, region: str, dpi: int, fill: bool, clean_px: int, aspect: float) -> dict:
    """The reference silhouette: {mask, kind: vector|raster, pen_mm, region_used, candidates, filtered}.
    A vector page is read from its part-pen paths only; a scan (or a page with no vector pens) is thresholded."""
    import drawing_prep as dp
    import refdiff
    try:
        src = dp.open_input(src_path, bridge)
    except dp.PrepError as ex:
        raise CompareError(ex.code, ex.message) from None
    try:
        doc = src.doc
        if not 1 <= page <= doc.page_count:
            raise ValueError(f"{src_path} has {doc.page_count} page(s); asked for {page}")
        pg = doc[page - 1]
        rows, suggested = ([], None) if src.raster or pg.rotation else refdiff.page_pens(pg)
        whole = (pg.rect.x0, pg.rect.y0, pg.rect.x1, pg.rect.y1)
        clip, cands, pen = (_parse_region(region) if region else whole), [], None
        if rows:
            views = [] if region else [v for v in dp.view_regions(src.fitz, pg) if v["kind"] == "view"]
            pen = refdiff.pick_pen(src.fitz, pg, tuple(views[0]["clip"]) if views else clip, rows, suggested)
            if pen and len(views) > 1:
                picked, cands = _pick_view(src.fitz, pg, views, pen, aspect, fill)
                clip = picked or clip
            elif pen and views:
                clip = tuple(views[0]["clip"])
        out = {"kind": "raster", "pen_mm": None, "region_used": [round(v, 1) for v in clip], "candidates": cands,
               "filtered": []}
        mask = None
        if pen:
            mask, info = refdiff.vector_mask(src.fitz, pg, clip, pen, fill)
        if mask is not None:
            if clean_px > 0:
                mask = refdiff._open(mask, clean_px)
            if mask.sum() < 0.0005 * mask.size:
                raise refdiff.RefDiffError("REFERENCE_UNREADABLE", "the part pen drew (almost) nothing in this region")
            out.update(kind="vector", pen_mm=pen, mask=mask)
            if info["ignored_paths"]:
                out["filtered"].append(f"{info['ignored_paths']} paths on other pens (dimensions, extension/centre "
                                       "lines, hatching, text)")
            if info["frame"]:
                out["filtered"].append("frame")
            return out
        png, _meta = dp.render_region(src.fitz, pg, clip, dpi=dpi)
        out["mask"] = refdiff.foreground_mask(png, fill=fill, clean_px=clean_px, clean=True, filtered=out["filtered"])
        return out
    finally:
        src.close()


def _localise(regions: list, view: str, bbox_min: dict, bbox_size: dict, axes: tuple) -> str:
    """Attach suspect features (and the design nodes that built them) and, where unambiguous, a never-applied
    design_step_hint to each region. Returns a note."""
    import refdiff
    items = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "feature_map", "name": ""}))
    fmap = (json.loads(items[0]) if items else {}).get("map", [])
    d = design.get()
    nodes = (d.live().get("nodes") or []) if d is not None else []
    node_of = {}
    if d is not None:
        for nid, names in (d.live().get("node_features") or {}).items():
            for n in names:
                node_of[n] = nid
    for r in regions:
        rng = refdiff.region_model_ranges(r, view, bbox_min, bbox_size, axes)
        r["model_mm"] = rng
        sus = refdiff.suspect_features(rng, fmap)
        for s in sus:
            if s["feature"] in node_of:
                s["design_node"] = node_of[s["feature"]]
        r["suspect_features"] = sus
        hint = refdiff.design_step_hint(r, nodes)
        if hint:
            r["design_step_hint"] = hint
    return "" if d is not None else "no active design session: suspect_features are tree names only, not design nodes"


def _overlay_budget() -> int:
    import drawing_prep as dp
    return max(0, dp.max_png_bytes(1) - 32 * 1024)          # room for the JSON text sharing the result


class CompareError(Exception):
    """A registry-style failure code the callers render (FAILED | code | message) or report as NOT CHECKED."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def compare_view(reference: str, view: str = "front", page: int = 1, region: str = "", dpi: int = 200,
                 fill: bool = True, clean_px: int = 0, localise: bool = True) -> tuple:
    """The compare pipeline shared by compare_to_reference and verify_against_interpretation(profile).
    -> (result dict, overlay PNG bytes); raises CompareError."""
    try:
        import refdiff
        import drawing_prep as dp  # noqa: F401  (surfaces a missing PyMuPDF/Pillow here, not mid-pipeline)
    except Exception as ex:  # noqa: BLE001
        raise CompareError("COMPARE_UNAVAILABLE", str(ex)) from None
    src = os.path.abspath(reference)
    if not os.path.exists(src):
        raise CompareError("FILE_NOT_FOUND", src)
    try:
        refdiff.view_axes(view)
        bbox_min, bbox_size = _model_bbox()
        model_img, view_frame = _capture(view, *CAPTURE_PX)
        if view_frame.get("has_perspective"):
            raise CompareError("PERSPECTIVE_ON", "the SolidWorks view has perspective turned on, so the capture is not a "
                               "flat projection and cannot be overlaid. Turn it off (View > Display > Perspective) and call again.")
        axes = refdiff.axes_from_frame(view_frame)
        axes_note = ""
        if axes is None:
            if view_frame.get("axis_aligned") is False:
                raise CompareError("VIEW_NOT_AXIS_ALIGNED", f"the '{view}' view is not aligned with a model axis (the standard "
                                   "views were redefined?), so it has no flat silhouette to compare.")
            axes = refdiff.view_axes(view)
            axes_note = (f"view axes ASSUMED from the default '{view}' orientation (the execution layer did not report "
                         "them); if the standard views were redefined the model_mm coordinates are wrong")
        width_mm, height_mm = refdiff.view_extents_mm(view, bbox_size, axes)
        model_mask = refdiff.foreground_mask(model_img)
        ref = _reference(src, page, region, max(50, min(dpi, 400)), fill, max(0, min(clean_px, 8)),
                         width_mm / height_mm)
        ref_mask = ref["mask"]
        frame = refdiff.frame_from_bbox(model_mask, width_mm, height_mm)
        reg = refdiff.register(ref_mask, model_mask)
        res = refdiff.diff(model_mask, reg.mask, frame, view, axes=axes)
        note = _localise(res.regions, view, bbox_min, bbox_size, axes) if (localise and res.regions) else ""
        title = f"{view}: blue = model only, red = reference only  IoU {res.iou:.3f}"
        overlay = refdiff.overlay_png(model_mask, reg.mask, title, max_bytes=_overlay_budget())
    except CompareError:
        raise
    except refdiff.RefDiffError as ex:
        raise CompareError(ex.code, str(ex)) from None
    except RuntimeError as ex:
        raise CompareError("COMPARE_MODEL_READ_FAILED", str(ex)) from None
    except (OSError, ValueError) as ex:
        raise CompareError("REFERENCE_UNREADABLE", f"{type(ex).__name__}: {ex}") from None
    except Exception as ex:  # noqa: BLE001
        raise CompareError("COMPARE_FAILED", f"{type(ex).__name__}: {ex}") from None
    out = {"view": view, "iou": round(res.iou, 4), "model_only_pct": round(100 * res.model_only_frac, 2),
           "ref_only_pct": round(100 * res.ref_only_frac, 2), "px_per_mm": round(frame.px_per_mm, 3),
           "registration": {"scale": round(reg.scale, 4), "aspect_error": round(reg.aspect_error, 4)},
           "model_extent_mm": [round(width_mm, 2), round(height_mm, 2)], "edge_tolerance_mm": res.tolerance_mm,
           "regions": res.regions, "reference_kind": ref["kind"], "region_used": ref["region_used"],
           "filtered": ref["filtered"],
           "verdict": "match" if res.iou >= 0.985 and not res.regions else ("close" if res.iou >= 0.95 else "differs")}
    if ref["pen_mm"]:
        out["pen_mm"] = ref["pen_mm"]
    if ref["candidates"]:
        out["region_candidates"] = ref["candidates"]
    notes = [n for n in (axes_note, note) if n]
    if notes:
        out["note"] = "; ".join(notes)
    return out, overlay


@mcp.tool(structured_output=False)
def compare_to_reference(reference: str, view: str = "front", page: int = 1, region: str = "",
                         dpi: int = 200, fill: bool = True, clean_px: int = 0, localise: bool = True) -> list:
    """Diff the ACTIVE part's silhouette in one standard view against the same view of a reference and say WHERE
    they differ; fix with design_step, then call again.

    reference: absolute path (.pdf/.dxf/.dwg/.slddrw/.png/.jpg/.bmp/.tif). view: front|back|left|right|top|bottom,
        the model view matching the reference view. region: JSON [x0,y0,x1,y1] in PAGE POINTS (prepare_drawing's)
        to isolate one view; omitted on a multi-view vector sheet the view with the model view's proportions is
        chosen (AMBIGUOUS_VIEW when two fit: pass region). page 1-based; dpi for rasterised references.
    fill: fill closed outlines. clean_px: erode/dilate to drop thin lines still left in a raster (also thins
        slender features; 2-3 at most). localise: name the features/design nodes at each region (slower).

    A vector reference is read from its part pen only (dimensions, hatching, frame ignored); a raster has thin
    lines, frame and specks dropped (result: reference_kind, pen_mm, region_used, filtered). The reference is
    scaled to the model's bounding box, so an overall size error is NOT visible (use verify_against_interpretation
    bbox); differing proportions give REGISTRATION_WEAK, not a score. Returns JSON {iou, model_only_pct,
    ref_only_pct, verdict, regions:[{kind: model_has_extra|model_missing, size_mm, view_mm, model_mm,
    touches_edge_of, suspect_features}]} in mm plus an overlay (blue = model only, red = reference only). A
    design_step_hint is a SUGGESTED patch (meters): apply only if you agree. Overlap evidence, not dimensions:
    label photo/render reads 'vision'. Needs Pillow+numpy (requirements-compare.txt); PDFs need PyMuPDF."""
    try:
        from mcp.server.mcpserver.utilities.types import Image
        out, overlay = compare_view(reference, view, page, region, dpi, fill, clean_px, localise)
    except CompareError as ex:
        return [f"FAILED | {ex.code} | {ex.message}"]
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | COMPARE_UNAVAILABLE | {ex}"]
    design.record_verification(design.get(), {"tool": "compare_to_reference", "view": view, "iou": out["iou"],
                                              "regions": len(out["regions"]), "verdict": out["verdict"]})
    return [json.dumps(out, ensure_ascii=False), Image(data=overlay, format="png")]
