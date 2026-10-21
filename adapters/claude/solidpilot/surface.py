"""What the host model is TOLD about the tools beyond their docstrings: server instructions and the
MCP behaviour annotations (readOnly / destructive / idempotent). One table, one place.

Annotations are hints a client may use to skip confirmation prompts for reads and to warn before a
destructive call; they never change what a tool does.
"""
from mcp.types import ToolAnnotations

INSTRUCTIONS = """\
SolidWorks CAD adapter. Typical order: ensure_ready -> open_new_part/open_document -> build -> verify -> export.
From a 2D drawing: analyze_drawing first. If it lists OPEN QUESTIONS, put them to the user with ask_user (or in
chat) and pass the answers back; never build around a doubt yourself. After a build, verify_against_interpretation()
with no arguments checks the model against the drawing itself.
Units: lengths are METERS and angles RADIANS everywhere except where a parameter or its description says
millimetres/degrees; read the unit before sending a number. CAD operations are not transactional: a failed build
leaves partial geometry, which the result reports. Every graph build prints run_id=...; replay_run reproduces it.
Read get_recipe('reverse') / get_recipe('forward') before writing a Feature Graph."""

# Tools that neither change the model/file system nor the active document.
READ_ONLY = frozenset({
    "verify_state", "analyze_assembly", "analyze_model", "analyze_slddrw_test", "get_selection", "get_recipe",
    "prepare_drawing", "annotate_regions", "verify_against_interpretation", "ask_user",
})
# Tools that can discard work (an unsaved close, a feature delete/suppress).
DESTRUCTIVE = frozenset({"close_document", "edit_feature"})
# Mutating tools that are safe to repeat with the same arguments.
IDEMPOTENT = frozenset({"ensure_ready", "activate_document", "set_part_material", "save_document"})


def annotate(mcp) -> None:
    for tool in mcp._tool_manager.list_tools():
        ro = tool.name in READ_ONLY
        tool.annotations = ToolAnnotations(
            read_only_hint=ro,
            destructive_hint=False if ro else tool.name in DESTRUCTIVE,
            idempotent_hint=ro or tool.name in IDEMPOTENT,
            open_world_hint=False,
        )
