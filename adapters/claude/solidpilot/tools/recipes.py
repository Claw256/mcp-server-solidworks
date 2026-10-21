import json
import os
from typing import Literal
from mcp.server.mcpserver.exceptions import ResourceError
from solidpilot.app import mcp
from solidpilot.paths import CAD_PLANNER_DIR


# ---------------------------------------------------------------------------
# RESOURCE surface: the IR-generation recipe + the two cad-planner contract JSONs
#
# These were the `get_recipe(section=...)` tool until 2026-07-30 (ADR-069). They are now MCP
# *resources*: the content is large, near-static, identical for every caller and re-read
# constantly — exactly what `resources/read` + the 2026-07-28 `ttlMs`/`cacheScope` hints are for.
# MEASURED reason to move (not estimated): `get_recipe` cost 2,369 chars of `tools/list` on EVERY
# turn (2.97% of the 47-tool surface; 1,928 of it the docstring), paid whether or not any recipe
# was read. A resource read costs ~50 chars MORE than the equivalent tool result (the
# `uri`+`mimeType` wrapper), so the win is entirely the per-turn surface — which is why the tool
# had to GO, not sit beside the resources.
#
# Shape decided with the user (2026-07-30):
#   - EVERY section is its own STATIC resource. Probe finding: templated URIs are readable but
#     appear in NO listing (`resources/list` shows static entries only), so a template-only
#     surface would be undiscoverable. Static-per-section makes ONE `resources/list` the manifest.
#   - `recipe://usage/{section}` stays as a TEMPLATE ALIAS so a slug guessed from the index
#     still resolves. Concrete resources win over templates in the SDK's resolution order, so
#     the static entries above are never shadowed by it.
#   - Version lives in the BODY only (`Version: 0.21.0`), never in the URI: bumps must not
#     break the URI pointers embedded in the tool docstrings below.
#   - The FROZEN `slddrw-testing-recipe-usage.md` pair (ADR-063) is deliberately NOT served —
#     it stays a repo file; port from it consciously if a rule there is wanted again.
# The recipe's PRIVATE full edition (`recipe.md`) is NEVER served — it carries lesson history and
# provenance, is gitignored, and only the PUBLIC `-usage` twin appears on this surface.
# ---------------------------------------------------------------------------
_CAD_PLANNER_DIR = CAD_PLANNER_DIR

# The served section slugs, EXPLICIT (this replaces get_recipe's `Literal[...]`). The surface is
# under the user's control by decision: a new `## slug — title` in recipe-usage.md does NOT
# publish itself as a resource until it is added here, deliberately.
_RECIPE_SECTIONS = (
    "contract", "canonicalization", "forward", "mapping", "mapping_part",
    "mapping_sheet_metal", "mapping_assembly", "verification", "reverse",
    "machined_from_image", "coverage",
)


def _recipe_sections():
    """Parse cad-planner/recipe-usage.md into its version header + {slug: (title, body)} by the
    '## slug — title' section headers. Read fresh on every call — the file ships with the server
    and is small; no caching keeps edits live without a reconnect."""
    path = os.path.join(_CAD_PLANNER_DIR, "recipe-usage.md")
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## ") and " — " in line:
            slug, _, title = line[3:].partition(" — ")
            current = slug.strip()
            sections[current] = [title.strip(), []]
        elif current is not None:
            sections[current][1].append(line)
    header = text.split("\n## ", 1)[0]  # the version/preamble block above the first section
    return header, {k: (t, "\n".join(body).strip()) for k, (t, body) in sections.items()}


def _recipe_section_text(section: str) -> str:
    """One section, rendered with its own `## slug — title` header restored.

    Read FRESH from disk on every call (via _recipe_sections), so editing recipe-usage.md goes
    live with no reconnect — only the registration metadata below is snapshotted at import."""
    _header, sections = _recipe_sections()
    if section not in sections:
        # MUST be ResourceError: the SDK re-raises ResourceError/MCPError verbatim but replaces any
        # other exception's message with a generic "Error creating resource from template <uri>"
        # (templates.py). Verified live — a plain ValueError left the caller with no idea what the
        # valid slugs were, and a model that guesses (e.g. 'drawing', which was in the retired
        # get_recipe enum but is not a section) needs the list to self-correct in one step.
        raise ResourceError(
            f"'{section}' is not a served recipe section. Served: "
            f"{', '.join(_RECIPE_SECTIONS)}. Read recipe://usage/index for the full surface.")
    title, body = sections[section]
    return f"## {section} — {title}\n\n{body}"


