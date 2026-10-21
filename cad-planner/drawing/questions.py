"""questions.py -- turn what the drawing leaves open into typed QUESTIONS, apply the user's ANSWERS,
and keep an ASSUMPTIONS LEDGER of every value that did not come straight off the drawing.

WHY: the direct-build gate used to either build silently around a doubt (an ambiguous bend note was
SKIPPED and a partial part was built) or refuse with a prose reason. Neither lets the person who owns
the design settle the doubt. A question is the same fact, made answerable: an id, the evidence, a
short list of options and a DEFAULT, and whether the build must wait for it.

Pure and CAD-neutral: no ezdxf, no SolidWorks. Inputs are the decoded artifact record, the gate verdict
from `lowering.assess`, and the drawing config.

  build_questions(art, verdict, cfg)   -> [question]
  apply_answers(art, cfg, answers)     -> (art', cfg', applied, errors)    # inputs are never mutated
  build_ledger(art, verdict, cfg, applied, accepted_defaults) -> [entry]

Question: {id, kind, field, prompt, options:[{value,label,evidence?}], default, blocking}
Ledger entry: {field, value, source: drawing|user|default, default?, note?}
"""
from __future__ import annotations

import copy
import re

try:
    from . import contour
except ImportError:
    import contour

SKIP = "skip"
_LINE_RE = re.compile(r"^l:(\d+)$")


def _bend_id(index):
    return "bend_%d" % index


def _line_evidence(view, idx):
    ln = view["geometry"]["lines"][idx]
    length = ((ln["x2"] - ln["x1"]) ** 2 + (ln["y2"] - ln["y1"]) ** 2) ** 0.5
    return "line l%d (%s) from (%g, %g) to (%g, %g), %.2f mm" % (
        idx, ln["c"], ln["x1"], ln["y1"], ln["x2"], ln["y2"], length)


def _views(art):
    return {v["vid"]: v for v in art.get("views", [])}


def build_questions(art, verdict, cfg):
    """Everything the drawing leaves for a person to settle, most blocking first."""
    views = _views(art)
    questions = []

    # --- a bend note that could not be matched to exactly one line
    for i, b in enumerate(art.get("bend_notes") or []):
        un = b.get("unpaired")
        if not un or b.get("bend_line") or b.get("user_decision"):
            continue                              # matched, or the user already decided
        view = views.get(b.get("view"))
        options = []
        for cand in un.get("candidates") or []:
            if view is not None and cand[0] == "l":
                options.append({"value": "l:%d" % cand[1], "label": "bend line l%d" % cand[1],
                                "evidence": _line_evidence(view, cand[1])})
        options.append({"value": SKIP, "label": "leave this bend unbuilt (declared as a gap)"})
        reason = un.get("reason")
        why = ("two or more lines are equally close" if reason == "ambiguous"
               else "no line of the right class lies under the note")
        questions.append({
            "id": _bend_id(i), "kind": "skipped_bend", "field": "bend_line",
            "prompt": "The %s %g deg R%g bend note at (%g, %g) could not be matched to ONE bend line (%s). "
                      "Which line does it annotate?" % (b["dir"], b["angle_deg"], b["radius"],
                                                       b["at"][0], b["at"][1], why),
            "options": options, "default": SKIP, "blocking": True,
        })

    # --- thickness
    if verdict and not verdict.get("direct") and verdict.get("reason") in (
            "thickness_unresolved", "thickness_ambiguous"):
        opts = []
        if verdict["reason"] == "thickness_ambiguous":
            for val in verdict.get("candidates") or []:
                opts.append({"value": "%g" % val, "label": "%g mm" % val})
        questions.append({
            "id": "thickness", "kind": "thickness", "field": "thickness_mm",
            "prompt": ("Sheet thickness: " + verdict["detail"] + ". What is the thickness in mm?"),
            "options": opts, "default": None, "blocking": True,
        })

    # --- assumptions that matter but never block
    if verdict and verdict.get("direct") and "k_factor" not in (art.get("overrides") or {}):
        k = cfg["sheet_metal"]["k_factor"]
        questions.append({
            "id": "k_factor", "kind": "assumption", "field": "k_factor",
            "prompt": "Bend allowance: K-factor %g (the configured default) is assumed. Keep it?" % k,
            "options": [{"value": "%g" % k, "label": "keep %g" % k}],
            "default": "%g" % k, "blocking": False,
        })
    return questions


