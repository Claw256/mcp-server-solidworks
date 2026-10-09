"""Offline tests for delivering export_image output to the user: the stdlib BMP->PNG converter, get_file's
BMP handling and relay-size budget, and export_image's full-size file / step-down behaviour.

The converter tests need nothing; the tool tests need the adapter's dependencies and are SKIPPED when missing.
Run:  python adapters/claude/tests/test_image_delivery.py   |   pytest adapters/claude/tests/test_image_delivery.py
"""
import os
import struct
import sys
import tempfile
import time
import zlib
from contextlib import contextmanager

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import drawing_prep as dp  # noqa: E402

_PNG_SIG = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _make_bmp(w, h, bpp, rows_rgb, top_down=False):
    """rows_rgb[y] = bytes r,g,b,r,g,b... (y = 0 is the TOP row). Bottom-up unless top_down."""
    step, stride = bpp // 8, ((w * bpp + 31) // 32) * 4
    body = bytearray()
    for y in (range(h) if top_down else range(h - 1, -1, -1)):
        px = rows_rgb[y]
        row = bytearray(w * step)
        row[0::step] = px[2::3]            # B
        row[1::step] = px[1::3]            # G
        row[2::step] = px[0::3]            # R
        if step == 4:
            row[3::4] = b"\xff" * w
        body += row + bytes(stride - len(row))
    hdr = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, -h if top_down else h, 1, bpp, 0, len(body), 2835, 2835, 0, 0)
    return hdr + info + bytes(body)


