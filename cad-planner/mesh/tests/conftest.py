"""Path wiring, mirroring cad-planner/drawing's tests: the package is imported FLAT (as the offline gate
does) and pycompiler comes from compiler/solidworks. Everything needing trimesh is importorskip'd."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MESH = os.path.dirname(_HERE)
_REPO = os.path.dirname(os.path.dirname(_MESH))
for _p in (_MESH, _HERE, os.path.join(_REPO, "compiler", "solidworks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def tm():
    pytest.importorskip("trimesh")
    pytest.importorskip("scipy")
    pytest.importorskip("numpy")
    import trimesh
    return trimesh


@pytest.fixture()
def stl(tmp_path, tm):
    def _write(mesh, name="part", fmt="stl", scale=1.0):
        m = mesh.copy()
        m.apply_scale(scale)
        p = tmp_path / ("%s.%s" % (name, fmt))
        m.export(str(p))
        return str(p)
    return _write
