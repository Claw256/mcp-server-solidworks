"""Design sessions: patch ops, reconcile (prefix reuse, truncation, restore), undo/redo, drift,
persistence. Offline: the compiler is the real pycompiler over a feature-tree-simulating fake port."""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

try:
    from fake_tree_port import Env, FAIL_HEIGHT, box, graph, mirror
    from solidpilot import design, runs
    from solidpilot.tools import design as tools
    _DEPS = True
except Exception:  # noqa: BLE001
    _DEPS = False


def _new(env, *nodes, **kw):
    out = tools.design_new(graph=json.dumps(graph(*nodes, **kw)) if nodes else "")
    env.port.calls.clear()
    return out


def _step(patch, label=""):
    return tools.design_step(json.dumps(patch), label)


def _ids(d=None):
    return [n["id"] for n in (d or design.get()).live()["nodes"]]


def test_new_with_graph_builds_step_one_and_ends_with_the_design_tail():
    if not _DEPS:
        return
    with Env() as env:
        out = tools.design_new(graph=json.dumps(graph(box("b1"))))
        assert out.startswith("COMPLETED | step 1 |") and "IR built 1/1 nodes" in out
        d = design.get()
        assert f"design_id={d.design_id} step=1 head=1/1" in out and "run_id=" in out and "state_version=" in out
        assert env.port.tree == ["Sketch1", "Boss-Extrude1"] and d.live()["tree"] == env.port.tree
        assert env.raw_calls[0][0] == "open_new_part"
        m, err = runs.load(out.split("run_id=")[1].split()[0])
        assert err is None and m["kind"] == "design_new" and m["status"] == "COMPLETED"


def test_add_appends_without_truncation_or_replay():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        out = _step([{"op": "add", "nodes": [box("b2", 0.02)]}], "second box")
        assert out.startswith("COMPLETED | step 2 |") and "kept 1 unchanged, built 1" in out, out
        assert "truncate" not in env.port.tools()
        assert env.port.tools() == ["create_sketch", "add_sketch_entity", "extrude_feature"]   # b2 only
        assert env.port.tree == ["Sketch1", "Boss-Extrude1", "Sketch2", "Boss-Extrude2"]
        assert _ids() == ["b1", "b2"]


def test_mid_list_replace_truncates_and_replays_the_suffix_with_dependents_rebuilt():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"), box("b2", 0.02), mirror("m1", "b2"))
        assert env.port.tree[-1] == "Mirror1"
        out = _step([{"op": "replace", "id": "b2", "node": box("b2", 0.04)}])
        assert out.startswith("COMPLETED | step 2"), out
        assert env.port.calls[0] == ("truncate", {"keep_count": 2})       # b1's boundary
        rebuilt = [c[0] for c in env.port.calls[1:]]
        assert rebuilt.count("extrude_feature") == 1 and rebuilt.count("create_pattern") == 1   # b2 AND its mirror
        mirror_call = [c for c in env.port.calls if c[0] == "create_pattern"][0][1]
        assert json.loads(mirror_call["features_json"]) == ["Boss-Extrude2"]
        assert "kept 1 unchanged, built 2" in out and "truncated 5 -> 2" in out


def test_set_merges_into_the_node_and_remove_drops_it():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"), box("b2"))
        _step([{"op": "set", "id": "b2", "params": {"height": 0.07}}])
        assert design.get().live()["nodes"][1]["height"] == 0.07
        env.port.calls.clear()
        out = _step([{"op": "remove", "id": "b2"}])
        assert out.startswith("COMPLETED | step 3") and _ids() == ["b1"]
        assert env.port.tree == ["Sketch1", "Boss-Extrude1"]
        assert env.port.tools() == ["truncate"]                             # nothing replayed after the cut


def test_insert_after_rebuilds_the_following_nodes():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"), box("b3"))
        out = _step([{"op": "add", "nodes": [box("b2", 0.01)], "after": "b1"}])
        assert out.startswith("COMPLETED | step 2"), out
        assert _ids() == ["b1", "b2", "b3"] and len(env.port.tree) == 6
        assert "kept 1 unchanged, built 2" in out


