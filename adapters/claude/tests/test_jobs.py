"""Background jobs: start, poll, cancel, one-at-a-time, and refusing CAD calls that would interleave."""
import os
import sys
import threading
import time

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

try:
    import server
    from solidpilot import bridge, jobs
    from solidpilot.tools import graph as graph_tools
    _DEPS = True
except Exception:  # noqa: BLE001
    _DEPS = False


def _wait_idle():
    for _ in range(400):
        if jobs.active() is None:
            return
        time.sleep(0.01)


def test_a_job_runs_in_the_background_and_reports_its_result():
    if not _DEPS:
        return
    _wait_idle()
    started = jobs.launch("unit", lambda: "COMPLETED | built 3/3")
    assert started.startswith("JOB_STARTED | job_id=")
    jid = started.split("job_id=")[1].split()[0]
    out = server.job_status(jid, wait_s=5)
    assert out.startswith("COMPLETED | job_id=" + jid) and "built 3/3" in out


def test_progress_is_reported_while_running_and_only_one_job_runs_at_a_time():
    if not _DEPS:
        return
    _wait_idle()
    release = threading.Event()

    def work():
        jobs.checkpoint("create_sketch")
        jobs.checkpoint("extrude_feature")
        release.wait(5)
        return "COMPLETED | done"
    jid = jobs.launch("unit", work).split("job_id=")[1].split()[0]
    for _ in range(200):
        if "ops=2" in jobs.status(jid, wait_s=0):
            break
        time.sleep(0.01)
    running = jobs.status(jid, wait_s=0)
    assert running.startswith("RUNNING | job_id=" + jid) and "ops=2" in running and "last_tool=extrude_feature" in running
    assert jobs.launch("other", lambda: "x").startswith("JOB_BUSY | job " + jid)
    release.set()
    assert jobs.status(jid, wait_s=5).startswith("COMPLETED")


def test_other_threads_cannot_send_cad_calls_while_a_job_runs_but_the_job_can():
    if not _DEPS:
        return
    _wait_idle()
    sent, release = [], threading.Event()
    real_call, real_state = bridge.call_tool, bridge.state.version
    bridge.call_tool = lambda tool, op, sv, params: (sent.append(tool) or {"status": "COMPLETED", "stateVersion": 1})

    def work():
        bridge._call_raw("create_sketch", {})                  # the job's own thread is exempt
        release.wait(5)
        return "COMPLETED | ok"
    try:
        jid = jobs.launch("unit", work).split("job_id=")[1].split()[0]
        for _ in range(200):
            if sent:
                break
            time.sleep(0.01)
        assert sent == ["create_sketch"]
        try:
            bridge._call_raw("extrude_feature", {})            # this (test) thread is NOT the job thread
            assert False, "expected the call to be refused"
        except bridge.CadOperationError as ex:
            assert "background build is running" in str(ex) and jid in str(ex)
        assert sent == ["create_sketch"]                       # nothing leaked through
        release.set()
        assert jobs.status(jid, wait_s=5).startswith("COMPLETED")
        bridge._call_raw("extrude_feature", {})                # allowed again once the job is over
        assert sent[-1] == "extrude_feature"
    finally:
        release.set()
        bridge.call_tool, bridge.state.version = real_call, real_state


def test_cancel_stops_the_job_before_its_next_operation():
    if not _DEPS:
        return
    _wait_idle()

    def work():
        for _ in range(500):
            jobs.checkpoint("add_sketch_entity")
            time.sleep(0.01)
        return "COMPLETED | should not get here"
    jid = jobs.launch("unit", work).split("job_id=")[1].split()[0]
    time.sleep(0.05)
    out = jobs.status(jid, wait_s=5, cancel=True)
    assert out.startswith("CANCELLED | job_id=" + jid) and "partial geometry may remain" in out


def test_a_crashing_job_ends_as_failed_with_the_reason():
    if not _DEPS:
        return
    _wait_idle()

    def work():
        raise RuntimeError("COM exploded")
    out = jobs.status(jobs.launch("unit", work).split("job_id=")[1].split()[0], wait_s=5)
    assert out.startswith("FAILED | job_id=") and "JOB_CRASHED | RuntimeError: COM exploded" in out
    assert jobs.status("nope").startswith("FAILED | JOB_NOT_FOUND")


def test_submit_feature_graph_background_starts_a_job_and_the_result_arrives_via_job_status():
    if not _DEPS:
        return
    _wait_idle()
    real = graph_tools._run_graph
    graph_tools._run_graph = lambda g, fresh, **kw: ("COMPLETED | IR built 1/1 nodes | run_id=abc", True, 4)
    try:
        started = server.submit_feature_graph('{"nodes": []}', background=True)
        assert started.startswith("JOB_STARTED")
        jid = started.split("job_id=")[1].split()[0]
        out = server.job_status(jid, wait_s=5)
    finally:
        graph_tools._run_graph = real
    assert out.startswith("COMPLETED | job_id=" + jid) and "IR built 1/1 nodes" in out and "state_version=4" in out
    assert server.submit_feature_graph("not json", background=True).startswith("FAILED | INVALID_JSON")


def test_cancel_through_the_compile_path_is_not_swallowed_and_is_recorded_as_cancelled():
    """JobCancelled must get past the broad `except Exception` handlers and end the run as CANCELLED."""
    if not _DEPS:
        return
    import tempfile
    import ir_execution_port
    from solidpilot import runs
    _wait_idle()
    started = threading.Event()

    def fake_compile(graph):
        started.set()
        try:
            for _ in range(500):
                jobs.checkpoint("add_sketch_entity")           # what AdapterExecutionPort.execute does per op
                time.sleep(0.01)
        except Exception:                                      # a compiler-style catch-all must NOT see the cancel
            return type("R", (), {"status": "FAILED", "summary": lambda self: "FAILED | swallowed",
                                  "to_dict": lambda self: {}})()
        raise AssertionError("never cancelled")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        old_env = os.environ.get("SOLIDPILOT_RUNS_DIR")
        os.environ["SOLIDPILOT_RUNS_DIR"] = d
        real_run, real_raw, real_resync = ir_execution_port.run_feature_graph, bridge._call_raw, bridge._resync_state_version
        ir_execution_port.run_feature_graph = fake_compile
        bridge._call_raw = lambda tool, params: {"status": "COMPLETED"}
        bridge._resync_state_version = lambda: 21
        try:
            jid = server.submit_feature_graph('{"nodes": []}', background=True).split("job_id=")[1].split()[0]
            assert started.wait(5)
            out = jobs.status(jid, wait_s=5, cancel=True)
            rows = runs.recent()
        finally:
            ir_execution_port.run_feature_graph, bridge._call_raw, bridge._resync_state_version = real_run, real_raw, real_resync
            if old_env is None:
                os.environ.pop("SOLIDPILOT_RUNS_DIR", None)
            else:
                os.environ["SOLIDPILOT_RUNS_DIR"] = old_env
    assert out.startswith("CANCELLED | job_id=" + jid), out
    assert rows and rows[0]["status"] == "CANCELLED"


def test_job_status_is_read_only_in_the_annotations():
    if not _DEPS:
        return
    tool = {t.name: t for t in server.mcp._tool_manager.list_tools()}["job_status"]
    assert tool.annotations.read_only_hint is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
