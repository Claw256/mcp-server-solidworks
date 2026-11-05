"""vocab.py -- every CLOSED vocabulary of the `draw` dialect, in one dependency-free place.

This is the drawing side's answer to pycompiler's ir_schema frozensets: the contract test
(tests/test_draw_contract.py) diffs each set here against the matching array in
cad-planner/contracts/draw-dialect.schema.json, both directions, exact equality. A token added on
one side alone fails the gate -- which is the point, since the schema is what the model is told
the dialect contains.

Kept free of imports on purpose: the contract test must run with no ezdxf, on any machine.
"""
from __future__ import annotations

# --- geometry / structure --------------------------------------------------------------------
EDGE_CLASSES = frozenset({"visible", "hidden", "cut_line", "center"})
VIEW_ROLES = frozenset({"view", "annotation", "frame_item"})
LOOP_ROLES = frozenset({"outer", "inner"})
# HOW a loop was resolved. A loop carries `tier` only when it is "B"; ABSENT means "A", so the
# overwhelmingly common case costs nothing. "A" = strict chaining, which stops dead at any
# junction of three or more and therefore never guesses. "B" = planar face traversal, used only
# where the view's visible graph has NO free end: the figure is then a closed subdivision whose
# faces are DEFINED by angular order at each vertex, so walking it is a reading, not a guess.
CHAIN_TIERS = frozenset({"A", "B"})
SEQ_CODES = frozenset({"l", "a", "c", "e"})                 # as used in a loop's `seq`
PRIMITIVE_KINDS = frozenset({"lines", "arcs", "circles", "ellipses"})   # view.geometry arrays
# "e"/"ellipses" since 0.5.0: a real DXF ELLIPSE entity, or an exploded curve fan the fitter
# collapsed (record carries `n` + `fit` then) -- see curvefit.py.

# --- reader advisories ---------------------------------------------------------------------------
# `sheet.advisories` is [{"code", "text"}], present ONLY when something fired; every code is one of
# these. They are FACTS about how trustworthy the read is (never silent conversions): a reader that
# guesses a unit/scale/layout says so here and keeps the unconverted value. Documented in the schema's
# `sheet_additions` prose and diffed by tests/test_reader_hardening.py.
ADVISORY_CODES = frozenset({
    "SCALE_MISMATCH",        # title-block scale text disagrees with the DIMLFAC-derived sheet scale
    "SCALE_CANDIDATES",      # several scale texts on a PDF page; which one was used is reported
    "UNITS_UNSET",           # $INSUNITS 0/absent: kept as mm, with an inch/mm hint -- NEVER converted
    "PROJECTION_MISMATCH",   # the drawing states one projection method, config.projection is the other
    "LAYOUT_AUTO",           # several layouts had content; only one was read (the others are named)
    "LAYOUT_MERGED",         # layout='*': several layouts were deliberately merged into one space
    "DIMLFAC_MIXED",         # dimensions use dimstyles with different DIMLFAC; per-dimension factors used
    "BLOCK_GEOMETRY_READ",   # INSERT block geometry/dimensions of a non-SolidWorks file were read
    "ZERO_VIEWS",            # no view cluster found: nothing to build from
})

