"""ask_user: elicitation when the client supports it, a structured chat fallback otherwise."""
import asyncio
import os
import sys
from types import SimpleNamespace

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

try:
    from solidpilot import clarify
    from solidpilot.tools import clarify as tool
    _DEPS = True
except Exception:  # noqa: BLE001
    _DEPS = False

BEND = {"id": "bend_1", "kind": "skipped_bend", "field": "bend_line", "blocking": True, "default": "skip",
        "prompt": "Which line?", "options": [{"value": "l:5", "label": "bend line l5", "evidence": "line l5"},
                                             {"value": "skip", "label": "leave unbuilt"}]}
THICK = {"id": "thickness", "kind": "thickness", "field": "thickness_mm", "blocking": True, "default": None,
         "prompt": "Thickness?", "options": []}


class _Ctx:
    """Fake MCP context. `replies` maps question id -> (action, answer)."""
    def __init__(self, replies=None, elicitation=True):
        self.replies, self.asked = replies or {}, []
        self._caps = SimpleNamespace(elicitation=object() if elicitation else None)

    @property
    def client_capabilities(self):
        return self._caps

    async def elicit(self, message, schema):
        qid = schema.__name__.replace("Answer_", "")
        self.asked.append((qid, message, schema))
        action, answer = self.replies[qid]
        data = schema(answer=answer) if action == "accept" else None
        return SimpleNamespace(action=action, data=data)


def _run(**kw):
    return asyncio.run(tool.ask_user(**kw))


def test_nothing_pending():
    if not _DEPS:
        return
    clarify.PENDING.clear()
    assert _run().startswith("NO_OPEN_QUESTIONS")


def test_no_elicitation_falls_back_to_a_chat_script():
    if not _DEPS:
        return
    clarify.remember("part.dxf", [BEND])
    for ctx in (None, _Ctx(elicitation=False)):
        out = _run(ctx=ctx)
        assert out.startswith("NEEDS_CHAT") and "[bend_1]" in out and "l:5 = bend line l5" in out


def test_elicitation_collects_answers_and_validates_enum_choices():
    if not _DEPS:
        return
    clarify.remember("part.dxf", [BEND, THICK])
    ctx = _Ctx({"bend_1": ("accept", "l:5"), "thickness": ("accept", "2.5")})
    out = _run(ctx=ctx)
    assert out.startswith('ANSWERS | {"bend_1": "l:5", "thickness": "2.5"}'), out
    # the closed list is an enum in the form's schema; thickness is free text
    props = {q: s.model_json_schema()["properties"]["answer"] for q, _m, s in ctx.asked}
    assert props["bend_1"].get("enum") == ["l:5", "skip"] and props["thickness"]["type"] == "string"


def test_decline_stops_and_reports_what_was_answered():
    if not _DEPS:
        return
    clarify.remember("part.dxf", [BEND, THICK])
    out = _run(ctx=_Ctx({"bend_1": ("accept", "skip"), "thickness": ("decline", None)}))
    assert out.startswith("DECLINED") and '"bend_1": "skip"' in out


def test_unknown_id_and_subset_selection():
    if not _DEPS:
        return
    clarify.remember("part.dxf", [BEND, THICK])
    assert _run(question_ids="nope", ctx=None).startswith("FAILED | UNKNOWN_QUESTION")
    ctx = _Ctx({"thickness": ("accept", "3")})
    assert _run(question_ids="thickness", ctx=ctx).startswith('ANSWERS | {"thickness": "3"}')
    assert [q for q, _m, _s in ctx.asked] == ["thickness"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
