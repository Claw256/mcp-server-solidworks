import json

from solidpilot import design as core
from solidpilot import jobs
from solidpilot.app import mcp


def _need(design_id):
    d = core.get(design_id)
    if d is None:
        return None, ("FAILED | NO_DESIGN | no design %s — start one with design_new"
                      % (repr(design_id) if design_id else "yet"))
    return d, None


@mcp.tool(structured_output=False)
def design_new(kind: str = "part", label: str = "", graph: str = "", background: bool = False) -> str:
    """Start an incremental design: open a blank part or assembly (kind='part'|'assembly') and begin a
    session you then grow with design_step and revisit with design_undo / design_redo / design_history.
    PRIMARY BUILD PATH for creating geometry — build in small verified steps, not one giant graph.

    graph (optional): a Feature Graph IR JSON string applied as step 1 (units METERS, angles RADIANS;
        read recipe://usage/forward and schema://feature-graph first). Its node types pick part vs
        assembly. A failed step 1 is rolled back, leaving the empty design.
    background=True: return a job id at once (see job_status).

    Result ends with design_id=... step=N head=N/total | run_id=..."""
    graph_obj = None
    if graph.strip():
        try:
            graph_obj = json.loads(graph)
        except Exception as ex:  # noqa: BLE001
            return f"FAILED | INVALID_JSON | the graph is not valid JSON: {ex}"
    if background:
        return jobs.launch("design_new", lambda: design_new(kind, label, graph, False))
    text, _ok, sv = core.new(kind, label, graph_obj)
    return text + f" | state_version={sv}"


@mcp.tool(structured_output=False)
def design_step(patch: str, label: str = "", background: bool = False) -> str:
    """Add the next step to the current design: patch the node list, and only what changed is rebuilt
    (appending replays nothing; editing node k rebuilds k and everything after it).

    patch: JSON list of ops on the node list: {"op":"add","nodes":[...],"after":"<id>"} (omit after = append
        at the end), {"op":"replace","id":"<id>","node":{...}}, {"op":"set","id":"<id>","params":{...}}
        (shallow-merge into the node), {"op":"remove","id":"<id>"}, {"op":"material","material":{"name":..}}.
        Node grammar: recipe://usage/forward. An extrude/revolve/etc. must directly follow its sketch node.
    label: short step name shown by design_history.

    The whole result is validated first (an invalid patch changes nothing). If a node fails to build the
    old state is rebuilt and the step is reported FAILED | STEP_FAILED with the design unchanged. If the
    document was edited outside these tools: FAILED | DESIGN_DRIFT. Verify after each step.
    background=True: return a job id at once (see job_status).

    Result ends with design_id=... step=N head=N/total | run_id=..."""
    d, err = _need("")
    if err:
        return err
    try:
        core.parse_patch(patch)
    except core.PatchError as ex:
        return f"FAILED | {ex.code} | {ex.message}"
    if background:
        return jobs.launch("design_step", lambda: design_step(patch, label, False))
    text, _ok, sv = core.step(d, patch, label)
    return text + f" | state_version={sv}"


@mcp.tool(structured_output=False)
def design_undo(steps: int = 1) -> str:
    """Undo the last `steps` design steps: the feature tree is cut back to that step's state and only what
    differs is rebuilt. design_redo restores them until a new design_step discards them.
    Material is document state: undoing a material step does not remove the material.

    Result ends with design_id=... step=N head=N/total | run_id=..."""
    d, err = _need("")
    if err:
        return err
    text, _ok, sv = core.undo(d, steps)
    return text + f" | state_version={sv}"


@mcp.tool(structured_output=False)
def design_redo(steps: int = 1) -> str:
    """Redo `steps` undone design steps (FAILED | NOTHING_TO_REDO when none, e.g. after a new design_step).

    Result ends with design_id=... step=N head=N/total | run_id=..."""
    d, err = _need("")
    if err:
        return err
    text, _ok, sv = core.redo(d, steps)
    return text + f" | state_version={sv}"


@mcp.tool(structured_output=False)
def design_history(design_id: str = "") -> str:
    """List a design's steps (label, what each changed), the head marker, steps available to redo and the
    current node ids/types. Read-only. design_id '' = the current design (also survives a restart; pass an
    id to inspect an older one)."""
    d, err = _need(design_id)
    return err or core.history_text(d)