# --- LINETYPE NAME -> edge class (dxf_read.edge_class) -------------------------------------------------
# First matching prefix wins. HIDDEN*/PHANTOM*/CENTER* are the SolidWorks names and keep their old
# class. Everything else is a CAD-vendor family (AutoCAD acad.lin, ISO 128: 'ISO02W100' and
# 'ACAD_ISO02W100' are the same name). A chain-type line (dash-dot family) is a CENTRE line when thin
# and a CUT line when drawn heavy (ISO 128 type H: thick at the ends): HEAVY_LINEWEIGHT (1/100 mm) is
# the lineweight at or above which the 3rd column applies -- the ONLY place lineweight is consulted,
# as a tie-break and never as the primary key. An unknown name stays 'visible' (the safe default: it
# is counted as geometry, not hidden away).
#   (prefix, class, heavy_class_or_None)
LINETYPE_CLASSES = (
    ("HIDDEN", "hidden", None),
    ("PHANTOM", "cut_line", None),
    ("CENTER", "center", None),
    ("DASHDOT", "center", "cut_line"),
    ("DASHED", "hidden", None),
    ("DIVIDE", "center", "cut_line"),
    ("DOT", "hidden", None),
    ("ACAD_ISO02", "hidden", None),        # ISO dash
    ("ACAD_ISO03", "hidden", None),        # ISO dash space
    ("ACAD_ISO04", "center", "cut_line"),  # ISO long-dash dot
    ("ACAD_ISO05", "cut_line", None),      # ISO long-dash double-dot
    ("ACAD_ISO06", "cut_line", None),      # ISO long-dash triple-dot
    ("ACAD_ISO07", "hidden", None),        # ISO dot
    ("ACAD_ISO08", "center", "cut_line"),  # ISO long-dash short-dash
    ("ACAD_ISO09", "center", "cut_line"),  # ISO long-dash double-short-dash
    ("ACAD_ISO10", "center", "cut_line"),  # ISO dash dot
    ("ACAD_ISO11", "center", "cut_line"),  # ISO double-dash dot
    ("ACAD_ISO12", "center", "cut_line"),  # ISO double-dash double-dot
    ("ACAD_ISO13", "center", "cut_line"),  # ISO triple-dash dot
    ("ACAD_ISO14", "center", "cut_line"),  # ISO triple-dash double-dot
    ("ACAD_ISO15", "center", "cut_line"),  # ISO triple-dash triple-dot
)
HEAVY_LINEWEIGHT = 35                      # 0.35 mm

# --- view graph --------------------------------------------------------------------------------
# How strongly a projection pair is evidenced: full shared span > shared midpoints (a silhouette
# may genuinely be longer in one view -- s-7's bevelled beam) > a section label ("A-A", which even
# a cross-scale section keeps). The consumer decides what each grade licenses.
ALIGN_GRADES = frozenset({"span", "mid", "label"})

# --- sheet metal -------------------------------------------------------------------------------
BEND_DIRECTIONS = frozenset({"UP", "DOWN"})
UNPAIRED_REASONS = frozenset({"no_candidate", "ambiguous"})

# --- the direct-buildable gate ------------------------------------------------------------------
# Each one is a reason the DRAWING does not force a decision. None of them is an error: the caller
# falls back to handing the model the full analysis, which is the pre-existing behaviour.
GATE_REASONS = frozenset({
    "no_bend_notes",            # not a sheet-metal flat pattern (v1 scope)
    "bend_notes_split",         # bend notes spread over more than one view
    "no_outer_loop",            # the blank outline did not close
    "multiple_outer_loops",     # more than one candidate blank in the flat view
    "stray_loop",               # a closed loop that is neither the blank nor one of its cutouts
    "cut_line_in_flat_view",    # a section is cut through the flat pattern
    "unclaimed_open_chain",     # a real line in the flat view that nothing accounts for
    "no_bend_paired",           # not one bend note could be matched -- systematic, not a one-off
    "thickness_unresolved",     # no thickness source
    "thickness_ambiguous",      # more than one, and they disagree
    "fixed_point_ambiguous",    # no safe point on the blank that every bend can fold around
    "units_not_mm",             # unknown $INSUNITS code, or a non-mm sheet the reader did not convert
    "mixed_bends_in_group",     # same-direction bends with different angle/radius; one IR bend can't carry both
})

# $INSUNITS code -> millimetres per drawing unit. 0 (unitless) and 4 (mm) are treated as mm; every
# other KNOWN code is converted to true mm by dxf_read.read_doc at the document; a code missing
# from this table is not converted and the direct-build gate refuses it (units_not_mm).
INSUNITS_MM = {0: 1.0, 1: 25.4, 2: 304.8, 3: 1609344.0, 4: 1.0, 5: 10.0, 6: 1000.0, 7: 1e6,
               8: 25.4e-6, 9: 0.0254, 10: 914.4, 13: 1e-3, 14: 100.0, 15: 1e4}

# The subset of gate reasons that are also `_resolve_thickness` states, so that helper's return
# values cannot drift away from the gate's vocabulary.
THICKNESS_STATES = frozenset({"ok", "thickness_unresolved", "thickness_ambiguous"})