def test_undo_redo_chain_and_a_new_step_discards_redo():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        _step([{"op": "add", "nodes": [box("b2")]}], "two")
        _step([{"op": "add", "nodes": [box("b3")]}], "three")
        env.port.calls.clear()
        out = tools.design_undo(2)
        assert "now at step 1" in out and "head=1/3" in out and _ids() == ["b1"]
        assert env.port.tools() == ["truncate"]                           # undo of appends replays nothing
        assert len(env.port.tree) == 2
        out = tools.design_redo()
        assert "now at step 2" in out and _ids() == ["b1", "b2"] and len(env.port.tree) == 4
        out = tools.design_redo(5)                                          # clamps to what exists
        assert "head=3/3" in out and _ids() == ["b1", "b2", "b3"]
        assert tools.design_redo().startswith("FAILED | NOTHING_TO_REDO")
        tools.design_undo(2)
        out = _step([{"op": "add", "nodes": [box("c2", 0.03)]}], "branch")
        assert "head=2/2" in out
        assert tools.design_redo().startswith("FAILED | NOTHING_TO_REDO")   # the old redo entries are gone
        tools.design_undo(9)
        assert tools.design_undo().startswith("FAILED | NOTHING_TO_UNDO") and env.port.tree == []


def test_undo_of_a_replace_restores_the_old_geometry():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"), box("b2", 0.02))
        _step([{"op": "replace", "id": "b1", "node": box("b1", 0.09)}])
        env.port.calls.clear()
        out = tools.design_undo()
        assert out.startswith("COMPLETED | now at step 1")
        ext = [c[1]["depth"] for c in env.port.calls if c[0] == "extrude_feature"]
        assert ext == [0.05, 0.02]                                          # both boxes rebuilt in the old shape
        assert design.get().live()["nodes"][0]["height"] == 0.05


def test_failed_step_restores_the_old_suffix_and_leaves_the_design_unchanged():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"), box("b2", 0.02))
        before = list(env.port.tree)
        out = _step([{"op": "replace", "id": "b2", "node": box("b2", FAIL_HEIGHT)}])
        assert out.startswith("FAILED | STEP_FAILED"), out
        assert "EXTRUSION_FAILED" in out and "design is unchanged" in out and "step=1 head=1/1" in out
        d = design.get()
        assert d.head == 1 and d.total == 1 and _ids() == ["b1", "b2"]
        assert env.port.tree == before and d.live()["tree"] == before
        m, _ = runs.load(out.split("run_id=")[1].split()[0])
        assert m["status"] == "FAILED"
        assert _step([{"op": "add", "nodes": [box("b3")]}]).startswith("COMPLETED | step 2")   # still usable


def test_failed_restore_is_design_corrupt():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"), box("b2", 0.02))
        real = env.port.execute
        state = {"n": 0}

        def flaky(tool, params, sv):
            if tool == "extrude_feature":
                state["n"] += 1
                if state["n"] <= 2:                                         # the new suffix AND the restore
                    return {"status": "FAILED", "stateVersion": env.port.sv,
                            "error": {"code": "EXTRUSION_FAILED", "message": "boom"}}
            return real(tool, params, sv)
        env.port.execute = flaky
        out = _step([{"op": "replace", "id": "b2", "node": box("b2", 0.04)}])
        assert out.startswith("FAILED | DESIGN_CORRUPT") and "design_new" in out and "design_history" in out


def test_validation_failure_performs_zero_cad_operations():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        for patch in ([{"op": "add", "nodes": [{"id": "x", "type": "frobnicate"}]}],
                      [{"op": "add", "nodes": [mirror("m", "ghost")]}],
                      [{"op": "set", "id": "b1", "params": {"height": -1}}]):
            env.raw_calls.clear()
            out = _step(patch)
            assert out.startswith("FAILED | VALIDATION_FAILED") and "no execution performed" in out, out
            assert env.port.calls == [] and env.raw_calls == []
        assert design.get().head == 1


def test_bad_patches_are_clear_failures():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        assert _step([{"op": "explode"}]).startswith("FAILED | PATCH_INVALID")
        assert _step([{"op": "remove", "id": "nope"}]).startswith("FAILED | NODE_NOT_FOUND")
        assert _step([{"op": "add", "nodes": [box("b2")], "after": "nope"}]).startswith("FAILED | NODE_NOT_FOUND")
        assert _step([{"op": "set", "id": "b1", "params": {"id": "z"}}]).startswith("FAILED | PATCH_INVALID")
        assert _step([{"op": "add", "nodes": [box("b2")], "wat": 1}]).startswith("FAILED | PATCH_INVALID")
        assert tools.design_step("not json").startswith("FAILED | INVALID_JSON")
        assert env.port.calls == []


