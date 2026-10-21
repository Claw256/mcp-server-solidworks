"""Minimal file logger for the MCP adapter (P0.6).

Writes to adapter.log next to this module. A FILE (not stdout) because the MCP
server speaks JSON-RPC over stdio — anything on stdout would corrupt the protocol.
Mirrors the C# execution-side ExecLog so the two logs line up per request.

Must NEVER raise — logging failures are swallowed.
"""
import os
import threading
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapter.log")
_lock = threading.Lock()
MAX_BYTES = 5 * 1024 * 1024          # rotate to adapter.log.1 beyond this (one generation kept)


def write(message: str) -> None:
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:                                  # correlate with the active run, if any
            from solidpilot import runs
            rid = runs.run_id()
            if rid:
                message = f"[run {rid[:8]}] {message}"
        except Exception:
            pass
        with _lock:
            try:
                if os.path.getsize(LOG_PATH) > MAX_BYTES:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
            except OSError:
                pass
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(f"{stamp}  {message}\n")
    except Exception:
        # logging must never break a tool call
        pass
