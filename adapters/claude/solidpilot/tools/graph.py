import hashlib
import json
import os
from solidpilot import bridge
from solidpilot.app import mcp


# ---------------------------------------------------------------------------
# Tool: rebuild_from_ir  (adapter-only — the analysis pipeline's IR door, IR-ADR-005)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def rebuild_from_ir(artifact_path: str, fresh_document: bool = True) -> str:
    """Rebuild a part from its analysis artifact's Feature Graph IR — the verification half of
    the round-trip ("the LLM proposes, the round-trip decides", IR-ADR-006). Reads
    `<...>.analysis.json`, takes `ir.graph`, and executes it through the SAME deterministic
    pycompiler as the forward door submit_feature_graph (two doors, ONE compiler — never forked).

    fresh_document (default True): open a NEW blank part first — the normal round-trip flow
        (rebuild fresh, then compare_parts against the original). Pass False only if you have
        already prepared the target document yourself.

    The rebuild reproduces the part AS-ANALYZED. If the source file changed since the artifact
    was written (hash mismatch) the result carries source_stale=true — re-run save_analysis and
    regenerate the IR rather than trusting a stale graph.

    Returns the compiler's per-node summary (COMPLETED n/n, or the feature-level error and how
    far it got — partial geometry may remain; CAD ops are not transactional). Afterwards, verify
    with compare_parts and only then label the artifact's ir.verification. If you are GENERATING
    the ir.graph yourself, read the MCP resource `recipe://usage/index` first — it indexes the
    mapping/canonicalization rules the compiler expects."""
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

    text, ok, _sv = _run_graph(graph, fresh_document)
    return text + (f" | artifact={os.path.basename(path)}{source_stale}" if ok else "")


# ---------------------------------------------------------------------------
# Tool: submit_feature_graph  (the FORWARD IR door — intent → IR → pycompiler → SolidWorks.
# Re-enabled GATE-FREE 2026-07-16 for the forward vocabulary effort (IR-ADR-017); the old
# experimental gates were deleted by IR-ADR-005. Two doors, ONE pycompiler: rebuild_from_ir
# replays an artifact's stored graph, this tool takes a graph directly — never fork the compiler.)
# ---------------------------------------------------------------------------
def _run_graph(graph_obj, fresh_document):
    """The ONE path from a Feature Graph to live geometry — shared by every IR door.

    IR-ADR-005 forbids FORKING the compiler, not having several doors into it. There are three
    (submit_feature_graph takes a graph, rebuild_from_ir replays an artifact's stored one,
    analyze_drawing(mode='build') lowers a drawing), and they all land here, so the document
    choice, the compiler entry point and the post-run resync cannot diverge between them.

    Returns (text, ok, state_version). On failure `text` is already a complete FAILED line."""
    if fresh_document:
        # The graph type picks the document: an assembly graph (component/mate nodes) needs an
        # assembly, a part graph a part.
        nodes = graph_obj.get("nodes") or [] if isinstance(graph_obj, dict) else []
        is_assembly_graph = any(isinstance(n, dict) and n.get("type") in ("component", "mate")
                                for n in nodes)
        new_tool = "open_new_assembly" if is_assembly_graph else "open_new_part"
        opened = bridge._call_raw(new_tool, {})
        if opened.get("status") != "COMPLETED":
            err = opened.get("error") or {}
            return (f"FAILED | {new_tool.upper()}_FAILED | {err.get('code')}: {err.get('message')}",
                    False, bridge.state.version)

    # Lazy import: a missing compiler tree degrades to a clean tool error instead of killing the
    # whole MCP server at startup.
    try:
        from ir_execution_port import run_feature_graph
    except Exception as ex:  # noqa: BLE001
        return f"FAILED | COMPILER_UNAVAILABLE | {ex}", False, bridge.state.version

    # NEVER let an exception crash the MCP server; resync state_version regardless (IR-ADR-001) —
    # one run performs MANY state-bumping sub-ops outside _call() (KNOWN-LIMITATIONS #5).
    try:
        result = run_feature_graph(graph_obj)
    except Exception as ex:  # noqa: BLE001
        sv = bridge._resync_state_version()
        return (f"FAILED | UNEXPECTED | {type(ex).__name__}: {ex} | state_version resynced to {sv}",
                False, sv)
    # ok must reflect the COMPILE outcome: callers (analyze_drawing build, rebuild_from_ir) append
    # success-only tails ("built from…", VERIFY hints) that are wrong on a half-built part.
    return result.summary(), result.status == "COMPLETED", bridge._resync_state_version()


@mcp.tool(structured_output=False)
def submit_feature_graph(graph: str, fresh_document: bool = True) -> str:
    """Build a part (or assembly) from a Feature Graph IR in ONE call — the deterministic
    compiler lowers each IR node to the right low-level tool sequence and resolves references
    (geometric anchors, runtime feature names) against live geometry.

    ★ PRIMARY BUILD PATH — prefer this whenever you are CREATING a part/assembly (including a
    reverse reconstruction from a drawing). Assemble the WHOLE feature graph and submit it ONCE;
    do NOT hand-build feature-by-feature with the individual low-level tools
    (create_sketch/extrude_feature/add_edge_feature/…). One graph keeps tree order intact, lets
    the compiler resolve anchors + runtime feature names deterministically, and takes far fewer
    round-trips. The low-level tools are for RECOVERY (re-doing one failed node), a genuine
    one-off edit, or a feature the IR vocabulary cannot yet express — not for routine
    construction. If you truly cannot express the whole part in one graph yet, still submit the
    LARGEST coherent batches (fresh_document=True for the first, append with fresh_document=False),
    never one node at a time.

    graph: the Feature Graph as a JSON STRING. Authoring from DESIGN INTENT: read the MCP resource
        `recipe://usage/forward` FIRST (grammar, anchor design, self-verification), plus
        `schema://feature-graph` — the schema IS the capability registry.
        Replaying an ANALYZED part instead: `recipe://usage/canonicalization` +
        `recipe://usage/mapping_part` (…/mapping_sheet_metal, …/mapping_assembly).
        Essentials: units METERS, angles RADIANS (the compiler converts at tool boundaries);
        nodes build in array order (tree order is law); extrude/revolve/rib/sweep/sheet_metal/
        sketched_bend consume the IMMEDIATELY preceding sketch node; loft profiles, a sweep's
        path, pattern seeds and mirror features reference EARLIER nodes by id; an optional
        graph-level material {name, library?} applies after the last node.
    fresh_document (default True): open a new blank document first — part graphs a part,
        assembly graphs (component/mate nodes) an assembly. Pass False to build into the
        CURRENT active document instead.

    Returns the compiler's per-node report: COMPLETED n/n, or FAILED with a feature-level error
    and how far it got. CAD ops are NOT transactional — on failure partial geometry remains
    (reported, never hidden). The adapter resyncs state_version after every run, so subsequent
    normal tools keep working. Verify the result objectively (analyze_model / compare_parts) —
    never assume."""
    try:
        graph_obj = json.loads(graph)
    except Exception as ex:  # noqa: BLE001
        return f"FAILED | INVALID_JSON | the graph is not valid JSON: {ex}"

    text, ok, sv = _run_graph(graph_obj, fresh_document)
    return text + f" | state_version={sv}"      # resynced after every run, success or not