def test_drift_is_detected_and_nothing_is_changed():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        env.port.tree.append("Fillet-by-hand")                              # a manual edit in SolidWorks
        out = _step([{"op": "add", "nodes": [box("b2")]}])
        assert out.startswith("FAILED | DESIGN_DRIFT") and "design_new" in out, out
        assert env.port.calls == [] and design.get().head == 1
        assert tools.design_undo().startswith("FAILED | DESIGN_DRIFT")
        env.port.tree.pop()
        assert _step([{"op": "add", "nodes": [box("b2")]}]).startswith("COMPLETED | step 2")


def test_reconcile_activates_the_designs_document_first():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        title = design.get().doc_title
        env.active = "Other"
        _step([{"op": "add", "nodes": [box("b2")]}])
        assert ("activate_document", {"title": title}) in env.raw_calls and env.active == title


def test_material_op_applies_once_and_is_part_of_the_snapshot():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        out = _step([{"op": "material", "material": {"name": "AISI 304"}}])
        assert out.startswith("COMPLETED | step 2"), out
        assert env.port.tools().count("set_part_material") == 1
        env.port.calls.clear()
        _step([{"op": "add", "nodes": [box("b2")]}])
        assert "set_part_material" not in env.port.tools()                  # unchanged material is not re-applied
        assert design.get().live()["material"] == {"name": "AISI 304"}


def test_journal_persists_across_a_process_restart():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        _step([{"op": "add", "nodes": [box("b2", 0.02)]}], "second")
        tools.design_undo()
        did = design.get().design_id
        assert os.path.isfile(os.path.join(design.designs_dir(), did + ".json"))
        design._reset_memory()                                              # "restart"
        d = design.get()
        assert d.design_id == did and d.head == 1 and d.total == 2 and _ids(d) == ["b1"]
        assert design.get(did) is d
        out = tools.design_redo()                                           # seeds come from the journal
        assert "now at step 2" in out and len(env.port.tree) == 4
        design._reset_memory()
        assert design.get("nope") is None and design.get("../x") is None


def test_history_lists_steps_head_and_redo():
    if not _DEPS:
        return
    with Env() as env:
        _new(env, box("b1"))
        _step([{"op": "add", "nodes": [box("b2")]}], "second box")
        _step([{"op": "set", "id": "b1", "params": {"height": 0.1}}], "taller")
        tools.design_undo()
        h = tools.design_history()
        lines = h.splitlines()
        assert "head=2/3" in lines[0]
        assert any(l.startswith(">  2  second box") for l in lines)
        assert any("taller" in l and "[undone" in l for l in lines)
        assert "current nodes: b1(box), b2(box)" in h and "redo available: 1 step(s)" in h
        assert h.rstrip().endswith("step=2 head=2/3")
        assert tools.design_history("missing").startswith("FAILED | NO_DESIGN")


def test_no_design_yet_is_a_clear_failure_and_adopt_builds_on_the_existing_tree():
    if not _DEPS:
        return
    with Env() as env:
        assert tools.design_step("[]").startswith("FAILED | NO_DESIGN")
        assert tools.design_undo().startswith("FAILED | NO_DESIGN")
        env.port.tree.extend(["Sketch1", "Boss-Extrude1"])                  # a part the user already has
        env.active = "Mine"
        from solidpilot.tools import graph as graph_tools
        text, ok, sv = graph_tools._run_graph(graph(box("b1")), False, kind="rebuild_from_ir")
        assert ok and "design_id=" in text and len(env.port.tree) == 4
        assert env.port.tree[:2] == ["Sketch1", "Boss-Extrude1"]
        assert not any(c[0].startswith("open_new") for c in env.raw_calls)
        d = design.get()
        assert d.base_count == 2 and d.doc_title == "Mine"
        assert tools.design_undo().startswith("COMPLETED | now at step 0") and env.port.tree == ["Sketch1", "Boss-Extrude1"]


def test_failed_initial_graph_leaves_an_empty_design_at_step_zero():
    if not _DEPS:
        return
    with Env() as env:
        out = tools.design_new(graph=json.dumps(graph(box("b1", FAIL_HEIGHT))))
        assert out.startswith("FAILED | STEP_FAILED") and "step=0 head=0/0" in out
        assert env.port.tree == [] and design.get().head == 0


def test_invalid_initial_graph_opens_no_document():
    if not _DEPS:
        return
    with Env() as env:
        out = tools.design_new(graph=json.dumps({"units": "meters", "nodes": [{"id": "x", "type": "nope"}]}))
        assert out.startswith("FAILED | VALIDATION_FAILED") and env.raw_calls == []
        assert tools.design_new(graph="{").startswith("FAILED | INVALID_JSON")
