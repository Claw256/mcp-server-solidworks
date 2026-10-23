"""Adapter-side bridge from the deterministic pycompiler to the LIVE execution layer.

This is the only place that wires the two together. It:
  - imports the pure pycompiler package — preferring an installed copy (the package is
    pip-installable via compiler/solidworks/pyproject.toml, ideally `pip install -e` so the
    installed copy IS the repo copy), falling back to a sys.path insert of the repo tree
    (the package lives one level below its backend dir, so it can't be imported by path
    name alone);
  - implements pycompiler's ExecutionPort over the EXISTING execution_client — the SAME
    /api/tool/execute + /state endpoints and the SAME idempotency / state_version machinery the
    low-level tools use (nothing forked, per the additive constraint).

pycompiler itself imports nothing from the adapter (no MCP SDK / httpx / server.py) — that one-way
dependency is what keeps the compiler relocatable to a standalone service later (IR-ADR-003).
"""
import os
import sys

try:
    from pycompiler.compiler import compile_and_run
    from pycompiler.execution_port import ExecutionPort
except ImportError:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _COMPILER_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "compiler", "solidworks"))
    if _COMPILER_ROOT not in sys.path:
        sys.path.insert(0, _COMPILER_ROOT)
    from pycompiler.compiler import compile_and_run
    from pycompiler.execution_port import ExecutionPort

from execution_client import call_tool, get_state          # noqa: E402
from solidpilot import jobs, runs                                      # noqa: E402
from solidpilot.bridge import is_state_mismatch as _is_state_mismatch   # noqa: E402  (single source of truth)


class AdapterExecutionPort(ExecutionPort):
    """Concrete ExecutionPort over the live execution REST layer.

    Owns transport robustness: one resync+retry on INVALID_STATE_VERSION (mirrors bridge._execute),
    then returns the final response so the compiler threads the resulting state_version. A fresh
    operation_id per sub-op keeps each individually idempotent on the server's OperationGuard.
    """

    def execute(self, tool, params, state_version):
        jobs.checkpoint(tool)                  # progress for job_status; raises JobCancelled on a cancel request
        resp = call_tool(tool, runs.operation_id(tool), state_version, params)
        if _is_state_mismatch(resp):
            fresh = get_state()
            resp = call_tool(tool, runs.operation_id(tool), fresh, params)
        return resp

    def get_state(self):
        return get_state()

    def feature_names(self):
        """Ordered user-feature names of the active document (execution tool `feature_tree`)."""
        resp = self.execute("feature_tree", {}, get_state())
        if (resp or {}).get("status") != "COMPLETED":
            raise RuntimeError("feature_tree failed: %s" % ((resp or {}).get("error") or resp))
        return list(((resp.get("cadState") or {}).get("features")) or [])

    def tree_size(self):
        return len(self.feature_names())

    def truncate(self, keep_count):
        """Delete user features beyond the first keep_count; returns the NEW state_version."""
        resp = self.execute("truncate_feature_tree", {"keep_count": int(keep_count)}, get_state())
        if (resp or {}).get("status") != "COMPLETED":
            raise RuntimeError("truncate_feature_tree failed: %s" % ((resp or {}).get("error") or resp))
        return resp.get("stateVersion")


def run_feature_graph(graph, start_index=0, seed_node_features=None, apply_material=None, port=None):
    """Compile + run a Feature Graph IR (dict) against the live execution layer.

    Returns a pycompiler CompileResult. start_index / seed_node_features / apply_material run only
    the tail of the graph (see compile_and_run); `port` substitutes the execution port (tests).
    Does NOT touch the bridge's state_version -- the caller (solidpilot.design) resyncs it afterwards
    via GET /state.
    """
    return compile_and_run(port or AdapterExecutionPort(), graph, start_index=start_index,
                           seed_node_features=seed_node_features, apply_material=apply_material)
