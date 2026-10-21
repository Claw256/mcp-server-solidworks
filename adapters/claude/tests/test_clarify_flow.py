"""analyze_drawing's clarification loop, offline: questions are raised, a build waits for them,
answers/defaults release it, and the assumptions ledger is printed. The DXF reader returns frozen
planner fixtures and the graph runner is stubbed, so no ezdxf parsing and no SolidWorks.
"""
import copy
import json
import os
import sys
import tempfile

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(_ADAPTER_DIR)), "cad-planner", "drawing", "tests", "fixtures")
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)


def _deps():
    try:
        import server
        from solidpilot.tools import drawing_in
        return server, drawing_in
    except Exception:  # noqa: BLE001
        return None, None


def _run(drawing_in, fixture, **kw):
    """-> (output, graphs_built). The reader and graph runner are replaced for the call."""
    with open(os.path.join(_FIXTURES, fixture), encoding="utf-8") as fh:
        art = json.load(fh)
    built = []
    real_read, real_run = drawing_in._draw.read, drawing_in._run_graph
    drawing_in._draw.read = lambda *a, **k: copy.deepcopy(art)
    drawing_in._run_graph = lambda graph, fresh_document, **kw: (built.append(graph) or ("COMPLETED 4/4", True, 9))
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            src = os.path.join(d, "part.dxf")
            open(src, "w").write("0\nEOF\n")
            return drawing_in.analyze_drawing(src, save_analysis=False, **kw), built
    finally:
        drawing_in._draw.read, drawing_in._run_graph = real_read, real_run


def test_auto_mode_lists_the_open_questions_and_the_ledger():
    server, di = _deps()
    if server is None:
        print("     (skipped: adapter deps / ezdxf not installed)")
        return
    out, built = _run(di, "ambiguous_bend.json")
    assert "OPEN QUESTIONS (2, 1 blocking)" in out and "[bend_1] (blocking, default: skip)" in out
    assert "l:5 = bend line l5" in out and "[k_factor] (assumption, default: 0.5)" in out
    assert "ASSUMPTIONS LEDGER" in out and not built


def test_build_waits_for_a_blocking_question():
    server, di = _deps()
    if server is None:
        return
    out, built = _run(di, "ambiguous_bend.json", mode="build")
    assert out.startswith("NEEDS_INPUT | 1 blocking question(s) — nothing was built.")
    assert built == []


def test_skip_answer_releases_the_build_and_lands_in_the_ledger():
    server, di = _deps()
    if server is None:
        return
    out, built = _run(di, "ambiguous_bend.json", mode="build", answers='{"bend_1": "skip", "k_factor": "0.4"}')
    assert len(built) == 1 and out.startswith("COMPLETED 4/4")
    assert "bend DOWN at (120, 124.5) = unbuilt (gap)  [user]" in out
    assert "k_factor = 0.4  [user]" in out
    assert "OPEN QUESTIONS" not in out


def test_accept_defaults_builds_and_marks_the_gap_as_defaulted():
    server, di = _deps()
    if server is None:
        return
    out, built = _run(di, "ambiguous_bend.json", mode="build", accept_defaults=True)
    assert len(built) == 1
    assert "[default] (accepted defaults)" in out


def test_bad_answers_fail_loudly_and_build_nothing():
    server, di = _deps()
    if server is None:
        return
    out, built = _run(di, "ambiguous_bend.json", mode="build", answers='{"bend_1": "l:999"}')
    assert out.startswith("FAILED | BAD_ANSWERS |") and built == []
    out, _ = _run(di, "ambiguous_bend.json", mode="build", answers="not json")
    assert out.startswith("FAILED | BAD_ANSWERS |")


