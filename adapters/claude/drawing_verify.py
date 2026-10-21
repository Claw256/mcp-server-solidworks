"""drawing_verify.py -- check a BUILT part against Claude's interpretation of a drawing.

Pure functions (no COM, no MCP) so they are unit-testable offline. The adapter tool
`verify_against_interpretation` gathers `measured` from analyze_model and calls `evaluate`.

WHY CODE AND NOT THE MODEL: when Claude reads a PDF/image drawing it records an interpretation
(overall size, holes, volume ...). Re-reading the built part's numbers and comparing them by eye
is exactly the step that silently passes a wrong part, so the comparison is mechanical and
reports per-item PASS / FAIL / WARN with the deltas.

`expected` (JSON, MILLIMETRES because that is what a drawing prints; every key optional):
    {"bbox_mm": [x, y, z],            # overall size. Compared as a SORTED triple unless
     "bbox_ordered": false,            #   bbox_ordered=true (then X,Y,Z model axes in order)
     "volume_mm3": 12345.0, "volume_tol_pct": 2.0,
     "holes": [{"diameter_mm": 8.0, "count": 4}],
     "cg_mm": [x, y, z], "cg_tol_mm": 1.0,
     "bolt_circles": [{"center_mm": [x, y, z], "axis": "Z", "radius_mm": 25.0,   # DESIGN INTENT: holes of
                       "diameter_mm": 6.0, "count": 6, "tol_mm": 0.1}],          #   this size on one circle
     "symmetric": ["X", "Z"],          # DESIGN INTENT: bbox is symmetric about the origin along these axes
     "tol_mm": 0.1}                    # default dimensional tolerance

`measured` (what analyze_model returned, SI METERS):
    {"bbox": {"min": [...], "max": [...], "size": [...]} | None,
     "volume": m^3 | None, "cg": [x, y, z] | None, "faces": [<analyze_model faces entries>]}
"""
import math

_PASS, _FAIL, _WARN, _SKIP = "PASS", "FAIL", "WARN", "SKIP"
_DIA_KEY_MM = 0.01          # holes whose diameters agree within 10 um are the same diameter


def _row(item, status, detail):
    return {"item": item, "status": status, "detail": detail}


def _cylinders(faces):
    """Concave (hole) cylinders as (diameter_mm, axis_line_key, turns). `hole` absent (older
    execution layer) => treated as a hole with a WARN at the caller; `u_span` absent => 1 turn."""
    out, unknown_sense = [], 0
    for f in faces or []:
        if f.get("kind") != "cylinder" or not isinstance(f.get("radius"), (int, float)):
            continue
        sense = f.get("hole")
        if sense is False:
            continue                                   # a boss / outer cylinder
        if sense is None:
            unknown_sense += 1
        o, a = f.get("origin"), f.get("axis")
        key = None
        if o and a:
            n = math.sqrt(sum(c * c for c in a)) or 1.0
            a = [c / n for c in a]
            # canonical direction, then the axis line's perpendicular foot from the origin
            if next((c for c in a if abs(c) > 1e-9), 1.0) < 0:
                a = [-c for c in a]
            t = sum(o[i] * a[i] for i in range(3))
            foot = [o[i] - t * a[i] for i in range(3)]
            key = (tuple(round(c, 3) for c in a), tuple(round(c * 1000.0, 2) for c in foot))
        u = f.get("u_span")
        turns = (u / (2 * math.pi)) if isinstance(u, (int, float)) and u > 0 else 1.0
        out.append((2.0 * f["radius"] * 1000.0, key, turns))
    return out, unknown_sense


def hole_summary(faces):
    """{diameter_mm (rounded): count} counting full turns per distinct axis line, so a hole that
    SolidWorks splits into two half-cylinder faces still counts once."""
    cyls, _unk = _cylinders(faces)
    per_axis = {}
    for dia, key, turns in cyls:
        d = round(dia / _DIA_KEY_MM) * _DIA_KEY_MM
        per_axis.setdefault((round(d, 4), key), 0.0)
        per_axis[(round(d, 4), key)] += turns
    counts = {}
    for (d, _key), turns in per_axis.items():
        counts[d] = counts.get(d, 0) + max(1, int(round(turns)))
    return counts


