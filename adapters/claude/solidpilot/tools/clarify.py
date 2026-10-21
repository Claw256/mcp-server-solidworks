"""ask_user: put the drawing's open questions to the person, directly when the client supports MCP
elicitation, otherwise as a structured list for the model to relay in chat.

The remote relay (gateway -> agent) calls tools without a client session, so there the fallback is
the normal path; with a direct stdio/HTTP client that declares elicitation, the user gets forms.
"""
import json
from typing import Literal, Optional

from pydantic import Field, create_model
from mcp.server.mcpserver import Context

from solidpilot import clarify
from solidpilot.app import mcp


def _supports_elicitation(ctx) -> bool:
    if ctx is None:
        return False
    try:
        caps = ctx.client_capabilities
    except Exception:                              # no request context (direct call / relay)
        return False
    return bool(caps and getattr(caps, "elicitation", None))


def _answer_model(q: dict):
    """A one-field form: an enum when the options are a closed list, free text otherwise."""
    values = tuple(o["value"] for o in q["options"])
    description = q["prompt"]
    if values and q["kind"] != "thickness":
        field_type = (Literal[values], Field(default=q.get("default") or values[0], description=description))
    else:
        field_type = (str, Field(default=q.get("default") or "", description=description))
    return create_model("Answer_" + q["id"], answer=field_type)


@mcp.tool(structured_output=False)
async def ask_user(question_ids: str = "", ctx: Optional[Context] = None) -> str:
    """Put the OPEN QUESTIONS from the last analyze_drawing to the user and collect the answers.

    question_ids: comma-separated ids (default: every open question). With a client that supports
    elicitation the user gets a form per question; otherwise this returns the questions for YOU to ask
    in chat (options + default) — then pass the replies to analyze_drawing(answers=...). Returns
    `ANSWERS | {json}`, `NEEDS_CHAT | ...` or `DECLINED | ...`."""
    pending = clarify.PENDING
    if not pending:
        return "NO_OPEN_QUESTIONS | run analyze_drawing first; it lists what the drawing leaves open."
    wanted = [i.strip() for i in question_ids.split(",") if i.strip()] or list(pending)
    unknown = [i for i in wanted if i not in pending]
    if unknown:
        return f"FAILED | UNKNOWN_QUESTION | {', '.join(unknown)} (open: {', '.join(pending)})"
    questions = [pending[i] for i in wanted]

    if not _supports_elicitation(ctx):
        return ("NEEDS_CHAT | this client cannot show forms — ask the user these in chat, offering the "
                "options and the default, then call analyze_drawing(..., answers='{\"<id>\": \"<value>\"}').\n"
                + clarify.render(questions, clarify.PENDING_SOURCE["file"] or ""))

    answers = {}
    for q in questions:
        options = "; ".join(f"{o['value']} = {o['label']}" + (f" ({o['evidence']})" if o.get("evidence") else "")
                            for o in q["options"])
        message = q["prompt"] + (f"\nOptions: {options}" if options else "")
        result = await ctx.elicit(message=message, schema=_answer_model(q))
        if result.action != "accept":
            return (f"DECLINED | the user {result.action}ed '{q['id']}' — nothing further was asked. "
                    f"Answered so far: {json.dumps(answers)}")
        answers[q["id"]] = str(result.data.answer)
    return ("ANSWERS | " + json.dumps(answers) + "\nNow re-call analyze_drawing on the same file with "
            "answers=<that JSON> and the mode you wanted.")
