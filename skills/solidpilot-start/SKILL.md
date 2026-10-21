---
name: solidpilot-start
description: Kick-start a SolidPilot session that rebuilds a SolidWorks 3D part or assembly from a 2D drawing (PDF, DXF, DWG or SLDDRW). Use when the user wants to model, recreate or reverse-engineer a part from a drawing with the SolidPilot / SolidWorks MCP tools, or says "start a SolidPilot workflow". Runs the preflight, reads the drawing, asks the user about anything the drawing leaves open, builds, verifies against the drawing and exports.
---

# SolidPilot: drawing to 3D, kick-start

You drive SolidWorks through the SolidPilot MCP tools. The goal is a part that matches the drawing, with every
assumption stated. Work in the order below; do not skip the preflight or the questions step.

## 0. Preflight (always first)

1. Check the SolidPilot tools are connected. If `ensure_ready` is not available, stop and tell the user to
   connect/reconnect the **solidworks** MCP server (the adapter does not hot-reload) and try again.
2. Call `ensure_ready`. It starts the execution server and launches SolidWorks if needed. If it fails, show the
   error text verbatim (it carries a hint) and follow the hint; do not retry blindly.
3. Call `verify_state` to see what is already open. Never close or overwrite the user's open documents without asking.

## 1. Get the drawing

Ask for the drawing if you were not given one: an **absolute path on the PC running SolidWorks** (`.pdf`,
`.dxf`, `.dwg`, `.slddrw`), or an attached PDF/image. Also ask, in one short message, only what you cannot read from
the drawing and that changes the model:

- the material and thickness if not printed (sheet metal especially),
- which revision to use if several files look like revisions of each other,
- whether they want a single part or an assembly from several drawings.

Offer defaults for each question. Do not interrogate; two or three questions at most.

## 2. Read the drawing

- Vector PDF/DXF/DWG/SLDDRW: call `analyze_drawing(file_path)`.
  - `DIRECT_BUILDABLE` (sheet-metal flat patterns): read the summary, answer any OPEN QUESTIONS, then build with
    `analyze_drawing(file_path, mode='build', answers='{...}')`.
  - `NOT_DIRECT | reason` (normal for machined parts): you get the whole analysis; build with `submit_feature_graph`
    (step 3).
- Scanned PDF or image, or when you need to *see* the drawing: `prepare_drawing(file_path)` returns legible image
  tiles plus the exact text layer. Take numbers from the text layer, not from measuring pixels. Use
  `annotate_regions` to check that a location you read is really where you think it is.
- Before interpreting numbers, read the rules: `get_recipe(section='reverse')`, and for machined parts
  `get_recipe(section='machined_from_image')`; before writing a graph, `get_recipe(section='forward')`.

## 3. State your interpretation, then ask

Write a short interpretation table before building: units, projection (first/third angle), overall size, each
feature with its source (`text` or `vision`), and every assumption. Then:

- If `analyze_drawing` listed **OPEN QUESTIONS**, put them to the user with `ask_user` (or in chat, with the options
  and the default). Pass the replies back as `answers='{"<id>": "<value>"}'`. Use `accept_defaults=true` only if the
  user said to use the defaults.
- Ask about anything else that is genuinely ambiguous (a dimension that disagrees between views, an unreadable
  value, a feature you cannot identify). Ask only what the drawing cannot settle. Never silently guess a dimension.

## 4. Build

- Prefer one `submit_feature_graph(graph)` call with the whole feature graph over many low-level tool calls.
  **Lengths are METERS and angles RADIANS in the graph.** The low-level feature tools take degrees in `*_deg`
  parameters.
- Save a restore point before a risky step: `checkpoint(action='save', name='before-holes')`. `checkpoint(action='restore',
  name=...)` reopens it as another document.
- For a build that may take minutes, pass `background=true` and poll `job_status`. While a job runs, other CAD
  calls are refused by design.
- Read each result. A `FEATURE_ERRORS=[...]` or `volume_delta_m3=0` after a feature means it did not do what you meant:
  fix the inputs before building on it. Every graph build ends with `run_id=...`; `replay_run(run_id)` reproduces it
  and `replay_run('')` lists recent runs.
- CAD operations are not transactional. A failed build leaves partial geometry; say so and either fix and
  continue or restart from a checkpoint.

## 5. Verify against the drawing

- After a drawing build, `verify_against_interpretation()` with no arguments checks the model against what the
  drawing itself implies (volume, round holes). Add your own numbers in millimetres when you have them, e.g.
  `{"bbox_mm":[x,y,z],"holes":[{"diameter_mm":8,"count":4}]}`, and design intent where it applies:
  `bolt_circles`, `symmetric`.
- Look at it: `export_image` and compare with the drawing by eye, especially orientation. A mirrored or wrong-side
  feature keeps the volume identical; `cg_mm` and a read-back of one face catch it.
- Report the PASS/FAIL rows honestly, including anything NOT CHECKED.

## 6. Hand over

Summarise in this shape: **verified** (with the numbers), **assumed** (the assumptions ledger, with its source:
drawing, you, or default), **could not model**, **could not read**. Offer to export (`export_document` to STEP/PDF/DXF,
or `deliver_document` when working through the remote gateway) and to move on to the next drawing or the assembly.

## Rules of thumb

- Do the simplest single parts first; do an assembly last, after its parts exist.
- Never invent a tool parameter: the tool descriptions are the contract.
- Quote error text exactly; most errors carry a `hint=` that names the next step, and `retryable=` tells you if
  the same call may be sent again.
- If a call times out (`TOOL_TIMEOUT`), read the message: "NOT executed" means safe to resend; otherwise call
  `verify_state` and look for a dialog in SolidWorks before doing anything else.
