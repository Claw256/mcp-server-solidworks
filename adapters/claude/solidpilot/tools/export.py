import uuid
import os
from typing import Literal
from solidpilot import bridge
from solidpilot.app import mcp
from solidpilot.bridge import _call


# ---------------------------------------------------------------------------
# Tool: export_document
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def export_document(format: Literal["STEP", "IGES", "STL", "PDF", "DWG", "DXF"], file_path: str) -> str:
    """Export the active SolidWorks document to a file. The document remains open after export.
    format: 'STEP', 'IGES', 'STL', 'PDF', 'DWG', 'DXF'.
    file_path: full output path including filename and extension (e.g. 'C:/output/part.step').
    Note: DWG and DXF require an active drawing document (use create_drawing first).
    PDF uses SolidWorks PDF export options. STEP/IGES/STL work with part or assembly documents.
    IGES must use a .igs extension (.iges is auto-corrected to .igs)."""
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
    Partial success is reported in the response Features list."""
    return _call("batch_export", {"file_path_base": file_path_base, "formats_json": formats_json})


@mcp.tool(structured_output=False)
def export_image(view: str = "isometric", width: int = 1280, height: int = 960, file_path: str = "",
                 hires: bool = False) -> list:
    """Picture of the ACTIVE document (the built part) returned as an image, to compare with the source drawing.

    view: isometric | front | back | left | right | top | bottom | current — or a comma list for up to 4
        ("isometric,front,top"). Changes the VIEW orientation only; the model and state_version are untouched.
    width/height: requested size. The image returned to YOU is reduced automatically so it always fits the
        model's image limits (and the relay size cap); a SAVED file (see below) keeps the full requested size,
        clamped only to 64..4096 px.
    file_path: optional .bmp path for the FIRST view (default: a temp file, deleted after). When given, the
        full-size BMP is kept and a same-name .png is written next to it.
    hires: keep a full-size file for the FIRST view even without file_path (saved in the staging folder).
        Hand the user the .png with get_file (it is converted/size-checked there).
    Compare with "Image 1: drawing", "Image 2: model" labels; remember the capture is a shaded view, not a
    measurement — use verify_against_interpretation for numbers."""
    import tempfile
    try:
        import drawing_prep as dp
        from mcp.server.mcpserver.utilities.types import Image
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | IMAGE_SUPPORT_UNAVAILABLE | {ex}"]
    views = [v.strip() for v in view.split(",") if v.strip()][:4] or ["isometric"]
    me, mt = dp.tier_limits()
    req_w, req_h = max(64, min(int(width), 4096)), max(64, min(int(height), 4096))
    w0, h0 = dp.resized_size(req_w, req_h, me, mt)        # what the MODEL gets
    budget = dp.max_png_bytes(len(views))

    def shoot(v, w, h, path):
        r = bridge._call_raw("export_image", {"view": v, "width": w, "height": h, "file_path": path})
        if r.get("status") != "COMPLETED":
            err = r.get("error") or {}
            raise RuntimeError(f"FAILED | EXPORT_IMAGE_FAILED | {v}: {err.get('code')}: {err.get('message')}")
        with open(path, "rb") as fh:
            return fh.read()

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
                file_png = dp.bmp_to_png(bmp)
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
                        png = dp.bmp_to_png(shoot(v, tw, th, tmp))
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
            notes.append(f"Image {i + 1}: {v} {tw}x{th}{shrunk}{file_note}")
        except RuntimeError as ex:
            return [str(ex)]
        except (OSError, ValueError) as ex:
            return [f"FAILED | IMAGE_CONVERT_FAILED | {v}: {type(ex).__name__}: {ex}"]
    return ["; ".join(notes)] + out
