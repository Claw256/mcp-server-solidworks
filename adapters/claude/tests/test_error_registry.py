"""error-codes.json is the single list of execution-layer error codes: every code the C# sources can
emit must be in it (with a hint), nothing in it may be dead, and the mapper must pass the advice on."""
import glob
import json
import os
import re
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXEC = os.path.join(os.path.dirname(os.path.dirname(_ADAPTER_DIR)), "execution", "solidworks")
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import response_mapper  # noqa: E402


def _registry():
    with open(os.path.join(_EXEC, "contracts", "error-codes.json"), encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


def _csharp_codes():
    codes = set()
    root = os.path.join(_EXEC, "SolidworksExecution")
    for f in glob.glob(os.path.join(root, "**", "*.cs"), recursive=True):
        if os.sep + "obj" + os.sep in f or os.sep + "bin" + os.sep in f:
            continue
        src = open(f, encoding="utf-8-sig").read()
        for m in re.finditer(r'(?:BuildFailed|ResponseBuilder\.Failed)\(\s*[^,]+,\s*[^,]+,\s*"([A-Z0-9_]+)"', src):
            codes.add(m.group(1))
        for m in re.finditer(r'public const string \w+ = "([A-Z][A-Z0-9_]+)";', src):     # ErrorCodes.*
            codes.add(m.group(1))
    return codes


def test_every_code_the_csharp_layer_can_emit_is_registered():
    emitted, reg = _csharp_codes(), _registry()
    assert len(emitted) > 60, f"source scan found only {len(emitted)} codes — the pattern is broken"
    assert not emitted - set(reg), f"unregistered error codes: {sorted(emitted - set(reg))}"


def test_no_dead_entries_and_every_entry_is_well_formed():
    emitted, reg = _csharp_codes(), _registry()
    assert not set(reg) - emitted, f"registry codes no C# source emits: {sorted(set(reg) - emitted)}"
    for code, e in reg.items():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", code), code
        assert isinstance(e["retryable"], bool) and len(e["hint"]) > 20, code


def test_the_mapper_appends_the_hint_and_marks_retryable_codes():
    try:
        response_mapper.map_response({"status": "FAILED", "error": {"code": "FACE_NOT_FOUND", "message": "no face"}})
        assert False
    except response_mapper.CadOperationError as ex:
        text = str(ex)
    assert "code=FACE_NOT_FOUND | message=no face | hint=No face matched." in text and "retryable" not in text
    try:
        response_mapper.map_response({"status": "FAILED", "error": {"code": "COM_ATTACH_FAILED", "message": "x"}})
        assert False
    except response_mapper.CadOperationError as ex:
        assert "hint=SolidWorks is not reachable. Call ensure_ready" in str(ex) and "retryable=same call may succeed" in str(ex)


def test_an_unknown_code_still_maps_cleanly_without_advice():
    try:
        response_mapper.map_response({"status": "FAILED", "error": {"code": "BRAND_NEW", "message": "m"}})
        assert False
    except response_mapper.CadOperationError as ex:
        assert str(ex) == "CAD operation failed | code=BRAND_NEW | message=m"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
