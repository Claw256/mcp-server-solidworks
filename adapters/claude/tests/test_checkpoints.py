"""checkpoint: save a copy, list, restore by reopening — against a fake execution layer."""
import os
import sys
import tempfile

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

try:
    import server
    from solidpilot import bridge
    _DEPS = True
except Exception:  # noqa: BLE001
    _DEPS = False


class _Env:
    def __init__(self, doc_type="PART", fail=None):
        self.calls, self.doc_type, self.fail = [], doc_type, fail

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old = os.environ.get("SOLIDPILOT_CHECKPOINT_DIR")
        os.environ["SOLIDPILOT_CHECKPOINT_DIR"] = self.tmp.name
        self.real = bridge._call_raw

        def fake(tool, params):
            self.calls.append((tool, dict(params)))
            if self.fail and self.fail[0] == tool:
                return {"status": "FAILED", "error": {"code": self.fail[1], "message": "boom"}}
            if tool == "verify_state":
                return {"status": "COMPLETED", "cadState": {"activeDocument": "bracket", "documentType": self.doc_type,
                                                            "features": ["Sketch1", "Boss-Extrude1"]}}
            if tool == "save_copy":
                open(params["file_path"], "wb").write(b"copy")
            if tool == "open_document":
                return {"status": "COMPLETED", "cadState": {"activeDocument": os.path.basename(params["file_path"])}}
            return {"status": "COMPLETED"}
        bridge._call_raw = fake
        return self

    def __exit__(self, *a):
        bridge._call_raw = self.real
        if self.old is None:
            os.environ.pop("SOLIDPILOT_CHECKPOINT_DIR", None)
        else:
            os.environ["SOLIDPILOT_CHECKPOINT_DIR"] = self.old
        self.tmp.cleanup()


def test_save_list_restore_round_trip():
    if not _DEPS:
        return
    with _Env() as env:
        assert server.checkpoint("list").startswith("NO_CHECKPOINTS")
        out = server.checkpoint("save", "before-fillet")
        assert out.startswith("SAVED | checkpoint 'before-fillet' = 2 features of 'bracket'")
        saved = [c for c in env.calls if c[0] == "save_copy"][0][1]["file_path"]
        assert saved == os.path.join(env.tmp.name, "before-fillet.sldprt") and os.path.isfile(saved)
        listing = server.checkpoint("list")
        assert "before-fillet" in listing and "2 features" in listing and "last=Boss-Extrude1" in listing
        back = server.checkpoint("restore", "before-fillet")
        assert back.startswith("RESTORED | before-fillet opened as 'before-fillet.sldprt'")
        assert env.calls[-1] == ("open_document", {"file_path": saved})


def test_assemblies_and_drawings_get_their_own_extension():
    if not _DEPS:
        return
    for dt, ext in (("ASSEMBLY", ".sldasm"), ("DRAWING", ".slddrw")):
        with _Env(doc_type=dt) as env:
            server.checkpoint("save", "a")
            assert [c for c in env.calls if c[0] == "save_copy"][0][1]["file_path"].endswith("a" + ext)


def test_refusals():
    if not _DEPS:
        return
    with _Env() as env:
        assert server.checkpoint("save", "../evil").startswith("FAILED | INVALID_NAME")
        assert server.checkpoint("save", "").startswith("FAILED | INVALID_NAME")
        server.checkpoint("save", "one")
        assert server.checkpoint("save", "one").startswith("FAILED | CHECKPOINT_EXISTS")     # never overwritten
        assert server.checkpoint("restore", "nope").startswith("FAILED | CHECKPOINT_NOT_FOUND")
        os.remove(os.path.join(env.tmp.name, "one.sldprt"))
        assert server.checkpoint("restore", "one").startswith("FAILED | CHECKPOINT_FILE_MISSING")


def test_a_failed_copy_is_reported_and_not_indexed():
    if not _DEPS:
        return
    with _Env(fail=("save_copy", "SAVE_FAILED")):
        assert server.checkpoint("save", "x").startswith("FAILED | SAVE_FAILED | SAVE_FAILED: boom")
        assert server.checkpoint("list").startswith("NO_CHECKPOINTS")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
