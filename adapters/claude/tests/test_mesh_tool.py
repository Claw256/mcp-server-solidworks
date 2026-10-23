"""analyze_mesh: the adapter tool over cad-planner/mesh. Offline: synthetic STLs written by trimesh, the real
pycompiler over the feature-tree-simulating fake port. Skipped without trimesh/scipy (requirements-mesh.txt)."""
import json
import os
import shutil
import sys
import tempfile

import pytest

pytest.importorskip("trimesh")
pytest.importorskip("scipy")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))), "cad-planner", "mesh", "tests"))

import fake_tree_port                                   # noqa: E402
from solidpilot import bridge, design, expectations     # noqa: E402
from solidpilot.tools import mesh as tool               # noqa: E402
import synth                                            # noqa: E402


class MeshPort(fake_tree_port.FakeTreePort):
    """The fake tree port plus the tools a mesh graph needs beyond boxes: offset reference planes and
    revolves / cuts / lofts, each recording a named feature like SolidWorks does."""
    _PREFIX = {"add_reference_geometry": "Plane", "revolve_feature": "Revolve", "loft_feature": "Loft",
               "cut_feature": "Cut-Extrude", "hole_feature": "Hole"}

    def execute(self, tool, params, state_version):
        resp = super().execute(tool, params, state_version)
        prefix = self._PREFIX.get(tool)
        if prefix and resp.get("status") == "COMPLETED":
            name = self._name(prefix)
            self.tree.append(name)
            resp["cadState"]["features"] = [name]
        return resp


class Env(fake_tree_port.Env):
    def __enter__(self):
        super().__enter__()
        self.port = MeshPort()          # design.make_port reads env.port lazily
        return self