def _contract_json(filename: str) -> str:
    with open(os.path.join(_CAD_PLANNER_DIR, "contracts", filename),
              "r", encoding="utf-8-sig") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# The IR schema, as the MODEL needs it (payload trim, 2026-09-07)
# ---------------------------------------------------------------------------
# `feature-graph.schema.json` serves two audiences at once and only one of them is the model.
# Gate 3 (`pycompiler/tests/test_ir_schema_contract.py`) machine-diffs every ARRAY under
# `covered_subset` against ir_schema.py's frozensets, and the prose beside those arrays documents
# that contract for whoever maintains it. So the FILE cannot be trimmed — the trim happens HERE,
# at serve time, on the copy the model receives.
#
# Dropped for every profile (measured; none of it is model-facing):
#   _meta.status                                2,135 B — the version narrative (IR-ADR references,
#                                                         "do NOT freeze before the MAT v1 set
#                                                         arrives"): a developer's changelog.
#   covered_subset._note/._grammar_note/.assumptions
#                                               1,529 B — half of it gate 3's own machine-diff
#                                                         instructions ("every ARRAY in this block
#                                                         is diffed against ir_schema.py").
# The covered_subset ARRAYS are left untouched: ~1,400 B carrying the entire capability list, the
# highest-value bytes in the file.
_SCHEMA_DEV_ONLY_SUBSET_KEYS = ("_note", "_grammar_note", "assumptions")

# What a PROFILE drops on top of that. Two values, because only two of them mean anything:
# `part` keeps every feature type ON PURPOSE — a drawing job does not know it is sheet metal until
# R21 decides, so the sheet-metal vocabulary must stay reachable. (A 'sheet_metal' profile would
# therefore be byte-identical to 'part', and an 'assembly' profile identical to 'all'; offering
# either would advertise a filter that does not exist.)
_SCHEMA_PROFILE_DROPS = {
    "all": (),
    "part": ("assembly_types_v06",),
}


