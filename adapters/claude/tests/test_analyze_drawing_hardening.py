"""analyze_drawing route advisories with a FAKE execution layer (no SolidWorks): ZERO_VIEWS, multi-sheet
.SLDDRW, a stale `<name>2dxf.dxf`, a read-only folder, and the reader's own advisories surfacing in the
answer. Skipped (not failed) without the adapter deps / ezdxf.
Run:  python adapters/claude/tests/test_analyze_drawing_hardening.py   |   pytest ...
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


def _lines(msp, pts):
    for a, b in zip(pts, pts[1:] + pts[:1]):
        msp.add_line(a, b)


def _write(ezdxf, path, kind="plate", insunits=4):
    doc = ezdxf.new("R2010", setup=True)
    doc.units = insunits
    msp = doc.modelspace()
    if kind == "empty":
        msp.add_text("NOTHING", dxfattribs={"insert": (0, 0), "height": 3})
    else:
        _lines(msp, [(-40, -40), (160, -40), (160, 100), (-40, 100)])
        _lines(msp, [(0, 0), (100, 0), (100, 50), (0, 50)])
    doc.saveas(path)


class _Fake:
    def __init__(self, ezdxf, kind="plate", write=True, extras=None, insunits=4):
        self.calls, self.ezdxf, self.kind, self.write = [], ezdxf, kind, write
        self.extras, self.insunits = extras, insunits

    def __call__(self, tool, params):
        self.calls.append((tool, dict(params)))
        if tool == "export_document" and self.write:
            _write(self.ezdxf, params["file_path"], self.kind, self.insunits)
        if tool == "analyze_slddrw_test":
            body = {"view_count": 1, "dimension_count": 0, "views": []}
            if self.extras is not None:
                body["extras"] = self.extras
            return {"status": "COMPLETED", "cadState": {"features": [json.dumps(body)]}}
        return {"status": "COMPLETED"}


def _run(server, fake, path, **kw):
    real = server.bridge._call_raw
    server.bridge._call_raw = fake
    try:
        return server.analyze_drawing(path, save_analysis=False, **kw)
    finally:
        server.bridge._call_raw = real


def test_zero_views_after_conversion_names_the_route():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.slddrw")
        open(src, "wb").write(b"x")
        out = _run(server, _Fake(ezdxf, kind="empty"), src)
        assert "ZERO_VIEWS — the DXF SolidWorks exported from part.slddrw holds no view" in out, out[:900]
        assert "ACTIVE sheet" in out
        dxf = os.path.join(d, "e.dxf")
        _write(ezdxf, dxf, "empty")
        plain = _run(server, _Fake(ezdxf), dxf)
        assert "ZERO_VIEWS — no view could be built" in plain


def test_multi_sheet_slddrw_is_flagged_as_probably_active_sheet_only():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    extras = {"sheets": [{"name": "Sheet1", "views": []}, {"name": "Sheet2", "views": []}], "errors": []}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.slddrw")
        open(src, "wb").write(b"x")
        out = _run(server, _Fake(ezdxf, extras=extras), src)
        assert "MULTI_SHEET @ sheet.slddrw (2 sheets: Sheet1, Sheet2)" in out, out[:1200]
        one = _run(server, _Fake(ezdxf, extras={"sheets": [{"name": "Sheet1", "views": []}], "errors": []}), src)
        assert "MULTI_SHEET" not in one


def test_stale_export_is_removed_and_never_read_when_the_export_writes_nothing():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.dwg")
        open(src, "wb").write(b"x")
        stale = os.path.join(d, "part.dwg2dxf.dxf")
        _write(ezdxf, stale)                                       # a perfectly readable OLD export
        fake = _Fake(ezdxf, write=False)                          # ... and an export that reports COMPLETED, writes nothing
        out = _run(server, fake, src)
        assert out.startswith("FAILED | DWG_CONVERT_FAILED") and "wrote no DXF" in out, out[:300]
        assert not os.path.exists(stale), "the stale file must be deleted, not silently read"
        assert fake.calls[-1][0] == "close_document"


def test_read_only_folder_exports_to_a_scratch_dir_without_crashing():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    import tempfile as tf
    from solidpilot.tools import drawing_in
    real = tf.TemporaryFile

    def deny(*a, **k):
        if k.get("dir"):
            raise PermissionError("read-only folder")
        return real(*a, **k)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        src = os.path.join(d, "part.dwg")
        open(src, "wb").write(b"x")
        tf.TemporaryFile = deny
        try:
            path, note = drawing_in._export_target(src, ".dwg")
            assert os.path.dirname(path) != d and path.endswith("part.dwg2dxf.dxf") and "not writable" in note
            fake = _Fake(ezdxf)
            out = _run(server, fake, src)
        finally:
            tf.TemporaryFile = real
        assert out.startswith("NOT_DIRECT"), out[:300]
        exported = [c for c in fake.calls if c[0] == "export_document"][0][1]["file_path"]
        assert os.path.dirname(exported) != d, exported
        assert "the DXF was exported to" in out                    # said in the WARNING line
        path2, note2 = drawing_in._export_target(src, ".dwg")      # a writable folder: beside the source
        assert os.path.dirname(path2) == d and note2 is None


def test_reader_advisories_reach_the_answer():
    ezdxf, server = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        dxf = os.path.join(d, "inch.dxf")
        _write(ezdxf, dxf, insunits=0)
        out = _run(server, _Fake(ezdxf), dxf)
        assert "UNITS_UNSET @ sheet" in out and "ADVISORIES" in out, out[:900]
        both = os.path.join(d, "two.dxf")
        doc = ezdxf.new("R2010", setup=True)
        _lines(doc.modelspace(), [(0, 0), (100, 0), (100, 50), (0, 50)])
        _lines(doc.paperspace(), [(0, 0), (30, 0), (30, 20), (0, 20)])
        doc.saveas(both)
        out2 = _run(server, _Fake(ezdxf), both)
        assert "ONLY 'Model' was read" in out2 and "layout='*'" in out2


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