def derive_expected(summary):
    """The `expected` dict a direct-build summary (lowering.assess -> summary) implies, in MILLIMETRES.

    Built from the DRAWING, not from anyone's reading of it: the blank's area x thickness gives the
    volume, and every cutout that is a true circle is a hole. A cutout counts as a circle only when its
    bbox is square and its area matches pi/4 * d^2 within 0.5% (so a 10 x 10 square cutout is not a
    hole). The bent-state bbox is deliberately NOT derived: it depends on bend order and K-factor.
    Returns {} when the summary carries nothing usable."""
    out = {}
    exp = (summary or {}).get("expected") or {}
    if isinstance(exp.get("volume_m3"), (int, float)) and exp["volume_m3"] > 0:
        out["volume_mm3"] = round(exp["volume_m3"] * 1e9, 3)
    holes = {}
    for c in ((summary or {}).get("blank") or {}).get("cutouts") or []:
        x0, y0, x1, y1 = c["bbox"]
        w, h = x1 - x0, y1 - y0
        if w > 0 and abs(w - h) <= 0.005 * w and abs(c["area_mm2"] - math.pi / 4.0 * w * w) <= 0.005 * c["area_mm2"]:
            d = round(w, 3)
            holes[d] = holes.get(d, 0) + 1
    if holes:
        out["holes"] = [{"diameter_mm": d, "count": n} for d, n in sorted(holes.items())]
    return out


def merge_expected(derived, override):
    """Derived values first; any key the caller supplies wins. -> (merged, [keys overridden])."""
    merged = dict(derived or {})
    overridden = [k for k in (override or {}) if k in merged]
    merged.update(override or {})
    return merged, overridden


_AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


def _unique_holes(faces):
    """[(diameter_mm, axis_unit, foot_mm)] one per distinct hole (a hole SolidWorks splits into two
    half-cylinder faces counts once). `foot` is the perpendicular foot of the axis line from the origin."""
    cyls, _unknown = _cylinders(faces)
    groups = {}
    for dia, key, turns in cyls:
        if key is None:
            continue
        g = groups.setdefault((round(dia, 3), key), 0.0)
        groups[(round(dia, 3), key)] = g + turns
    return [(dia, key[0], key[1]) for (dia, key), turns in groups.items() if turns >= 0.5]


def _bolt_circle_row(spec, faces, tol_default):
    """Holes of one diameter whose axes lie parallel to `axis` at distance `radius` from the circle centre."""
    axis = _AXES[str(spec["axis"]).upper()]
    centre = [float(v) for v in spec["center_mm"]]
    d_want, r_want, n_want = float(spec["diameter_mm"]), float(spec["radius_mm"]), int(spec["count"])
    tol = float(spec.get("tol_mm", tol_default))
    c_dot = sum(centre[i] * axis[i] for i in range(3))
    c_perp = [centre[i] - c_dot * axis[i] for i in range(3)]          # the circle centre's axis line, as a foot
    on_circle, off = 0, []
    for dia, a, foot in _unique_holes(faces):
        if abs(dia - d_want) > max(tol, _DIA_KEY_MM):
            continue
        if sum(abs(abs(a[i]) - axis[i]) for i in range(3)) > 1e-3:        # not parallel to the circle axis
            continue
        dist = math.sqrt(sum((foot[i] - c_perp[i]) ** 2 for i in range(3)))
        if abs(dist - r_want) <= tol:
            on_circle += 1
        else:
            off.append(round(dist, 3))
    name = "bolt circle %d x O%.3g r=%.4g" % (n_want, d_want, r_want)
    detail = "expected %d holes on the circle, built %d" % (n_want, on_circle)
    if off:
        detail += " (same-size holes at other radii: %s mm)" % sorted(off)
    return _row(name, _PASS if on_circle == n_want else _FAIL, detail)


