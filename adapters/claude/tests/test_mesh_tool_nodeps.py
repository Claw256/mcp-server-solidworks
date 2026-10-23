"""analyze_mesh argument validation and the missing-dependency path; runs with or without trimesh."""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from solidpilot.tools import mesh as tool  # noqa: E402


def test_arguments_are_validated_before_anything_is_read():
    assert tool.analyze_mesh("x.stl", mode="explode").startswith("FAILED | BAD_MODE |")
    assert tool.analyze_mesh("x.stl", units="furlong").startswith("FAILED | BAD_UNITS |")
    assert tool.analyze_mesh("x.stl", smooth="extreme").startswith("FAILED | BAD_SMOOTH |")
    assert tool.analyze_mesh("x.stl").startswith("FAILED | FILE_NOT_FOUND |")


def test_without_the_mesh_dependencies_every_mode_says_how_to_install_them():
    if tool.preload():
        return                                   # deps present: the real paths are covered by test_mesh_tool.py
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.stl")
        open(p, "w").write("solid x\nendsolid x\n")
        for mode in ("analyze", "plan", "build", "verify"):
            out = tool.analyze_mesh(p, mode=mode)
            assert out.startswith("FAILED | MESH_READER_UNAVAILABLE |") and "requirements-mesh.txt" in out, (mode, out)
