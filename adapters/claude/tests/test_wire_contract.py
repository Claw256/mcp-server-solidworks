"""The documented wire contract (execution/solidworks/contracts/*.json) must match what the C# models
actually serialise: property names, camelCase unless an explicit [JsonProperty] says otherwise."""
import json
import os
import re

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_EXEC = os.path.join(_REPO, "execution", "solidworks")


def _wire_names(cs_file, cls):
    with open(os.path.join(_EXEC, "SolidworksExecution", "Models", cs_file), encoding="utf-8-sig") as fh:
        src = fh.read()
    body = re.search(r"public class %s\s*\{(.*?)\n    \}" % cls, src, re.S).group(1)
    names, pending = set(), None
    for line in body.splitlines():
        m = re.search(r'\[JsonProperty\("([^"]+)"', line)
        if m:
            pending = m.group(1)
        p = re.search(r"public [\w<>?]+ (\w+) \{ get; set; \}", line)
        if p:
            names.add(pending or p.group(1)[0].lower() + p.group(1)[1:])
            pending = None
    return names


def _contract(name):
    with open(os.path.join(_EXEC, "contracts", name), encoding="utf-8") as fh:
        return json.load(fh)


def test_execution_response_contract_matches_the_csharp_model():
    wire = _wire_names("ExecutionResponse.cs", "ExecutionResponse")
    documented = set()
    for status in ("COMPLETED", "FAILED", "DUPLICATE"):
        documented |= set(_contract("execution-response.json")[status])
    assert documented <= wire, f"documented but not emitted: {sorted(documented - wire)}"
    assert wire <= documented, f"emitted but undocumented: {sorted(wire - documented)}"


def test_state_format_contract_matches_the_csharp_model():
    assert set(_contract("state-format.json")) == _wire_names("CadState.cs", "CadState")


def test_response_mapper_reads_only_documented_wire_names():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "response_mapper.py"), encoding="utf-8") as fh:
        used = set(re.findall(r'(?:response|state)\.get\("(\w+)"', fh.read()))
    allowed = _wire_names("ExecutionResponse.cs", "ExecutionResponse") | _wire_names("CadState.cs", "CadState")
    assert used <= allowed, f"mapper reads undocumented fields: {sorted(used - allowed)}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
