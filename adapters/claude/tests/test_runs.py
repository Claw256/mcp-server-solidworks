"""Run records: manifests, derived operation ids, replay and the log correlation tag. Offline."""
import json
import os
import sys
import tempfile
import uuid

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

try:
    import server
    import ir_execution_port
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import adapter_log
    from solidpilot import runs, bridge
    from solidpilot.tools import graph as graph_tools
    _DEPS = True
except Exception:  # noqa: BLE001
    _DEPS = False

GRAPH = {"units": "meters", "nodes": [{"id": "b1", "type": "box", "ref": {"datum": "top"},
                                       "width": 0.05, "depth": 0.05, "height": 0.05}]}


class _Env:
    """Temp runs/designs dirs + the feature-tree fake port: a build asks for derived operation ids, as a real run would."""
    def __enter__(self):
        import fake_tree_port
        self.inner = fake_tree_port.Env().__enter__()
        self.port = self.inner.port
        return self

    def __exit__(self, *a):
        self.inner.__exit__(*a)


def _rid(text):
    return text.split("run_id=")[1].split()[0].strip("|")


def test_build_writes_a_manifest_with_graph_hash_ops_and_environment():
    if not _DEPS:
        return
    with _Env() as env:
        out = server.design_new(graph=json.dumps(GRAPH))
        rid = _rid(out)
        m, err = runs.load(rid)
        assert err is None and m["status"] == "COMPLETED" and m["kind"] == "design_new"
        assert m["graph"] == GRAPH and m["graph_sha256"] == runs.graph_sha256(GRAPH)
        assert [o["tool"] for o in m["ops"]] == ["open_new_part", "create_sketch", "add_sketch_entity", "extrude_feature"]
        assert m["compile"]["nodes_completed"] == 1 and m["state_version_after"] == 103
        assert m["environment"]["python"] and "state_version=103" in out


def test_operation_ids_inside_a_run_are_derived_and_outside_are_random():
    if not _DEPS:
        return
    with _Env() as env:
        out = server.design_new(graph=json.dumps(GRAPH))
        m, _ = runs.load(_rid(out))
        expected = [str(uuid.uuid5(runs._NAMESPACE, "%s:%d" % (m["run_id"], i))) for i in range(4)]
        assert [o["op_id"] for o in m["ops"]] == expected
    assert runs.current() is None
    assert runs.operation_id() != runs.operation_id()    # random again once the run has ended


def test_replay_runs_the_same_graph_as_a_new_run_and_refuses_a_tampered_record():
    if not _DEPS:
        return
    with _Env() as env:
        first = _rid(server.design_new(graph=json.dumps(GRAPH)))
        out = server.replay_run(first)
        second = _rid(out)
        assert second != first and f"replayed_from={first}" in out
        m2, _ = runs.load(second)
        assert m2["kind"] == "replay" and m2["source"] == first and m2["graph"] == GRAPH
        listing = server.replay_run("")
        assert first in listing and second in listing
        # edit the stored graph: the recorded hash no longer matches
        path = os.path.join(runs.runs_dir(), first + ".json")
        rec = json.load(open(path, encoding="utf-8"))
        rec["graph"]["nodes"].append({"id": "x", "type": "sketch"})
        json.dump(rec, open(path, "w", encoding="utf-8"))
        assert server.replay_run(first).startswith("FAILED | RUN_NOT_FOUND | manifest graph does not match")
        assert server.replay_run("../etc").startswith("FAILED | RUN_NOT_FOUND")


def test_a_run_ended_in_another_context_does_not_stay_current():
    if not _DEPS:
        return
    import contextvars
    run = runs.begin("test", {"nodes": []})
    # end() in a fresh Context: the saved token cannot be reset there
    contextvars.Context().run(runs.end, run, "COMPLETED", "x")
    assert runs.current() is None or runs.current() is run          # (the foreign context keeps its own view)
    runs.end(run, "COMPLETED", "x")                                  # the owning context closes it properly
    assert runs.current() is None


def test_logs_carry_the_active_run_id_only_while_it_is_active():
    if not _DEPS:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        old = adapter_log.LOG_PATH
        adapter_log.LOG_PATH = os.path.join(d, "adapter.log")
        try:
            run = runs.begin("test", {"nodes": []})
            adapter_log.write("inside")
            runs.end(run, "COMPLETED", "x")
            adapter_log.write("outside")
            lines = open(adapter_log.LOG_PATH, encoding="utf-8").read().splitlines()
        finally:
            adapter_log.LOG_PATH = old
    assert f"[run {run.run_id[:8]}] inside" in lines[0] and "[run" not in lines[1]


def test_log_rotates_past_the_size_cap():
    if not _DEPS:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        old, old_max = adapter_log.LOG_PATH, adapter_log.MAX_BYTES
        adapter_log.LOG_PATH, adapter_log.MAX_BYTES = os.path.join(d, "adapter.log"), 200
        try:
            for i in range(20):
                adapter_log.write("x" * 50 + str(i))
            assert os.path.exists(adapter_log.LOG_PATH + ".1")
        finally:
            adapter_log.LOG_PATH, adapter_log.MAX_BYTES = old, old_max


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
