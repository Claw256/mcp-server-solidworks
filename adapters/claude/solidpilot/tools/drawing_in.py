import json
import os
import sys
from typing import Literal
import slddrw_merge
from solidpilot import bridge
from solidpilot import clarify
from solidpilot import jobs
from solidpilot import expectations
from solidpilot.app import mcp
from solidpilot.paths import CAD_PLANNER_DIR
from solidpilot.tools.graph import _run_graph
from solidpilot.payloads import _analysis_items, _kv_dict


# ---------------------------------------------------------------------------
# Tool: analyze_drawing  (2026-07-27 — the SHIPPING drawing reader: DXF/DWG)
# ---------------------------------------------------------------------------
# The reader is a permanent module of the CAD-neutral planner layer (ADR-064): a drawing is design
# intent expressed in 2D, so it belongs beside the IR schema and the recipe, not in a research dir.
_CAD_PLANNER_PKG_DIR = CAD_PLANNER_DIR

# Imported EAGERLY, at startup, in the MAIN thread -- deliberately, and NOT lazily inside the tool
# like pycompiler is. ezdxf pulls in numpy, and loading numpy's C extensions from an MCP worker
# thread DEADLOCKS on Windows: the tool hung forever with no error and no traceback (diagnosed by
# faulthandler thread dump, 2026-07-27 -- stuck in importlib create_module for numpy._core.
# multiarray). A failure here must still never kill the server, so it degrades to a clean per-call
# error instead.
try:
    if _CAD_PLANNER_PKG_DIR not in sys.path:
        sys.path.insert(0, _CAD_PLANNER_PKG_DIR)
    import drawing as _draw
    _DXF_READER_IMPORT_ERROR = None
except Exception as _exc:                     # noqa: BLE001 - startup must survive anything
    _draw = None
    _DXF_READER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc} (pip install ezdxf)"


