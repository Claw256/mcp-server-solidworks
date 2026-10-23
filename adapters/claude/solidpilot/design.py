"""Design sessions: an incremental, undoable way to build a part or assembly.

A DESIGN is an ordered Feature Graph node list (the IR) plus a JOURNAL of snapshots of that list.
Every operation is "reconcile the live document to a target node list":

  * step  = apply a patch to the head snapshot -> new snapshot (redo entries are discarded);
  * undo / redo = move the head pointer to an existing snapshot.

reconcile() validates the WHOLE target first (an invalid target changes nothing), finds the longest
common prefix between the live nodes and the target, truncates the SolidWorks feature tree back to
the boundary recorded after the last common node, and compiles only the remaining suffix with the
prefix's runtime feature names seeded in. A pure append therefore replays nothing. If the new suffix
fails, the tree is truncated back and the OLD suffix is rebuilt, so a failed step leaves the design
as it was; only a failed restore is DESIGN_CORRUPT.

The journal is persisted to `<designs dir>/<design_id>.json` (SOLIDPILOT_DESIGNS_DIR overrides; the
default is the `designs` sibling of the runs directory). Persisting never raises: a build must not
fail because a record could not be saved.
"""
import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from solidpilot import bridge
from solidpilot import jobs
from solidpilot import runs

ASSEMBLY_TYPES = ("component", "mate")


