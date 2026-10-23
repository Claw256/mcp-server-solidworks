import hashlib
import json
import os
from solidpilot import design
from solidpilot import jobs
from solidpilot import runs
from solidpilot.app import mcp


# ---------------------------------------------------------------------------
# Tool: rebuild_from_ir  (adapter-only — the analysis pipeline's IR door, IR-ADR-005)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def rebuild_from_ir(artifact_path: str, fresh_document: bool = True, background: bool = False) -> str:
    """Rebuild a part from its analysis artifact's Feature Graph IR — the verification half of
    the round-trip ("the LLM proposes, the round-trip decides"). Reads
    `<...>.analysis.json`, takes `ir.graph`, and executes it through the SAME deterministic
    pycompiler as design_new/design_step (two doors, ONE compiler — never forked).

    fresh_document (default True): open a NEW blank part first — the normal round-trip flow
        (rebuild fresh, then compare_parts against the original). Pass False only if you have
        already prepared the target document yourself.

    The rebuild reproduces the part AS-ANALYZED. If the source file changed since the artifact
    was written (hash mismatch) the result carries source_stale=true — re-run save_analysis and
    regenerate the IR rather than trusting a stale graph.

    Returns the compiler's per-node summary (COMPLETED n/n, or the feature-level error; a failed
    build is rolled back, leaving the new design at step 0). The result ends with design_id=...
    so the part can be edited with design_step. Afterwards, verify
    with compare_parts and only then label the artifact's ir.verification. If you are GENERATING
    the ir.graph yourself, read the MCP resource `recipe://usage/index` first — it indexes the
    mapping/canonicalization rules the compiler expects.

    background=True: start the build and return a job id at once (see job_status) — for builds that may
    outlast a chat call."""
    if background:
        return jobs.launch("rebuild_from_ir", lambda: rebuild_from_ir(artifact_path, fresh_document, False))
    path = os.path.abspath(artifact_path)
    if not os.path.isfile(path):
        return f"FAILED | ARTIFACT_NOT_FOUND | {path}"
    try:
        # utf-8-sig: tolerate a BOM — artifacts hand-edited or written by other Windows tools
        # (e.g. PowerShell 5.1 Out-File) may carry one; save_analysis itself writes without.
        with open(path, "r", encoding="utf-8-sig") as fh:
            artifact = json.load(fh)
    except (ValueError, OSError) as ex:
        return f"FAILED | ARTIFACT_UNREADABLE | {ex}"

    ir = artifact.get("ir") or {}
    graph = ir.get("graph")
    if not isinstance(graph, dict) or not graph.get("nodes"):
        status = (ir.get("verification") or {}).get("status")
        reason = ((ir.get("verification") or {}).get("detail") or {}).get("reason")
        return (f"FAILED | NO_IR_GRAPH | the artifact carries no executable ir.graph"
                f"{f' (verification: {status} | {reason})' if status else ''} — run the AI/IR "
                f"pass per cad-planner/recipe.md first")

    # Stale-source signal (cache discipline, ADR-040): informative, not blocking — the graph
    # legitimately rebuilds the part AS-ANALYZED.
    source_stale = ""
    src = (artifact.get("identity") or {}).get("source_path")
    recorded = (artifact.get("identity") or {}).get("source_hash")
    if src and recorded and os.path.isfile(src):
        with open(src, "rb") as fh:
            current = "sha256:" + hashlib.sha256(fh.read()).hexdigest()
        if current != recorded:
            source_stale = " | source_stale=true (file changed since analysis — regenerate the artifact)"

    text, ok, _sv = _run_graph(graph, fresh_document, kind="rebuild_from_ir", source=path)
    return text + (f" | artifact={os.path.basename(path)}{source_stale}" if ok else "")


# ---------------------------------------------------------------------------
# The IR doors (rebuild_from_ir, replay_run, analyze_drawing build) all land in solidpilot.design:
# each is a NEW design whose step 1 is the whole graph, so the result stays editable with
# design_step / design_undo. Two doors, ONE compiler (IR-ADR-005) — never fork it.
# ---------------------------------------------------------------------------
def _run_graph(graph_obj, fresh_document, kind="graph", source=None):
    """Build graph_obj as step 1 of a new design (fresh_document=False: adopt the active document).

    Returns (text, ok, state_version); the text ends with `design_id=... | run_id=...` so the build can be
    named, inspected, replayed and edited."""
    return design.build_graph(graph_obj, fresh_document, kind=kind, source=source)


@mcp.tool(structured_output=False)
def replay_run(run_id: str = "", fresh_document: bool = True, background: bool = False) -> str:
    """Re-run a recorded build exactly: the same Feature Graph, through the same compiler, as a NEW run.

    Every graph build (design_new/design_step, rebuild_from_ir, analyze_drawing build) ends with `run_id=...`
    and leaves a manifest holding the graph, its sha256, the environment and the per-node log. Use this to
    reproduce a result, to retry after fixing the environment (e.g. a modal dialog), or to rebuild on a
    fresh document. run_id='' lists the most recent runs. The manifest's graph hash is verified first; an
    edited record is refused. A replay on a non-fresh document builds on top of the active document's tree.
    fresh_document (default True) opens a new blank document first, as the original did.
    background=True starts the replay as a job (see job_status)."""
    if background and run_id.strip():
        return jobs.launch("replay_run", lambda: replay_run(run_id, fresh_document, False))
    if not run_id.strip():
        rows = runs.recent()
        if not rows:
            return "NO_RUNS | nothing has been built yet (runs are recorded in " + runs.runs_dir() + ")"
        return "RECENT RUNS (newest first)\n" + "\n".join(
            f"  {r['run_id']}  {r['status']:<9} {r['kind']:<20} {r['nodes']} nodes  {r['ended_utc']}"
            + (f"  <- {r['source']}" if r.get("source") else "") for r in rows)
    manifest, err = runs.load(run_id.strip())
    if err:
        return f"FAILED | RUN_NOT_FOUND | {err}"
    text, ok, sv = _run_graph(manifest["graph"], fresh_document, kind="replay", source=manifest["run_id"])
    return text + f" | state_version={sv} | replayed_from={manifest['run_id']}"


@mcp.tool(structured_output=False)
def job_status(job_id: str = "", wait_s: float = 20.0, cancel: bool = False) -> str:
    """Check (or cancel) a background build started with background=True.

    job_id: the id from JOB_STARTED ('' = the most recent job). wait_s (default 20, max 25): how long to
    wait for it to finish before answering RUNNING with progress (operations executed, last tool).
    cancel=True: stop it before its next operation — geometry built so far REMAINS (CAD ops are not
    transactional). Returns RUNNING | ..., or COMPLETED/FAILED/CANCELLED followed by the build's own result.
    While a job runs every other CAD call is refused so nothing lands in the middle of the build."""
    return jobs.status(job_id.strip(), wait_s, cancel)
