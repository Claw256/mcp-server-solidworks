import json
import uuid
import os
from typing import Literal
from solidpilot import bridge
from solidpilot.app import mcp
from solidpilot.bridge import _call
from solidpilot.payloads import _analysis_items


# ---------------------------------------------------------------------------
# Tool: export_document
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def export_document(format: Literal["STEP", "IGES", "STL", "PDF", "DWG", "DXF"], file_path: str) -> str:
    """Export the active SolidWorks document to a file. The document remains open after export.
    format: 'STEP', 'IGES', 'STL', 'PDF', 'DWG', 'DXF'.
    file_path: full output path including filename and extension (e.g. 'C:/output/part.step'). The extension
    must match the format (STEP .step/.stp, IGES .igs, STL .stl, PDF .pdf, DWG .dwg, DXF .dxf) or the call
    fails with INVALID_PARAMETER; IGES .iges is auto-corrected to .igs.
    Note: DWG and DXF require an active drawing document (use create_drawing first); their version, units,
    line styles and multi-sheet behaviour come from the user's SolidWorks DXF/DWG export settings (the API
    has no export-options object for them), and typically only the active sheet is written.
    PDF of a drawing exports ALL sheets and does not open a viewer. STEP/IGES/STL work with part or
    assembly documents.
    COMPLETED means the file exists, is non-empty and was just written (else EXPORT_FILE_MISSING)."""
    # export does NOT change CAD state; the execution layer returns the same state_version.
    return _call("export_document", {"format": format, "file_path": file_path})


# ---------------------------------------------------------------------------
# Tool: batch_export
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def batch_export(file_path_base: str, formats_json: str) -> str:
    """Export the active document to multiple formats in one call.
    file_path_base: output path without extension (e.g. 'C:/output/mypart').
    formats_json: JSON array of format strings (e.g. '[\"STEP\",\"STL\",\"PDF\"]').
    Each format is exported as file_path_base.<ext>. DWG/DXF skipped if active doc is not a drawing.
    PDF of a drawing exports all sheets; DWG/DXF options follow the user's SolidWorks export settings.
    A format counts as exported only if its file exists, is non-empty and was just written.
    Partial success is reported in the response Features list."""
    return _call("batch_export", {"file_path_base": file_path_base, "formats_json": formats_json})


_STYLES = ("shaded", "hidden_lines", "wireframe")
_LABELS = ("", "faces", "edges", "both")


def _json_arg(name: str, text: str, keys: tuple):
    """A JSON-object argument -> dict (None when empty); ValueError names the problem."""
    if not text or not text.strip():
        return None
    try:
        obj = json.loads(text)
    except ValueError as ex:
        raise ValueError(f"{name} must be a JSON object ({ex})") from ex
    if not isinstance(obj, dict) or not obj or not set(obj) <= set(keys):
        raise ValueError(f"{name} must be a JSON object with keys from {', '.join(keys)}")
    return obj


def _feature_json(features: list, prefix: str):
    for item in features or []:
        if isinstance(item, str) and item.startswith(prefix):
            try:
                return json.loads(item[len(prefix):])
            except ValueError:
                return None
    return None


