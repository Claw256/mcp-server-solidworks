"""Offline tests for questions.py: open decisions -> typed questions, answers -> a buildable
artifact, and the assumptions ledger. Runs on the frozen fixtures; no ezdxf, no SolidWorks.

  pytest cad-planner/drawing/tests/test_questions.py
"""
import copy
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DRAWING_ROOT = os.path.dirname(_HERE)
_FIXTURES = os.path.join(_HERE, "fixtures")
if _DRAWING_ROOT not in sys.path:
    sys.path.insert(0, _DRAWING_ROOT)

import lowering  # noqa: E402
import questions  # noqa: E402


def _load(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _cfg():
    with open(os.path.join(_DRAWING_ROOT, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_ambiguous_bend_becomes_a_blocking_question_with_candidates_and_a_skip_default():
    art, cfg = _load("ambiguous_bend.json"), _cfg()
    qs = questions.build_questions(art, lowering.assess(art, cfg), cfg)
    bend = [q for q in qs if q["kind"] == "skipped_bend"]
    assert len(bend) == 1 and bend[0]["blocking"] is True
    assert bend[0]["id"] == "bend_1"
    values = [o["value"] for o in bend[0]["options"]]
    assert values == ["l:5", "l:6", "skip"]
    assert bend[0]["default"] == "skip"
    assert "line l5" in bend[0]["options"][0]["evidence"]


def test_k_factor_is_a_non_blocking_assumption_on_the_direct_path():
    art, cfg = _load("s1_flat.json"), _cfg()
    qs = questions.build_questions(art, lowering.assess(art, cfg), cfg)
    assert [q["id"] for q in qs] == ["k_factor"]
    assert qs[0]["blocking"] is False and qs[0]["default"] == "0.5"


def test_missing_thickness_asks_and_the_answer_makes_the_drawing_buildable():
    art, cfg = _load("no_thickness.json"), _cfg()
    verdict = lowering.assess(art, cfg)
    assert verdict["reason"] == "thickness_unresolved"
    qs = questions.build_questions(art, verdict, cfg)
    assert [q["id"] for q in qs] == ["thickness"] and qs[0]["blocking"]

    art2, cfg2, applied, errors = questions.apply_answers(art, cfg, {"thickness": "2.0"})
    assert not errors and applied == [{"id": "thickness", "field": "thickness_mm", "value": 2.0}]
    verdict2 = lowering.assess(art2, cfg2)
    assert verdict2["direct"], verdict2
    assert verdict2["summary"]["thickness"] == {"value_mm": 2.0, "source": "user answer"}
    assert "overrides" not in art                      # inputs are never mutated


def test_choosing_a_candidate_pairs_the_bend_and_it_gets_built():
    # One real hidden line (l5) with a stray duplicate candidate removed: the answer is unambiguous.
    art, cfg = _load("ambiguous_bend.json"), _cfg()
    art["views"][0]["geometry"]["lines"].pop()                       # drop l6 ...
    art["views"][0]["open_chains"] = [c for c in art["views"][0]["open_chains"]
                                      if c["seq"][0][1] != 6]        # ... and its chain
    art["bend_notes"][1]["unpaired"]["candidates"] = [["l", 5]]
    art2, cfg2, applied, errors = questions.apply_answers(art, cfg, {"bend_1": "l:5"})
    assert not errors
    b = art2["bend_notes"][1]
    assert b["bend_line"]["seg"] == ["l", 5] and b["bend_line"]["user_confirmed"] and "unpaired" not in b
    verdict = lowering.assess(art2, cfg2)
    assert verdict["direct"], verdict
    assert len(verdict["summary"]["bends"]) == 2 and verdict["summary"]["skipped_bends"] == []
    assert art["bend_notes"][1].get("unpaired")                      # original untouched


def test_picking_one_half_of_a_split_bend_line_is_still_refused_by_the_gate():
    # ambiguous_bend.json's candidates l5 and l6 are the two halves of ONE hidden line. Building a
    # half-length bend would be wrong, so after the user picks l5 the gate must still name l6.
    art, cfg = _load("ambiguous_bend.json"), _cfg()
    art2, cfg2, _applied, errors = questions.apply_answers(art, cfg, {"bend_1": "l:5"})
    assert not errors
    verdict = lowering.assess(art2, cfg2)
    assert not verdict["direct"] and verdict["reason"] == "unclaimed_open_chain"
    assert "l6" in verdict["detail"]


def test_skip_answer_keeps_the_gap_and_is_recorded():
    art, cfg = _load("ambiguous_bend.json"), _cfg()
    art2, cfg2, applied, errors = questions.apply_answers(art, cfg, {"bend_1": "skip"})
    assert not errors and applied[0]["value"] == "skip"
    ledger = questions.build_ledger(art2, lowering.assess(art2, cfg2), cfg2, applied, False)
    gap = [e for e in ledger if e["field"].startswith("bend DOWN")][0]
    assert gap["value"] == "unbuilt (gap)" and gap["source"] == "user"


def test_bad_answers_are_reported_not_half_applied():
    art, cfg = _load("ambiguous_bend.json"), _cfg()
    _a, _c, applied, errors = questions.apply_answers(
        art, cfg, {"bend_1": "l:999", "thickness": "-3", "k_factor": "7", "nope": "x", "bend_9": "skip"})
    assert applied == []
    assert len(errors) == 5


def test_k_factor_answer_changes_the_config_and_the_ledger():
    art, cfg = _load("s1_flat.json"), _cfg()
    art2, cfg2, applied, errors = questions.apply_answers(art, cfg, {"k_factor": "0.33"})
    assert not errors and cfg2["sheet_metal"]["k_factor"] == 0.33 and cfg["sheet_metal"]["k_factor"] == 0.5
    ledger = {e["field"]: e for e in questions.build_ledger(
        art2, lowering.assess(art2, cfg2), cfg2, applied, False)}
    assert ledger["k_factor"]["source"] == "user" and ledger["k_factor"]["value"] == 0.33
    assert ledger["thickness_mm"]["source"] == "drawing"


def test_ledger_marks_untouched_assumptions_as_defaults():
    art, cfg = _load("ambiguous_bend.json"), _cfg()
    ledger = questions.build_ledger(art, lowering.assess(art, cfg), cfg, [], True)
    by_field = {e["field"]: e for e in ledger}
    assert by_field["k_factor"]["source"] == "default"
    gap = [e for e in ledger if e["field"].startswith("bend DOWN")][0]
    assert gap["source"] == "default" and gap["note"] == "accepted defaults"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
