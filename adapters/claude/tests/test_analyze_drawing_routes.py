"""analyze_drawing input routes with a FAKE execution layer (no SolidWorks): .dxf direct, .dwg and
.slddrw via open -> export DXF -> close, the scratch document always closed, layout selection.

Skipped (not failed) without the adapter deps / ezdxf.
Run:  python adapters/claude/tests/test_analyze_drawing_routes.py   |   pytest ...
"""
import json
import os
import sys
import tempfile

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)


def _deps():
    try:
        import ezdxf
        import server
        return ezdxf, server
    except Exception:  # noqa: BLE001
        return None, None


def _write_plate(ezdxf, path, with_second_layout=False):
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    for a, b in (((-40, -40), (160, -40)), ((160, -40), (160, 100)), ((160, 100), (-40, 100)),
                 ((-40, 100), (-40, -40)), ((0, 0), (100, 0)), ((100, 0), (100, 50)),
                 ((100, 50), (0, 50)), ((0, 50), (0, 0))):
        msp.add_line(a, b)
    if with_second_layout:
        lo = doc.layouts.new("Sheet2")
        lo.add_line((0, 0), (30, 0))
        lo.add_line((30, 0), (30, 20))
    doc.saveas(path)


class _Fake:
    def __init__(self, ezdxf, write_on_export=True, with_second_layout=False, fail_export=False,
                 extras=None, fail_extras=False):
        self.calls, self.ezdxf = [], ezdxf
        self.extras, self.fail_extras = extras, fail_extras
        self.write_on_export, self.second, self.fail_export = write_on_export, with_second_layout, fail_export

    def __call__(self, tool, params):
        self.calls.append((tool, dict(params)))
        if tool == "export_document":
            if self.fail_export:
                return {"status": "FAILED", "error": {"code": "EXPORT_FAILED", "message": "boom"}}
            if self.write_on_export:
                _write_plate(self.ezdxf, params["file_path"], self.second)
        if tool == "analyze_slddrw_test":
            if self.fail_extras:
                raise RuntimeError("COM went away")
            body = {"view_count": 1, "dimension_count": 0, "views": []}
            if self.extras is not None:
                body["extras"] = self.extras
            return {"status": "COMPLETED", "cadState": {"features": [json.dumps(body)]}}
        return {"status": "COMPLETED"}


def _run(server, fake, path, **kw):
    real = server._call_raw
    server._call_raw = fake
    try:
        return server.analyze_drawing(path, save_analysis=False, **kw)
    finally:
        server._call_raw = real


def test_slddrw_and_dwg_go_through_open_export_close():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        for ext in (".slddrw", ".dwg"):
            src = os.path.join(d, "part" + ext)
            open(src, "wb").write(b"x")
            fake = _Fake(ezdxf)
            out = _run(server, fake, src)
            want = ["open_document", "export_document", "close_document"]
            if ext == ".slddrw":        # the native extras read sits between export and close
                want.insert(2, "analyze_slddrw_test")
            assert [c[0] for c in fake.calls] == want, fake.calls
            exported = fake.calls[1][1]
            if ext == ".slddrw":
                assert fake.calls[2][1] == {"include_extras": True}
            assert exported["format"] == "DXF" and exported["file_path"].endswith(ext[1:] + "2dxf.dxf")
            assert out.startswith("NOT_DIRECT"), out[:120]
            assert os.path.basename(src) in out                       # converted_from provenance


_EXTRAS = {
    "sheets": [{"name": "Sheet1", "projection": "first_angle", "scale_num": 1, "scale_den": 1,
                "notes": [{"text": "DEBURR ALL EDGES"}],
                "views": [{"name": "Drawing View1", "referenced_model": "plate.sldprt",
                           "referenced_configuration": "Default", "dimensions": []}]}],
    "errors": ["view_scale[Drawing View1]: boom"],
}