class _Overlay:
    """The grid / index labels drawn adapter-side over a capture; every failure degrades to the plain image."""

    def __init__(self, grid: bool, labels: str, blocked: str):
        self.grid, self.labels, self.blocked = grid, labels, blocked
        self.notes = {}           # view -> one-line outcome
        self._data = None

    @property
    def wanted(self) -> bool:
        return self.grid or bool(self.labels)

    def _model(self):
        """(bbox_min_mm, bbox_size_mm, faces, edges) from analyze_model, read once."""
        if self._data is None:
            items = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "bbox", "name": ""}))
            if not items:
                raise RuntimeError("analyze_model(bbox) returned nothing")
            b = json.loads(items[0])
            mm = lambda d: {k.upper(): float(v) * 1000.0 for k, v in (d.items() if isinstance(d, dict) else zip("xyz", d))}  # noqa: E731
            lists = {}
            for kind in ("faces", "edges"):
                lists[kind] = []
                if self.labels in (kind, "both"):
                    got = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": kind, "name": ""}))
                    lists[kind] = (json.loads(got[0]) if got else {}).get(kind, [])
            self._data = (mm(b["min"]), mm(b["size"]), lists["faces"], lists["edges"])
        return self._data

    def apply(self, png: bytes, view: str, vf: dict) -> bytes:
        if not self.wanted:
            return png
        if self.blocked:
            self.notes[view] = f"grid/labels skipped: {self.blocked}"
            return png
        try:
            import refdiff
            from solidpilot import viewlabels as vl
        except Exception:  # noqa: BLE001
            self.notes[view] = "grid/labels skipped: they need numpy + Pillow (requirements-compare.txt); plain image returned"
            return png
        try:
            if vf.get("has_perspective"):
                self.notes[view] = "grid/labels skipped: perspective is on (not a flat projection); turn it off in SolidWorks"
                return png
            axes = refdiff.axes_from_frame(vf)
            if axes is None:
                if vf.get("axis_aligned") is False:
                    self.notes[view] = "grid/labels skipped: this view is not aligned with a model axis (use front/top/right...)"
                    return png
                axes = refdiff.view_axes(view)          # frame not reported: assume the default orientation
            bmin, bsize, faces, edges = self._model()
            mask = refdiff.foreground_mask(png)
            frame = refdiff.frame_from_bbox(mask, *refdiff.view_extents_mm(view, bsize, axes))
            proj = vl.Projector(axes, bmin, bsize, frame)
            h, w = mask.shape
            labels, omitted = [], []
            if self.labels:
                labels, omitted = vl.plan_labels(faces, edges, proj, mask, self.labels,
                                                 char_px=round(0.6 * vl.font_size(w, h)))
            ticks = vl.grid_ticks(proj, w, h) if self.grid else None
            (ua, us), (va, vs) = axes
            legend = f"right {'+' if us > 0 else '-'}{ua}, up {'+' if vs > 0 else '-'}{va}" + (
                f"; grid {ticks['step_mm']:g} mm, model coordinates" if ticks else "")
            out = vl.draw_overlay(png, labels, ticks, legend)
            parts = []
            if ticks:
                parts.append(f"grid {ticks['step_mm']:g} mm (model coordinates)")
            if self.labels:
                nf = sum(1 for lb in labels if lb.kind == "f")
                parts.append(f"labels f{nf} faces / e{len(labels) - nf} edges" + (
                    f", {len(omitted)} omitted for overlap" if omitted else "")
                    + " (front-facing only; occlusion not tested)")
            self.notes[view] = "overlay: " + "; ".join(parts)
            return out
        except refdiff.RefDiffError as ex:
            self.notes[view] = f"grid/labels skipped: {ex.code}: {ex}"
        except Exception as ex:  # noqa: BLE001
            self.notes[view] = f"grid/labels skipped: {type(ex).__name__}: {ex}"
        return png


