# adapters/claude — Architecture

Role: **MCP Adapter / Protocol Bridge (Claude)**

The communication bridge between the AI host (Claude) and the system. Pure translation/routing — no CAD logic, no planning logic.

## The MCP boundary sits at the TOP

This is the key architectural decision: **MCP is the boundary where the AI host meets the system — not an internal transport.**

- **Target (Feature Graph architecture):** the adapter exposes a small set of high-level design-session tools (`design_new`, `design_step`, `design_undo`, `design_redo`, `design_history`), plus a few read-only tools like `verify_state`. The host model (Claude) emits the CAD-neutral IR; everything below the IR (compiler → resolver → execution) is deterministic and is reached over plain REST, **not** MCP.
- **Phase 1 (current, transitional):** the adapter still exposes the low-level tools directly (60 today including the orchestration tools, plus adapter-only orchestration tools like `save_analysis` / `rebuild_from_ir`) so the working end-to-end path keeps running while the IR vocabulary grows. The low-level surface collapses into the design tools once the vocabulary and resolver are ready.
- **The recipe/contract prose is a RESOURCE surface, not tools** (ADR-069, 2026-07-30): 13 read-only resources over `cad-planner/` (`recipe://usage/*`, `schema://*`). Tools are for *doing*; resources are for *reading* near-static, caller-independent content. The distinction is economic as well as conceptual — a tool's docstring is paid on every turn, a resource's body only when read.

```
Target:   Claude ──MCP: design_new / design_step──▶ [adapter] ──REST──▶ compiler ──▶ execution
Phase 1:  Claude ──MCP: low-level tools ──────────▶ [adapter] ──REST──▶ execution
          Claude ──MCP: rebuild_from_ir──────────────▶ [adapter] ─(in-process pycompiler)─REST──▶ execution
```

## Layout

`server.py` is a thin entry point; the adapter lives in the `solidpilot/` package:

```
server.py                    imports every tool module (registering its tools), normalises the tool surface
solidpilot/
  app.py                     the shared MCPServer instance (+ server instructions)
  bridge.py                  the ONE door to the execution layer: state_version (BridgeState), the single
                             resync/retry path, _call (mapped text) / _call_raw (raw dict), session facts
  runs.py                    run records: derived operation ids, manifests (graph + sha256 + environment),
                             log correlation — what replay_run reproduces
  design.py                  design sessions: journal of graph snapshots (.solidpilot/designs/), reconcile
                             (common prefix -> truncate feature tree -> recompile suffix), undo/redo, drift check
  jobs.py                    background jobs for long builds (one at a time, progress, cancel)
  clarify.py / expectations.py   open drawing questions + what the last build implies the part should measure
  surface.py                 server instructions, MCP annotations (readOnly/destructive/idempotent)
  payloads.py / paths.py     shared helpers
  tools/                     documents · assembly · sketch · features · drawing_out · export · analysis ·
                             drawing_in · clarify (ask_user) · recipes · compare · graph · checkpoints
```

Tests patch `solidpilot.bridge` (`server.bridge._call_raw`, `server.bridge.call_tool`); tool modules reach
`_call_raw` as `bridge._call_raw(...)` so the patch is seen. `agent.py` (the gateway relay worker) imports
`server` and serves the same surface.

## The 2D-drawing → 3D loop

`analyze_drawing` reads the drawing and either builds directly or returns the analysis. Where the drawing
leaves a decision to a person (a bend note that matches two lines, a missing thickness) it returns typed
**OPEN QUESTIONS** (`cad-planner/drawing/questions.py`): `mode='build'` then answers `NEEDS_INPUT` instead of
building around the doubt. `ask_user` puts them to the user (MCP elicitation when the client supports it,
otherwise a chat script — the remote relay has no client session, so the fallback is its normal path);
`answers=` / `accept_defaults=` release the build. Every build prints an **ASSUMPTIONS LEDGER**, and
`verify_against_interpretation()` with no arguments checks the model against what the drawing itself
implies (volume, round holes), optionally plus design intent (`bolt_circles`, `symmetric`).
`checkpoint` saves/reopens copies to branch alternatives; `replay_run` and `job_status` cover
reproducibility and long builds.

## Responsibilities

- Implement the MCP protocol server (official MCP Python SDK v2 — `MCPServer`, stdio).
- Translate host tool calls into Execution Layer REST requests.
- Generate `operation_id` per call (random for a one-off call; derived from run id + sequence inside a graph
  run) and track `state_version` (`bridge.state`).
- Map `ExecutionResponse` → MCP result (`response_mapper.py`), including `diagnostics` and error hints from
  `execution/solidworks/contracts/error-codes.json`.

## Constraints

- No CAD logic, no planning logic, no COM.
- Provider-specific shell only — the reusable bridge core (id/version/mapping/client) should be factored out so additional adapters (OpenAI, etc.) reuse it instead of re-implementing.

## Model-facing guidance lives HERE, not in the contracts

The host model only ever sees the **MCP tool definition** (docstring → description, type hints → input schema). It does **not** see `tool-schemas.json`. Therefore tool-usage guidance that prevents the model from sending bad data (enums, units, required params, value ranges) must be encoded in the MCP layer — via `Literal` types and Pydantic `Field` constraints — and `tool-schemas.json` must be kept in sync by a contract test, not hand-maintained in parallel (see `tests/test_schema_contract.py`). `tests/test_surface_budget.py` keeps the model-facing surface from regressing (size ceilings, no internal tracker ids, consistent annotations), and `tests/test_compiler_adapter_parity.py` checks that every parameter the compiler emits exists in the adapter schema.