def _summarise_direct(art, summary, graph):
    """The DIRECT_BUILDABLE result: everything needed to JUDGE the build, and nothing else.

    The contour deliberately stays out. A real laser outline is scores of segments; showing it and
    then having the model echo it back into submit_feature_graph would cost the tokens twice over
    and invite a transcription error in between — which is the whole reason the lowering exists."""
    b, th = summary["blank"], summary["thickness"]
    cuts = ", ".join("%gx%g @ (%g,%g)"
                     % (c["bbox"][2] - c["bbox"][0], c["bbox"][3] - c["bbox"][1],
                        round((c["bbox"][0] + c["bbox"][2]) / 2, 2),
                        round((c["bbox"][1] + c["bbox"][3]) / 2, 2))
                     for c in b["cutouts"]) or "none"
    lines = [
        "DIRECT_BUILDABLE | sheet-metal flat pattern | %s | sha256 %s"
        % (art["source"]["file"], art["source"]["sha256"][:12]),
        "  blank      %g x %g mm | outer loop %d segments | cutouts: %s"
        % (b["size"][0], b["size"][1], b["outer_segments"], cuts),
        "  thickness  %g mm  (%s)   bend radius %g mm   K %g"
        % (th["value_mm"], th["source"], summary["bends"][0]["radius_mm"], summary["k_factor"]),
        "  bends      %d, each corroborated by its line's edge class:" % len(summary["bends"]),
    ]
    for r in summary["bends"]:
        x1, y1, x2, y2 = r["line"]
        lines.append("               %-4s %g deg R%g  line %s  midpoint (%g, %g)%s"
                     % (r["dir"], r["angle_deg"], r["radius_mm"], r["class"],
                        round((x1 + x2) / 2, 4), round((y1 + y2) / 2, 4),
                        "  [WARNING: in a closed loop]" if r["in_loop"] else ""))
    for r in summary["skipped_bends"]:
        lines.append("  SKIPPED    %s note at %s — %s (candidates %s). It will NOT be built."
                     % (r["dir"], r["at"], r["reason"], r["candidates"]))
    lines += [
        "  expected   blank area %g mm^2 -> V = %r m^3  (bending does not change it at K=0.5)"
        % (b["area_mm2"], summary["expected"]["volume_m3"]),
        "  graph      %d IR nodes: sketch + sheet_metal, then sketch + sketched_bend per direction"
        % len(graph["nodes"]),
        "  NEXT       mode='build' builds it | mode='ir' shows the graph first.",
        "             AFTER building, verify: analyze_model('mass_properties')",
        "             against the expected volume above, and read one bend face back — a mirrored",
        "             fold is invisible to volume, area and topology alike (recipe R14).",
    ]
    if summary["skipped_bends"]:
        lines.append("             A skipped bend is invisible to ALL of those (the blank volume is "
                     "unchanged): add it by hand or declare it as a gap (R15).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ADVISORIES — the third home (payload trim phase 5, 2026-09-07)
# ---------------------------------------------------------------------------
# Three channels carry what the model needs, and each costs differently: a tool docstring is paid
# EVERY session whatever the job, a recipe section once per job that fetches it, and a payload note
# only when the thing it warns about actually happens. So guidance that applies to ONE flag belongs
# on that flag: `reverse` used to spend a paragraph on "if you see printed_mismatch, do X" in every
# read, whether or not any dimension carried it.
#
# Computed HERE, in the adapter, deliberately NOT in the reader: the saved artifact stays pure DATA
# (the research pool is not polluted, ANALYSIS_VERSION does not move, the draw-dialect contract and
# gate 4 are untouched). Only the ANSWER gains a short advisory block.
#
# A closed set, one line each, emitted ONCE per condition with the places it fired.
def _advisories(art) -> str:
    """A short plain-text block naming the conditions this read actually raised. '' when none."""
    out = []
    views = art.get("views") or []

    scaled = [v["vid"] for v in views if v.get("scale_factor") is not None]
    if scaled:
        out.append("view scale_factor @ %s — this view is drawn at a DIFFERENT ratio from the "
                   "sheet; say which factor produced a number before you quote it."
                   % ", ".join(scaled))

    pdf = (art.get("sheet") or {}).get("pdf")
    if pdf:
        if "ASSUMED" in str(pdf.get("scale_source")):
            out.append("pdf scale ASSUMED 1:1 — no scale text was found; every length below is wrong by the "
                       "true ratio unless the drawing really is 1:1. Read the title block and re-run with "
                       "scale='1:N'.")
        heavy = [p for p in (pdf.get("pens") or []) if p.get("primitives", 0) >= 50]
        if pdf.get("pen_used_mm") is None and len(heavy) > 1:
            sug = pdf.get("pen_suggested_mm")
            out.append("pdf pens x%d (sheet.pdf.pens) — every pen was read as part geometry, so dimension lines, "
                       "outlined text and the title-block grid probably formed junk views. Re-run with "
                       "pen_mm=%s%s." % (len(heavy), sug if sug else "<the part pen>",
                                         " (the arc-richest pen)" if sug else ""))
        if pdf.get("unassigned_numbers"):
            out.append("pdf unassigned_numbers x%d (sheet.pdf) — numbers the reader could not tie to a "
                       "dimension; read them from the page with vision (prepare_drawing) before building."
                       % len(pdf["unassigned_numbers"]))
        out.append("pdf source — dimensions were RECOGNISED from arrowheads and text, not read from "
                   "DIMENSION entities; cross-check the overall size with verify_against_interpretation.")

    layouts = [l for l in ((art.get("sheet") or {}).get("layouts") or []) if l.get("entities")]
    if len(layouts) > 1 and not (art.get("sheet") or {}).get("layout_selected"):
        out.append("layouts @ sheet (%s) — several layouts carry content and were MERGED into one "
                   "coordinate space; frames, views and the title block will not make sense. Re-run "
                   "analyze_drawing with layout='<name>' to read one at a time."
                   % ", ".join("%s:%d" % (l["name"], l["entities"]) for l in layouts))

    not_read = (art.get("sheet") or {}).get("not_read") or {}
    if not_read:
        out.append("not_read @ sheet (%s) — the reader saw these and did not emit them; a named "
                   "block you cannot account for is evidence that never reached you."
                   % ", ".join(sorted(not_read)))

    ang, lin = [], []
    for k, d in enumerate(art.get("dimensions") or []):
        if d.get("printed_mismatch"):
            (ang if d.get("kind") == "angular" else lin).append("dim[%d]" % k)
    if ang:
        out.append("printed_mismatch/ANGULAR @ %s — the value is a JUDGEMENT, not a reading; "
                   "corroborate it against the geometry or an ellipse ratio before building."
                   % ", ".join(ang))
    if lin:
        out.append("printed_mismatch @ %s — the scale, the arc match or the per-view factor is "
                   "wrong. Reading the flag is free; ignoring it is not." % ", ".join(lin))

    tier_b = ["%s/%s" % (v["vid"], lp.get("id"))
              for v in views for lp in (v.get("loops") or []) if lp.get("tier") == "B"]
    if tier_b:
        out.append("tier:B @ %s — this boundary was WALKED, not chained; check its area against a "
                   "stated length or width before you build on it." % ", ".join(tier_b))

    unpaired = [b for b in (art.get("bend_notes") or []) if b.get("unpaired")]
    if unpaired:
        out.append("unpaired bend x%d — the reader refused to guess, so those bends are NOT built. "
                   "Resolve them from the drawing or declare a gap; never split the difference."
                   % len(unpaired))
    in_loop = [b for b in (art.get("bend_notes") or [])
               if (b.get("bend_line") or {}).get("in_loop")]
    if in_loop:
        out.append("bend_line in_loop x%d — the match landed on OUTLINE geometry, and a bend line "
                   "belongs to no closed contour. Say so." % len(in_loop))

    vg = art.get("view_graph")
    if isinstance(vg, dict) and vg.get("solved") is False:
        out.append("view_graph.solved:false (%s) — the axes did not join; that joining is yours."
                   % vg.get("reason", "reason in the field"))

    sw_line = slddrw_merge.advisory(art)
    if sw_line:
        out.append(sw_line)

    if not out:
        return ""
    return "\nADVISORIES (%d) — only what actually fired in THIS read:\n  %s" % (
        len(out), "\n  ".join(out))


def _wire_json(obj) -> str:
    """The drawing payload, serialized as tightly as JSON allows.

    `json.dumps` defaults to ', ' and ': ' — two bytes of whitespace per comma and colon, which on
    an artifact made mostly of short numeric arrays is 18,854 B across the 10 samples (11 points of
    payload, measured). Nothing is lost: JSON whitespace is not data.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


@mcp.tool(structured_output=False)
def analyze_drawing(file_path: str, save_analysis: bool = True,
                    mode: Literal["auto", "ir", "build"] = "auto", layout: str = "",
                    page: int = 1, scale: str = "", pen_mm: float = 0.0,
                    answers: str = "", accept_defaults: bool = False, background: bool = False) -> str:
    """Read a 2D technical drawing — **.DXF, .DWG, a native .SLDDRW, or a VECTOR .PDF** — and either BUILD the part
    from it or hand you the evidence to build it yourself. This is the drawing→part front end: start
    here for ANY "build the part from this drawing" job. A .DWG or .SLDDRW is opened in SolidWorks and
    exported to DXF automatically (a .SLDDRW also needs its model reachable, or its views are blank).
    A vector PDF page is rebuilt as a DXF (lines, arcs, circles, text, dimensions recognised from arrowhead
    pairs and Ø/R numbers) and read the same way — see sheet.pdf for what was recognised, the scale it used
    (and whether that was ASSUMED) and `unassigned_numbers` it could not place. A scan/photo has nothing to
    extract: use prepare_drawing and your own vision.

    **mode='auto' (default) returns ONE OF TWO SHAPES — the reader decides, not you:**

    (A) `DIRECT_BUILDABLE` — a short plain-text summary. Every decision this drawing needs is
        forced by the drawing itself (today: sheet-metal flat patterns), so the whole part has
        already been lowered to Feature Graph IR deterministically. You get the blank size, the
        thickness and its source, every bend with its edge-class corroboration, and the EXPECTED
        VOLUME — enough to judge the build — but NOT the contour, on purpose: a real outline is
        scores of segments and echoing it back would cost the tokens twice. Read it, then call
        `mode='build'`. Anything the reader could not attribute is listed as SKIPPED and will not
        be built; completing or declaring it is then yours (recipe R15).

    (B) `NOT_DIRECT | <reason>` followed by the WHOLE analysis — the drawing needs real reading
        (the normal path for machined parts); the reason names exactly which decision the drawing
        left open. You get sheet/frame, every view complete (boxes, size, geometry, loops with
        their `seq`, open chains), alignment, view_graph, every dimension, bend note and note, in
        one answer. Read it per get_recipe('reverse') and build with submit_feature_graph.

    OPEN QUESTIONS — where the drawing leaves a decision to a person (a bend note that matches two lines,
    a missing or conflicting thickness) the answer ends with an `OPEN QUESTIONS` block: ids, evidence,
    options and a default. `mode='ir'|'build'` then returns `NEEDS_INPUT` instead of building around the
    doubt. Ask the user (`ask_user`, or in chat), then re-call with answers='{"bend_1":"l:5"}'. Pass
    accept_defaults=true only when the user said to use the defaults (an unmatched bend is then left
    unbuilt, as a declared gap). Every build prints an ASSUMPTIONS LEDGER of the values that did not come
    straight off the drawing.

    background=True (with mode='build'): start the build as a job and return a job id at once — for builds
    that may outlast a chat call; poll job_status.

    mode='ir'    — the lowered Feature Graph IR, without building. For inspection or saving.
    mode='build' — lower and BUILD, through the same deterministic pycompiler as
                   submit_feature_graph. **CHANGES GEOMETRY.** Opens a new
                   part. Re-derives from the same file, so it needs no cached graph — but verify
                   the sha256 in the summary is the file you looked at.

    ⮕ Before interpreting a single number below, call `get_recipe(section='reverse')` — the TOOL,
      not the `recipe://usage/reverse` resource, which most hosts give the model no way to read
     . It holds the reading discipline (projection standard, face binding, contour gaps,
      sheet-metal bend arithmetic) and the self-verification rules. When you then need the IR
      vocabulary, `get_recipe('feature_graph_schema', profile='part')` is the right shape for this
      job — it drops the assembly vocabulary and keeps every part and sheet-metal feature type.

    The artifact — WHAT EVERY FIELD MEANS. (The DECISIONS you make with them are the recipe's;
    read get_recipe('reverse') for those. Nothing is documented in both places.)
      sheet — dxf_version, units, **scale_factor** and the sheet box. `scale_factor` is ALREADY
        applied to every size/coordinate/dimension below, so all numbers are TRUE model mm and
        multiplying again builds a half- or double-size part. An ANGLE is never scaled (DIMLFAC is
        a length factor).
        `not_read` counts what the reader saw and did NOT emit, by entity class and — for an
        INSERT — by block name, e.g. {"HATCH": 5, "INSERT": {"WALZRICHTUNG": 1}}. Nothing vanishes
        silently: a named block you cannot account for is evidence that exists on the sheet and
        did not reach you.
      frame — {paper_box, title_block_top, primitives, circles[], note_count}: the border/title
        block cluster, which is never emitted as a view. A candidate is accepted ONLY when it
        CONTAINS every other cluster, so `paper_box: null` means this sheet has no frame and every
        cluster was a view candidate. A cluster in the title-block corner that no dimension points
        at arrives as `role: "frame_item"` (a projection symbol, a weld symbol) — furniture.
      frame_notes[] / notes[] — the sheet's text in reading order, `frame_notes` being the title
        block's. Text INSIDE inserted blocks (SW_NOTE, BIEGETEILE, a company stamp) is included,
        one entry per ROW, nested to any depth. The DWG route splits a row into per-glyph
        fragments that are re-joined left to right, so match on content, not on exact spacing.
      views[] — one per detected view: `vid`, `role`, `paper_box` (sheet coords, for reading the
        LAYOUT), `geom_box`, `size` [w,h] in true mm, `dropped_duplicates`? (exact duplicates the
        DWG route re-emitted), `scale_factor`? — PRESENT ONLY when this view is drawn at a ratio
        DIFFERENT from the sheet's (a detail or section labelled 'A-A 1 : 1' on a 1:10 sheet); its
        geometry and dimensions are already scaled with THAT factor.
        Then `geometry` {lines, arcs, circles, ellipses} in view-local
        true mm with the view's own bottom-left as origin. The EDGE CLASS is carried on every
        primitive as `c`: 'visible' (near-side edge) | 'hidden' (obscured — a real feature seen
        through material) | 'cut_line' (a section's cutting line, not part geometry) | 'center'
        (an axis).
        `lines` is the one array grouped BY that class instead of carrying it per record:
        {"visible": [[index, x1, y1, x2, y2], …], "hidden": […]}. THE FIRST NUMBER IS THE INDEX —
        the one a `seq` entry names — written in rather than implied by position, so ['l', 12, 1]
        is found by looking for the row starting with 12, in whichever group, and never by
        recomputing an offset. Indices run 0..n-1 across all the groups together.
        `ellipses` {cx, cy, rx, ry, rot (+ t1, t2, x1..y2 for an arc)} are PARAMETRIC — a circle
        on a plane tilted by θ projects with ry/rx = cos θ and rx its true radius, so a Ø10 hole
        on a 20° face arrives as rx 5 / ry 4.699, not as 48 line segments. A record carrying
        `n` + `fit` was recovered from an exploded fan of n segments (fit = max deviation, mm);
        one without is a real DXF ellipse.
        The sheet border/title block is NOT a view; a cluster INSIDE a view is that view's feature.
      views[].loops[] — the CLOSED contours, already chained: {id, class, role outer|inner, parent,
        area (mm^2, arc bulges exact), bbox, seq, tier?}. `seq` is [[code, index, dir], …]
        referencing that view's own geometry arrays — the contour IN ORDER, ready to transcribe as
        a sketch path profile. Traverse it as given; consecutive entries share an endpoint exactly.
        `tier` is PRESENT ONLY when it is "B", so the common case costs nothing. Absent = Tier A,
        strict chaining, which stops dead at any junction of three or more and therefore never
        guesses. "B" = planar face traversal, run ONLY on a view whose visible graph has no free
        end — with no dangling edge the figure is a closed planar subdivision and its faces are
        DEFINED by the angular order of the edges at each vertex, so walking it is a reading. It
        appears only where Tier A found nothing, and the direct-build path ignores it by design.
      views[].open_singles[] — [position, code, index] triples: the SINGLE-segment open chains,
        which are most of them. Nothing is abbreviated away — the id is 'O<position>', the class is
        that primitive's own `c`, the direction is 1 — and a chain whose id/class/direction are not
        all recoverable stays a full record in `open_chains` instead. Read the two together: a
        primitive named here is exactly as unattributed as one in `open_chains`.
      views[].open_chains[] — the MULTI-segment chains belonging to no contour: bend lines, centre
        lines, and (in an ortho view) silhouette fragments the chainer refused to guess through
        a T-junction.
      alignment[] — GRADED view pairs: shares='x' is a vertical projection pair, 'y' a horizontal
        one, and `grade` says how strong the evidence is. 'span' = the two share that axis's span
        edge-for-edge, the strongest. 'mid' = only the midpoints coincide — still a measurement,
        but silhouette ends may genuinely differ, and on a bend-note sheet a mid pair may relate
        the FLAT and the BENT state (two states of the part, not two projections). 'label' = a
        section caption ties the section view `b` to the view `a` carrying its cut line, with
        shares=null, so it survives a cross-scale section.
      view_graph — {solved, views:{vid: [h_axis, v_axis]}} when the pairs pin every view's two
        paper axes onto the part's three. TOPOLOGY ONLY, over ANONYMOUS axes ax0/ax1/ax2: which
        axis is depth and which view is "front" is the projection convention's SIGN, not a field.
        `solved: false` names why, and the joining is yours again.
      dimensions[] — `value` (TRUE), `kind` (linear|aligned|angular|diameter|radius|ordinate),
        `defpts` (the dimension's own reference points, sheet coords) and the owning `view`.
        `printed` is the string the CAD system actually DREW, taken from the dimension's own
        block, with `%%c`→Ø, `%%d`→°, `%%p`→± decoded (the symbol is evidence about what was
        dimensioned); `text` beside it is the raw undecoded override (e.g. '8x <>' = a count).
        The DWG→DXF route corrupts the stored measurement in two whole classes — an ANGULAR value
        comes back as 180+θ, a RADIUS loses its arc side — so the reader ARBITRATES: for radius and
        diameter the printed value wins outright, and for an angle it takes the candidate ≤ 180°.
        `printed_mismatch: true` marks what it could not settle.
        `kind_from: "printed"` marks a dimension whose TYPE the drawn symbol overruled — a plain
        linear dim drawn across a circle with a hand-typed Ø is re-emitted as `kind: "diameter"`.
        `measures` links a dimension to the primitive the reader matched it to.
      bend_notes[] — sheet metal: {dir UP|DOWN, angle_deg, radius, view} plus either `bend_line`
        (the line it annotates, that line's edge class, and `in_loop` — true would mean the match
        landed on outline geometry, which a bend line never is) or `unpaired` with the reason and
        the candidates. Direction comes from the NOTE; the class corroborates it.
      notes[] — other free text on the sheet (e.g. a bare '2 mm' thickness note, 'SECTION C-C'),
        text inside note blocks included.

    file_path: absolute path to the .DXF, .DWG, .SLDDRW or .PDF.
    page / scale / pen_mm: PDF only. page is 1-based; scale like '1:2' (paper:true) overrides the title-block text.
        pen_mm: keep ONLY that stroke width as part geometry. CAD PDFs draw the part with one pen and the dimensions,
        outlined text, centre lines and title-block grid with others; with every pen mixed in, dimension glyphs
        cluster into dozens of junk "views". sheet.pdf.pens lists each pen (primitives, arcs/circles) and
        pen_suggested_mm names the arc-richest one (usually the part) — re-run with pen_mm=<that>.
    layout: DXF layout name to read ('' = every layout merged, the historic behaviour). A sheet file
        with several sheets/layouts overlays them all when this is blank — sheet.layouts lists the names
        and entity counts, and the answer warns when more than one has content; pass one to read it alone.
    save_analysis (default True): also write `<name>.analysis-v<version>.json` beside the source —
        a research artifact for cross-part study, versioned by the analyzer. Same version
        overwrites. You never need to read it back: this answer already carries everything in it."""
    if background and mode == "build":
        return jobs.launch("analyze_drawing", lambda: analyze_drawing(
            file_path, save_analysis, mode, layout, page, scale, pen_mm, answers, accept_defaults, False))
    src = os.path.abspath(file_path)
    if not os.path.exists(src):
        return f"FAILED | FILE_NOT_FOUND | {src}"
    ext = os.path.splitext(src)[1].lower()
    if ext not in (".dxf", ".dwg", ".slddrw", ".pdf"):
        return ("FAILED | UNSUPPORTED_TYPE | analyze_drawing reads .DXF, .DWG, .SLDDRW or a vector .PDF. For "
                "an image or a scan use prepare_drawing and read it yourself.")
    # DWG/SLDDRW are not DXF; the reader parses DXF only. SolidWorks opens the file as a drawing and
    # exports DXF — measured lossless for geometry, dimensions, DIMLFAC, linetypes and notes (DWG).
    dxf_path = src if ext in (".dxf", ".pdf") else os.path.splitext(src)[0] + "." + ext[1:] + "2dxf.dxf"

    _save_warnings: list = []
    _extras: dict = {}

    def _load():
        """Convert (DWG) + read + save -> (art, cfg, None) | (None, None, error string)."""
        converted_from = None
        if ext in (".dwg", ".slddrw"):
            opened = bridge._call_raw("open_document", {"file_path": src})
            if opened.get("status") != "COMPLETED":
                err = opened.get("error") or {}
                return None, None, f"FAILED | DWG_OPEN_FAILED | {err.get('code')}: {err.get('message')}"
            try:
                exported = bridge._call_raw("export_document", {"format": "DXF", "file_path": dxf_path})
            except Exception as exc:             # noqa: BLE001 - still must close the scratch import
                return None, None, f"FAILED | DWG_CONVERT_FAILED | {type(exc).__name__}: {exc}"
            else:
                # .SLDDRW only: the drawing is still open, so read the SolidWorks-native facts the DXF
                # lost. Additive and best-effort — any failure is a warning, never a failed read.
                if ext == ".slddrw":
                    try:
                        got = slddrw_merge.parse_extras(
                            bridge._call_raw("analyze_slddrw_test", {"include_extras": True}))
                        if got is None:
                            _save_warnings.append("native slddrw extras unavailable (no extras in the "
                                                  "analyze_slddrw_test response); DXF-only read")
                        else:
                            _extras.update(got)
                    except Exception as exc:     # noqa: BLE001
                        _save_warnings.append(f"native slddrw extras failed ({type(exc).__name__}: "
                                              f"{exc}); DXF-only read")
            finally:
                bridge._call_raw("close_document", {})  # the import is scratch — discard, never save
            if exported.get("status") != "COMPLETED":
                err = exported.get("error") or {}
                return None, None, f"FAILED | DWG_CONVERT_FAILED | {err.get('code')}: {err.get('message')}"
            converted_from = os.path.basename(src)
        if _draw is None:
            return None, None, f"FAILED | READER_UNAVAILABLE | {_DXF_READER_IMPORT_ERROR}"
        try:
            cfg = _draw.load_config()
            if ext == ".pdf":
                art = _draw.read_pdf(src, cfg, page=page, scale=scale, pen_mm=pen_mm)
            else:
                art = _draw.read(dxf_path, cfg, layout=layout) if layout else _draw.read(dxf_path, cfg)
        except ValueError as exc:
            code = "PDF_NOT_VECTOR" if ext == ".pdf" else "DXF_READ_FAILED"
            return None, None, f"FAILED | {code} | {exc}"
        except Exception as exc:
            code = "PDF_READ_FAILED" if ext == ".pdf" else "DXF_READ_FAILED"
            return None, None, f"FAILED | {code} | {type(exc).__name__}: {exc}"
        if converted_from:
            art["source"]["converted_from"] = converted_from
        if _extras:
            try:
                slddrw_merge.merge_extras(art, _extras)
            except Exception as exc:             # noqa: BLE001 - merge is additive, never fatal
                _save_warnings.append(f"native slddrw extras not merged: {type(exc).__name__}: {exc}")
        if save_analysis:
            out = os.path.join(os.path.dirname(dxf_path),
                               "%s.analysis-v%s.json" % (os.path.splitext(os.path.basename(src))[0],
                                                         art["analysis_version"]))
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    json.dump(_draw.wire_encode(art), fh, indent=1, ensure_ascii=False)
            except OSError as exc:                # research-only, never the result — but say so
                _save_warnings.append(f"analysis artifact not saved: {exc}")
        return art, cfg, None

    art, cfg, err = _load()
    if err:
        return err
    full = _wire_json(_draw.wire_encode(art))
    if _save_warnings:
        full = "WARNING: " + "; ".join(_save_warnings) + "\n" + full
    _sh = art.get("sheet") or {}
    mm_per_unit = _sh.get("units_mm_per_unit", 1.0)
    _applied = _sh.get("units_converted_from_mm_per_unit", 1.0)
    if mm_per_unit is None or (mm_per_unit != 1.0 and _applied != mm_per_unit):
        full = (f"WARNING: $INSUNITS={_sh.get('units')!r} is not a unit the reader converts "
                f"({mm_per_unit} mm per unit) — every length below is in drawing units, NOT mm. "
                f"Convert before writing IR.\n") + full
    elif _applied != 1.0:
        full = (f"NOTE: the sheet is in $INSUNITS={_sh.get('units')!r} ({_applied} mm per unit); every "
                f"length below was already converted to TRUE mm — do not convert again. Free text "
                f"(notes, dimension overrides) is still as typed, in the drawing's own unit.\n") + full

    # The GATE decides the result shape. The model must never be asked to choose between "give me
    # the analysis" and "just build it" BEFORE it has seen any geometry — that was the flaw in
    # making this an opt-in flag (ADR-064).
    try:
        verdict = _draw.assess(art, cfg)
    except Exception as exc:                      # noqa: BLE001 - a gate bug must not lose the read
        import traceback
        return (f"NOT_DIRECT | GATE_ERROR | {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc(limit=4)}\n{full}")

    # Open decisions: turn them into questions, apply any answers, and re-gate on the answered artifact.
    answers_d, answers_err = clarify.parse_answers(answers)
    if answers_err:
        return f"FAILED | BAD_ANSWERS | {answers_err}"
    applied: list = []
    if answers_d:
        try:
            art, cfg, applied, bad = _draw.apply_answers(art, cfg, answers_d)
        except Exception as exc:                  # noqa: BLE001
            return f"FAILED | BAD_ANSWERS | {type(exc).__name__}: {exc}"
        if bad:
            return "FAILED | BAD_ANSWERS | " + "; ".join(bad)
        verdict = _draw.assess(art, cfg)
    questions = _draw.build_questions(art, verdict, cfg)
    clarify.remember(src, questions)
    qblock = clarify.render(questions, src)
    qtail = ("\n" + qblock) if qblock else ""
    ledger_block = ""
    if verdict["direct"]:
        ledger_block = clarify.render_ledger(_draw.build_ledger(art, verdict, cfg, applied, accept_defaults))
    blocking = [q for q in questions if q["blocking"]]
    if mode in ("ir", "build") and blocking and not accept_defaults:
        return (f"NEEDS_INPUT | {len(blocking)} blocking question(s) — nothing was built.\n"
                f"{qblock}")

    if not verdict["direct"]:
        if mode in ("ir", "build"):
            return (f"FAILED | NOT_DIRECTLY_BUILDABLE | {verdict['reason']} | {verdict['detail']} — "
                    f"call analyze_drawing(mode='auto') and build with submit_feature_graph.")
        # ONE SHAPE, the whole analysis (payload phase 1, 2026-09-07). This used to be a SUMMARY
        # with the geometry pulled back per view, and the split was retired on its own measurement:
        # once the wire form landed, the complete artifact came out smaller on all 10 samples than
        # the summary plus the pulls it made necessary (f-3: 14.0 KB against 28.7).
        return (f"NOT_DIRECT | {verdict['reason']} | {verdict['detail']}\n"
                f"The whole analysis is below. Read it per get_recipe(section='reverse') and build "
                f"with submit_feature_graph.{_advisories(art)}{qtail}\n{full}")

    try:
        graph = _draw.lower_flat_pattern(art, cfg, verdict)
    except Exception as exc:                      # noqa: BLE001
        import traceback
        return (f"NOT_DIRECT | LOWERING_ERROR | {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc(limit=4)}\n{full}")

    if mode == "ir":
        return _wire_json(graph)
    if mode == "auto":
        # The direct path reports its own skipped bends; the advisories add what the READ raised
        # (unread entities, a walked boundary, a mismatched dimension) — equally true when the
        # drawing turned out to be buildable without you.
        return (_summarise_direct(art, verdict["summary"], graph) + _advisories(art)
                + (("\n" + ledger_block) if ledger_block else "") + qtail)

    # mode == "build": the THIRD IR door — same _run_graph, same pycompiler (IR-ADR-005).
    s = verdict["summary"]
    text, ok, sv = _run_graph(graph, fresh_document=True, kind="analyze_drawing", source=src)
    if not ok:
        return text
    try:                                          # what verify_against_interpretation checks by default
        import drawing_verify
        derived = drawing_verify.derive_expected(s)
        expectations.remember(src, derived)
    except Exception:                             # noqa: BLE001 - verification aid, never fails a build
        derived = {}
    tail = ["", "built from %s (sha256 %s) | state_version=%s"
            % (art["source"]["file"], art["source"]["sha256"][:12], sv),
            "VERIFY: analyze_model('mass_properties') should read V = %r m^3 "
            "(blank %g mm^2 x %g mm)." % (s["expected"]["volume_m3"], s["blank"]["area_mm2"],
                                          s["thickness"]["value_mm"]),
            "        then read one bend face back — volume, area and topology cannot see a "
            "mirrored fold (recipe R14)."]
    for r in s["skipped_bends"]:
        tail.append("NOT BUILT: %s bend at %s (%s). The blank volume is unchanged by it, so no "
                    "measurement will catch it — add it or declare it (R15)."
                    % (r["dir"], r["at"], r["reason"]))
    if derived:
        tail.append("verify_against_interpretation() with no arguments now checks the model against the "
                    "drawing itself (%s)." % ", ".join(sorted(derived)))
    if ledger_block:
        tail += ["", ledger_block]
    return text + "\n".join(tail)


@mcp.tool(structured_output=False)
def prepare_drawing(file_path: str, page: int = 1, region: str = "", dpi: int = 300) -> list:
    """Make a PDF/image drawing legible: returns the page as safe-size image tiles PLUS its exact text layer.

    Why: a PDF reaches you rasterised at a size you can't control, small dimension text degrades, and pixel
    coordinates can't be mapped back. Here every image is sized to fit (never silently downscaled) and carries
    its origin, so a pixel (px,py) in image n is page point (clip_x0 + px/px_per_pt, clip_y0 + py/px_per_pt).
    The text layer gives dimension strings EXACTLY (page points, y down) — prefer it to reading digits by eye.
    kind='vector' = text+paths (exact numbers); 'scan' = raster only (read by vision, mark as such).

    file_path: .pdf or .png/.jpg/.tif/.bmp. page: 1-based. dpi: render density (default 300; lowered
    automatically to fit). region: JSON [x0,y0,x1,y1] in PAGE POINTS to zoom one area (e.g. a dimension
    cluster or the title block) — use the text layer's coordinates. Needs PyMuPDF (requirements-pdf.txt)."""
    src = os.path.abspath(file_path)
    if not os.path.exists(src):
        return [f"FAILED | FILE_NOT_FOUND | {src}"]
    try:
        import drawing_prep as dp
        from mcp.server.mcpserver.utilities.types import Image
        fitz, doc = dp.open_source(src)
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | PDF_READER_UNAVAILABLE | {ex}"]
    try:
        if not 1 <= page <= doc.page_count:
            return [f"FAILED | BAD_PAGE | {src} has {doc.page_count} page(s); asked for {page}"]
        pg = doc[page - 1]
        rect = [pg.rect.x0, pg.rect.y0, pg.rect.x1, pg.rect.y1]
        meta = {"file": os.path.basename(src), "page": page, "pages": doc.page_count,
                "kind": dp.classify_page(pg), "page_pt": [round(rect[2], 1), round(rect[3], 1)],
                "page_mm_on_paper": [round(dp.to_mm(rect[2]), 1), round(dp.to_mm(rect[3]), 1)]}
        shots, regions = [], []
        if region:
            try:
                regions = [tuple(float(v) for v in json.loads(region))]
                if len(regions[0]) != 4:
                    raise ValueError("need 4 numbers")
            except Exception as ex:  # noqa: BLE001
                return [f"FAILED | BAD_REGION | region must be JSON [x0,y0,x1,y1] in page points: {ex}"]
        else:
            regions, dpi = dp.plan_tiles_auto(rect, dpi)
        for clip in regions:
            png, info = dp.render_region(fitz, pg, clip, dpi=dpi)
            shots.append((png, info))
        if not region and len(regions) > 1:       # a fitted overview first, so the layout is clear
            png, info = dp.render_region(fitz, pg, rect, dpi=72)
            shots.insert(0, (png, dict(info, overview=True)))
        items, total, truncated = dp.text_layer(pg)
        meta["images"] = [dict(n=i + 1, **info) for i, (_p, info) in enumerate(shots)]
        meta["text_layer"] = {"total": total, "shown": len(items), "truncated": truncated,
                              "items": [f"{w['t']}@{w['x0']},{w['y0']}" for w in items]}
        meta["note"] = ("Coordinates are PAGE POINTS (72/in, origin top-left, y down). Dimension values come "
                        "from the text layer when kind is vector/mixed; true size = paper size x the drawing "
                        "scale printed in the title block.")
        return [json.dumps(meta, ensure_ascii=False)] + [Image(data=png, format="png") for png, _i in shots]
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | PREPARE_FAILED | {type(ex).__name__}: {ex}"]
    finally:
        doc.close()


@mcp.tool(structured_output=False)
def annotate_regions(file_path: str, marks: str, page: int = 1, region: str = "", dpi: int = 300) -> list:
    """Draw YOUR claimed locations on the drawing and look at the result — a self-check before you rely on them.

    Your localisation of a dimension, a hole or a feature is approximate (vision docs: verify before use). Give
    the places you think things are; get back the same region with each mark drawn in red and numbered, plus
    each mark's pixel position. If a box does not sit on what you meant, correct the coordinates and call again.

    marks: JSON list in PAGE POINTS (the coordinates prepare_drawing's text layer and tiles use), e.g.
        [{"box":[x0,y0,x1,y1],"label":"Ø8 hole"},{"point":[x,y],"label":"datum A"}]
    page (1-based), region (JSON [x0,y0,x1,y1] in page points; default = the marks plus a margin), dpi: as in
    prepare_drawing. The file is never modified. Needs PyMuPDF (requirements-pdf.txt)."""
    src = os.path.abspath(file_path)
    if not os.path.exists(src):
        return [f"FAILED | FILE_NOT_FOUND | {src}"]
    try:
        import drawing_prep as dp
        from mcp.server.mcpserver.utilities.types import Image
        parsed = dp.parse_marks(json.loads(marks))
        clip = None
        if region:
            clip = tuple(float(v) for v in json.loads(region))
            if len(clip) != 4:
                raise ValueError("region needs 4 numbers [x0,y0,x1,y1]")
    except (ValueError, TypeError) as ex:
        return [f"FAILED | BAD_MARKS | {ex}"]
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | PDF_READER_UNAVAILABLE | {ex}"]
    try:
        fitz, doc = dp.open_source(src)
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | PDF_READER_UNAVAILABLE | {ex}"]
    try:
        if not 1 <= page <= doc.page_count:
            return [f"FAILED | BAD_PAGE | {src} has {doc.page_count} page(s); asked for {page}"]
        png, info, resolved = dp.annotate_region(fitz, doc[page - 1], parsed, clip=clip, dpi=max(30, min(dpi, 600)))
        meta = {"file": os.path.basename(src), "page": page, "image": info,
                "marks": [{"label": m["label"], "kind": m["kind"], "page_pt": m["pts"], "px": m["px"]}
                          for m in resolved]}
        return [json.dumps(meta, ensure_ascii=False), Image(data=png, format="png")]
    except Exception as ex:  # noqa: BLE001
        return [f"FAILED | ANNOTATE_FAILED | {type(ex).__name__}: {ex}"]
    finally:
        doc.close()


@mcp.tool(structured_output=False)
def verify_against_interpretation(expected: str = "") -> str:
    """Check the ACTIVE built part against your interpretation of a drawing (PDF/image/DXF) — a
    mechanical PASS/FAIL per item instead of eyeballing numbers.

    expected: optional. After analyze_drawing(mode='build') leave it EMPTY to check the model against
        the drawing itself (volume, round-hole sizes/counts derived from the artifact — no transcription
        by you). Keys you pass override the derived ones. Otherwise a JSON string, MILLIMETRES (what the
        drawing prints), every key optional:
        {"bbox_mm":[x,y,z] (overall size; axes compared SORTED unless "bbox_ordered":true),
         "volume_mm3":n, "volume_tol_pct":2, "cg_mm":[x,y,z], "cg_tol_mm":1,
         "holes":[{"diameter_mm":8,"count":4}], "tol_mm":0.1}
    Measures bbox + mass properties + faces via analyze_model and reports FAIL rows with deltas, WARN for
    built-but-unlisted holes, and NOT CHECKED for anything it could not measure. cg_mm is the check that
    catches a mirrored / wrong-face feature (volume stays equal). A PASS covers only what you listed."""
    try:
        given = json.loads(expected) if expected.strip() else {}
        if not isinstance(given, dict):
            raise ValueError("must be a JSON object")
    except Exception as ex:  # noqa: BLE001
        return f"FAILED | INVALID_JSON | expected must be a JSON object: {ex}"
    try:
        import drawing_verify
    except Exception as ex:  # noqa: BLE001
        return f"FAILED | VERIFIER_UNAVAILABLE | {ex}"
    source, derived = expectations.recall()
    exp, overridden = drawing_verify.merge_expected(derived, given)
    if not exp:
        return ("FAILED | NOTHING_TO_VERIFY | no expected values given and no drawing has been built this "
                "session — pass expected, or build with analyze_drawing(mode='build') first.")
    provenance = ""
    if derived:
        provenance = "expected from the drawing %s: %s" % (os.path.basename(source or "?"), ", ".join(sorted(derived)))
        if overridden:
            provenance += "; overridden by you: " + ", ".join(sorted(overridden))
        provenance += "\n"

    measured = {"bbox": None, "volume": None, "cg": None, "faces": None}
    try:
        if "bbox_mm" in exp:
            try:
                items = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "bbox", "name": ""}))
                measured["bbox"] = json.loads(items[0]) if items else None
            except RuntimeError:
                measured["bbox"] = None           # older execution layer -> reported as NOT CHECKED
        if "volume_mm3" in exp or "cg_mm" in exp:
            mass = _kv_dict(_analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
            measured["volume"] = mass.get("volume")
            if all(isinstance(mass.get(k), (int, float)) for k in ("cx", "cy", "cz")):
                measured["cg"] = [mass["cx"], mass["cy"], mass["cz"]]
        if "holes" in exp:
            items = _analysis_items(bridge._call_raw("analyze_model", {"analysis_type": "faces", "name": ""}))
            measured["faces"] = json.loads(items[0]).get("faces", []) if items else []
    except RuntimeError as ex:
        return f"FAILED | MEASURE_FAILED | {ex}"
    try:
        return provenance + drawing_verify.render(drawing_verify.evaluate(exp, measured))
    except (KeyError, TypeError, ValueError) as ex:
        return f"FAILED | BAD_EXPECTED | {type(ex).__name__}: {ex} (see the tool description for the shape)"