@mcp.tool(structured_output=False)
def export_image(view: str = "isometric", width: int = 1280, height: int = 960, file_path: str = "",
                 hires: bool = False, style: str = "", section: str = "", zoom_to: str = "",
                 highlight: str = "", grid: bool = False, labels: str = "") -> list:
    """Picture of the ACTIVE document (the built part) returned as an image, to compare with the source drawing.

    view: isometric | front | back | left | right | top | bottom | current — or a comma list for up to 4
        ("isometric,front,top"). The view, display style and selection are put back afterwards; the model is untouched.
    width/height: requested size. The image returned to YOU is reduced automatically so it always fits the
        model's image limits (and the relay size cap); a SAVED file (see below) keeps the full requested size,
        clamped only to 64..4096 px.
    file_path: optional .bmp path for the FIRST view (default: a temp file, deleted after). When given, the
        full-size BMP is kept and a same-name .png is written next to it.
    hires: keep a full-size file for the FIRST view even without file_path (saved in the staging folder).
        Hand the user the .png with get_file (it is converted/size-checked there).
    style: shaded | hidden_lines | wireframe (default: display as is).
    section: JSON {"plane":"front|top|right|<plane name>","offset_mm":0,"reverse":false} - a graphics-only cut.
    zoom_to: JSON, ONE of {"face":i} {"edge":i} {"feature":"name"} {"bbox_mm":[x0,y0,z0,x1,y1,z1]}.
    highlight: JSON {"faces":[i],"edges":[i],"features":["name"]}, shown in the selection colour.
    grid: draw mm grid lines + values in MODEL coordinates. labels: faces | edges | both - draw f<i>/e<i> index labels,
        the same `i` as analyze_model faces/edges, so "face 12" can be acted on. Grid/labels need an axis-aligned view
        (not isometric), no zoom_to/section and Pillow+numpy; otherwise the plain image plus a one-line note.
    Compare with "Image 1: drawing", "Image 2: model" labels; remember the capture is a picture, not a
    measurement — use verify_against_interpretation for numbers."""
    import tempfile
    try:
        import drawing_prep as dp
        from mcp.server.mcpserver.utilities.types import Image
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | IMAGE_SUPPORT_UNAVAILABLE | {ex}"]
    extra = {}
    try:
        if style and style not in _STYLES:
            raise ValueError(f"style must be one of {', '.join(_STYLES)}")
        if labels not in _LABELS:
            raise ValueError("labels must be faces, edges or both")
        if style:
            extra["style"] = style
        for name, text, keys in (("section", section, ("plane", "offset_mm", "reverse")),
                                 ("zoom_to", zoom_to, ("face", "edge", "feature", "bbox_mm")),
                                 ("highlight", highlight, ("faces", "edges", "features"))):
            obj = _json_arg(name, text, keys)
            if obj is not None:
                extra[name] = json.dumps(obj)
    except ValueError as ex:
        return [f"FAILED | INVALID_PARAMETER | {ex}"]
    overlay = _Overlay(grid, labels, "zoom_to / section change the framing (need the whole model in view)"
                       if ("zoom_to" in extra or "section" in extra) else "")
    views = [v.strip() for v in view.split(",") if v.strip()][:4] or ["isometric"]
    me, mt = dp.tier_limits()
    req_w, req_h = max(64, min(int(width), 4096)), max(64, min(int(height), 4096))
    w0, h0 = dp.resized_size(req_w, req_h, me, mt)        # what the MODEL gets
    budget = dp.max_png_bytes(len(views))
    frames, warnings = {}, []

    def shoot(v, w, h, path):
        r = bridge._call_raw("export_image", {"view": v, "width": w, "height": h, "file_path": path, **extra})
        if r.get("status") != "COMPLETED":
            err = r.get("error") or {}
            raise RuntimeError(f"FAILED | EXPORT_IMAGE_FAILED | {v}: {err.get('code')}: {err.get('message')}")
        feats = (r.get("cadState") or {}).get("features")
        frames[v] = _feature_json(feats, "view_frame=") or {}
        for msg in _feature_json(feats, "capture_warnings=") or []:
            if msg not in warnings:
                warnings.append(msg)
        with open(path, "rb") as fh:
            return fh.read()

    def to_png(bmp, v):
        return overlay.apply(dp.bmp_to_png(bmp), v, frames.get(v, {}))

    out, notes = [], []
    for i, v in enumerate(views):
        keep = i == 0 and (bool(file_path) or hires)
        if keep:
            if file_path:
                path = os.path.abspath(file_path)
            else:
                staging = os.path.abspath(os.getenv("AGENT_STAGING_DIR", os.path.join(os.path.expanduser("~"), "SolidPilotStaging")))
                os.makedirs(staging, exist_ok=True)
                path = os.path.join(staging, f"solidpilot_{uuid.uuid4().hex[:8]}.bmp")
        else:
            path = None
        try:
            file_png, file_note = None, ""
            if keep:
                bmp = shoot(v, req_w, req_h, path)
                fw, fh_ = dp.bmp_size(bmp)
                file_png = to_png(bmp, v)
                png_path = os.path.splitext(path)[0] + ".png"
                with open(png_path, "wb") as fh:
                    fh.write(file_png)
                if len(file_png) <= dp.max_png_bytes(1):
                    fit = "get_file will deliver it"
                else:
                    s = dp.fit_scale(len(file_png))
                    fit = (f"too big for get_file's {dp.RELAY_MAX_BYTES // (1024 * 1024)} MB relay cap; "
                           f"largest likely to fit ≈ {int(fw * s)}x{int(fh_ * s)}")
                file_note = (f"; file {fw}x{fh_} -> {path} + {png_path} "
                             f"(PNG {len(file_png) / 1048576:.2f} MB, {fit})")
            tw, th = w0, h0
            for _ in range(12):
                if file_png is not None and (tw, th) == (fw, fh_):
                    png = file_png                      # the saved render is already the inline size
                else:
                    tmp = os.path.join(tempfile.gettempdir(), f"solidpilot_{uuid.uuid4().hex[:8]}.bmp")
                    try:
                        png = to_png(shoot(v, tw, th, tmp), v)
                    finally:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                if len(png) <= budget:
                    break
                tw, th = max(64, int(tw * 0.9)), max(64, int(th * 0.9))      # too big for the relay: step down
            else:
                return [f"FAILED | RESULT_TOO_LARGE | {v}: PNG still {len(png)} bytes at {tw}x{th}; "
                        f"the relay cap leaves {budget} bytes per image"]
            out.append(Image(data=png, format="png"))
            shrunk = f" (reduced from {w0}x{h0} to fit the relay size cap)" if (tw, th) != (w0, h0) else ""
            overlay_note = f" [{overlay.notes[v]}]" if v in overlay.notes else ""
            notes.append(f"Image {i + 1}: {v} {tw}x{th}{shrunk}{overlay_note}{file_note}")
        except RuntimeError as ex:
            return [str(ex)]
        except (OSError, ValueError) as ex:
            return [f"FAILED | IMAGE_CONVERT_FAILED | {v}: {type(ex).__name__}: {ex}"]
    if warnings:
        notes.append("capture warnings: " + "; ".join(warnings))
    return ["; ".join(notes)] + out