def test_slddrw_extras_are_merged_into_the_artifact():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.slddrw")
        open(src, "wb").write(b"x")
        out = _run(server, _Fake(ezdxf, extras=_EXTRAS), src)
        body = json.loads(out.splitlines()[-1])                    # the artifact is the last line
        blk = body["sheet"]["slddrw"]
        assert blk["sheets"][0]["views"][0]["referenced_model"] == "plate.sldprt"
        assert body["slddrw_notes"][0]["text"] == "DEBURR ALL EDGES"
        assert body["sheet"]["projection_detected"] == "first_angle"
        assert body["sheet"]["projection_source"] == "slddrw sheet"
        assert "plate.sldprt" in out and "native sub-read" in out       # advisory line


def test_failed_extras_call_never_fails_the_read():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.slddrw")
        open(src, "wb").write(b"x")
        fake = _Fake(ezdxf, fail_extras=True)
        out = _run(server, fake, src)
        assert "WARNING: native slddrw extras failed" in out, out[:300]
        assert "COM went away" in out and "NOT_DIRECT" in out and '"slddrw"' not in out
        assert fake.calls[-1][0] == "close_document"           # scratch doc still closed


def test_dwg_never_calls_analyze_slddrw_test():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.dwg")
        open(src, "wb").write(b"x")
        fake = _Fake(ezdxf, extras=_EXTRAS)
        out = _run(server, fake, src)
        assert "analyze_slddrw_test" not in [c[0] for c in fake.calls]
        assert '"slddrw"' not in out


def test_scratch_document_is_closed_even_when_export_fails():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.slddrw")
        open(src, "wb").write(b"x")
        fake = _Fake(ezdxf, fail_export=True)
        out = _run(server, fake, src)
        assert out.startswith("FAILED | DWG_CONVERT_FAILED"), out
        assert fake.calls[-1][0] == "close_document"


def test_layouts_warning_and_selection():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.slddrw")
        open(src, "wb").write(b"x")
        merged = _run(server, _Fake(ezdxf, with_second_layout=True), src)
        assert "layouts @ sheet" in merged and "Sheet2:2" in merged, merged[-600:]
        one = _run(server, _Fake(ezdxf, with_second_layout=True), src, layout="Sheet2")
        assert "layouts @ sheet" not in one
        bad = _run(server, _Fake(ezdxf, with_second_layout=True), src, layout="Nope")
        assert bad.startswith("FAILED | DXF_READ_FAILED") and "Sheet2" in bad


def test_images_are_pointed_at_prepare_drawing():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "a.png")
        open(src, "wb").write(b"x")
        out = _run(server, _Fake(ezdxf), src)
        assert out.startswith("FAILED | UNSUPPORTED_TYPE") and "prepare_drawing" in out


def _pdf_available():
    try:
        import pymupdf  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        try:
            import fitz  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False


def test_vector_pdf_goes_through_read_pdf_and_scan_is_refused():
    ezdxf, server = _deps()
    if server is None or not _pdf_available():
        print("     (skipped: adapter deps / ezdxf / PyMuPDF not installed)")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(_ADAPTER_DIR), "..", "cad-planner", "drawing", "tests"))
    import test_pdf_read as tp
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        good = os.path.join(d, "plate.pdf")
        tp._drawing(good, scale_text="SHEET 1 OF 1")                       # no scale text on the page
        fake = _Fake(ezdxf)
        out = _run(server, fake, good)
        assert fake.calls == [], "a PDF never touches SolidWorks"
        assert out.startswith("NOT_DIRECT"), out[:200]
        assert "pdf scale ASSUMED" in out and "pdf source" in out
        scaled = _run(server, fake, good, scale="1:2")
        assert "pdf scale ASSUMED" not in scaled
        fitz = tp.fitz
        blank = os.path.join(d, "blank.pdf")
        doc = fitz.open()
        doc.new_page().insert_text(fitz.Point(50, 50), "text only", fontsize=10)
        doc.save(blank)
        doc.close()
        assert _run(server, fake, blank).startswith("FAILED | PDF_NOT_VECTOR")


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print("ok   -", t.__name__)
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print("FAIL -", t.__name__, "::", type(ex).__name__, ex)
    print("\n%d/%d passed" % (len(_TESTS) - failed, len(_TESTS)))
    sys.exit(1 if failed else 0)
