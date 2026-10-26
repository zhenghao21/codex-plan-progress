---
name: plan-progress
description: Show and maintain this Codex chat's existing multi-step execution plan in the Windows task-progress window. Use when the user asks to show progress or when this chat forms an actionable execution plan; skip ordinary questions with no plan.
---

# Task progress

Keep the floating checklist aligned with the actual plan. Do not invent a plan merely to display the window.

For a nested plan, check that the loaded `set_plan` tool accepts and preserves `children`. An already open chat may still have the previous flat tool definition. If `children` is absent from that definition or missing from the returned plan, publish the complete nested JSON through the local launcher below instead; never silently flatten or lose substeps.

- Prefer the available `set_plan` MCP tool to publish the complete list. Each item has `step` and `status`; supported statuses are `pending`, `in_progress`, `completed`, and `blocked`. Include the current chat's `thread_id` when it is known. Never substitute a different chat's ID. The MCP server can use the caller's thread metadata when no ID is supplied.
- A node may include `children`, an array of real substeps with the same `step`, `status`, and optional `children` fields. Send the whole tree when updating it. The entire tree is limited to 100 nodes and 6 levels, with the top level counted as level 1. Omit empty `children`; nodes at level 6 have no children. Do not invent substeps to populate an expandable list.
- Child lists start collapsed. Expanding or collapsing changes only the display, never any status. Update each finished substep explicitly and keep the parent status explicit; completed children do not automatically complete their parent.
- Progress counts every node in the tree, including parents and children. A hidden unfinished child remains in the total; all nodes must explicitly be `completed` before the plan is fully complete.
- Flat plans can use native `update_plan`, whose successful calls are mirrored by the trusted plugin hook. For nested plans use `set_plan`, because the native tool does not carry `children`; do not overwrite a nested plan with a flat update. Update the plan as execution changes and immediately after completing each item.
- Mark an item completed only when its work has actually finished. An ended assistant reply, failed operation, interruption, or blocked task does not make pending items complete.
- `show_progress` starts the window; `get_progress` reads the calling chat's plan. The window follows the active primary local chat in Codex. It checks the active chat container and the composer conversation ID through the already enabled local debugging endpoint, never message bodies. Sidebar metadata is used only for a matching title. Home, plugin, extension, settings and modal views hide it, as do chats without a stored plan. Hiding the sidebar does not hide a valid chat's plan. If the active chat cannot be established, the window hides rather than showing another chat's plan.
- Do not call a progress tool if no list or state changed, except when the user explicitly asks to show or inspect progress.

If the MCP tools are unavailable, use the local launcher at `../../scripts/launcher.ps1` relative to this skill directory. `show --thread-id <ID>` opens the window. Save a UTF-8 JSON file with `plan` and optional `title`, then call `set-plan --file <absolute-JSON-path> --thread-id <ID>`. Pass the thread ID through the command argument, not inside the JSON. A JSON file avoids shell quoting errors. Use the actual chat ID supplied by the runtime; do not guess an ID or bind whichever chat was most recently modified.

Automatic hooks require the user to review and trust them through Codex `/hooks`. Installation does not grant hook trust. Never bypass this review. Until trusted, MCP `set_plan` or the local launcher can still maintain a plan. Viewing follows the visible chat independently of tool calls, so background plan updates never change the selected chat. Do not enable a debugging port or change Codex startup settings without user authorization; the installer uses the existing loopback endpoint on port 9229 (or `CODEX_PLAN_PROGRESS_CDP_PORT`).
