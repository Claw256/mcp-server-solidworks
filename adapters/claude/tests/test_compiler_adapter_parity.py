"""Every parameter the compiler EMITS to a tool must exist in that tool's adapter schema.

Replays the whole compiler test-suite with a recording FakePort and checks (tool, param) against the
MCP tool schemas. This is the drift that a rename (angle -> angle_deg) or a typo creates between the
compiler's lowering and the adapter: the compiler would send a name the adapter rejects, or, one layer
down, the execution layer would silently ignore it and use a default.
"""
import os
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_ADAPTER_DIR))
for _p in (_ADAPTER_DIR, os.path.join(_REPO, "compiler", "solidworks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import server
    from pycompiler.tests import test_compiler as tc
    _OK = True
except Exception:  # noqa: BLE001
    _OK = False

# Params the compiler adds that the adapter tool never exposes, on purpose. Keep this EMPTY unless there
# is a stated reason; each entry is (tool, param).
_ALLOWED = set()


def _record_emissions():
    emitted = {}
    real = tc.FakePort.execute

    def spy(self, tool, params, state_version):
        emitted.setdefault(tool, set()).update(params)
        return real(self, tool, params, state_version)
    tc.FakePort.execute = spy
    try:
        ran = 0
        for name in sorted(dir(tc)):
            fn = getattr(tc, name)
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                except Exception:        # noqa: BLE001 - a test that fails on its own is the compiler suite's problem
                    pass
                ran += 1
    finally:
        tc.FakePort.execute = real
    return emitted, ran


def test_compiler_only_emits_parameters_the_adapter_accepts():
    if not _OK:
        return
    schema = {t.name: set((t.parameters or {}).get("properties", {})) for t in server.mcp._tool_manager.list_tools()}
    emitted, ran = _record_emissions()
    assert ran > 30 and len(emitted) > 10, f"replay looks hollow: {ran} tests, {len(emitted)} tools"
    problems = []
    for tool, params in sorted(emitted.items()):
        if tool not in schema:
            problems.append(f"compiler calls '{tool}', which the adapter does not expose")
            continue
        for p in sorted(params):
            if p not in schema[tool] and (tool, p) not in _ALLOWED:
                problems.append(f"compiler sends {tool}.{p}, not a parameter of the adapter tool "
                                f"(accepts: {sorted(schema[tool])})")
    assert not problems, "compiler/adapter parameter drift:\n  - " + "\n  - ".join(problems)


def test_degree_parameters_carry_their_unit_in_the_name():
    if not _OK:
        return
    schema = {t.name: (t.parameters or {}).get("properties", {}) for t in server.mcp._tool_manager.list_tools()}
    for tool, param in (("extrude_feature", "angle_deg"), ("add_edge_feature", "angle_deg"),
                        ("create_pattern", "angle_deg"), ("sheet_metal_feature", "angle_deg"),
                        ("hole_wizard", "csink_angle_deg")):
        assert param in schema[tool], f"{tool} lost {param}"
    bare = [(t, p) for t, props in schema.items() for p in props if p in ("angle", "csink_angle")]
    assert not bare, f"degree parameters without a unit suffix: {bare}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
