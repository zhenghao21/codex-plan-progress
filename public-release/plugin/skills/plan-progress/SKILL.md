---
name: plan-progress
description: Show and maintain this Codex chat's existing multi-step execution plan in a Windows floating checklist through the local CLI. Use when the user asks to show progress or this chat forms an actionable execution plan; skip ordinary questions with no plan.
---

# Task progress

This edition uses a local CLI. It contains no MCP server or lifecycle hooks. Native `update_plan` calls alone do not update this window.

## Update the plan

Resolve `../../scripts/launcher.ps1` relative to this skill's directory. Save a UTF-8 JSON file containing `plan` and optional `title`, then run the launcher with `set-plan --file <absolute-JSON-path>` using local PowerShell. Pass arguments as separate values. A plan item contains `step` and `status`; accepted statuses are `pending`, `in_progress`, `completed`, and `blocked`.

Each item may contain `children`, an array of actual substeps using the same fields recursively. Keep the entire tree within 100 nodes and 6 levels, counting top-level items as level 1; omit empty `children`. Publish the full tree on each update. Add substeps only when the real plan contains them, never to fill the display.

Child lists start collapsed. Expansion and collapse affect only visibility, not task status. Update every completed substep explicitly. Keep each parent's status as its actual stated status; children finishing does not automatically mark the parent completed.

Displayed progress counts every parent and child node, including collapsed children. The plan is fully complete only when every node explicitly has status `completed`; an unfinished hidden child still counts as unfinished.

Publish the full plan when a real multi-step plan is established, whenever it changes, and immediately after each item actually completes. Do not invent a plan to trigger the window. An ended reply, interruption, elapsed time, or failed operation does not complete pending work. Send an empty `plan` only when there is intentionally no plan to display.

The CLI uses `CODEX_THREAD_ID` or `CODEX_SESSION_ID` from the invoking runtime. If both are supplied, they must agree. If neither exists, pass `--thread-id` only when this same runtime explicitly supplies the current chat ID. Stop the progress update if no reliable ID is available; do not guess from the visible chat, title, recent activity, logs, or another chat. Never change the runtime variables to bypass an ID conflict. A background/subagent task must not publish its separate plan into the parent's chat.

Use `get` to inspect the current chat's stored plan, or `show` when the user asks to display it. Avoid redundant calls when nothing changed. This CLI starts a local window but does not navigate Codex.

## Maintain execution state

For an existing plan, call `set-state --run-state running` when starting or resuming execution. Publishing a plan with `set-plan` also marks execution as running. Before ending the current execution or waiting for the user's response, call `set-state --run-state idle`. For an intentional interruption, call `set-state --run-state interrupted` while execution is still available.

`set-state` changes only execution state; it preserves the plan, item statuses, and completed checkmarks, and does not start another window. Mark completion through actual plan-item updates, not by changing execution state. `get` remains read-only. All commands use the same current-runtime ID and conflict checks described above.

These state updates are explicit. This edition cannot automatically detect a killed process or a hard user interruption that prevents another command from running; its last saved running state may remain until execution resumes or a subsequent explicit state update corrects it. Do not claim automatic pause detection.

## Environment and behavior

The source edition needs Windows, local execution, Python 3.10+ with Tk, and Node.js 22+ on PATH. If packaged runtime executables are supplied, the launcher uses them instead. Do not install dependencies implicitly for a routine plan update; report an unavailable prerequisite while continuing the user's actual task.

The viewer follows the active primary **local Codex chat** through an already enabled loopback debugging endpoint on port 9229 (or `CODEX_PLAN_PROGRESS_CDP_PORT`). It reads active-chat visibility and the composer conversation ID, never message bodies. Sidebar metadata is only used for a matching title. Home, plugin, extension, settings and visible modal views hide the window; hiding the sidebar does not hide a valid chat's plan. This depends on Codex's internal UI structure and is not guaranteed in a clean/default installation. Do not enable debugging, alter startup options, or change app settings as part of a plan update.

If the endpoint, selection, or mapping is unavailable, or the selected chat has no plan, the window hides. A background update must not select its own chat or show the last stored chat as a fallback. Plans and window placement are stored under `%LOCALAPPDATA%\CodexPlanProgressPublic` (or `CODEX_PLAN_PROGRESS_DATA`). The `get` output may enter the invoking conversation, so do not claim that plan content can never leave the device.
