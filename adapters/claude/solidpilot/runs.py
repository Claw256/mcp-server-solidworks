"""Run records: make a build reproducible and its logs correlatable.

A RUN is one Feature Graph execution (design_new/design_step/undo/redo, rebuild_from_ir, analyze_drawing(build),
replay_run). While a run is active:
  * every operation id is DERIVED (uuid5 of run_id + sequence), not random, so the C# execution log, the
    adapter log and the manifest all name the same operations, and re-sending an operation inside a run
    is recognisable;
  * every adapter log line carries the run id.
When it ends, a MANIFEST is written to `<runs dir>/<run_id>.json`: the graph itself (so the run can be
replayed), its hash, the environment it ran in, the compiler's per-node log and the ordered operations.

The runs directory defaults to `adapters/claude/.solidpilot/runs` (git-ignored); SOLIDPILOT_RUNS_DIR
overrides it. Manifest writing never raises: a build must not fail because a record could not be saved.
"""
import contextvars
import hashlib
import json
import os
import platform
import sys
import uuid
from datetime import datetime, timezone

from solidpilot.paths import ADAPTER_DIR

_NAMESPACE = uuid.UUID("5c0a1a52-6f0e-4a4b-9a43-1d6e0f3a9e11")
_current: contextvars.ContextVar = contextvars.ContextVar("solidpilot_run", default=None)


def runs_dir() -> str:
    return os.environ.get("SOLIDPILOT_RUNS_DIR") or os.path.join(ADAPTER_DIR, ".solidpilot", "runs")


def graph_sha256(graph) -> str:
    """Stable over key order, so the same graph always hashes the same."""
    blob = json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Run:
    def __init__(self, kind, graph, source=None):
        self.run_id = uuid.uuid4().hex
        self.kind, self.graph, self.source = kind, graph, source
        self.graph_sha256 = graph_sha256(graph)
        self.started = datetime.now(timezone.utc)
        self.ops = []                                   # [{seq, tool, op_id}]
        self._token = None

    def operation_id(self, tool) -> str:
        seq = len(self.ops)
        op_id = str(uuid.uuid5(_NAMESPACE, "%s:%d" % (self.run_id, seq)))
        self.ops.append({"seq": seq, "tool": tool, "op_id": op_id})
        return op_id


def begin(kind, graph, source=None) -> Run:
    run = Run(kind, graph, source)
    run._token = _current.set(run)
    return run


def current():
    return _current.get()


def run_id() -> str:
    r = _current.get()
    return r.run_id if r else ""


def operation_id(tool="?") -> str:
    """The id for the next operation: derived inside a run, random outside one."""
    r = _current.get()
    return r.operation_id(tool) if r else str(uuid.uuid4())


def _environment(session) -> dict:
    try:
        import mcp
        mcp_version = getattr(mcp, "__version__", None)
    except Exception:                                    # noqa: BLE001
        mcp_version = None
    try:
        from importlib import metadata
        mcp_version = mcp_version or metadata.version("mcp")
    except Exception:                                    # noqa: BLE001
        pass
    env = {"python": sys.version.split()[0], "platform": platform.platform(), "mcp": mcp_version}
    env.update({k: v for k, v in (session or {}).items() if v is not None})
    return env


def end(run, status, summary, compile_result=None, state_version=None, session=None):
    """Close the run and write its manifest. -> the manifest path, or None if it could not be written."""
    try:
        if run._token is not None:
            _current.reset(run._token)
    except Exception:                                    # noqa: BLE001
        # begin() and end() ran in different contexts: the token cannot be used. A finished run must not
        # stay current, or later unrelated calls would derive their ids from it and be logged under it.
        _current.set(None)
    manifest = {
        "run_id": run.run_id, "kind": run.kind, "source": run.source,
        "started_utc": run.started.isoformat(), "ended_utc": datetime.now(timezone.utc).isoformat(),
        "status": status, "summary": summary,
        "graph_sha256": run.graph_sha256, "graph": run.graph,
        "environment": _environment(session),
        "state_version_after": state_version,
        "ops": run.ops, "compile": compile_result,
    }
    try:
        os.makedirs(runs_dir(), exist_ok=True)
        path = os.path.join(runs_dir(), run.run_id + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1, ensure_ascii=False, default=str)
        return path
    except Exception:                                    # noqa: BLE001 - a record must never fail a build
        return None


def load(run_id_value):
    """-> (manifest, error). Verifies the stored graph still matches its recorded hash."""
    if not run_id_value or any(c in run_id_value for c in "/\\."):
        return None, "run_id must be the plain id printed by the build (no path)"
    path = os.path.join(runs_dir(), run_id_value + ".json")
    if not os.path.isfile(path):
        return None, "no run %s in %s" % (run_id_value, runs_dir())
    try:
        with open(path, "r", encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError) as ex:
        return None, "manifest unreadable: %s" % ex
    if graph_sha256(m.get("graph")) != m.get("graph_sha256"):
        return None, "manifest graph does not match its recorded sha256 — the record was edited"
    return m, None


def recent(limit=10):
    """Newest manifests first, as one-line dicts."""
    d = runs_dir()
    if not os.path.isdir(d):
        return []
    files = sorted((f for f in os.listdir(d) if f.endswith(".json")),
                   key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            with open(os.path.join(d, f), "r", encoding="utf-8") as fh:
                m = json.load(fh)
            out.append({"run_id": m["run_id"], "kind": m["kind"], "status": m["status"],
                        "ended_utc": m["ended_utc"], "nodes": len((m.get("graph") or {}).get("nodes") or []),
                        "source": m.get("source")})
        except Exception:                                # noqa: BLE001
            continue
    return out
