"""Render DXF / DWG / SLDDRW drawings so they can be seen like a PDF.

Contract (callers: drawing_prep, compare_ref)::

    class RenderError(Exception):  # .code (str), .message (str)
    def resolve(path, workdir=None, bridge=None) -> dict

resolve() returns::

    {"pdf":         absolute path to a file PyMuPDF can open. Identical to `path` for .pdf and raster
                    images (passed through untouched); a freshly rendered/exported PDF otherwise,
    "source_kind":  "pdf" | "image" | "dxf" | "dwg" | "slddrw",
    "text_items":   [{"t", "x0","y0","x1","y1", "rot"}]  exact DXF text (TEXT, MTEXT, dimension text,
                    ATTRIB, MULTILEADER) in PAGE POINTS (72/in, origin top-left, y down); [] otherwise,
    "to_source":    None, or for DXF {"kind":"dxf","units":"mm","pt_per_unit":f,"origin_pt":[ox,oy],
                    "flip_y":True}:  page_x = ox + x*pt_per_unit ;  page_y = oy - y*pt_per_unit
                    (x, y in DXF drawing units; origin_pt is where DXF (0,0) lands on the page),
    "warnings":     [str],
    "temp_files":   [paths the caller must delete]}

DXF: one vector PDF page just large enough for the drawing extents plus a margin, at real size
(1 DXF unit = 1 mm of paper for mm/unitless drawings, 1 inch for inch drawings; reduced only when the
page would exceed the PDF size limit). Layout: Model, or the single paper-space layout with content;
if both have content, Model is rendered and a warning says so. DWG/SLDDRW: exported to PDF through
SolidWorks via `bridge._call_raw("export_document", ...)`; this module never starts SolidWorks.

Error codes: UNSUPPORTED_TYPE, FILE_NOT_FOUND, DXF_RENDER_FAILED, DWG_RENDER_FAILED, SLDDRW_RENDER_FAILED.
"""
from __future__ import annotations

import math
import os
import tempfile

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif")
_MM_PT = 72.0 / 25.4
_MARGIN_MM = 5.0
_MAX_PAGE_PT = 14000.0          # PDF hard limit is 14400 pt
_MIN_STROKE_MM = 0.18           # keep thin lines visible at tile-render dpi

# Entity types the vector backend draws; anything else is reported as skipped.
_DRAWN = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE", "TEXT", "MTEXT",
          "DIMENSION", "INSERT", "HATCH", "SOLID", "TRACE", "POINT", "LEADER", "MLEADER",
          "MULTILEADER", "3DFACE", "MESH", "XLINE", "RAY", "ATTRIB", "ATTDEF", "VIEWPORT",
          "IMAGE", "WIPEOUT", "TOLERANCE", "3DSOLID", "BODY", "REGION", "SURFACE", "MLINE",
          "ACAD_TABLE", "TABLE"}


class RenderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def resolve(path: str, workdir: str | None = None, bridge=None) -> dict:
    src = os.path.abspath(path)
    if not os.path.exists(src):
        raise RenderError("FILE_NOT_FOUND", src)
    ext = os.path.splitext(src)[1].lower()
    if ext == ".pdf":
        return _result(src, "pdf")
    if ext in _IMAGE_EXT:
        return _result(src, "image")
    if ext == ".dxf":
        return _render_dxf(src, workdir)
    if ext in (".dwg", ".slddrw"):
        return _export_via_solidworks(src, ext[1:], workdir, bridge)
    raise RenderError("UNSUPPORTED_TYPE",
                      f"{ext or '(no extension)'}: expected .pdf, an image, .dxf, .dwg or .slddrw")


def _result(pdf, kind, text_items=None, to_source=None, warnings=None, temp_files=None) -> dict:
    return {"pdf": pdf, "source_kind": kind, "text_items": text_items or [], "to_source": to_source,
            "warnings": warnings or [], "temp_files": temp_files or []}


# --------------------------------------------------------------------------- DWG / SLDDRW
def _export_via_solidworks(src, kind, workdir, bridge) -> dict:
    code = "DWG_RENDER_FAILED" if kind == "dwg" else "SLDDRW_RENDER_FAILED"
    if bridge is None:
        raise RenderError(code, f".{kind} needs SolidWorks to render and no bridge is available; "
                                "export it to PDF or DXF from SolidWorks first")
    wd = workdir or tempfile.mkdtemp(prefix="sp_render_")
    os.makedirs(wd, exist_ok=True)
    out = os.path.join(wd, os.path.splitext(os.path.basename(src))[0] + f".{kind}.render.pdf")
    opened = False
    try:
        r = bridge._call_raw("open_document", {"file_path": src})
        if r.get("status") != "COMPLETED":
            raise RenderError(code, "open_document failed: " + _err(r))
        opened = True
        r = bridge._call_raw("export_document", {"format": "PDF", "file_path": out})
        if r.get("status") != "COMPLETED":
            raise RenderError(code, "export_document PDF failed: " + _err(r))
    except RenderError:
        raise
    except Exception as exc:                      # noqa: BLE001
        raise RenderError(code, f"{type(exc).__name__}: {exc}") from exc
    finally:
        if opened:
            try:
                bridge._call_raw("close_document", {})   # scratch import: discard, never save
            except Exception:                            # noqa: BLE001
                pass
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RenderError(code, f"export_document reported success but wrote no PDF at {out}")
    return _result(out, kind, temp_files=[out])


