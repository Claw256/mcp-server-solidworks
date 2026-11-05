"""Parameter parity for the drawing-output / export tools: every `p?.Value<..>("name")` the C# reads for these
tools appears in the published schema and as an adapter parameter, and the adapter forwards it. No SolidWorks."""
import inspect
import json
import os
import re
import sys

import pytest

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)
_EXEC = os.path.join(os.path.dirname(os.path.dirname(_ADAPTER_DIR)), "execution", "solidworks")
_SERVICES = os.path.join(_EXEC, "SolidworksExecution", "Services")

server = pytest.importorskip("server")
from solidpilot.tools import drawing_out, export  # noqa: E402

TOOLS = {
    "create_drawing": drawing_out,
    "add_drawing_view": drawing_out,
    "add_flat_pattern_view": drawing_out,
    "add_drawing_dimension": drawing_out,
    "auto_dimension_drawing": drawing_out,
    "auto_center_marks": drawing_out,
    "add_hole_callout": drawing_out,
    "add_section_view": drawing_out,
    "export_document": export,
    "batch_export": export,
}
_SERVICE_FILES = ("SolidWorksService.cs", "SolidWorksService.Drawing.cs")


def _csharp_params(tool):
    for name in _SERVICE_FILES:
        with open(os.path.join(_SERVICES, name), encoding="utf-8-sig") as fh:
            src = fh.read()
        m = re.search(r'\[Tool\("%s"[^\]]*\]' % tool, src)
        if not m:
            continue
        rest = src[m.end():]
        nxt = re.search(r'\[Tool\(', rest)
        body = rest[:nxt.start()] if nxt else rest
        return set(re.findall(r'p\?\.Value<[^>]*>\("(\w+)"\)', body))
    raise AssertionError(f"no C# handler for {tool}")


def _schema_params(tool):
    with open(os.path.join(_EXEC, "contracts", "tool-schemas.json"), encoding="utf-8") as fh:
        return set(json.load(fh)[tool]["input"]["params"])


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_csharp_schema_and_adapter_agree(tool):
    cs = _csharp_params(tool)
    assert cs, f"{tool}: no params parsed from the C# handler"
    schema = _schema_params(tool)
    adapter = set(inspect.signature(getattr(TOOLS[tool], tool)).parameters)
    assert cs <= schema, f"{tool}: C# reads params missing from the schema: {sorted(cs - schema)}"
    assert schema <= cs, f"{tool}: schema lists params the C# never reads: {sorted(schema - cs)}"
    assert cs <= adapter, f"{tool}: C# reads params the adapter cannot send: {sorted(cs - adapter)}"


def _sent(monkeypatch, fn, **kwargs):
    seen = {}
    monkeypatch.setattr(drawing_out, "_call", lambda tool, params: seen.update(tool=tool, params=params) or "ok")
    fn(**kwargs)
    return seen


def test_section_view_forwards_view_name_only_when_given(monkeypatch):
    seen = _sent(monkeypatch, drawing_out.add_section_view, px=0.1, py=0.1, edge_x=0.05, edge_y=0.05)
    assert "view_name" not in seen["params"]
    seen = _sent(monkeypatch, drawing_out.add_section_view, px=0.1, py=0.1, edge_x=0.05, edge_y=0.05,
                 view_name="Drawing View1")
    assert seen["params"]["view_name"] == "Drawing View1"


def test_auto_dimension_forwards_view_name(monkeypatch):
    seen = _sent(monkeypatch, drawing_out.auto_dimension_drawing, all_views=False, view_name="Drawing View2")
    assert seen["params"]["view_name"] == "Drawing View2" and seen["params"]["all_views"] is False


def test_dimension_value_is_documented_as_meters_everywhere():
    doc = drawing_out.add_drawing_dimension.__doc__ or ""
    assert "METERS" in doc
    with open(os.path.join(_EXEC, "contracts", "tool-schemas.json"), encoding="utf-8") as fh:
        text = json.load(fh)["add_drawing_dimension"]["input"]["params"]["value"]
    assert "METERS" in text and "document units" not in text


def test_stale_section_fallback_text_is_gone():
    with open(os.path.join(_EXEC, "contracts", "tool-schemas.json"), encoding="utf-8") as fh:
        assert "CreateSectionViewAt2" not in json.load(fh)["add_section_view"]["description"]