@pytest.fixture()
def work():
    d = tempfile.mkdtemp(prefix="mesh_tool_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _stl(work, name, mesh_m):
    """Write a meter mesh as an STL in millimetres (the tool's default units)."""
    m = mesh_m.copy()
    m.apply_scale(1000.0)
    path = os.path.join(work, name)
    m.export(path)
    return path


def _box_hole(work):
    return _stl(work, "bracket.stl", synth.box_with_hole())


def _vase(work):
    return _stl(work, "vase.stl", synth.vase()[0])


def _sphere(work):
    return _stl(work, "ball.stl", synth.sphere())


def test_analyze_is_compact_text_and_changes_nothing(work):
    out = tool.analyze_mesh(_box_hole(work))
    assert out.startswith("MESH ANALYSIS | bracket.stl | units=mm"), out
    assert "recommended_strategy=" in out and "segments:" in out and len(out) < 6000
    assert not os.path.exists(os.path.join(work, ".solidpilot"))


def test_plan_writes_the_graph_beside_the_mesh_and_reports_summary(work):
    src = _box_hole(work)
    out = tool.analyze_mesh(src, mode="plan")
    assert out.startswith("MESH PLAN | bracket.stl"), out
    gp = os.path.join(work, ".solidpilot", "bracket.mesh.graph.json")
    assert os.path.isfile(gp) and gp in out
    g = json.load(open(gp, encoding="utf-8"))
    assert g["nodes"] and "smoothing:" in out and "expected: volume" in out and "strategy=" in out
    assert len(out) < 6000


def test_build_box_with_hole_creates_a_design_and_registers_expectations(work):
    src = _box_hole(work)
    with Env() as env:
        out = tool.analyze_mesh(src, mode="build")
        assert out.startswith("COMPLETED | step 1 |"), out
        d = design.get()
        assert f"design_id={d.design_id}" in out and "MESH PLAN" not in out
        assert env.port.tree, "nothing was built"
        source, exp = expectations.recall()
        assert source == src and exp["volume_mm3"] > 0 and len(exp["bbox_mm"]) == 3
        assert abs(exp["volume_mm3"] - (80 * 50 * 30 - 3.14159 * 100 * 30)) < 0.03 * 110575
        rec = tool._RECORDS[d.design_id]
        assert rec["source"] == src and rec["units"] == "mm" and len(rec["transform"]) == 4
        assert os.path.isfile(os.path.join(work, ".solidpilot", "bracket.mesh.plan.json"))
        assert not [c for c in env.raw_calls if c[0] == "open_document"]


def test_build_low_poly_sphere_and_vase(work):
    with Env() as env:
        for src in (_sphere(work), _vase(work)):
            out = tool.analyze_mesh(src, mode="build", strategy="auto")
            assert out.startswith("COMPLETED | step 1 |"), out
            assert "design_id=" in out and env.port.tree


def test_build_with_reference_opens_the_mesh_then_reactivates_the_design(work):
    src = _box_hole(work)
    with Env() as env:
        out = tool.analyze_mesh(src, mode="build", reference=True)
        assert out.startswith("COMPLETED | step 1 |"), out
        opened = [c for c in env.raw_calls if c[0] == "open_document"]
        assert opened == [("open_document", {"file_path": src, "mesh_as": "graphics", "units": "mm"})]
        assert env.raw_calls[-1] == ("activate_document", {"title": design.get().doc_title})
        assert "reference: the source mesh is open" in out


def test_a_failed_reference_open_only_warns(work):
    src = _box_hole(work)
    with Env() as env:
        raw = bridge._call_raw

        def failing(tool_name, params):
            if tool_name == "open_document":
                return {"status": "FAILED", "error": {"code": "OPEN_FAILED", "message": "nope"}}
            return raw(tool_name, params)
        bridge._call_raw = failing
        try:
            out = tool.analyze_mesh(src, mode="build", reference=True)
        finally:
            bridge._call_raw = raw
        assert out.startswith("COMPLETED | step 1 |"), out
        assert "reference body NOT opened" in out and "OPEN_FAILED" in out


def _stub_export(env, copy_from):
    raw = bridge._call_raw

    def patched(tool_name, params):
        if tool_name == "export_document":
            env.raw_calls.append((tool_name, dict(params)))
            shutil.copy(copy_from, params["file_path"])
            return {"status": "COMPLETED"}
        return raw(tool_name, params)
    bridge._call_raw = patched
    return raw


def test_verify_passes_when_the_export_is_the_source_mesh(work):
    src = _box_hole(work)
    with Env() as env:
        assert tool.analyze_mesh(src, mode="build").startswith("COMPLETED")
        # the plan transform maps file coordinates (mm) to the ALIGNED metre frame the part is built in: write the aligned mesh as the "export"
        import numpy as np
        import trimesh
        rec = tool._RECORDS[design.get().design_id]
        m = trimesh.load(src)
        m.apply_transform(np.asarray(rec["transform"]))
        built = os.path.join(work, "export_stub.stl")
        m.export(built)
        raw = _stub_export(env, built)
        try:
            out = tool.analyze_mesh(src, mode="verify")
        finally:
            bridge._call_raw = raw
        assert out.startswith("MESH VERIFY | bracket.stl | verdict=PASS"), out
        assert "deviation (% of bbox diagonal" in out and "volume:" in out and "bbox_m:" in out
        exports = [c for c in env.raw_calls if c[0] == "export_document"]
        assert exports and exports[0][1]["format"] == "STL" and exports[0][1]["file_path"].endswith(".stl")
        assert not os.path.exists(os.path.dirname(exports[0][1]["file_path"]))      # temp dir cleaned up


def test_verify_fails_for_a_different_part(work):
    src = _box_hole(work)
    other = _sphere(work)
    with Env() as env:
        assert tool.analyze_mesh(src, mode="build").startswith("COMPLETED")
        raw = _stub_export(env, other)
        try:
            out = tool.analyze_mesh(src, mode="verify")
        finally:
            bridge._call_raw = raw
        assert "verdict=FAIL" in out and "NEXT: fix local errors with design_step" in out, out


def test_verify_without_a_build_record_warns(work):
    src = _box_hole(work)
    with Env() as env:
        raw = _stub_export(env, src)
        try:
            out = tool.analyze_mesh(src, mode="verify")
        finally:
            bridge._call_raw = raw
        assert out.startswith("MESH VERIFY") and "no build record" in out, out


def test_verify_reports_an_export_failure(work):
    src = _box_hole(work)
    with Env() as env:
        raw = bridge._call_raw
        bridge._call_raw = lambda t, p: ({"status": "FAILED", "error": {"code": "EXPORT_ERR", "message": "x"}}
                                         if t == "export_document" else raw(t, p))
        try:
            out = tool.analyze_mesh(src, mode="verify")
        finally:
            bridge._call_raw = raw
        assert out.startswith("FAILED | EXPORT_FAILED |") and "EXPORT_ERR" in out


def test_background_build_launches_a_job(work):
    src = _box_hole(work)
    with Env():
        out = tool.analyze_mesh(src, mode="build", background=True)
        assert out.startswith("JOB_STARTED"), out
        from solidpilot import jobs
        assert jobs.status("", 20, False).startswith("COMPLETED")


# ----------------------------------------------------------------------------------------------- errors
def test_error_paths(work):
    src = _box_hole(work)
    assert tool.analyze_mesh(os.path.join(work, "nope.stl")).startswith("FAILED | FILE_NOT_FOUND |")
    assert tool.analyze_mesh(src, mode="explode").startswith("FAILED | BAD_MODE |")
    assert tool.analyze_mesh(src, units="furlong").startswith("FAILED | BAD_UNITS |")
    assert tool.analyze_mesh(src, smooth="extreme").startswith("FAILED | BAD_SMOOTH |")
    assert tool.analyze_mesh(src, strategy="magic").startswith("FAILED | BAD_STRATEGY |")
    assert tool.analyze_mesh(src, stations=-1).startswith("FAILED | BAD_ARGUMENT |")
    txt = os.path.join(work, "a.step")
    open(txt, "w").write("x")
    assert tool.analyze_mesh(txt).startswith("FAILED | UNSUPPORTED_FORMAT |")
    junk = os.path.join(work, "junk.stl")
    open(junk, "w").write("not a mesh")
    out = tool.analyze_mesh(junk)
    assert out.startswith("FAILED | MESH_READ_FAILED |"), out


def test_reader_unavailable_is_reported_with_the_install_hint(work):
    src = _box_hole(work)
    real = tool._mesh.analyze

    def boom(*a, **k):
        raise tool._mesh.MeshReaderUnavailable("trimesh missing")
    tool._mesh.analyze = boom
    try:
        out = tool.analyze_mesh(src)
    finally:
        tool._mesh.analyze = real
    assert out.startswith("FAILED | MESH_READER_UNAVAILABLE |") and "requirements-mesh.txt" in out, out


def test_package_import_failure_is_a_clean_error(work):
    src = _box_hole(work)
    real, err = tool._mesh, tool._MESH_IMPORT_ERROR
    tool._mesh, tool._MESH_IMPORT_ERROR = None, "ImportError: boom"
    try:
        out = tool.analyze_mesh(src)
        assert tool.preload() is False
    finally:
        tool._mesh, tool._MESH_IMPORT_ERROR = real, err
    assert out.startswith("FAILED | MESH_READER_UNAVAILABLE |") and "requirements-mesh.txt" in out


def test_plan_failure_maps_to_mesh_plan_failed(work):
    src = _box_hole(work)
    real = tool._mesh.plan

    def boom(*a, **k):
        raise tool._mesh.PlanError("no strategy")
    tool._mesh.plan = boom
    try:
        out = tool.analyze_mesh(src, mode="plan")
    finally:
        tool._mesh.plan = real
    assert out.startswith("FAILED | MESH_PLAN_FAILED | no strategy"), out
