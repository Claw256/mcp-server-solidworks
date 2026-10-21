"""checkpoint: save a copy of the work in progress, list copies, and reopen one to branch from it.

CAD operations are not transactional, so a bad feature cannot be rolled back. A checkpoint is a plain
file copy (SolidWorks `SaveAs3` with the Copy option: the active document keeps its name, path and
feature tree). Restoring OPENS the copy as another document, so both the original and the branch stay
available; compare them with compare_parts(doc_a, doc_b) or verify each with analyze_model.
"""
import json
import os
import re
from datetime import datetime, timezone
from typing import Literal

from solidpilot import bridge, runs
from solidpilot.app import mcp
from solidpilot.paths import ADAPTER_DIR

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_EXT = {"PART": ".sldprt", "ASSEMBLY": ".sldasm", "DRAWING": ".slddrw"}


def checkpoint_dir() -> str:
    return os.environ.get("SOLIDPILOT_CHECKPOINT_DIR") or os.path.join(ADAPTER_DIR, ".solidpilot", "checkpoints")


def _index_path() -> str:
    return os.path.join(checkpoint_dir(), "index.json")


def _load_index() -> dict:
    try:
        with open(_index_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_index(index: dict) -> None:
    os.makedirs(checkpoint_dir(), exist_ok=True)
    with open(_index_path(), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1, ensure_ascii=False)


@mcp.tool(structured_output=False)
def checkpoint(action: Literal["save", "restore", "list"] = "list", name: str = "") -> str:
    """Save, list or reopen a named copy of the document you are building — to try an alternative without
    losing the current state, or to get back after a bad feature (CAD ops cannot be undone in bulk).

    action='save' (name required): store a copy of the ACTIVE document; it keeps being edited as before.
    action='restore' (name required): OPEN that copy as another document and make it active — the original
        stays open (activate_document switches back; compare_parts(doc_a, doc_b) compares the two).
    action='list': the saved checkpoints, newest first.
    name: letters, digits, '_' or '-' (max 40). Saving over an existing name is refused."""
    if action == "list":
        index = _load_index()
        if not index:
            return "NO_CHECKPOINTS | none saved yet (checkpoint(action='save', name=...))"
        rows = sorted(index.items(), key=lambda kv: kv[1]["saved_utc"], reverse=True)
        return "CHECKPOINTS (newest first)\n" + "\n".join(
            f"  {n:<24} {e['document']:<24} {e['features']} features  last={e['last_feature']}  {e['saved_utc']}"
            for n, e in rows)
    if not _NAME.match(name or ""):
        return "FAILED | INVALID_NAME | name must be 1-40 characters: letters, digits, '_' or '-', starting alphanumeric"
    index = _load_index()
    if action == "restore":
        entry = index.get(name)
        if entry is None:
            return f"FAILED | CHECKPOINT_NOT_FOUND | {name} (saved: {', '.join(sorted(index)) or 'none'})"
        if not os.path.isfile(entry["path"]):
            return f"FAILED | CHECKPOINT_FILE_MISSING | {entry['path']} was deleted or moved"
        r = bridge._call_raw("open_document", {"file_path": entry["path"]})
        if r.get("status") != "COMPLETED":
            err = r.get("error") or {}
            return f"FAILED | RESTORE_FAILED | {err.get('code')}: {err.get('message')}"
        title = (r.get("cadState") or {}).get("activeDocument")
        return (f"RESTORED | {name} opened as '{title}' and is now ACTIVE — the document you were building is "
                f"still open; activate_document switches back and compare_parts compares the two.")
    # save
    if name in index:
        return f"FAILED | CHECKPOINT_EXISTS | {name} already exists; choose a new name (checkpoints are never overwritten)"
    state = bridge._call_raw("verify_state", {})
    if state.get("status") != "COMPLETED":
        err = state.get("error") or {}
        return f"FAILED | NO_DOCUMENT | {err.get('code')}: {err.get('message')}"
    cad = state.get("cadState") or {}
    ext = _EXT.get(cad.get("documentType") or "PART", ".sldprt")
    path = os.path.join(checkpoint_dir(), name + ext)
    r = bridge._call_raw("save_copy", {"file_path": path})
    if r.get("status") != "COMPLETED":
        err = r.get("error") or {}
        return f"FAILED | SAVE_FAILED | {err.get('code')}: {err.get('message')}"
    feats = cad.get("features") or []
    index[name] = {"path": path, "document": cad.get("activeDocument"), "features": len(feats),
                   "last_feature": feats[-1] if feats else None, "run_id": runs.run_id() or None,
                   "saved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        _save_index(index)
    except OSError as ex:
        return f"FAILED | INDEX_NOT_SAVED | the copy exists at {path} but the index could not be written: {ex}"
    return (f"SAVED | checkpoint '{name}' = {len(feats)} features of '{cad.get('activeDocument')}' -> {path}. "
            f"Keep building; checkpoint(action='restore', name='{name}') reopens it.")
