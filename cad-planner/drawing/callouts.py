"""callouts.py -- structured meaning for the TEXT on a drawing: hole callouts, tolerances, projection.

Pure string functions (no ezdxf, no PyMuPDF) so the DXF reader, the PDF reader and the tests share
one set of rules. They PROPOSE structure from text; they never invent: anything that does not match
returns None and the raw text stays in `notes` for Claude to read.

    parse_callout("4X Ø8 THRU")      -> {count: 4, dia: 8.0, thru: True, kind: 'hole'}
    parse_callout("M6x1 ▽12")        -> {dia: 6.0, thread: 'M6x1', pitch: 1.0, depth: 12.0, kind: 'tapped'}
    parse_callout("Ø8 ⌴Ø14 ▽6")      -> {dia: 8.0, cbore: {dia: 14.0, depth: 6.0}, kind: 'hole'}
    parse_callout("Ø8 ⌵Ø16 x 90°")   -> {dia: 8.0, csink: {dia: 16.0, angle_deg: 90.0}, kind: 'hole'}
    parse_tolerance("50 ±0.1")       -> {plus: 0.1, minus: 0.1}
    parse_tolerance("50 +0.2/-0.1")  -> {plus: 0.2, minus: 0.1}
    parse_tolerance("Ø20 H7")        -> {fit: 'H7'}
    detect_projection("THIRD ANGLE PROJECTION") -> 'third_angle'
"""
import re

_NUM = r"(\d+(?:[.,]\d+)?)"
_DIA = r"[ØøΦφ⌀∅]"
_DEPTH_SYM = r"[▽↧⊽]"
_CBORE_SYM = r"[⌴]"
_CSINK_SYM = r"[⌵]"

# AutoCAD %% codes and MTEXT formatting. Decoded, never merely stripped: the symbol is evidence.
_ESC = (("%%c", "Ø"), ("%%C", "Ø"), ("%%d", "°"), ("%%D", "°"), ("%%p", "±"), ("%%P", "±"), ("%%%", "%"))