# ---------------------------------------------------------------------------
# Persistence + registry
# ---------------------------------------------------------------------------
def designs_dir() -> str:
    return os.environ.get("SOLIDPILOT_DESIGNS_DIR") or os.path.join(os.path.dirname(runs.runs_dir()), "designs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Design:
    def __init__(self, design_id, kind, doc_title, label="", base_names=None, graph_meta=None):
        self.design_id = design_id
        self.kind = kind
        self.doc_title = doc_title
        self.label = label
        self.base_count = len(base_names or [])
        self.graph_meta = dict(graph_meta or {})
        self.created = self.updated = _now()
        self.head = 0
        # snapshot 0 = the document as the design found it (blank, or the adopted tree)
        self.snapshots = [{"step": 0, "label": "start", "patch": None, "nodes": [], "material": None,
                           "node_features": {}, "node_info": {}, "tree": list(base_names or []),
                           "created": self.created}]

    @property
    def total(self):
        return len(self.snapshots) - 1

    def live(self):
        return self.snapshots[self.head]

    def to_dict(self):
        return {"design_id": self.design_id, "kind": self.kind, "doc_title": self.doc_title,
                "label": self.label, "base_count": self.base_count, "graph_meta": self.graph_meta,
                "created_utc": self.created, "updated_utc": self.updated, "head": self.head,
                "snapshots": self.snapshots}

    @classmethod
    def from_dict(cls, d):
        o = cls(d["design_id"], d["kind"], d["doc_title"], d.get("label", ""), [], d.get("graph_meta"))
        o.base_count = d.get("base_count", 0)
        o.created, o.updated = d.get("created_utc", o.created), d.get("updated_utc", o.updated)
        o.head, o.snapshots = d["head"], d["snapshots"]
        return o


_designs: dict = {}
_current_id = None
_lock = threading.RLock()


def _safe_id(design_id):
    return bool(design_id) and not any(c in design_id for c in "/\\.")


def save(design) -> None:
    design.updated = _now()
    try:
        os.makedirs(designs_dir(), exist_ok=True)
        path = os.path.join(designs_dir(), design.design_id + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(design.to_dict(), fh, indent=1, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception:                                    # noqa: BLE001 - a record must never fail a build
        pass


def _load(design_id):
    if not _safe_id(design_id):
        return None
    path = os.path.join(designs_dir(), design_id + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return Design.from_dict(json.load(fh))
    except (OSError, ValueError, KeyError):
        return None


def register(design) -> None:
    global _current_id
    with _lock:
        _designs[design.design_id] = design
        _current_id = design.design_id
    save(design)


def get(design_id=""):
    """The named design, or (blank) the current one: the most recently used, else the newest on disk."""
    global _current_id
    with _lock:
        design_id = (design_id or "").strip()
        if design_id:
            d = _designs.get(design_id) or _load(design_id)
            if d is not None:
                _designs[design_id] = d
            return d
        if _current_id and _current_id in _designs:
            return _designs[_current_id]
        try:
            files = [f for f in os.listdir(designs_dir()) if f.endswith(".json")]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(designs_dir(), f)), reverse=True)
        except OSError:
            return None
        for f in files:
            d = _load(f[:-5])
            if d is not None:
                _designs[d.design_id] = d
                _current_id = d.design_id
                return d
        return None


def _touch(design) -> None:
    global _current_id
    _current_id = design.design_id


def _reset_memory() -> None:
    """Forget every in-memory design (tests: simulate a process restart; the journal stays on disk)."""
    global _current_id
    with _lock:
        _designs.clear()
        _current_id = None


# ---------------------------------------------------------------------------
# Patch ops
# ---------------------------------------------------------------------------
class PatchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


_OP_KEYS = {"add": {"op", "nodes", "after"}, "replace": {"op", "id", "node"}, "set": {"op", "id", "params"},
            "remove": {"op", "id"}, "material": {"op", "material"}}


def parse_patch(patch):
    """JSON text (or an already-parsed value) -> list of op dicts. Raises PatchError."""
    if isinstance(patch, str):
        try:
            patch = json.loads(patch)
        except ValueError as ex:
            raise PatchError("INVALID_JSON", "the patch is not valid JSON: %s" % ex)
    if isinstance(patch, dict):
        patch = [patch]
    if not isinstance(patch, list) or not patch:
        raise PatchError("PATCH_INVALID", "the patch must be a non-empty JSON list of ops "
                         "[{op: add|replace|set|remove|material, ...}]")
    return patch


def apply_patch(nodes, material, ops):
    """(nodes, material, ops) -> (new_nodes, new_material). Pure; raises PatchError. The result is
    NOT validated here — reconcile validates the whole target."""
    nodes = copy.deepcopy(nodes)
    material = copy.deepcopy(material)

    def find(nid, where):
        for k, n in enumerate(nodes):
            if isinstance(n, dict) and n.get("id") == nid:
                return k
        raise PatchError("NODE_NOT_FOUND", "%s: no node with id %r (current ids: %s)"
                         % (where, nid, ", ".join(str(n.get("id")) for n in nodes) or "none"))

    for k, op in enumerate(ops):
        where = "patch op #%d" % (k + 1)
        if not isinstance(op, dict) or op.get("op") not in _OP_KEYS:
            raise PatchError("PATCH_INVALID", "%s: 'op' must be one of add, replace, set, remove, material "
                             "(got %r)" % (where, op.get("op") if isinstance(op, dict) else op))
        name = op["op"]
        extra = sorted(set(op) - _OP_KEYS[name])
        if extra:
            raise PatchError("PATCH_INVALID", "%s (%s): unknown key(s) %s; allowed: %s"
                             % (where, name, extra, sorted(_OP_KEYS[name])))
        if name == "add":
            new = op.get("nodes")
            if not isinstance(new, list) or not new or not all(isinstance(n, dict) for n in new):
                raise PatchError("PATCH_INVALID", "%s (add): 'nodes' must be a non-empty list of node objects" % where)
            at = len(nodes) if op.get("after") in (None, "") else find(op["after"], where) + 1
            nodes[at:at] = copy.deepcopy(new)
        elif name == "replace":
            node = op.get("node")
            if not isinstance(node, dict):
                raise PatchError("PATCH_INVALID", "%s (replace): 'node' must be a node object" % where)
            at = find(op.get("id"), where)
            node = copy.deepcopy(node)
            node.setdefault("id", op["id"])
            nodes[at] = node
        elif name == "set":
            params = op.get("params")
            if not isinstance(params, dict) or not params:
                raise PatchError("PATCH_INVALID", "%s (set): 'params' must be a non-empty object" % where)
            if "id" in params:
                raise PatchError("PATCH_INVALID", "%s (set): a node's id cannot be changed with set; use "
                                 "replace" % where)
            nodes[find(op.get("id"), where)].update(copy.deepcopy(params))
        elif name == "remove":
            del nodes[find(op.get("id"), where)]
        else:                                             # material
            m = op.get("material")
            if m is not None and not isinstance(m, dict):
                raise PatchError("PATCH_INVALID", "%s (material): 'material' must be {name, library?} or null" % where)
            material = copy.deepcopy(m)
    return nodes, material


def patch_summary(patch):
    """Compact one-line description of a stored patch."""
    if not isinstance(patch, list):
        return "initial graph" if patch else ""
    bits = []
    for op in patch:
        if not isinstance(op, dict):
            continue
        o = op.get("op")
        if o == "add":
            bits.append("+" + ",".join(str(n.get("id")) for n in op.get("nodes") or [] if isinstance(n, dict)))
        elif o == "replace":
            bits.append("~" + str(op.get("id")))
        elif o == "set":
            bits.append("~" + str(op.get("id")) + "{" + ",".join(sorted((op.get("params") or {}))) + "}")
        elif o == "remove":
            bits.append("-" + str(op.get("id")))
        elif o == "material":
            bits.append("material")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Compiler access (patched in tests)
# ---------------------------------------------------------------------------
def make_port():
    import ir_execution_port
    return ir_execution_port.AdapterExecutionPort()


def _compile(port, graph, **kw):
    import ir_execution_port
    return ir_execution_port.run_feature_graph(graph, port=port, **kw)


def _validate(graph):
    import ir_execution_port                     # noqa: F401  (puts the compiler tree on sys.path)
    from pycompiler.ir_schema import validate
    return validate(graph)


def _graph(design, nodes, material):
    g = dict(design.graph_meta)
    g.setdefault("units", "meters")
    g["nodes"] = nodes
    if material:
        g["material"] = material
    return g


def infer_kind(nodes):
    return "assembly" if any(isinstance(n, dict) and n.get("type") in ASSEMBLY_TYPES
                             for n in (nodes or [])) else "part"


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------
class Outcome:
    def __init__(self, ok, code, text, compile_result=None, data=None):
        self.ok, self.code, self.text, self.compile, self.data = ok, code, text, compile_result, data


def _fail(code, message, compile_result=None):
    return Outcome(False, code, "FAILED | %s | %s" % (code, message), compile_result)


def _boundary(snapshot, node):
    return ((snapshot.get("node_info") or {}).get((node or {}).get("id")) or {}).get("boundary")


def _merge_info(live_info, prefix_nodes, result):
    info = {n["id"]: live_info[n["id"]] for n in prefix_nodes if n.get("id") in live_info}
    for rec in result.node_log if result is not None else []:
        if rec.get("id") and rec.get("id") != "_material" and rec.get("status") == "COMPLETED":
            info[rec["id"]] = {"features": list(rec.get("features") or []), "boundary": rec.get("boundary")}
    return info


def _activate(design):
    resp = bridge._call_raw("activate_document", {"title": design.doc_title})
    if resp.get("status") != "COMPLETED":
        err = resp.get("error") or {}
        return _fail("DOCUMENT_UNAVAILABLE",
                     "cannot activate the design's document %r (%s: %s) — it was closed or renamed. "
                     "Open it again, or start over with design_new and replay the nodes from design_history."
                     % (design.doc_title, err.get("code"), err.get("message")))
    title = (resp.get("cadState") or {}).get("activeDocument")
    if title:
        design.doc_title = title
    return None


def reconcile(design, target, material, activate=True):
    """Bring the design's document to `target` nodes (+ `material`). Never raises for CAD failures.

    Success -> Outcome(ok, data={node_features, node_info, tree}); the caller commits the journal.
    A failed compile restores the previous suffix and returns code STEP_FAILED with the design
    unchanged (design.live() is refreshed with the restored names)."""
    live = design.live()
    ln = live["nodes"]
    graph = _graph(design, target, material)
    if target:                                               # 1) validate the WHOLE target first
        errors = _validate(graph)
        if errors:
            return _fail("VALIDATION_FAILED", "%s | no execution performed — the design is unchanged"
                         % "; ".join(errors))
    try:
        if activate:
            bad = _activate(design)
            if bad:
                return bad
        port = make_port()
        names = port.feature_names()
        if names != live["tree"]:                            # 2) drift: never clobber manual edits
            return _fail("DESIGN_DRIFT",
                         "the document's feature tree no longer matches the design journal (expected %d "
                         "features %s, found %d %s) — it was edited outside the design tools. No changes "
                         "made. Revert the manual edit, or start over with design_new and replay the "
                         "nodes shown by design_history."
                         % (len(live["tree"]), _short(live["tree"]), len(names), _short(names)))
        apply_mat = material is not None and material != live.get("material")
        if target == ln and not apply_mat:
            return Outcome(True, "NOOP", "COMPLETED | no change (the document already matches)",
                           data={"node_features": live["node_features"], "node_info": live["node_info"],
                                 "tree": names})
        # 3) longest common prefix, backed off to a safe boundary
        i = 0
        while i < min(len(ln), len(target)) and ln[i] == target[i]:
            i += 1
        while i > 0:
            prev = target[i - 1]
            if (i < len(target) and prev.get("type") == "sketch") or _boundary(live, prev) is None:
                i -= 1            # an open sketch cannot survive a truncation; no boundary = unknown
                continue
            break
        keep = design.base_count if i == 0 else _boundary(live, target[i - 1])
        seed = {n["id"]: live["node_features"][n["id"]] for n in target[:i]
                if n.get("id") in live["node_features"]}
        prefix = target[:i]
        truncated_from = None
        if len(names) > keep:                                # 4) truncate to the recorded boundary
            truncated_from = len(names)
            try:
                port.truncate(keep)
            except Exception as ex:                          # noqa: BLE001
                return _fail("DESIGN_CORRUPT", "could not truncate the feature tree to %d features (%s). The "
                             "document may be partly truncated; the next call will report DESIGN_DRIFT. Next "
                             "step: design_new, then replay the nodes from design_history." % (keep, ex))
        # 5) compile the suffix with the prefix's names seeded in
        result = None
        if i < len(target) or apply_mat:
            result = _compile(port, graph, start_index=i, seed_node_features=seed, apply_material=apply_mat)
        if result is None or result.status == "COMPLETED":
            data = {"node_features": dict(result.node_features) if result is not None else dict(seed),
                    "node_info": _merge_info(live["node_info"], prefix, result),
                    "tree": port.feature_names()}
            done = len(target)
            return Outcome(True, "OK", "COMPLETED | IR built %d/%d nodes | kept %d unchanged, built %d%s | exec_calls=%d"
                           % (done, done, i, done - i,
                              (", tree truncated %d -> %d features" % (truncated_from, keep)) if truncated_from else "",
                              result.exec_calls if result is not None else 0),
                           result.to_dict() if result is not None else None, data)
        # 6) failure: restore the old suffix so the design is unchanged
        err = result.error or {}
        why = ("%s | node %s (%s)%s [%s]: %s" % ("STEP_FAILED", err.get("node_id"), err.get("node_type"),
                                                 (" step=%s" % err["step"]) if err.get("step") else "",
                                                 err.get("code"), err.get("message")))
        if err.get("code") == "VALIDATION_FAILED":
            return _fail("VALIDATION_FAILED", "%s | no execution performed — the design is unchanged"
                         % err.get("message"), result.to_dict())
        restored = _restore(design, port, graph_old=_graph(design, ln, live.get("material")), ln=ln, i=i,
                            keep=keep, seed=seed, prefix=prefix)
        if restored is not None:
            return _fail("DESIGN_CORRUPT", "%s | and the previous state could NOT be restored (%s). Next step: "
                         "design_new, then replay the nodes from design_history." % (why, restored),
                         result.to_dict())
        return _fail("STEP_FAILED", why.split(" | ", 1)[1] + " | the design is unchanged (the previous "
                     "state was rebuilt); completed %d/%d new nodes before the failure"
                     % (result.nodes_completed, len(target) - i), result.to_dict())
    except jobs.JobCancelled:
        raise
    except Exception as ex:                                   # noqa: BLE001 - never crash the MCP server
        return _fail("UNEXPECTED", "%s: %s | the document may be partly changed; the next call will report "
                     "DESIGN_DRIFT if so" % (type(ex).__name__, ex))


def _short(names, n=6):
    return "[%s%s]" % (", ".join(names[:n]), ", ..." if len(names) > n else "")


def _restore(design, port, graph_old, ln, i, keep, seed, prefix):
    """Truncate back to the prefix boundary and rebuild the old suffix. -> None on success, else a reason."""
    live = design.live()
    try:
        if len(port.feature_names()) > keep:
            port.truncate(keep)
        res = None
        if i < len(ln):
            res = _compile(port, graph_old, start_index=i, seed_node_features=seed, apply_material=False)
            if res.status != "COMPLETED":
                return "%s: %s" % ((res.error or {}).get("code"), (res.error or {}).get("message"))
        live["node_features"] = dict(res.node_features) if res is not None else dict(seed)
        live["node_info"] = _merge_info(live["node_info"], prefix, res)
        live["tree"] = port.feature_names()
        save(design)
        return None
    except jobs.JobCancelled:
        raise
    except Exception as ex:                                   # noqa: BLE001
        return "%s: %s" % (type(ex).__name__, ex)


# ---------------------------------------------------------------------------
# Journal operations (each wrapped in a run manifest)
# ---------------------------------------------------------------------------
def _tail(design):
    return "design_id=%s step=%d head=%d/%d" % (design.design_id, design.head, design.head, design.total)


def _in_run(kind, graph_obj, source, fn):
    """Run fn() -> (text, ok, compile_dict|None, design|None) inside a run manifest; resync after.
    -> (text ending with the design tail and run_id=..., ok, state_version)."""
    run = runs.begin(kind, graph_obj, source)
    try:
        text, ok, compile_dict, design = fn()
    except jobs.JobCancelled:
        sv = bridge._resync_state_version()               # stopped between ops: adapter state is behind
        runs.end(run, "CANCELLED", "cancelled by request; partial geometry may remain", state_version=sv,
                 session=bridge.session)
        raise
    except BaseException:
        runs.end(run, "CRASHED", "exception escaped the design code", session=bridge.session)
        raise
    sv = bridge._resync_state_version()
    runs.end(run, "COMPLETED" if ok else "FAILED", (text.splitlines() or [""])[0][:300],
             compile_result=compile_dict, state_version=sv, session=bridge.session)
    if design is not None:
        text += " | " + _tail(design)
    return text + " | run_id=" + run.run_id, ok, sv


def _commit(design, snapshot, outcome):
    snapshot["node_features"] = outcome.data["node_features"]
    snapshot["node_info"] = outcome.data["node_info"]
    snapshot["tree"] = outcome.data["tree"]


def _step_core(design, nodes, material, patch, label, activate=True):
    out = reconcile(design, nodes, material, activate=activate)
    if not out.ok:
        save(design)
        return out.text, False, out.compile
    snap = {"step": design.head + 1, "label": label or patch_summary(patch) or "step", "patch": patch,
            "nodes": nodes, "material": material, "created": _now()}
    _commit(design, snap, out)
    del design.snapshots[design.head + 1:]                    # a new step discards the redo entries
    design.snapshots.append(snap)
    design.head += 1
    _touch(design)
    save(design)
    return out.text.replace("COMPLETED |", "COMPLETED | step %d |" % design.head, 1), True, out.compile


def step(design, patch, label="", kind="design_step"):
    """Apply a patch (JSON text or list) to the design's head snapshot. -> (text, ok, state_version)."""
    try:
        ops = parse_patch(patch)
        live = design.live()
        nodes, material = apply_patch(live["nodes"], live.get("material"), ops)
    except PatchError as ex:
        return ("FAILED | %s | %s | no execution performed — the design is unchanged | %s"
                % (ex.code, ex.message, _tail(design)), False, bridge.state.version)

    def fn():
        text, ok, comp = _step_core(design, nodes, material, ops, label)
        return text, ok, comp, design
    return _in_run(kind, _graph(design, nodes, material), None, fn)


def _move(design, delta, kind):
    n = abs(delta)
    if delta < 0:
        if design.head == 0:
            return ("FAILED | NOTHING_TO_UNDO | the design is at step 0 | %s" % _tail(design), False,
                    bridge.state.version)
        target = max(0, design.head - n)
    else:
        if design.head >= design.total:
            return ("FAILED | NOTHING_TO_REDO | no undone steps to redo | %s" % _tail(design), False,
                    bridge.state.version)
        target = min(design.total, design.head + n)
    snap = design.snapshots[target]

    def fn():
        out = reconcile(design, snap["nodes"], snap.get("material"))
        if not out.ok:
            save(design)
            return out.text, False, out.compile, design
        _commit(design, snap, out)
        design.head = target
        _touch(design)
        save(design)
        return (out.text.replace("COMPLETED |", "COMPLETED | now at step %d (%s) |"
                                 % (target, snap.get("label")), 1), True, out.compile, design)
    return _in_run(kind, _graph(design, snap["nodes"], snap.get("material")), None, fn)


def undo(design, steps=1):
    return _move(design, -max(1, int(steps)), "design_undo")


def redo(design, steps=1):
    return _move(design, max(1, int(steps)), "design_redo")


def _open_document(kind):
    new_tool = "open_new_assembly" if kind == "assembly" else "open_new_part"
    opened = bridge._call_raw(new_tool, {})
    if opened.get("status") != "COMPLETED":
        err = opened.get("error") or {}
        return None, "FAILED | %s_FAILED | %s: %s" % (new_tool.upper(), err.get("code"), err.get("message"))
    return (opened.get("cadState") or {}).get("activeDocument") or "", None


def new(kind="part", label="", graph_obj=None, fresh_document=True, run_kind="design_new", source=None):
    """Start a design (blank document, or adopt the active one) and, if graph_obj is given, apply it as
    step 1. -> (text, ok, state_version). A graph's own node types decide part vs assembly."""
    has_graph = isinstance(graph_obj, dict) and graph_obj.get("nodes")
    if has_graph:
        kind = infer_kind(graph_obj.get("nodes"))
    if kind not in ("part", "assembly"):
        return "FAILED | INVALID_KIND | kind must be 'part' or 'assembly'", False, bridge.state.version
    if has_graph:
        errors = _validate(graph_obj)                           # zero CAD changes if the graph is invalid
        if errors:
            return ("FAILED | VALIDATION_FAILED | %s | no execution performed" % "; ".join(errors), False,
                    bridge.state.version)

    def fn():
        try:
            port = make_port()
            if fresh_document:
                title, err = _open_document(kind)
                if err:
                    return err, False, None, None
                base = []
            else:
                state = bridge._call_raw("verify_state", {})
                title = (state.get("cadState") or {}).get("activeDocument")
                if state.get("status") != "COMPLETED" or not title:
                    return ("FAILED | NO_ACTIVE_DOCUMENT | there is no active document to adopt; use "
                            "fresh_document=True or open one first", False, None, None)
                base = port.feature_names()
        except jobs.JobCancelled:
            raise
        except Exception as ex:                                 # noqa: BLE001
            return "FAILED | UNEXPECTED | %s: %s" % (type(ex).__name__, ex), False, None, None
        meta = {k: v for k, v in (graph_obj or {}).items() if k not in ("nodes", "material")} if has_graph else {}
        meta.setdefault("units", "meters")
        design = Design(uuid.uuid4().hex[:12], kind, title, label, base, meta)
        register(design)
        head = "design started on %s %r (%d existing feature%s)" % (
            kind, title, len(base), "" if len(base) == 1 else "s")
        if not has_graph:
            return "COMPLETED | " + head, True, None, design
        text, ok, comp = _step_core(design, copy.deepcopy(graph_obj["nodes"]), graph_obj.get("material"),
                                    "initial graph", label or "initial graph", activate=False)
        return text, ok, comp, design
    return _in_run(run_kind, graph_obj if has_graph else {"nodes": []}, source, fn)


def build_graph(graph_obj, fresh_document, kind="graph", source=None, label=""):
    """The door the IR tools share (rebuild_from_ir, replay_run, analyze_drawing build): a NEW design
    with the whole graph as step 1. fresh_document=False adopts the active document and builds on top of
    its existing tree. -> (text, ok, state_version); on failure the design stays at step 0."""
    if not isinstance(graph_obj, dict) or not graph_obj.get("nodes"):
        return "FAILED | VALIDATION_FAILED | graph.nodes must be a non-empty array. | no execution performed", \
            False, bridge.state.version
    try:
        import ir_execution_port                              # noqa: F401
    except Exception as ex:                                   # noqa: BLE001
        return "FAILED | COMPILER_UNAVAILABLE | %s" % ex, False, bridge.state.version
    return new(infer_kind(graph_obj.get("nodes")), label or kind, graph_obj, fresh_document, kind, source)


# ---------------------------------------------------------------------------
# History (read-only)
# ---------------------------------------------------------------------------
def history_text(design) -> str:
    lines = ["DESIGN %s | %s %r%s | head=%d/%d" % (design.design_id, design.kind, design.doc_title,
                                                  (" | " + design.label) if design.label else "",
                                                  design.head, design.total)]
    for s in design.snapshots:
        mark = ">" if s["step"] == design.head else " "
        undone = "  [undone - design_redo restores it]" if s["step"] > design.head else ""
        what = patch_summary(s.get("patch")) if s["step"] else "document as found (%d existing features)" % design.base_count
        lines.append("%s %2d  %s  (%s; %d nodes)%s" % (mark, s["step"], s.get("label") or "", what,
                                                      len(s["nodes"]), undone))
    live = design.live()
    lines.append("current nodes: " + (", ".join("%s(%s)" % (n.get("id"), n.get("type")) for n in live["nodes"])
                                      or "none"))
    if live.get("material"):
        lines.append("material: %s" % json.dumps(live["material"]))
    if design.head < design.total:
        lines.append("redo available: %d step(s)" % (design.total - design.head))
    lines.append(_tail(design))
    return "\n".join(lines)