def _feature_graph_schema_text(profile: str = "all") -> str:
    """`feature-graph.schema.json` with the developer-only blocks removed, re-serialized compactly.

    Falls back to the raw file if it will not parse: a malformed schema must still reach the caller
    (an unreadable capability registry is a loud failure, not a silent empty one).
    """
    raw = _contract_json("feature-graph.schema.json")
    try:
        doc = json.loads(raw)
    except ValueError:
        return raw
    meta = doc.get("_meta")
    if isinstance(meta, dict):
        meta.pop("status", None)
    subset = doc.get("covered_subset")
    if isinstance(subset, dict):
        for key in _SCHEMA_DEV_ONLY_SUBSET_KEYS:
            subset.pop(key, None)
    for key in _SCHEMA_PROFILE_DROPS.get(profile, ()):
        doc.pop(key, None)
    return json.dumps(doc, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Tool: get_recipe  (RESTORED 2026-09-03, ADR-077 — reverses ADR-069's retirement)
# ---------------------------------------------------------------------------
# ADR-069 moved the recipe from a TOOL to 13 MCP resources on a measured saving of 2,146 chars of
# `tools/list` per turn. The measurement was right; the assumption underneath it was not. MCP has
# three primitives and only ONE of them is in the MODEL's action space:
#
#     tool      tools/call       <- the model can call this
#     resource  resources/read   <- the CLIENT calls this; the model needs a host-provided bridge
#     prompt    prompts/get      <- the user invokes this
#
# ADR-069 probed the host and found it DID bridge resources for the model, so the migration looked
# safe. That bridge is gone: neither this host nor the one running the isolated tests exposes any
# `ReadMcpResource`-style tool, so the recipe left the model's action space entirely. Two isolated
# reconstructions of f-3 both reported they could not read `recipe://usage/reverse` and proceeded
# on their own judgement — which is exactly the "a resource no host reads is WORSE than a tool"
# risk ADR-069 named and accepted. A server cannot push a resource at a model; the only handle we
# can put in a model's hand is a TOOL. So the tool comes back.
#
# The RESOURCES STAY. They are spec-correct, the user can attach them by hand, and a host that
# does bridge them still benefits. Both channels read the same `recipe-usage.md` on every call, so
# there is exactly one copy of the rules and no sync burden.
@mcp.tool(structured_output=False)
def get_recipe(
    section: Literal["index", "contract", "canonicalization", "forward", "mapping",
                     "mapping_part", "mapping_sheet_metal", "mapping_assembly", "verification",
                     "reverse", "coverage", "feature_graph_schema",
                     "analysis_artifact_schema"] = "index",
    profile: Literal["all", "part"] = "all",
) -> str:
    """The IR-generation recipe — REQUIRED READING before writing any Feature Graph IR
    (an artifact's `ir.graph`), reconstructing a part from a drawing, or producing a drawing
    meant for reconstruction. Serves the rules section-by-section so token cost stays
    proportional to the task.

    Call order for the ARTIFACT→IR flow: 'contract' + 'canonicalization' + 'mapping' first,
    then the vocabulary section matching the document ('mapping_part' / 'mapping_sheet_metal' /
    'mapping_assembly'), then 'verification' before labeling anything. 'forward' holds the
    INTENT→IR authoring discipline (grammar cheat-sheet, anchor design without an original,
    computed-expectation self-verification) — read it FIRST when writing a graph for
    submit_feature_graph from design intent. 'reverse' holds the DXF/DWG drawing→part
    reconstruction discipline (scale/DIMLFAC, edge classes, view clustering, projection
    standard, contour tiers, sheet-metal bend arithmetic) — read it FIRST when rebuilding a part
    from a 2D drawing, before you interpret a single number of `analyze_drawing`'s answer.

    section='index' (default): the version header + a one-line table of contents.
    section='feature_graph_schema': the Feature Graph IR schema / capability registry JSON —
        the node types and params the compiler accepts (what is NOT in it cannot be built).
        `profile='part'` drops the ASSEMBLY vocabulary from the answer (~2.4 KB) and is what a
        part or drawing→part job wants; every feature type stays, sheet metal included, because a
        drawing is not known to be sheet metal until you decide it (R21). `profile='all'`
        (default) is the whole registry.
    section='analysis_artifact_schema': the persistent analysis-artifact contract JSON
        (identity/hash, recipe, parameters, ir block + the formal 'verified' definition).
    The same rules are also published as `recipe://usage/*` MCP resources; use whichever your
    host actually lets you reach. Read-only; does NOT touch SolidWorks or state_version."""
    if section == "feature_graph_schema":
        return _feature_graph_schema_text(profile)
    if section == "analysis_artifact_schema":
        return _contract_json("analysis-artifact.schema.json")
    header, sections = _recipe_sections()
    if section == "index":
        toc = "\n".join(f"- {slug} — {title}" for slug, (title, _b) in sections.items())
        return (f"{header.strip()}\n\nSections (pass as `section`):\n{toc}\n"
                "- feature_graph_schema — the IR schema / capability registry (JSON)\n"
                "- analysis_artifact_schema — the analysis-artifact contract (JSON)")
    return _recipe_section_text(section)


@mcp.resource("recipe://usage/index", name="recipe_index", mime_type="text/markdown",
              description="START HERE for IR work: the recipe version header + every section URI. "
                          "Rules for turning an analysis artifact or a 2D drawing into a Feature "
                          "Graph IR.")
def _recipe_index() -> str:
    """The table of contents, generated from the file so it can never drift from what is served."""
    header, sections = _recipe_sections()
    lines = []
    for slug in _RECIPE_SECTIONS:
        title = (sections.get(slug) or ("(missing from recipe-usage.md)",))[0]
        lines.append(f"- recipe://usage/{slug} — {title}")
    return (
        f"{header.strip()}\n\n"
        f"Sections (read by URI):\n" + "\n".join(lines) + "\n\n"
        "Contracts:\n"
        "- schema://feature-graph — the IR schema / capability registry (JSON). What is NOT in\n"
        "  it cannot be built; it doubles as the capability list.\n"
        "- schema://analysis-artifact — the persistent analysis-artifact contract (JSON):\n"
        "  identity/hash, recipe, parameter table, ir block + the formal 'verified' definition.\n\n"
        "Reading order — ARTIFACT→IR: contract + canonicalization + mapping, then the vocabulary\n"
        "section matching the document (mapping_part / mapping_sheet_metal / mapping_assembly),\n"
        "then verification BEFORE labeling anything. INTENT→IR (no original part): forward.\n"
        "DXF/DWG DRAWING→PART: reverse. PDF/IMAGE DRAWING→PART: machined_from_image."
    )


def _register_recipe_sections() -> None:
    """Register one STATIC resource per served section.

    Static (not template-only) because a templated URI appears in NO listing — `resources/list`
    carries concrete resources only — so this is what makes the rule set DISCOVERABLE: one
    `resources/list` call returns every section with its title as the description.

    Titles are snapshotted at import for the descriptions (the listing metadata is fixed at
    registration time); the BODIES stay fresh per read. A missing/unreadable recipe-usage.md
    degrades to a generic description instead of killing the MCP server at startup — the same
    stance as the lazily-imported compiler.
    """
    try:
        _header, sections = _recipe_sections()
    except OSError:
        sections = {}
    for slug in _RECIPE_SECTIONS:
        title = (sections.get(slug) or ("",))[0]
        reader = (lambda s: lambda: _recipe_section_text(s))(slug)
        reader.__name__ = f"_recipe_{slug}"
        mcp.resource(
            f"recipe://usage/{slug}",
            name=f"recipe_{slug}",
            mime_type="text/markdown",
            description=(f"IR recipe — {title}" if title
                         else f"IR recipe section '{slug}' (see recipe://usage/index)"),
        )(reader)


_register_recipe_sections()


_PDF_WORKFLOW_PROMPT = """\
Reconstruct the machined part shown in this drawing: {file_path}

1. Read the recipe first: get_recipe(section='machined_from_image') and get_recipe(section='forward').
2. Look at the drawing (attach/read it). If prepare_drawing is available, call it for legible tiles and the
   exact text layer. Take numbers from text, not from measuring pixels.
3. Write the interpretation table (units, projection, overall size, each feature with source text|vision,
   assumptions). Ask the user ONLY about things the drawing cannot settle.
4. Build with submit_feature_graph (METERS, RADIANS).
5. Verify with verify_against_interpretation (bbox_mm, volume_mm3, holes, cg_mm), fix once, resubmit.
6. Finish with the ledger: verified / assumed / could not model / could not read."""


@mcp.prompt()
def pdf_drawing_to_part(file_path: str) -> str:
    """Workflow for turning a PDF/image engineering drawing into a SolidWorks part."""
    return _PDF_WORKFLOW_PROMPT.format(file_path=file_path)


@mcp.resource("recipe://usage/{section}", name="recipe_section", mime_type="text/markdown",
              description="Alias: any recipe-usage.md section by slug. The per-section URIs above "
                          "are the discoverable form; this only catches a slug read off the index.")
def _recipe_section_alias(section: str) -> str:
    return _recipe_section_text(section)


@mcp.resource("schema://feature-graph", name="feature_graph_schema",
              mime_type="application/json",
              description="The CAD-neutral Feature Graph IR schema AND capability registry — the "
                          "node types and params the deterministic compiler accepts. What is not "
                          "in it cannot be built. Read before authoring any ir.graph.")
def _feature_graph_schema() -> str:
    # Same trimmed copy the tool serves at profile='all' — one shape, whichever channel reaches
    # the model. A resource URI takes no arguments, so the resource is always the full registry.
    return _feature_graph_schema_text("all")


@mcp.resource("schema://analysis-artifact", name="analysis_artifact_schema",
              mime_type="application/json",
              description="The persistent per-file analysis-artifact contract "
                          "(.solidpilot/*.analysis.json): identity/hash, recipe, lifted parameter "
                          "table, ir block + the formal 'verified' definition.")
def _analysis_artifact_schema() -> str:
    return _contract_json("analysis-artifact.schema.json")