def clean(raw):
    """MTEXT/TEXT as the drawing reads: formatting runs and braces removed, \\P -> space, %% codes decoded."""
    if not raw:
        return ""
    t = str(raw)
    for code, glyph in _ESC:
        t = t.replace(code, glyph)
    t = t.replace("\\P", " ").replace("\\~", " ").replace("\n", " ")
    t = re.sub(r"\\[A-Za-z][^;\\]*;", "", t)          # \fArial|b0;  \H2.5;  \C1;  \A1;
    t = re.sub(r"\\[Uu]\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), t)   # \U+2300
    t = t.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", t).strip()


def _f(s):
    return float(s.replace(",", "."))


def parse_callout(raw):
    """A hole/thread callout -> dict, or None when the text is not one."""
    t = clean(raw)
    if not t:
        return None
    out = {"text": t}
    has_signal = False

    m = re.match(r"^\s*(\d+)\s*[xX×]\s*(?=\S)", t)               # '4X ' / '4 x ' count prefix
    if m and not re.match(r"^\s*\d+\s*[xX×]\s*\d", t):            # but not a '4x5' size
        out["count"] = int(m.group(1))
        body = t[m.end():]
    else:
        body = t

    th = re.search(r"\bM\s*" + _NUM + r"(?:\s*[xX×]\s*" + _NUM + r")?", body)
    if th:
        out["thread"] = "M" + th.group(1).replace(",", ".") + (("x" + th.group(2).replace(",", ".")) if th.group(2) else "")
        out["dia"] = _f(th.group(1))
        if th.group(2):
            out["pitch"] = _f(th.group(2))
        has_signal = True
    cb = re.search(_CBORE_SYM + r"\s*" + _DIA + r"?\s*" + _NUM + r"(?:\s*" + _DEPTH_SYM + r"\s*" + _NUM + r")?", body) \
        or re.search(r"C['’]?BORE\s*" + _DIA + r"?\s*" + _NUM + r"(?:\s*(?:DEPTH|" + _DEPTH_SYM + r")\s*" + _NUM + r")?", body, re.I)
    cs = re.search(_CSINK_SYM + r"\s*" + _DIA + r"?\s*" + _NUM + r"(?:\s*[xX×]\s*" + _NUM + r"\s*°)?", body) \
        or re.search(r"C['’]?SINK\s*" + _DIA + r"?\s*" + _NUM + r"(?:\s*[xX×]\s*" + _NUM + r"\s*°)?", body, re.I) \
        or re.search(r"\bCSK\s*" + _DIA + r"?\s*" + _NUM + r"(?:\s*[xX×]\s*" + _NUM + r"\s*°)?", body, re.I)
    if cb:
        out["cbore"] = {"dia": _f(cb.group(1))}
        if cb.group(2):
            out["cbore"]["depth"] = _f(cb.group(2))
        has_signal = True
    if cs:
        out["csink"] = {"dia": _f(cs.group(1))}
        if cs.group(2):
            out["csink"]["angle_deg"] = _f(cs.group(2))
        has_signal = True

    # the main diameter is the FIRST Ø that is not the counterbore's / countersink's own
    main = body
    for sub in (cb, cs):
        if sub:
            main = main.replace(sub.group(0), " ")
    d = re.search(_DIA + r"\s*" + _NUM, main)
    if d and "dia" not in out:
        out["dia"] = _f(d.group(1))
        has_signal = True
    elif d and "thread" in out:
        pass
    if re.search(r"\b(THRU|THROUGH|DURCHGEHEND|DURCH)\b", body, re.I):
        out["thru"] = True
        has_signal = True
    dp = re.search(r"(?:" + _DEPTH_SYM + r"|\bDEPTH\b|\bDEEP\b|\bTIEF\b)\s*" + _NUM, main, re.I)
    if dp:
        out["depth"] = _f(dp.group(1))
        has_signal = True

    if not has_signal or ("dia" not in out and "thread" not in out and "cbore" not in out and "csink" not in out):
        return None
    # a bare 'Ø8' with nothing else is a DIMENSION label, not a hole callout: require a hole word, a
    # count, a depth / THRU, a thread, or a cbore/csink
    if not any(k in out for k in ("count", "thru", "depth", "thread", "cbore", "csink")):
        return None
    out["kind"] = "tapped" if "thread" in out else "hole"
    return out


_TOL_SYM = r"[±]"
# ISO 286 deviation letters (holes upper-case, shafts lower-case) + grade 1-18, anchored behind a
# digit so a bare 'M6' thread or an 'R5' radius is never read as a fit.
_FIT_TOKEN = (r"(?:[A-HJKMNPRSTUVXYZ]|JS|CD|EF|FG|ZA|ZB|ZC|[a-hjkmnprstuvxyz]|js|cd|ef|fg|za|zb|zc)"
              r"\d{1,2}")
_FIT_RE = re.compile(r"\d\s*(" + _FIT_TOKEN + r")(?:\s*/\s*(" + _FIT_TOKEN + r"))?\s*$")


def parse_tolerance(raw):
    """Tolerance text of a dimension -> {plus, minus} | {fit} | {limits:[lo, hi]}, or None."""
    t = clean(raw)
    if not t:
        return None
    m = re.search(_TOL_SYM + r"\s*" + _NUM, t)
    if m:
        v = _f(m.group(1))
        return {"plus": v, "minus": v}
    m = re.search(r"\+\s*" + _NUM + r"\s*[/ ]\s*[-−–]\s*" + _NUM, t)
    if m:
        return {"plus": _f(m.group(1)), "minus": _f(m.group(2))}
    m = re.search(r"[-−–]\s*" + _NUM + r"\s*[/ ]\s*\+\s*" + _NUM, t)
    if m:
        return {"plus": _f(m.group(2)), "minus": _f(m.group(1))}
    m = re.search(r"\+\s*" + _NUM + r"\b(?!\s*[/ ]\s*[-−–])", t)       # '+0.2' alone: one-sided
    if m and not re.search(r"[-−–]\s*\d", t):
        return {"plus": _f(m.group(1)), "minus": 0.0}
    fit = _FIT_RE.search(t)             # an ISO 286 fit AFTER a size: 'Ø20 H7', '20 h6', 'Ø20 H7/g6'
    if fit:
        return {"fit": fit.group(1) + (("/" + fit.group(2)) if fit.group(2) else "")}
    m = re.match(r"^\s*" + _NUM + r"\s*[/ ]\s*" + _NUM + r"\s*$", t)   # '50.05 49.95' limit pair
    if m:
        a, b = _f(m.group(1)), _f(m.group(2))
        return {"limits": [min(a, b), max(a, b)]}
    return None


def detect_projection(texts):
    """'first_angle' | 'third_angle' | None from any of the sheet's text (title block notes)."""
    blob = " ".join(clean(t) for t in texts).upper()
    if re.search(r"THIRD\s*ANGLE|3RD\s*ANGLE|3\.\s*WINKEL|DRITTE\s+WINKEL|THIRD-ANGLE|\bUS\s+PROJECTION", blob):
        return "third_angle"
    if re.search(r"FIRST\s*ANGLE|1ST\s*ANGLE|1\.\s*WINKEL|ERSTE\s+WINKEL|FIRST-ANGLE|\bISO\s*E\b|E[-\s]PROJ|PROJEKTIONSMETHODE\s*1", blob):
        return "first_angle"
    return None
