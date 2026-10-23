import math
import sys

import pytest

pytest.importorskip("trimesh")
import numpy as np  # noqa: E402

import geom  # noqa: E402
import mesh_read  # noqa: E402
import synth  # noqa: E402


def test_units_to_meters(stl):
    p = stl(synth.box_with_hole(), scale=1000.0)            # file in millimetres
    md = mesh_read.read_mesh(p, "mm")
    assert md.report["watertight"]
    assert md.report["bbox_m"][0] == pytest.approx(0.08, rel=1e-6)
    assert md.report["volume_m3"] == pytest.approx(0.08 * 0.05 * 0.03 - math.pi * 0.01 ** 2 * 0.03, rel=0.01)
    for u, f in (("cm", 100.0), ("m", 1.0), ("in", 1 / 0.0254)):
        q = stl(synth.cylinder(), name="c_" + u, scale=f)
        assert max(mesh_read.read_mesh(q, u).report["bbox_m"]) == pytest.approx(0.06, rel=1e-6)


def test_bad_inputs(tmp_path, stl):
    with pytest.raises(mesh_read.MeshReadError):
        mesh_read.read_mesh(str(tmp_path / "nope.stl"))
    bad = tmp_path / "x.txt"
    bad.write_text("hi")
    with pytest.raises(mesh_read.MeshReadError):
        mesh_read.read_mesh(str(bad))
    with pytest.raises(mesh_read.MeshReadError):
        mesh_read.read_mesh(stl(synth.cylinder()), units="furlong")


def test_formats_obj_ply(stl):
    for fmt in ("obj", "ply"):
        md = mesh_read.read_mesh(stl(synth.l_bracket(), fmt=fmt), "m")
        assert md.report["watertight"] and md.report["faces"] > 20


def test_alignment_recovers_axes_and_transform(stl):
    src = synth.l_bracket()
    rot = synth.rotated(src)
    md = mesh_read.read_mesh(stl(rot), "m")
    ext = sorted(md.report["bbox_m"], reverse=True)
    ref = sorted(src.extents, reverse=True)
    for a, b in zip(ext, ref):
        assert a == pytest.approx(b, rel=0.02)
    # canonical: longest extent along X, smallest along Y
    e = md.mesh.extents
    assert e[0] >= e[2] >= e[1] - 1e-9
    # transform maps source coordinates onto the aligned mesh
    raw = mesh_read.read_mesh(stl(rot), "m", align=False).mesh
    pts = np.c_[raw.vertices, np.ones(len(raw.vertices))] @ md.transform.T
    d = np.abs(np.sort(pts[:, :3], axis=0) - np.sort(md.mesh.vertices, axis=0)).max()
    assert d < 1e-9
    assert np.linalg.det(md.transform[:3, :3]) > 0
    # bbox centred
    assert np.abs((md.mesh.bounds[0] + md.mesh.bounds[1]) / 2).max() < 1e-9


def test_cad_aligned_part_is_left_alone(stl):
    m = synth.box_with_hole()
    md = mesh_read.read_mesh(stl(m), "m")
    assert md.report["alignment"] == "identity"
    assert np.allclose(md.transform[:3, :3], np.eye(3))


def test_open_mesh_repair_and_warning(tm, stl):
    m = synth.cylinder()
    holes = m.copy()
    holes.update_faces(np.arange(len(holes.faces)) != 5)     # punch a one-triangle hole
    md = mesh_read.read_mesh(stl(holes), "m")
    assert md.report["watertight_raw"] is False
    assert md.report["watertight"] is True and md.report["repaired"] is True


def test_inside_out_is_flipped(tm, stl):
    m = synth.cylinder()
    m.invert()
    md = mesh_read.read_mesh(stl(m), "m")
    assert md.mesh.volume > 0


def test_decimation_of_dense_mesh(stl):
    m = synth.sphere(0.03, 5)                                # 20480 faces
    md = mesh_read.read_mesh(stl(m), "m", max_faces=4000)
    assert md.report["decimated"] is not None
    assert md.report["faces"] < 8000
    assert md.report["volume_m3"] == pytest.approx(m.volume, rel=1e-6)    # volume of the ORIGINAL


def test_missing_dependency_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "trimesh", None)       # import trimesh -> ImportError
    with pytest.raises(geom.MeshReaderUnavailable) as ei:
        mesh_read.read_mesh("whatever.stl")
    assert "requirements-mesh.txt" in str(ei.value)
    assert geom.MeshReaderUnavailable.code == "MESH_READER_UNAVAILABLE"
    assert geom.preload() is False
