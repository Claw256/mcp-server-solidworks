"""Synthetic meshes generated in-test (trimesh primitives + a tiny Delaunay prism builder). METERS."""
import math

import numpy as np
import trimesh


def _inside(poly, pts):
    x, y = pts[:, 0], pts[:, 1]
    x0, y0 = poly[:, 0], poly[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    res = np.zeros(len(pts), dtype=bool)
    for a, b, c, d in zip(x0, y0, x1, y1):
        cond = (b > y) != (d > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xx = a + (y - b) * (c - a) / (d - b)
        res ^= cond & (xx > x)
    return res


def _dense(poly, h):
    out = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        k = max(int(np.ceil(np.linalg.norm(b - a) / h)), 1)
        for t in range(k):
            out.append(a + (b - a) * t / k)
    return np.array(out)


def prism(outer, holes=(), height=0.02, h=0.004):
    """Extrude a polygon (with circular/polygonal holes) along +Z, z in [0, height]."""
    from scipy.spatial import Delaunay
    outer = np.asarray(outer, float)
    loops = [outer] + [np.asarray(x, float) for x in holes]
    pts = np.vstack([_dense(l, h) for l in loops])
    gx = np.arange(outer[:, 0].min() + h / 2, outer[:, 0].max(), h)
    gy = np.arange(outer[:, 1].min() + h / 2, outer[:, 1].max(), h)
    grid = np.array([(x, y) for x in gx for y in gy])
    keep = _inside(outer, grid)
    for hl in holes:
        keep &= ~_inside(np.asarray(hl, float), grid)
    # keep grid points clear of the boundary to avoid slivers
    from scipy.spatial import cKDTree
    d, _ = cKDTree(pts).query(grid)
    grid = grid[keep & (d > h * 0.45)]
    allp = np.vstack([pts, grid])
    tri = Delaunay(allp)
    cen = allp[tri.simplices].mean(axis=1)
    ok = _inside(outer, cen)
    for hl in holes:
        ok &= ~_inside(np.asarray(hl, float), cen)
    return trimesh.creation.extrude_triangulation(allp, tri.simplices[ok], height)


def circle_pts(cx, cy, r, n=48):
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.c_[cx + r * np.cos(t), cy + r * np.sin(t)]


def box_with_hole(w=0.08, d=0.05, t=0.03, hole_d=0.02):
    outer = np.array([[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]])
    m = prism(outer, [circle_pts(0.012, 0.0, hole_d / 2, 64)], t, h=0.004)
    return m


def l_bracket(a=0.06, b=0.04, th=0.01, length=0.05):
    pts = np.array([[0, 0], [a, 0], [a, th], [th, th], [th, b], [0, b]], float)
    return prism(pts, (), length, h=0.0025)


def cylinder(r=0.02, h=0.06):
    return trimesh.creation.cylinder(radius=r, height=h, sections=96)


def vase(sections=96):
    # (radius, height) profile of a closed solid of revolution about Y
    prof = np.array([[0.0, 0.0], [0.03, 0.0], [0.034, 0.02], [0.026, 0.06], [0.015, 0.09], [0.02, 0.12],
                     [0.0, 0.12]])
    # smooth it a bit by sampling a spline-ish curve
    t = np.linspace(0, 1, 60)
    r = 0.02 + 0.014 * np.sin(2.2 * math.pi * t) * (1 - 0.3 * t) + 0.004 * t
    y = 0.12 * t
    line = np.c_[np.r_[0.0, r, 0.0], np.r_[0.0, y, 0.12]]
    m = trimesh.creation.revolve(line, angle=2 * math.pi, sections=sections)
    # trimesh revolves about Z; rotate to Y
    m.apply_transform(trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0]))
    return m, line


def vase_radius(line, y):
    return float(np.interp(y, line[:, 1], line[:, 0]))


def torus(R=0.03, r=0.008):
    return trimesh.creation.torus(R, r, major_sections=72, minor_sections=36)


def ellipsoid(a=0.05, b=0.03, c=0.02, subdiv=4):
    m = trimesh.creation.icosphere(subdivisions=subdiv, radius=1.0)
    m.apply_scale([a, b, c])
    return m


def sphere(r=0.03, subdiv=4):
    return trimesh.creation.icosphere(subdivisions=subdiv, radius=r)


def rotated(mesh, deg=(23, 31, 47)):
    T = trimesh.transformations.euler_matrix(*[math.radians(d) for d in deg])
    m = mesh.copy()
    m.apply_transform(T)
    return m
