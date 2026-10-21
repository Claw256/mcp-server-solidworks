"""response_mapper renders the execution layer's post-tool diagnostics for the model."""
import os
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

from response_mapper import map_response  # noqa: E402


def _ok(**extra):
    r = {"status": "COMPLETED", "stateVersion": 4, "cadState": {"activeDocument": "p", "features": ["Boss-Extrude1"]}}
    r.update(extra)
    return r


def test_no_diagnostics_leaves_the_text_unchanged():
    assert "diagnostics" not in map_response(_ok()) and "FEATURE_ERRORS" not in map_response(_ok())


def test_new_feature_error_is_stated_with_a_warning():
    out = map_response(_ok(diagnostics={
        "new_feature_errors": [{"feature": "Cut-Extrude2", "type": "Cut", "code": 1,
                                "error": "swFeatureErrorBadSketch", "warning": False}],
        "volume_delta_m3": 0.0, "whats_wrong": 1}))
    assert "FEATURE_ERRORS=[Cut-Extrude2 (Cut): swFeatureErrorBadSketch]" in out
    assert "WARNING: SolidWorks reports the feature(s) above in error" in out
    assert "volume_delta_m3=0.0" in out and "whats_wrong" not in out


def test_warning_only_errors_do_not_raise_the_alarm():
    out = map_response(_ok(diagnostics={
        "new_feature_errors": [{"feature": "Fillet1", "type": "Fillet", "error": "w", "warning": True}]}))
    assert "[warning]" in out and "WARNING:" not in out


def test_volume_delta_and_preexisting_issues():
    out = map_response(_ok(diagnostics={"new_feature_errors": [], "volume_delta_m3": -1.2e-06, "whats_wrong": 2}))
    assert "volume_delta_m3=-1.2e-06" in out and "whats_wrong=2 (pre-existing" in out
    assert "FEATURE_ERRORS" not in out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
