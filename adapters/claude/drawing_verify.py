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

    verdict = "FAIL" if any(r["status"] == _FAIL for r in rows) else "PASS"
    if not rows:
        verdict = "FAIL"
        unchecked.append("nothing was compared — give at least one of bbox_mm / volume_mm3 / holes / cg_mm")
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