def _err(resp) -> str:
    e = (resp or {}).get("error") or {}
    return f"{e.get('code')}: {e.get('message')}" if e else str(resp)


# --------------------------------------------------------------------------- DXF
def _render_dxf(src, workdir) -> dict:
    try:
        import ezdxf
        from ezdxf import bbox as ezbbox
        from ezdxf.addons.drawing import Frontend, RenderContext, layout as ezlayout
        from ezdxf.addons.drawing import pymupdf as ezpdf
        from ezdxf.addons.drawing.config import Configuration
        import fitz  # noqa: F401  (PyMuPDF must be present for the backend)
    except Exception as exc:                      # noqa: BLE001
        raise RenderError("DXF_RENDER_FAILED",
                          f"DXF rendering needs ezdxf and PyMuPDF ({type(exc).__name__}: {exc}); "
                          "pip install -r adapters/claude/requirements-pdf.txt and pip install ezdxf") from exc
    try:
        doc = ezdxf.readfile(src)
    except ezdxf.DXFError as exc:
        raise RenderError("DXF_RENDER_FAILED", f"not a readable DXF (corrupt or not DXF): {exc}") from exc
    except Exception as exc:                      # noqa: BLE001
        raise RenderError("DXF_RENDER_FAILED", f"cannot read DXF: {type(exc).__name__}: {exc}") from exc

    warnings: list[str] = []
    msp = doc.modelspace()
    papers = [lo for lo in doc.layouts if lo.name != "Model" and len(lo) > 0]
    n_model = len(msp)
    if n_model:
        lay = msp
        if papers:
            warnings.append("DXF has content in Model and paper space (%s); rendered Model only"
                            % ", ".join(p.name for p in papers))
    elif papers:
        lay = papers[0]
        if len(papers) > 1:
            warnings.append("DXF has several paper-space layouts with content (%s); rendered '%s' only"
                            % (", ".join(p.name for p in papers), lay.name))
    else:
        raise RenderError("DXF_RENDER_FAILED", "the DXF has no entities in Model or any paper-space layout")

    try:
        ext = ezbbox.extents(lay, fast=False)
    except Exception as exc:                      # noqa: BLE001
        raise RenderError("DXF_RENDER_FAILED", f"cannot compute drawing extents: {exc}") from exc
    if not ext.has_data:
        raise RenderError("DXF_RENDER_FAILED", "the DXF has no drawable geometry (empty extents)")

    unit_mm = 25.4 if doc.header.get("$INSUNITS", 0) == 1 else 1.0
    units = "in" if unit_mm == 25.4 else "mm"
    xmin, ymin = ext.extmin.x, ext.extmin.y
    xmax, ymax = ext.extmax.x, ext.extmax.y
    w_u, h_u = max(xmax - xmin, 1e-6), max(ymax - ymin, 1e-6)
    ppu = unit_mm * _MM_PT
    margin_pt = _MARGIN_MM * _MM_PT
    if max(w_u, h_u) * ppu + 2 * margin_pt > _MAX_PAGE_PT:
        ppu = (_MAX_PAGE_PT - 2 * margin_pt) / max(w_u, h_u)
        warnings.append(f"drawing is very large; page reduced to {ppu / _MM_PT / unit_mm:.3f} of real size "
                        "(coordinates still map back through to_source)")
    # page in mm, exactly extents*scale + margin; the layout backend centres a fit_page=True drawing,
    # and with the page sized to the extents the content fills the page inside the margins.
    scale_mm_per_unit = ppu / _MM_PT
    page_w_mm = w_u * scale_mm_per_unit + 2 * _MARGIN_MM
    page_h_mm = h_u * scale_mm_per_unit + 2 * _MARGIN_MM

    cfg = Configuration(min_lineweight=_MIN_STROKE_MM,
                        lineweight_scaling=1.0)
    skipped: dict[str, int] = {}
    for e in lay:
        t = e.dxftype()
        if t not in _DRAWN:
            skipped[t] = skipped.get(t, 0) + 1
    try:
        ctx = RenderContext(doc)
        backend = ezpdf.PyMuPdfBackend()
        Frontend(ctx, backend, config=cfg).draw_layout(lay, finalize=True)
        page = ezlayout.Page(page_w_mm, page_h_mm, ezlayout.Units.mm,
                             margins=ezlayout.Margins.all(_MARGIN_MM))
        settings = ezlayout.Settings(fit_page=True, scale=1.0,
                                     page_alignment=ezlayout.PageAlignment.MIDDLE_CENTER,
                                     min_stroke_width=_MIN_STROKE_MM, fixed_stroke_width=0.25)
        data = backend.get_pdf_bytes(page, settings=settings)
    except Exception as exc:                      # noqa: BLE001
        raise RenderError("DXF_RENDER_FAILED", f"{type(exc).__name__}: {exc}") from exc

    wd = workdir or tempfile.mkdtemp(prefix="sp_render_")
    os.makedirs(wd, exist_ok=True)
    out = os.path.join(wd, os.path.splitext(os.path.basename(src))[0] + ".dxf.render.pdf")
    with open(out, "wb") as fh:
        fh.write(data)

    # fit_page may add slack on one axis (aspect); calibrate the mapping from the actual page
    import fitz
    with fitz.open(out) as pdf:
        pw, ph = pdf[0].rect.width, pdf[0].rect.height
    # drawing is centred inside the margin box and uniformly scaled to fit
    avail_w, avail_h = pw - 2 * margin_pt, ph - 2 * margin_pt
    s = min(avail_w / w_u, avail_h / h_u)
    cx_pt, cy_pt = pw / 2.0, ph / 2.0
    cx_u, cy_u = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    origin = [cx_pt - cx_u * s, cy_pt + cy_u * s]
    to_source = {"kind": "dxf", "units": units, "pt_per_unit": s, "origin_pt": origin, "flip_y": True}

    items = _text_items(lay, to_source)
    if skipped:
        warnings.append("renderer skipped unsupported entities: " +
                        ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())))
    return _result(out, "dxf", items, to_source, warnings, [out])


