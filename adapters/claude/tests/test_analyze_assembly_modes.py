"""analyze_assembly adapter wiring with a FAKE bridge (no SolidWorks): the `interference` mode forwards
its options and the other modes forward nothing extra; the new mode and params are advertised.

Run:  pytest tests/test_analyze_assembly_modes.py
"""
import asyncio
import json
import os
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import server  # noqa: E402
from solidpilot.tools import assembly  # noqa: E402


def _run(monkeypatch, **kw):
    calls = []
    monkeypatch.setattr(assembly, "_call", lambda tool, params: calls.append((tool, dict(params))) or "ok")
    assert assembly.analyze_assembly(**kw) == "ok"
    return calls


def test_interference_forwards_options_and_default_limit(monkeypatch):
    calls = _run(monkeypatch, analysis_type="interference")
    assert calls == [("analyze_assembly", {"analysis_type": "interference", "coincident": False,
                                           "subassemblies_as_components": False, "limit": 20})]
    calls = _run(monkeypatch, analysis_type="interference", coincident=True,
                 subassemblies_as_components=True, limit=5)
    assert calls[0][1] == {"analysis_type": "interference", "coincident": True,
                           "subassemblies_as_components": True, "limit": 5}


def test_other_modes_forward_no_interference_options(monkeypatch):
    for mode in ("components", "components_flat", "mates"):
        assert _run(monkeypatch, analysis_type=mode, coincident=True)[0][1] == {"analysis_type": mode}
    assert _run(monkeypatch, analysis_type="faces", component="Shaft-1")[0][1] == \
        {"analysis_type": "faces", "component": "Shaft-1"}


def test_the_interference_mode_is_advertised_to_the_model():
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "analyze_assembly")
    props = tool.input_schema["properties"]
    assert "interference" in json.dumps(props["analysis_type"])
    assert {"coincident", "subassemblies_as_components", "limit"} <= set(props)

