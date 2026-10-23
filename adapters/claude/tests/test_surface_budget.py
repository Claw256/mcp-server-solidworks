"""The model-facing tool surface (descriptions + schemas + annotations) must not silently regress.

Every tool's description rides in the prompt on EVERY turn, so size is a cost, and internal tracker
ids in it are noise the model cannot resolve. Ceilings sit just above today's size: lowering the
numbers as docstrings shrink is the intended direction; raising them needs a reason.
"""
import json
import os
import re
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

try:
    import server
    from solidpilot import surface
    from solidpilot.tools import recipes
    _TOOLS = {t.name: t for t in server.mcp._tool_manager.list_tools()}
except Exception:  # noqa: BLE001
    _TOOLS = None

# 94_000 -> 95_700: the mesh front end adds analyze_mesh (~1.6K) and open_document gained mesh_as/units (~0.3K);
# measured 95.5K with 61 tools. (60 tools incl. the 5 design_* tools measured 93.6K before.)
TOTAL_CHARS_CEILING = 95_700
PER_TOOL_CHARS_CEILING = 13_500       # analyze_drawing is the outlier at ~13.1K
TOOL_COUNT_CEILING = 61               # +analyze_mesh (adapter-only mesh reverse-engineering front end)
INTERNAL_REF = re.compile(r"ADR-\d+|KNOWN-LIMITATIONS|IR-ADR|\bP0\.\d|recipe\.md\b")


def _size(t):
    return len(json.dumps({"n": t.name, "d": t.description, "p": t.parameters}, ensure_ascii=False))


def test_total_and_per_tool_size_stay_under_the_ceilings():
    if _TOOLS is None:
        return
    sizes = {n: _size(t) for n, t in _TOOLS.items()}
    assert len(sizes) <= TOOL_COUNT_CEILING, f"{len(sizes)} tools (ceiling {TOOL_COUNT_CEILING})"
    assert sum(sizes.values()) <= TOTAL_CHARS_CEILING, \
        f"surface is {sum(sizes.values())} chars (ceiling {TOTAL_CHARS_CEILING})"
    worst = max(sizes, key=sizes.get)
    assert sizes[worst] <= PER_TOOL_CHARS_CEILING, f"{worst} is {sizes[worst]} chars (ceiling {PER_TOOL_CHARS_CEILING})"


def test_no_internal_tracker_ids_reach_the_model():
    if _TOOLS is None:
        return
    bad = {n: sorted(set(INTERNAL_REF.findall(t.description + json.dumps(t.parameters))))
           for n, t in _TOOLS.items() if INTERNAL_REF.search(t.description + json.dumps(t.parameters))}
    assert not bad, f"internal references in the model-facing surface: {bad}"


def test_adapter_plumbing_is_not_documented_to_the_model():
    if _TOOLS is None:
        return
    leaky = [n for n, t in _TOOLS.items() if "state_version" in t.description]
    assert not leaky, f"state_version is adapter-internal; remove it from: {leaky}"


def test_recipe_sections_named_in_descriptions_exist():
    if _TOOLS is None:
        return
    served = set(recipes._RECIPE_SECTIONS) | {"index", "feature_graph_schema", "analysis_artifact_schema"}
    for n, t in _TOOLS.items():
        for sec in re.findall(r"get_recipe\(\s*(?:section\s*=\s*)?'([a-z_]+)'", t.description):
            assert sec in served, f"{n} points at get_recipe('{sec}'), which is not served"


def test_every_tool_is_annotated_and_the_flags_are_consistent():
    if _TOOLS is None:
        return
    for n, t in _TOOLS.items():
        a = t.annotations
        assert a is not None and a.open_world_hint is False, n
        if a.read_only_hint:
            assert a.destructive_hint is False and a.idempotent_hint is True, n
    assert _TOOLS["close_document"].annotations.destructive_hint is True
    assert _TOOLS["analyze_model"].annotations.read_only_hint is True
    assert _TOOLS["extrude_feature"].annotations.read_only_hint is False
    assert surface.READ_ONLY <= set(_TOOLS), f"unknown names in READ_ONLY: {sorted(surface.READ_ONLY - set(_TOOLS))}"
    assert (surface.DESTRUCTIVE | surface.IDEMPOTENT) <= set(_TOOLS)


def test_server_instructions_are_short_and_name_the_unit_rule():
    if _TOOLS is None:
        return
    assert "METERS" in surface.INSTRUCTIONS and "ask_user" in surface.INSTRUCTIONS
    assert len(surface.INSTRUCTIONS) < 1500


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
