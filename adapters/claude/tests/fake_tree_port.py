"""Shared offline fakes for the design-session tests: a feature-tree-simulating ExecutionPort and an
environment that wires it (plus a fake bridge) into solidpilot.design."""
import os
import sys
import tempfile

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import ir_execution_port                       # noqa: E402,F401  (puts the compiler tree on sys.path)
from pycompiler.execution_port import ExecutionPort   # noqa: E402
from solidpilot import bridge, design, jobs, runs      # noqa: E402

FAIL_HEIGHT = 0.123          # a box with this height fails to extrude


def box(nid, h=0.05, w=0.05):
    return {"id": nid, "type": "box", "ref": {"datum": "top"}, "width": w, "depth": w, "height": h}


def mirror(nid, of):
    return {"id": nid, "type": "mirror", "plane": {"datum": "front"}, "features": [of]}


def graph(*nodes, **extra):
    g = {"units": "meters", "nodes": list(nodes)}
    g.update(extra)
    return g


class FakeTreePort(ExecutionPort):
    """Simulates a SolidWorks feature tree: create_sketch/extrude_feature/create_pattern append names
    (numbered per prefix, like SolidWorks), truncate() deletes the tail."""

    def __init__(self):
        self.tree = []
        self.sv = 0
        self.calls = []

    def _name(self, prefix):
        return "%s%d" % (prefix, sum(1 for n in self.tree if n.startswith(prefix)) + 1)

    def execute(self, tool, params, state_version):
        jobs.checkpoint(tool)
        runs.operation_id(tool)
        self.calls.append((tool, dict(params)))
        self.sv += 1
        prefix = {"create_sketch": "Sketch", "extrude_feature": "Boss-Extrude",
                  "create_pattern": "Mirror"}.get(tool)
        if tool == "extrude_feature" and params.get("depth") == FAIL_HEIGHT:
            return {"status": "FAILED", "stateVersion": self.sv,
                    "error": {"code": "EXTRUSION_FAILED", "message": "injected failure"}}
        resp = {"status": "COMPLETED", "stateVersion": self.sv, "cadState": {"features": []}}
        if prefix:
            name = self._name(prefix)
            self.tree.append(name)
            resp["cadState"]["features"] = [name]
            if tool == "create_sketch":
                resp["cadState"]["activeSketch"] = name
        return resp

    def get_state(self):
        return self.sv

    def feature_names(self):
        return list(self.tree)

    def tree_size(self):
        return len(self.tree)

    def truncate(self, keep_count):
        self.calls.append(("truncate", {"keep_count": keep_count}))
        del self.tree[keep_count:]
        self.sv += 1
        return self.sv

    def tools(self):
        return [c[0] for c in self.calls]


class Env:
    """Temp runs/designs dirs, a FakeTreePort behind design.make_port and a fake bridge."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.saved_env = {k: os.environ.get(k) for k in ("SOLIDPILOT_RUNS_DIR", "SOLIDPILOT_DESIGNS_DIR")}
        os.environ["SOLIDPILOT_RUNS_DIR"] = os.path.join(self.tmp.name, "runs")
        os.environ["SOLIDPILOT_DESIGNS_DIR"] = os.path.join(self.tmp.name, "designs")
        self.port = FakeTreePort()
        self.raw_calls = []
        self.active = None
        self.real = (bridge._call_raw, bridge._resync_state_version, design.make_port)

        def raw(tool, params):
            runs.operation_id(tool)
            self.raw_calls.append((tool, dict(params)))
            if tool in ("open_new_part", "open_new_assembly"):
                self.port.tree.clear()
                self.active = "Part%d" % sum(1 for c in self.raw_calls if c[0].startswith("open_new"))
                return {"status": "COMPLETED", "cadState": {"activeDocument": self.active}}
            if tool == "activate_document":
                self.active = params["title"]
                return {"status": "COMPLETED", "cadState": {"activeDocument": self.active}}
            if tool == "verify_state":
                return {"status": "COMPLETED", "cadState": {"activeDocument": self.active}}
            return {"status": "COMPLETED"}
        bridge._call_raw = raw
        bridge._resync_state_version = lambda: 100 + self.port.sv
        design.make_port = lambda: self.port
        design._reset_memory()
        return self

    def __exit__(self, *a):
        bridge._call_raw, bridge._resync_state_version, design.make_port = self.real
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        design._reset_memory()
        self.tmp.cleanup()
