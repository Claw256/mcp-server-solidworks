# execution/solidworks — Architecture

Role: **Execution Layer / Truth Engine (SolidWorks-specific)**

Tech: C# .NET Framework 4.8, ASP.NET Web API 2 (OWIN self-host)

The deterministic execution core. **The only layer allowed to touch the SolidWorks COM API.**

Position in the system:
```
… ─▶ compiler/solidworks ──REST──▶ execution/solidworks ──COM──▶ SolidWorks
```
The compiler (deterministic) drives this layer over plain REST. In Phase 1 the MCP adapter also drives it directly with the low-level tools while the IR vocabulary grows. All COM work runs serialized on a single dedicated STA thread (`StaExecutor`).

## Responsibilities

- Direct interaction with the SolidWorks API.
- Execution of CAD operations.
- State management (documents, sketches, features) — single source of truth for CAD state.
- Input validation and safety checks.
- REST endpoints for all tool operations (`POST /api/tool/execute`).
- Idempotency + `state_version` optimistic concurrency (`OperationGuard`; replay history is bounded to 5000 operations).
- One pipeline for every tool call (`ToolPipeline`): tool lookup from the `[Tool]` registry, duplicate replay,
  `state_version` check, dialog guard, post-tool `diagnostics`, timing. A call that exceeds
  `SOLIDPILOT_TOOL_TIMEOUT_S` (default 15 min) answers `TOOL_TIMEOUT` instead of hanging the caller.
- Feature-tree tools for the compiler's incremental design: `feature_tree` (read-only, ordered user-feature names;
  excludes origin, default planes and system folders) and `truncate_feature_tree(keep_count)` (destructive: deletes
  later features last-first, rebuilds once, bumps `state_version`). Execution-only: no model-facing adapter tool.
- Feature-mutating tools (`[Tool(..., Diagnose = true)]`) run with modal rebuild/feature dialogs suppressed and
  restored afterwards (`SOLIDPILOT_SUPPRESS_DIALOGS=0` disables), and return `diagnostics`: new feature errors
  (`IFeature.GetErrorCode2`), the What's Wrong count and the volume delta.

## Constraints

- Contains NO AI logic and NO planning logic.
- Must be fully deterministic; all operations synchronous and verified before response.
- Adapter-agnostic and compiler-agnostic — zero knowledge of who is calling it.

## Contracts

The SolidWorks-specific low-level contracts now live **here** (moved from the old `solidworks-planner/`):
```
execution/solidworks/contracts/
  tool-schemas.json        ← input/output schema for every low-level CAD tool
  state-format.json        ← CadState structure
  execution-response.json  ← COMPLETED / FAILED / DUPLICATE shapes (the real camelCase wire names)
  error-codes.json         ← every error code: retryable flag + next-step hint for the model
```
`adapters/claude/tests` diffs these against the C# sources (`test_wire_contract.py`, `test_error_registry.py`,
`test_schema_contract.py`), so a code, tool or field added in one place and not the other fails the suite.
The CAD-neutral Feature Graph IR (`cad-planner/contracts/feature-graph.schema.json`) compiles **down** to these tools — the execution layer itself is unaware of the IR.