def apply_answers(art, cfg, answers):
    """Apply {question_id: value} to COPIES of the artifact and config.

    -> (art', cfg', applied, errors). `applied` is [{id, field, value}]; `errors` names answers that
    were rejected (unknown id, a line that does not exist, a non-positive thickness) so the caller can
    say so instead of building on a half-applied answer."""
    art2, cfg2 = copy.deepcopy(art), copy.deepcopy(cfg)
    applied, errors = [], []
    views = _views(art2)
    notes = art2.get("bend_notes") or []
    for qid, raw in (answers or {}).items():
        value = str(raw).strip()
        if qid.startswith("bend_"):
            try:
                b = notes[int(qid[5:])]
            except (ValueError, IndexError):
                errors.append("%s: no such bend note" % qid)
                continue
            if value == SKIP:
                b["user_decision"] = SKIP
                applied.append({"id": qid, "field": "bend_line", "value": SKIP})
                continue
            m = _LINE_RE.match(value)
            view = views.get(b.get("view"))
            if not m or view is None or int(m.group(1)) >= len(view["geometry"]["lines"]):
                errors.append("%s: %r is not 'skip' or the id of a line in view %s (e.g. 'l:12')"
                              % (qid, value, b.get("view")))
                continue
            li = int(m.group(1))
            in_loop = {idx for code, idx in contour.loop_member_segments(view) if code == "l"}
            b["bend_line"] = {"seg": ["l", li], "class": view["geometry"]["lines"][li]["c"],
                              "note_offset": None, "in_loop": li in in_loop, "user_confirmed": True}
            b.pop("unpaired", None)
            applied.append({"id": qid, "field": "bend_line", "value": value})
        elif qid == "thickness":
            try:
                t = float(value)
                if t <= 0:
                    raise ValueError
            except ValueError:
                errors.append("thickness: %r is not a positive number of mm" % value)
                continue
            art2.setdefault("overrides", {})["thickness_mm"] = t
            applied.append({"id": qid, "field": "thickness_mm", "value": t})
        elif qid == "k_factor":
            try:
                k = float(value)
                if not 0.0 < k < 1.0:
                    raise ValueError
            except ValueError:
                errors.append("k_factor: %r is not a number between 0 and 1" % value)
                continue
            cfg2["sheet_metal"]["k_factor"] = k
            art2.setdefault("overrides", {})["k_factor"] = k
            applied.append({"id": qid, "field": "k_factor", "value": k})
        else:
            errors.append("%s: unknown question id" % qid)
    return art2, cfg2, applied, errors


def build_ledger(art, verdict, cfg, applied, accepted_defaults):
    """Every value the build rests on that was NOT simply read off the drawing."""
    by_id = {a["id"]: a for a in applied}
    ledger = []

    th = ((verdict or {}).get("summary") or {}).get("thickness")
    if th:
        user = "thickness" in by_id
        ledger.append({"field": "thickness_mm", "value": th["value_mm"],
                       "source": "user" if user else "drawing", "note": th["source"]})

    k = cfg["sheet_metal"]["k_factor"]
    if "k_factor" in by_id:
        ledger.append({"field": "k_factor", "value": k, "source": "user"})
    else:
        ledger.append({"field": "k_factor", "value": k, "source": "default", "default": k,
                       "note": "drawing config default (SolidWorks 0.5)"})

    sheet = art.get("sheet") or {}
    pdf = sheet.get("pdf")
    if pdf and "ASSUMED" in str(pdf.get("scale_source")):
        ledger.append({"field": "drawing_scale", "value": "1:1", "source": "default",
                       "note": "no scale text found in the PDF; assumed 1:1"})
    if sheet.get("units_converted_from_mm_per_unit", 1.0) != 1.0:
        ledger.append({"field": "units", "value": "mm", "source": "drawing",
                       "note": "converted from %s (x%s)" % (sheet.get("units"),
                                                           sheet["units_converted_from_mm_per_unit"])})

    for i, b in enumerate(art.get("bend_notes") or []):
        qid = _bend_id(i)
        if qid in by_id:
            a = by_id[qid]
            ledger.append({"field": "bend %s at (%g, %g)" % (b["dir"], b["at"][0], b["at"][1]),
                           "value": "unbuilt (gap)" if a["value"] == SKIP else a["value"],
                           "source": "user"})
        elif b.get("unpaired"):
            ledger.append({"field": "bend %s at (%g, %g)" % (b["dir"], b["at"][0], b["at"][1]),
                           "value": "unbuilt (gap)", "source": "default", "default": SKIP,
                           "note": "accepted defaults" if accepted_defaults else "not answered"})
    return ledger
