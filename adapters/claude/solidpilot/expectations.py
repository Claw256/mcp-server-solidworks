"""What the last built drawing implies the part should measure (see drawing_verify.derive_expected).

In-memory, per adapter process: it is re-derived from the drawing on every build, so nothing here has
to survive a restart. `verify_against_interpretation` falls back to it when the model gives no numbers.
"""
LAST: dict = {"source": None, "expected": {}}


def remember(source: str, expected: dict) -> None:
    LAST["source"], LAST["expected"] = source, dict(expected or {})


def recall():
    return LAST["source"], dict(LAST["expected"])
