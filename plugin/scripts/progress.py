"""Local Codex plan state, lifecycle hook and stdio MCP server. No network I/O."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

STATUSES = {"pending", "in_progress", "completed", "blocked"}
MAX_PLAN_NODES = 100
MAX_PLAN_DEPTH = 6
CONTEXT = (
    "计划进度插件已启用。仅在确实形成多步执行计划时建立并维护计划。平面计划可用 update_plan；"
    "有真实子步骤时或无 update_plan 时，使用本插件 set_plan（传完整 plan，子步骤放 children）。"
    "全树最多100项、6层；不得编造子步骤。每完成一步立即更新对应 status，"
    "进行中用 in_progress，完成用 completed，受阻用 blocked。简单问答不创建计划。"
    "子列表默认折叠只影响显示，不改变状态；父任务完成状态须显式更新，不由子项自动推断。"
    "不要按经过时间、工具次数或回合结束推断完成；未完成的任务必须保留。"
    "本插件小窗跟随桌面当前可见会话；切换到没有计划的会话时隐藏。"
    "计划必须写入工具调用所属会话，不要用后台任务覆盖其他会话的计划。"
)


def default_data_dir():
    return Path(os.environ.get("CODEX_PLAN_PROGRESS_DATA") or
                Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local"))) / "CodexPlanProgress")


def now():
    return datetime.now(timezone.utc).isoformat()


def thread_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise ValueError("缺少有效的当前会话 ID；请从 Codex 会话内调用。")
    return value


def normalize_plan(value):
    count = 0

    def normalize_nodes(nodes, depth):
        nonlocal count
        if not isinstance(nodes, list):
            raise ValueError("plan 和 children 必须是数组。")
        if nodes and depth > MAX_PLAN_DEPTH:
            raise ValueError("计划最多 6 层，顶层计为第 1 层。")
        if count + len(nodes) > MAX_PLAN_NODES:
            raise ValueError("计划包含子步骤在内最多 100 项。")
        result = []
        for item in nodes:
            count += 1
            if count > MAX_PLAN_NODES:
                raise ValueError("计划包含子步骤在内最多 100 项。")
            if not isinstance(item, dict):
                raise ValueError("每项必须包含 step 和 status。")
            step, status = item.get("step"), item.get("status")
            if not isinstance(step, str) or not step.strip() or len(step) > 2000:
                raise ValueError("step 必须是 1 至 2000 字的任务名称。")
            if not isinstance(status, str) or status not in STATUSES:
                raise ValueError("status 必须为 pending/in_progress/completed/blocked。")
            node = {"step": step.strip(), "status": status}
            if "children" in item:
                children = normalize_nodes(item["children"], depth + 1)
                if children:
                    node["children"] = children
            result.append(node)
        return result

    return normalize_nodes(value, 1)


def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return fallback


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        # Antivirus or a short-lived reader can briefly hold the destination on Windows.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.025)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "state.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 3
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("进度文件正忙，请重试。")
                time.sleep(0.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, ident):
        return self.directory / "threads" / (thread_id(ident) + ".json")

    def get(self, ident):
        return read_json(self.path(ident), {"thread_id": ident, "title": "当前会话",
            "plan": [], "run_state": "idle", "updated_at": now(), "source": "plugin"})

    def update(self, ident, *, plan=None, title=None, run_state=None, source=None,
               activate=False, turn_id=None, new_turn=False):
        ident = thread_id(ident)
        if plan is not None:
            plan = normalize_plan(plan)
        if title is not None and (not isinstance(title, str) or len(title) > 500):
            raise ValueError("title 必须是最多 500 字的文字。")
        with state_lock(self.directory):
            state = self.get(ident)
            # A late Stop/Interrupt from the previous turn must not pause the new turn.
            if (turn_id and state.get("turn_id") and turn_id != state["turn_id"]
                    and run_state in {"idle", "interrupted"} and not new_turn):
                return state
            if new_turn:
                # Continuing or asking to view progress must preserve the previous plan.
                state["turn_id"] = turn_id
            elif turn_id:
                state["turn_id"] = turn_id
            if plan is not None:
                state["plan"] = plan
            if title is not None:
                state["title"] = title.strip() or "当前会话"
            if run_state is not None:
                if run_state not in {"running", "idle", "interrupted"}:
                    raise ValueError("无效的执行状态。")
                state["run_state"] = run_state
            if source:
                state["source"] = source
            state["updated_at"] = now()
            atomic_json(self.path(ident), state)
            current = read_json(self.directory / "current.json", {})
            if activate or not current or current.get("thread_id") == ident:
                atomic_json(self.directory / "current.json", state)
            return state


def launch_overlay(directory):
    if os.environ.get("CODEX_PLAN_PROGRESS_NO_UI") == "1":
        return
    script = Path(__file__).with_name("overlay.py")
    executable = Path(sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "overlay.log").open("ab") as output:
        subprocess.Popen([str(executable), str(script), "--data-dir", str(directory)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
            start_new_session=os.name != "nt", close_fds=True)


def is_subagent(event):
    if event.get("agent_id") or event.get("agent_type"):
        return True
    path = event.get("transcript_path")
    if path:
        try:
            with Path(path).open(encoding="utf-8") as stream:
                first = json.loads(stream.readline())
            payload = first.get("payload", {})
            if first.get("type") == "session_meta":
                origin = payload.get("source")
                return (payload.get("thread_source") in {"agent", "subagent"} or
                        isinstance(origin, dict) and "subagent" in origin or
                        payload.get("id", event.get("session_id")) != event.get("session_id"))
        except (OSError, ValueError):
            pass
    return False


def handle_hook(event, store):
    if not isinstance(event, dict):
        raise ValueError("hook 输入必须是 JSON 对象。")
    if is_subagent(event):
        return None
    ident = thread_id(event.get("session_id"))
    name, turn = event.get("hook_event_name"), event.get("turn_id")
    if name == "UserPromptSubmit":
        prompt = event.get("prompt", "")
        title = prompt.strip().splitlines()[0][:80] if isinstance(prompt, str) and prompt.strip() else None
        if store.get(ident).get("title") not in {None, "", "当前会话"}:
            title = None
        store.update(ident, title=title, run_state="running", activate=True, turn_id=turn, new_turn=True)
        launch_overlay(store.directory)
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": CONTEXT}}
    if name == "SessionStart":
        # Restoring a background session must not steal the foreground plan.
        store.update(ident)
        launch_overlay(store.directory)
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": CONTEXT}}
    if name == "PostToolUse":
        tool = event.get("tool_name", "").split(".")[-1]
        response = event.get("tool_response")
        if isinstance(response, dict) and (response.get("isError") or response.get("error")):
            return None
        if tool == "update_plan" or tool.endswith("__update_plan"):
            # update_plan reports "Plan updated" on success. Unknown or failed
            # output must not make an attempted completion appear successful.
            output = response
            if isinstance(response, dict):
                output = response.get("output") or "\n".join(
                    part.get("text", "") for part in response.get("content", [])
                    if isinstance(part, dict) and part.get("type") == "text")
            if not isinstance(output, str) or output.strip().rstrip(".").lower() != "plan updated":
                return None
            args = event.get("tool_input", {})
            if isinstance(args, str):
                args = json.loads(args)
            store.update(ident, plan=args.get("plan"), run_state="running", source="update_plan", turn_id=turn)
            launch_overlay(store.directory)
    elif name in {"Stop", "Interrupt", "SessionEnd"}:
        store.update(ident, run_state="interrupted" if name == "Interrupt" else "idle", turn_id=turn)
    return None


def request_identity(params):
    args = params.get("arguments") or {}
    meta = params.get("_meta") or {}
    wire = meta.get("x-codex-turn-metadata", {})
    if isinstance(wire, str):
        try:
            wire = json.loads(wire)
        except ValueError:
            wire = {}
    if not isinstance(wire, dict):
        wire = {}
    contextual = wire.get("thread_id") or meta.get("openai/threadId") or meta.get("threadId")
    explicit = args.get("thread_id")
    if contextual and explicit and contextual != explicit:
        raise ValueError("传入会话与当前工具调用会话不一致。")
    return thread_id(contextual or explicit or os.environ.get("CODEX_THREAD_ID") or
                     os.environ.get("CODEX_SESSION_ID")), wire.get("turn_id")


def plan_schema(depth=MAX_PLAN_DEPTH):
    # Expand the bounded tree directly so tool clients need not resolve $ref.
    properties = {"step": {"type": "string", "minLength": 1, "maxLength": 2000},
                  "status": {"type": "string", "enum": sorted(STATUSES)}}
    if depth > 1:
        properties["children"] = plan_schema(depth - 1)
    return {"type": "array", "maxItems": MAX_PLAN_NODES, "items": {"type": "object",
        "properties": properties, "required": ["step", "status"], "additionalProperties": False}}


PLAN_SCHEMA = plan_schema()
TOOLS = [
    {"name": "set_plan", "description": "Create or update the current chat execution plan. Send the complete plan; real substeps use optional children arrays. Maximum 100 nodes across the entire tree and 6 levels including the top level. Every node has an explicit status; never infer parent completion from child statuses or collapsed display. Update after each completed step, not elapsed time or end of turn. Empty plan hides the window.",
     "inputSchema": {"type": "object", "properties": {"plan": PLAN_SCHEMA,
         "title": {"type": "string", "maxLength": 500}, "thread_id": {"type": "string"}}, "required": ["plan"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_progress", "description": "Read the stored plan tree and completion count across all parent and child nodes for the current chat.",
     "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "string"}}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "show_progress", "description": "Start the progress floating window. It automatically displays the currently visible Codex chat's stored plan, and hides when that chat has no plan. This does not navigate Codex or override the visible chat selection.",
     "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "string"}}, "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
]


def call_tool(params, store):
    name = params.get("name")
    if name not in {t["name"] for t in TOOLS}:
        raise ValueError("未知进度工具。")
    ident, turn = request_identity(params)
    args = params.get("arguments") or {}
    if name == "set_plan":
        if "plan" not in args:
            raise ValueError("缺少 plan。")
        state = store.update(ident, plan=args["plan"], title=args.get("title"),
            run_state="running", source="set_plan", turn_id=turn)
        launch_overlay(store.directory)
    elif name == "show_progress":
        state = store.update(ident, activate=True)
        launch_overlay(store.directory)
    else:
        state = store.get(ident)
    result = dict(state)
    completed = total = 0
    nodes = list(state["plan"])
    while nodes:
        node = nodes.pop()
        total += 1
        completed += node["status"] == "completed"
        nodes.extend(node.get("children", []))
    result["completed"] = completed
    result["total"] = total
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}


def serve(store):
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("Invalid request")
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        if "id" not in request:
            continue
        answer = {"jsonrpc": "2.0", "id": request["id"]}
        method, params = request.get("method"), request.get("params") or {}
        try:
            if method == "initialize":
                supported = {"2024-11-05", "2025-03-26", "2025-06-18"}
                version = params.get("protocolVersion")
                answer["result"] = {"protocolVersion": version if version in supported else "2025-06-18",
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-plan-progress", "version": "1.2.0"},
                    "instructions": CONTEXT}
            elif method == "ping":
                answer["result"] = {}
            elif method == "tools/list":
                answer["result"] = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    answer["result"] = call_tool(params, store)
                except (ValueError, OSError, TimeoutError) as error:
                    answer["result"] = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
            else:
                answer["error"] = {"code": -32601, "message": "Method not found"}
        except (ValueError, TypeError, AttributeError) as error:
            answer["error"] = {"code": -32602, "message": str(error)}
        print(json.dumps(answer, ensure_ascii=False), flush=True)


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Codex 当前会话计划进度")
    parser.add_argument("command", choices=["serve", "hook", "set-plan", "show", "get"])
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID"))
    parser.add_argument("--json", dest="json_value")
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    store = Store(args.data_dir)
    if args.command == "serve":
        serve(store)
    elif args.command == "hook":
        result = handle_hook(json.load(sys.stdin), store)
        if result:
            print(json.dumps(result, ensure_ascii=False))
    else:
        ident = thread_id(args.thread_id)
        if args.command == "set-plan":
            data = (read_json(args.file) if args.file else json.loads(args.json_value)
                    if args.json_value else json.load(sys.stdin))
            if isinstance(data, list):
                data = {"plan": data}
            state = store.update(ident, plan=data["plan"], title=data.get("title"), run_state="running", source="local")
            launch_overlay(store.directory)
        elif args.command == "show":
            state = store.update(ident, activate=True)
            launch_overlay(store.directory)
        else:
            state = store.get(ident)
        print(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TimeoutError, KeyError) as exc:
        print("Codex Plan Progress: " + str(exc), file=sys.stderr)
        sys.exit(1)