def _to_page(ts, x, y):
    s = ts["pt_per_unit"]
    return ts["origin_pt"][0] + x * s, ts["origin_pt"][1] - y * s


def _text_items(lay, ts) -> list:
    out: list = []
    for e in lay:
        try:
            _collect(e, ts, out, 0)
        except Exception:                         # noqa: BLE001 - text is best-effort
            continue
    return out


def _collect(e, ts, out, depth):
    from ezdxf import bbox as ezbbox
    t = e.dxftype()
    if t in ("TEXT", "ATTRIB", "ATTDEF"):
        if t == "ATTDEF":
            return
        txt = e.dxf.text
        _add(out, ts, txt, ezbbox.extents([e], fast=False), float(e.dxf.get("rotation", 0.0)))
    elif t == "MTEXT":
        txt = e.plain_text()
        _add(out, ts, txt, ezbbox.extents([e], fast=False), float(e.dxf.get("rotation", 0.0)))
    elif t == "DIMENSION":
        got = False
        try:
            for v in e.virtual_entities():
                if v.dxftype() in ("TEXT", "MTEXT"):
                    _collect(v, ts, out, depth + 1)
                    got = True
        except Exception:                         # noqa: BLE001
            pass
        if not got:
            txt = e.dxf.get("text", "") or ""
            if txt in ("", "<>"):
                try:
                    txt = f"{e.get_measurement():g}"
                except Exception:                 # noqa: BLE001
                    txt = ""
            m = e.dxf.get("text_midpoint")
            if txt and m is not None:
                h = 2.5
                _add_pt(out, ts, txt, m.x - h * len(txt) * 0.3, m.y - h / 2, m.x + h * len(txt) * 0.3,
                        m.y + h / 2, 0.0)
    elif t in ("MULTILEADER", "MLEADER"):
        ctx = getattr(e, "context", None)
        mt = getattr(ctx, "mtext", None)
        txt = getattr(mt, "default_content", None) if mt is not None else None
        if txt:
            loc = getattr(mt, "insert", None) or getattr(ctx, "base_point", None)
            h = float(getattr(ctx, "char_height", 2.5) or 2.5)
            if loc is not None:
                _add_pt(out, ts, txt, loc.x, loc.y - h, loc.x + h * 0.6 * len(txt), loc.y, 0.0)
    elif t == "INSERT" and depth < 4:
        for a in e.attribs:
            _collect(a, ts, out, depth + 1)
        try:
            for v in e.virtual_entities():
                if v.dxftype() in ("TEXT", "MTEXT", "DIMENSION", "INSERT", "ATTRIB"):
                    _collect(v, ts, out, depth + 1)
        except Exception:                         # noqa: BLE001
            pass


def _add(out, ts, txt, box, rot):
    if not txt or not txt.strip() or not box.has_data:
        return
    _add_pt(out, ts, txt, box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y, rot)


def _add_pt(out, ts, txt, x0, y0, x1, y1, rot):
    px0, py0 = _to_page(ts, x0, y1)               # DXF top-left -> page top-left
    px1, py1 = _to_page(ts, x1, y0)
    out.append({"t": txt.strip(), "x0": px0, "y0": py0, "x1": px1, "y1": py1,
                "rot": float(rot) % 360.0})