def test_missing_thickness_asks_then_builds_with_the_users_value():
    server, di = _deps()
    if server is None:
        return
    out, built = _run(di, "no_thickness.json", mode="build")
    assert out.startswith("NEEDS_INPUT") and "[thickness]" in out and built == []
    out, built = _run(di, "no_thickness.json", mode="build", answers='{"thickness": "2.0"}')
    assert len(built) == 1 and "thickness_mm = 2.0  [user]" in out
    # defaults cannot invent a thickness: the old refusal stands
    out, built = _run(di, "no_thickness.json", mode="build", accept_defaults=True)
    assert out.startswith("FAILED | NOT_DIRECTLY_BUILDABLE | thickness_unresolved") and built == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)


def test_build_then_verify_checks_the_part_against_the_drawing_itself():
    """After a build, verify_against_interpretation() with no arguments uses what the drawing implies."""
    server, di = _deps()
    if server is None:
        return
    _out, _built = _run(di, "s1_flat.json", mode="build")            # 80 x 118.5664, two 10x10 cutouts, 2 mm
    from solidpilot import expectations
    source, derived = expectations.recall()
    assert source.endswith("part.dxf") and abs(derived["volume_mm3"] - 18570.624) < 0.01
    assert "holes" not in derived                                    # the cutouts are SQUARES, not holes

    def fake(tool, params):
        kind = params.get("analysis_type")
        if kind == "mass_properties":
            return {"status": "COMPLETED", "cadState": {"features": ["volume=1,8570624E-05", "cx=0", "cy=0", "cz=0"]}}
        return {"status": "COMPLETED", "cadState": {"features": []}}

    real = server.bridge._call_raw
    server.bridge._call_raw = fake
    try:
        ok = server.verify_against_interpretation()
        bad = server.verify_against_interpretation('{"volume_mm3": 30000}')
    finally:
        server.bridge._call_raw = real
    assert "expected from the drawing part.dxf: volume_mm3" in ok and "verify_against_interpretation: PASS" in ok
    assert "overridden by you: volume_mm3" in bad and "verify_against_interpretation: FAIL" in bad


def test_verify_with_nothing_built_and_nothing_given_says_so():
    server, di = _deps()
    if server is None:
        return
    from solidpilot import expectations
    expectations.remember(None, {})
    assert server.verify_against_interpretation().startswith("FAILED | NOTHING_TO_VERIFY")


def test_verify_tool_measures_what_the_intent_checks_need():
    """symmetric needs the bbox min/max and bolt_circles the faces: the tool must fetch them."""
    server, di = _deps()
    if server is None:
        return
    import math
    faces = [{"kind": "cylinder", "origin": [0.025 * math.cos(2 * math.pi * k / 3), 0.0, 0.025 * math.sin(2 * math.pi * k / 3)],
              "axis": [0, 1, 0], "radius": 0.003, "hole": True, "u_span": 2 * math.pi} for k in range(3)]
    asked = []

    def fake(tool, params):
        asked.append(params.get("analysis_type"))
        kind = params.get("analysis_type")
        if kind == "bbox":
            return {"status": "COMPLETED", "cadState": {"features": [
                json.dumps({"min": [-0.03, 0.0, -0.03], "max": [0.03, 0.01, 0.03], "size": [0.06, 0.01, 0.06]})]}}
        if kind == "faces":
            return {"status": "COMPLETED", "cadState": {"features": [json.dumps({"faces": faces})]}}
        return {"status": "COMPLETED", "cadState": {"features": []}}

    real = server.bridge._call_raw
    server.bridge._call_raw = fake
    try:
        out = server.verify_against_interpretation(json.dumps({
            "symmetric": ["X", "Z"],
            "bolt_circles": [{"center_mm": [0, 0, 0], "axis": "Y", "radius_mm": 25, "diameter_mm": 6, "count": 3}]}))
    finally:
        server.bridge._call_raw = real
    assert "verify_against_interpretation: PASS" in out and "symmetric about X=0" in out and "bolt circle 3 x O6" in out
    assert sorted(asked) == ["bbox", "faces"]