def _decode_png(png):
    """Full RGB8 decoder incl. all five filters -> (w, h, rows, filter_types_used)."""
    assert png[:8] == _PNG_SIG
    pos, idat, w, h = 8, b"", None, None
    while pos < len(png):
        n, tag = struct.unpack_from(">I4s", png, pos)
        body = png[pos + 8:pos + 8 + n]
        assert struct.unpack_from(">I", png, pos + 8 + n)[0] == (zlib.crc32(tag + body) & 0xFFFFFFFF)
        if tag == b"IHDR":
            w, h = struct.unpack_from(">II", body)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw, s = zlib.decompress(idat), 3 * w
    rows, prev, used = [], bytearray(s), set()
    for y in range(h):
        t = raw[y * (s + 1)]
        line = bytearray(raw[y * (s + 1) + 1:(y + 1) * (s + 1)])
        used.add(t)
        for i in range(s):
            a = line[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            if t == 0:
                p = 0
            elif t == 1:
                p = a
            elif t == 2:
                p = b
            elif t == 3:
                p = (a + b) // 2
            else:
                q = a + b - c
                pa, pb, pc = abs(q - a), abs(q - b), abs(q - c)
                p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            line[i] = (line[i] + p) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, rows, used


def _old_bmp_to_png(data):
    """The converter this replaced: filter 0 on every row, zlib level 6."""
    off = struct.unpack_from("<I", data, 10)[0]
    _hdr, w, h, _pl, bpp, _comp = struct.unpack_from("<IiiHHI", data, 14)
    top_down, h = h < 0, abs(h)
    stride, step = ((w * bpp + 31) // 32) * 4, bpp // 8
    rows = []
    for y in range(h):
        src = off + (y if top_down else h - 1 - y) * stride
        row = bytearray(1 + w * 3)
        px = data[src:src + w * step]
        row[1::3], row[2::3], row[3::3] = px[2::step], px[1::step], px[0::step]
        rows.append(bytes(row))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    return (_PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


def _mixed_rows(w, h, seed=7):
    """Rows of random noise, horizontal ramps, vertical ramps and repeats so every filter wins somewhere."""
    import random
    rnd = random.Random(seed)
    rows = []
    for y in range(h):
        k = y % 4
        if k == 0:
            rows.append(bytes(rnd.randrange(256) for _ in range(3 * w)))
        elif k == 1:
            rows.append(bytes((x * 3 + c * 40 + y) & 255 for x in range(w) for c in range(3)))
        elif k == 2:
            rows.append(bytes((x * 7 + c * 11) & 255 for x in range(w) for c in range(3)))
        else:
            rows.append(bytes((y * 9 + c * 5 + (x >> 1)) & 255 for x in range(w) for c in range(3)))
    return rows


def _gradient_rows(w, h):
    return [bytes((x * 255) // w if c == 0 else (y * 255) // h if c == 1 else ((x + y) * 255) // (w + h)
                  for x in range(w) for c in range(3)) for y in range(h)]


def _fast_bmp(w, h, kind):
    """Big BMP built from a few unique rows (no per-pixel Python): 'smooth' gradient or 'noise'."""
    stride = ((w * 24 + 31) // 32) * 4
    if kind == "noise":
        body = os.urandom(stride * h)
    else:
        base = [bytes((((x + y) >> 3) & 255, ((x * 2 + y) >> 4) & 255, (x >> 2) & 255) [c] for x in range(w) for c in range(3))
                for y in range(0, 64)]
        body = b"".join(base[(y >> 5) & 63] + bytes(stride - w * 3) for y in range(h))
    hdr = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    return hdr + info + body


@contextmanager
def _patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def _load_tools():
    try:
        import agent
        import server
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.server.mcpserver.utilities.types import Image
        for name in ("get_file", "stage_file"):     # importing agent registers these on the shared server.mcp;
            try:                                    # drop them so test_schema_contract (adapter-only surface) stays valid
                server.mcp.remove_tool(name)
            except Exception:  # noqa: BLE001
                pass
        return agent, server, ToolError, Image
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------
# bmp_to_png
# ---------------------------------------------------------------------------------------------
def test_bmp_to_png_round_trips_exactly_all_layouts():
    for w, h in ((1, 1), (5, 4), (8, 6), (17, 13), (40, 24)):
        rows = _mixed_rows(w, h)
        for bpp in (24, 32):
            for top_down in (False, True):
                gw, gh, got, _used = _decode_png(dp.bmp_to_png(_make_bmp(w, h, bpp, rows, top_down)))
                assert (gw, gh) == (w, h) and got == rows, (w, h, bpp, top_down)


def test_bmp_to_png_uses_every_filter_and_decodes():
    _w, _h, _rows, used = _decode_png(dp.bmp_to_png(_make_bmp(48, 32, 24, _mixed_rows(48, 32))))
    assert {1, 2, 4} <= used, used            # Sub, Up and Paeth all win on some row


def test_bmp_to_png_rejects_bad_input():
    for bad in (b"not a bmp", _make_bmp(8, 8, 24, _mixed_rows(8, 8))[:100]):
        try:
            dp.bmp_to_png(bad)
        except ValueError:
            continue
        raise AssertionError("must be rejected")


def test_bmp_to_png_smaller_than_old_on_gradient():
    bmp = _make_bmp(160, 120, 24, _gradient_rows(160, 120))
    new, old = dp.bmp_to_png(bmp), _old_bmp_to_png(bmp)
    assert _decode_png(new)[2] == _decode_png(old)[2]
    assert len(new) < len(old), (len(new), len(old))


def test_bmp_to_png_fast_enough_for_4096x3072():
    bmp = _fast_bmp(4096, 3072, "smooth")
    t0 = time.time()
    png = dp.bmp_to_png(bmp)
    dt = time.time() - t0
    assert dp.bmp_size(bmp) == (4096, 3072) and struct.unpack_from(">II", png, 16) == (4096, 3072)
    assert dt < 30, f"too slow: {dt:.1f}s"


# ---------------------------------------------------------------------------------------------
# get_file
# ---------------------------------------------------------------------------------------------
def test_relay_cap_constants_agree():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    assert t[0].MAX_RESULT_BYTES == dp.RELAY_MAX_BYTES == 4 * 1024 * 1024
    assert dp.max_png_bytes(1) * 4 // 3 < dp.RELAY_MAX_BYTES


def test_get_file_converts_bmp_to_png_image():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    agent, _server, _ToolError, Image = t
    rows = _mixed_rows(12, 9)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "view.bmp")
        with open(path, "wb") as fh:
            fh.write(_make_bmp(12, 9, 24, rows))
        with _patched(agent, STAGING_DIR=d):
            res = agent.get_file(path)
    imgs = [r for r in res if isinstance(r, Image)]
    assert len(imgs) == 1 and not any(type(r).__name__ == "EmbeddedResource" for r in res), res
    assert imgs[0]._mime_type == "image/png"
    assert _decode_png(imgs[0].data)[2] == rows
    assert "12x9" in res[0].text and "PNG" in res[0].text


def test_get_file_rejects_fake_bmp_and_oversize_png_clearly():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    agent, _server, ToolError, _Image = t
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d, _patched(agent, STAGING_DIR=d):
        fake = os.path.join(d, "fake.bmp")
        with open(fake, "wb") as fh:
            fh.write(b"this is not a bitmap")
        try:
            agent.get_file(fake)
        except ToolError as ex:
            assert "INVALID_IMAGE" in str(ex)
        else:
            raise AssertionError("fake bmp must raise")
        big = os.path.join(d, "noise.bmp")         # incompressible 1500x1000 -> PNG ~4.5 MB > cap
        with open(big, "wb") as fh:
            fh.write(_fast_bmp(1500, 1000, "noise"))
        try:
            agent.get_file(big)
        except ToolError as ex:
            msg = str(ex)
            assert "RESULT_TOO_LARGE" in msg and "bytes" in msg and "likely to fit" in msg, msg
            import re
            m = re.search(r"about (\d+)x(\d+)", msg)
            assert m and int(m.group(1)) < 1500 and int(m.group(2)) < 1000, msg
        else:
            raise AssertionError("oversize must raise before the relay")
        raw = os.path.join(d, "huge.step")         # non-image blobs get the same early check
        with open(raw, "wb") as fh:
            fh.write(os.urandom(3_300_000))
        try:
            agent.get_file(raw)
        except ToolError as ex:
            assert "RESULT_TOO_LARGE" in str(ex)
        else:
            raise AssertionError("oversize blob must raise")


# ---------------------------------------------------------------------------------------------
# export_image
# ---------------------------------------------------------------------------------------------
def _fake_renderer(calls, kind="smooth"):
    def fake_call_raw(tool, params):
        calls.append((tool, dict(params)))
        with open(params["file_path"], "wb") as fh:
            fh.write(_fast_bmp(params["width"], params["height"], kind))
        return {"status": "COMPLETED"}
    return fake_call_raw


def test_export_image_file_is_full_size_png_sibling_inline_stays_tier_limited():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    _agent, server, _ToolError, Image = t
    me, mt = dp.tier_limits()
    req_w, req_h = 3200, 2400
    assert not dp._fits(req_w, req_h, me, mt)             # the point of the test: above the model tier
    calls = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        target = os.path.join(d, "shot.bmp")
        with _patched(server, _call_raw=_fake_renderer(calls)):
            res = server.export_image(view="isometric", width=req_w, height=req_h, file_path=target)
        png_path = os.path.join(d, "shot.png")
        assert os.path.exists(target) and os.path.exists(png_path)
        with open(target, "rb") as fh:
            assert dp.bmp_size(fh.read(64)) == (req_w, req_h)
        with open(png_path, "rb") as fh:
            png = fh.read()
        assert png[:8] == _PNG_SIG and struct.unpack_from(">II", png, 16) == (req_w, req_h)
    imgs = [r for r in res if isinstance(r, Image)]
    assert len(imgs) == 1
    inline_w, inline_h = struct.unpack_from(">II", imgs[0].data, 16)
    assert dp._fits(inline_w, inline_h, me, mt) and (inline_w, inline_h) != (req_w, req_h)
    sizes = [(c[1]["width"], c[1]["height"]) for c in calls]
    assert (req_w, req_h) in sizes and (inline_w, inline_h) in sizes
    assert all(c[1]["file_path"] == target or not os.path.exists(c[1]["file_path"]) for c in calls)
    note = res[0]
    assert f"{req_w}x{req_h}" in note and png_path in note and target in note, note


def test_export_image_hires_without_file_path_writes_to_staging():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    _agent, server, _ToolError, Image = t
    calls = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d, \
            _patched(os, environ={**os.environ, "AGENT_STAGING_DIR": d}), \
            _patched(server, _call_raw=_fake_renderer(calls)):
        res = server.export_image(view="front", width=2800, height=2100, hires=True)
        pngs = [f for f in os.listdir(d) if f.endswith(".png")]
        bmps = [f for f in os.listdir(d) if f.endswith(".bmp")]
    assert len(pngs) == 1 and len(bmps) == 1 and sum(isinstance(r, Image) for r in res) == 1, (res, pngs)
    assert "2800x2100" in res[0]


def test_export_image_default_call_unchanged_no_files_left():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    _agent, server, _ToolError, Image = t
    calls = []
    with _patched(server, _call_raw=_fake_renderer(calls)):
        res = server.export_image(view="isometric,front", width=800, height=600)
    assert len(res) == 3 and all(isinstance(r, Image) for r in res[1:])
    assert len(calls) == 2 and not any(os.path.exists(c[1]["file_path"]) for c in calls)


def test_export_image_steps_down_until_it_fits_and_reports_size():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    _agent, server, _ToolError, Image = t
    calls = []
    # budget ~200 kB per image; a 400x300 noise render is ~360 kB, so it needs a few 10% steps
    with _patched(dp, RELAY_MAX_BYTES=64 * 1024 + 266_668), \
            _patched(server, _call_raw=_fake_renderer(calls, "noise")):
        res = server.export_image(view="isometric", width=400, height=300)
    img = [r for r in res if isinstance(r, Image)][0]
    w, h = struct.unpack_from(">II", img.data, 16)
    assert w < 400 and h < 300
    assert len(img.data) <= 266_668 // 4 * 3
    assert f"{w}x{h}" in res[0] and "reduced from 400x300" in res[0], res[0]
    assert len(calls) >= 3


def test_export_image_impossible_budget_fails_clearly():
    t = _load_tools()
    if not t:
        print("     (skipped: adapter deps not installed)")
        return
    _agent, server, _ToolError, Image = t
    calls = []
    with _patched(dp, RELAY_MAX_BYTES=64 * 1024 + 400), _patched(server, _call_raw=_fake_renderer(calls, "noise")):
        res = server.export_image(view="isometric", width=400, height=300)
    assert len(res) == 1 and "RESULT_TOO_LARGE" in res[0], res
    assert len(calls) <= 12


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print("ok   -", t.__name__)
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print("FAIL -", t.__name__, "::", type(ex).__name__, ex)
    print("\n%d/%d passed" % (len(_TESTS) - failed, len(_TESTS)))
    sys.exit(1 if failed else 0)