def evaluate(expected, measured):
    """-> {"verdict": PASS|FAIL, "rows": [...], "unchecked": [...]}"""
    rows, unchecked = [], []
    tol = float(expected.get("tol_mm", 0.1))

    if "bbox_mm" in expected:
        bb = (measured.get("bbox") or {}).get("size")
        if not bb:
            unchecked.append("bbox_mm (no bbox measured — analyze_model(bbox) unavailable)")
        else:
            got = [v * 1000.0 for v in bb]
            want = list(expected["bbox_mm"])
            if not expected.get("bbox_ordered"):
                got, want = sorted(got), sorted(want)
            worst = max(abs(g - w) for g, w in zip(got, want))
            rows.append(_row("bbox_mm", _PASS if worst <= tol else _FAIL,
                             "expected %s, built %s, worst delta %.3f mm (tol %.3g)%s"
                             % ([round(w, 3) for w in want], [round(g, 3) for g in got], worst, tol,
                                "" if expected.get("bbox_ordered") else " [axes sorted]")))

    if "volume_mm3" in expected:
        v = measured.get("volume")
        if v is None:
            unchecked.append("volume_mm3 (no mass properties measured)")
        else:
            got, want = v * 1e9, float(expected["volume_mm3"])
            pct = abs(got - want) / want * 100.0 if want else float("inf")
            lim = float(expected.get("volume_tol_pct", 2.0))
            rows.append(_row("volume_mm3", _PASS if pct <= lim else _FAIL,
                             "expected %.1f, built %.1f, delta %.2f%% (tol %.3g%%)" % (want, got, pct, lim)))

    if "cg_mm" in expected:
        cg = measured.get("cg")
        if not cg:
            unchecked.append("cg_mm (no centre of gravity measured)")
        else:
            d = math.sqrt(sum((cg[i] * 1000.0 - expected["cg_mm"][i]) ** 2 for i in range(3)))
            lim = float(expected.get("cg_tol_mm", 1.0))
            rows.append(_row("cg_mm", _PASS if d <= lim else _FAIL,
                             "centre of gravity is %.3f mm from expected (tol %.3g) — a mirrored or "
                             "wrong-face feature moves this while volume stays equal" % (d, lim)))

    if "holes" in expected:
        faces = measured.get("faces")
        if faces is None:
            unchecked.append("holes (no faces measured)")
        else:
            _cyl, unknown = _cylinders(faces)
            if unknown:
                rows.append(_row("holes.sense", _WARN,
                                 "%d cylinder face(s) carry no hole/boss flag (older execution layer) — "
                                 "bosses may be counted as holes" % unknown))
            built = hole_summary(faces)
            wanted = {}
            for h in expected["holes"]:
                d = round(round(float(h["diameter_mm"]) / _DIA_KEY_MM) * _DIA_KEY_MM, 4)
                wanted[d] = wanted.get(d, 0) + int(h.get("count", 1))
            for d, n in sorted(wanted.items()):
                near = [(bd, bc) for bd, bc in built.items() if abs(bd - d) <= max(tol, _DIA_KEY_MM)]
                got = sum(bc for _bd, bc in near)
                rows.append(_row("hole Ø%.3g" % d, _PASS if got == n else _FAIL,
                                 "expected %d, built %d%s" % (n, got, "" if got else
                                                              " (built hole diameters: %s)" % sorted(built))))
            matched = {bd for d in wanted for bd in built if abs(bd - d) <= max(tol, _DIA_KEY_MM)}
            for bd in sorted(set(built) - matched):
                rows.append(_row("hole Ø%.3g" % bd, _WARN,
                                 "%d built but not in the interpretation (a counterbore/countersink "
                                 "diameter, or a missed feature)" % built[bd]))

    if "bolt_circles" in expected:
        faces = measured.get("faces")
        if faces is None:
            unchecked.append("bolt_circles (no faces measured)")
        else:
            for spec in expected["bolt_circles"]:
                rows.append(_bolt_circle_row(spec, faces, tol))

    if "symmetric" in expected:
        bb = measured.get("bbox") or {}
        lo, hi = bb.get("min"), bb.get("max")
        if not lo or not hi:
            unchecked.append("symmetric (no bbox measured)")
        else:
            for ax in expected["symmetric"]:
                i = "XYZ".index(str(ax).upper())
                off = abs(lo[i] + hi[i]) * 1000.0                          # 0 when centred on the origin
                rows.append(_row("symmetric about %s=0" % str(ax).upper(), _PASS if off <= tol else _FAIL,
                                 "bbox %s spans %.3f..%.3f mm; centre is %.3f mm from the origin (tol %.3g)"
                                 % (str(ax).upper(), lo[i] * 1000.0, hi[i] * 1000.0, off / 2.0, tol / 2.0)))

    verdict = "FAIL" if any(r["status"] == _FAIL for r in rows) else "PASS"
    if not rows:
        verdict = "FAIL"
        unchecked.append("nothing was compared — give at least one of bbox_mm / volume_mm3 / holes / cg_mm / bolt_circles / symmetric")
    return {"verdict": verdict, "rows": rows, "unchecked": unchecked}


def render(result):
    lines = ["verify_against_interpretation: %s" % result["verdict"]]
    for r in result["rows"]:
        lines.append("  [%s] %s — %s" % (r["status"], r["item"], r["detail"]))
    for u in result["unchecked"]:
        lines.append("  [NOT CHECKED] %s" % u)
    lines.append("Dimensions read by vision (not the PDF text layer) deserve a second look on any FAIL; "
                 "a PASS covers only what the interpretation listed.")
    return "\n".join(lines)
