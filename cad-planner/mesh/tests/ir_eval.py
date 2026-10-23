"""Independent reference evaluator for the IR the planner emits (volume + bbox of the modelled solid).

Deliberately separate from lower.py's own estimate: it re-derives areas from the EMITTED primitives
(lines, arcs, circles, ellipses, splines as polylines), so a wrong primitive shows up as a volume error.
Handles the shapes the planner writes: extrude stacks on the front datum, one revolve, one loft.
"""
import math

import numpy as np


def _poly_area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def _pip(pt, poly):
    x, y = pt
    x0, y0 = poly[:, 0], poly[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    c = (y0 > y) != (y1 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xx = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return bool(np.count_nonzero(c & (xx > x)) % 2)


def _arc_pts(p, n=64):
    cx, cy = p["cx"], p["cy"]
    a0 = math.atan2(p["y1"] - cy, p["x1"] - cx)
    a1 = math.atan2(p["y2"] - cy, p["x2"] - cx)
    r = math.hypot(p["x1"] - cx, p["y1"] - cy)
    d = (a1 - a0) % (2 * math.pi) if p["dir"] == 1 else -((a0 - a1) % (2 * math.pi))
    t = a0 + d * np.linspace(0, 1, n)
    return np.c_[cx + r * np.cos(t), cy + r * np.sin(t)]


def profile_loops(profile):
    """Closed polygons (k,2) of a sketch profile; construction geometry ignored."""
    loops, segs = [], []
    for p in profile:
        if p.get("construction"):
            continue
        k = p["kind"]
        if k == "circle":
            t = np.linspace(0, 2 * math.pi, 361)[:-1]
            r = p["diameter"] / 2
            loops.append(np.c_[p.get("cx", 0) + r * np.cos(t), p.get("cy", 0) + r * np.sin(t)])
        elif k == "ellipse":
            cx, cy = p["cx"], p["cy"]
            ax = np.array([p["x1"] - cx, p["y1"] - cy])
            bx = np.array([p["x2"] - cx, p["y2"] - cy])
            t = np.linspace(0, 2 * math.pi, 361)[:-1]
            loops.append(np.c_[cx + ax[0] * np.cos(t) + bx[0] * np.sin(t), cy + ax[1] * np.cos(t) + bx[1] * np.sin(t)])
        elif k == "line":
            segs.append(np.array([[p["x1"], p["y1"]], [p["x2"], p["y2"]]]))
        elif k == "arc":
            segs.append(_arc_pts(p))
        elif k == "spline":
            segs.append(np.array(p["points"]).reshape(-1, 2))
        elif k == "rectangle":
            w, h = p["width"] / 2, p["height"] / 2
            cx, cy = p.get("cx", 0), p.get("cy", 0)
            loops.append(np.array([[cx - w, cy - h], [cx + w, cy - h], [cx + w, cy + h], [cx - w, cy + h]]))
    used = [False] * len(segs)
    for s in range(len(segs)):
        if used[s]:
            continue
        used[s] = True
        chain = [segs[s]]
        while True:
            end = chain[-1][-1]
            if np.linalg.norm(end - chain[0][0]) < 1e-7 and len(chain) > 1 or (len(chain) == 1 and np.linalg.norm(end - chain[0][0]) < 1e-7):
                break
            nxt = None
            for j in range(len(segs)):
                if used[j]:
                    continue
                if np.linalg.norm(segs[j][0] - end) < 1e-7:
                    nxt = segs[j]
                elif np.linalg.norm(segs[j][-1] - end) < 1e-7:
                    nxt = segs[j][::-1]
                if nxt is not None:
                    used[j] = True
                    break
            if nxt is None:
                break
            chain.append(nxt)
        pts = np.vstack([c[:-1] for c in chain])
        loops.append(pts)
    return loops


def net_area_and_loops(profile):
    loops = profile_loops(profile)
    areas = [abs(_poly_area(l)) for l in loops]
    net = 0.0
    signs = []
    for i, l in enumerate(loops):
        depth = sum(1 for j, o in enumerate(loops) if j != i and areas[j] > areas[i] and _pip(l[0], o))
        signs.append(1.0 if depth % 2 == 0 else -1.0)
        net += signs[-1] * areas[i]
    return net, loops, signs


def evaluate(graph):
    """-> {'volume_m3', 'bbox_m'} of the IR graph."""
    nodes = graph["nodes"]
    byid = {n["id"]: n for n in nodes}
    vol, lo, hi = 0.0, np.full(3, np.inf), np.full(3, -np.inf)
    for n in nodes:
        if n["type"] == "extrude":
            sk = byid[n["sketch"]]
            z0 = sk["ref"].get("offset", 0.0)
            net, loops, signs = net_area_and_loops(sk["profile"])
            vol += net * n["depth"]
            allp = np.vstack([l for l, s in zip(loops, signs) if s > 0])
            lo = np.minimum(lo, [allp[:, 0].min(), allp[:, 1].min(), z0])
            hi = np.maximum(hi, [allp[:, 0].max(), allp[:, 1].max(), z0 + n["depth"]])
        elif n["type"] == "revolve":
            sk = byid[n["sketch"]]
            net, loops, signs = net_area_and_loops(sk["profile"])
            v = 0.0
            for l, s in zip(loops, signs):
                a = _poly_area(l)
                x, y = l[:, 0], l[:, 1]
                x1, y1 = np.roll(x, -1), np.roll(y, -1)
                cr = x * y1 - x1 * y
                cx = float(((x + x1) * cr).sum() / (6 * a))
                v += s * 2 * math.pi * abs(a) * abs(cx)
            vol += v
            allp = np.vstack(loops)
            R = np.abs(allp[:, 0]).max()
            lo = np.minimum(lo, [-R, allp[:, 1].min(), -R])
            hi = np.maximum(hi, [R, allp[:, 1].max(), R])
        elif n["type"] == "loft":
            zs, ar, bb = [], [], []
            for pid in n["profiles"]:
                sk = byid[pid]
                net, loops, signs = net_area_and_loops(sk["profile"])
                zs.append(sk["ref"].get("offset", 0.0))
                ar.append(net)
                allp = np.vstack(loops)
                bb.append((allp.min(axis=0), allp.max(axis=0)))
            zs, ar = np.array(zs), np.array(ar)
            vol += float(np.sum((ar[1:] + ar[:-1]) / 2 * np.diff(zs)))
            lo = np.minimum(lo, [min(b[0][0] for b in bb), min(b[0][1] for b in bb), zs.min()])
            hi = np.maximum(hi, [max(b[1][0] for b in bb), max(b[1][1] for b in bb), zs.max()])
    return {"volume_m3": vol, "bbox_m": list(hi - lo)}
