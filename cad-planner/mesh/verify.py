"""verify.py -- how far is the BUILT part from the SOURCE mesh?

    compare(source_path, built_path, units='mm', transform=None) -> dict

Metrics: volume, bounding box, surface area, and a sampled two-way surface distance (source->built
and built->source: mean / p95 / max, i.e. a Hausdorff-style figure) -- all deviations also expressed
RELATIVE TO THE SOURCE BBOX DIAGONAL, which is what the verdict thresholds use. `transform` is the
4x4 from plan() (source-file coordinates -> the build frame the part was modelled in); without it the
source is just scaled to meters and both meshes are centred on their bbox centres before comparing
(`align='center'`, the default; 'none' compares in place).

The built STL's unit is guessed when `built_units='auto'` (SolidWorks exports mm unless the user
changed it): the candidate whose bbox diagonal best matches the source wins.
Distance estimates carry a sampling floor (`sample_spacing_m`); the verdict adds it to each limit.
"""
from __future__ import annotations

import math

try:
    from . import mesh_read
    from .geom import deps, SurfaceIndex, sample_surface
except ImportError:
    import mesh_read
    from geom import deps, SurfaceIndex, sample_surface

DEFAULT_THRESHOLDS = {            # fractions of the source bbox diagonal / of volume
    "mean_frac": 0.003, "p95_frac": 0.010, "max_frac": 0.030,
    "volume_frac": 0.02, "bbox_frac": 0.01,
}
MARGINAL_FACTOR = 2.5


def _guess_units(np, built_diag_native, src_diag_m):
    best, best_err = "m", None
    for u, s in mesh_read.UNIT_TO_M.items():
        err = abs(math.log(max(built_diag_native * s, 1e-12) / src_diag_m))
        if best_err is None or err < best_err:
            best, best_err = u, err
    return best


def compare(source_path, built_path, units="mm", transform=None, built_units="auto", align="center",
            samples=40000, thresholds=None):
    np, _ = deps()
    th = dict(DEFAULT_THRESHOLDS)
    th.update(thresholds or {})
    if transform is not None:
        raw = mesh_read.read_mesh(source_path, "m", align=False, repair=True)   # file units, untouched
        T = np.asarray(transform, dtype=float)
        src = raw.mesh.copy()
        src.apply_transform(T)
        src_info = raw.report
    else:
        md = mesh_read.read_mesh(source_path, units, align=False)
        src, src_info = md.mesh, md.report
    src_ext = src.bounds[1] - src.bounds[0]
    src_diag = float(np.linalg.norm(src_ext))

    probe = mesh_read.read_mesh(built_path, "m", align=False, repair=False, max_faces=10 ** 9)
    bu = built_units
    if bu == "auto":
        bu = _guess_units(np, float(np.linalg.norm(probe.mesh.bounds[1] - probe.mesh.bounds[0])), src_diag)
    built = probe.mesh.copy()
    built.apply_scale(mesh_read.UNIT_TO_M[bu])
    if built.is_watertight and built.volume < 0:
        built.invert()

    center_off = np.zeros(3)
    if align == "center":
        center_off = (src.bounds[0] + src.bounds[1]) / 2.0 - (built.bounds[0] + built.bounds[1]) / 2.0
        built.apply_translation(center_off)
    b_ext = built.bounds[1] - built.bounds[0]

    n = int(samples)
    ia = SurfaceIndex(src, n=max(n * 3, 60000), seed=21)
    ib = SurfaceIndex(built, n=max(n * 3, 60000), seed=22)
    ps, _ = sample_surface(src, n, seed=31)
    pb, _ = sample_surface(built, n, seed=32)
    d_sb = ib.distance(ps)                       # source points -> built surface
    d_bs = ia.distance(pb)                       # built points -> source surface
    floor = max(ia.spacing, ib.spacing) * 0.5
    both = np.concatenate([d_sb, d_bs])
    dist = {"mean_m": float(both.mean()), "p95_m": float(max(np.percentile(d_sb, 95), np.percentile(d_bs, 95))),
            "max_m": float(both.max()),
            "source_to_built_mean_m": float(d_sb.mean()), "built_to_source_mean_m": float(d_bs.mean())}
    rel = {k.replace("_m", "_pct_diag"): round(100.0 * v / src_diag, 4) for k, v in dist.items()}

    v_src = float(abs(src.volume)) if src.is_watertight else None
    v_bld = float(abs(built.volume)) if built.is_watertight else None
    vol_err = None if not (v_src and v_bld) else (v_bld - v_src) / v_src
    bbox_err = [float((b - s) / src_diag) for s, b in zip(src_ext, b_ext)]
    area_err = (float(built.area) - float(src.area)) / float(src.area)

    checks = [("mean", dist["mean_m"], th["mean_frac"] * src_diag + floor),
              ("p95", dist["p95_m"], th["p95_frac"] * src_diag + floor),
              ("max", dist["max_m"], th["max_frac"] * src_diag + floor * 2),
              ("bbox", max(abs(e) for e in bbox_err), th["bbox_frac"])]
    if vol_err is not None:
        checks.append(("volume", abs(vol_err), th["volume_frac"]))
    worst, reasons = "pass", []
    for name, val, lim in checks:
        if val > lim * MARGINAL_FACTOR:
            worst = "fail"
            reasons.append("%s %.3g exceeds %.3g by more than x%.1f" % (name, val, lim, MARGINAL_FACTOR))
        elif val > lim:
            if worst == "pass":
                worst = "marginal"
            reasons.append("%s %.3g exceeds limit %.3g" % (name, val, lim))
    if not (src.is_watertight and built.is_watertight):
        reasons.append("volume comparison skipped or approximate: a mesh is not watertight")
    return {
        "verdict": worst, "reasons": reasons,
        "source": {"volume_m3": v_src, "bbox_m": [round(float(v), 9) for v in src_ext],
                   "area_m2": round(float(src.area), 9), "diag_m": round(src_diag, 9)},
        "built": {"volume_m3": v_bld, "bbox_m": [round(float(v), 9) for v in b_ext],
                  "area_m2": round(float(built.area), 9), "units_assumed": bu},
        "volume_error_pct": None if vol_err is None else round(100 * vol_err, 3),
        "bbox_error_pct_diag": [round(100 * e, 3) for e in bbox_err],
        "area_error_pct": round(100 * area_err, 3),
        "distance": {**{k: round(v, 9) for k, v in dist.items()}, **rel},
        "center_offset_m": [round(float(v), 9) for v in center_off],
        "sample_spacing_m": round(float(floor * 2), 9),
        "thresholds": th,
    }
