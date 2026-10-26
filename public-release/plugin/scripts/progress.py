"""Current-chat plan storage and local CLI. No MCP server or lifecycle hooks."""
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


def default_data_dir():
    return Path(os.environ.get("CODEX_PLAN_PROGRESS_DATA") or
                Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local"))) / "CodexPlanProgressPublic")


def now():
    return datetime.now(timezone.utc).isoformat()


def thread_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise ValueError("缺少有效的当前会话 ID；请从 Codex 会话内调用。")
    return value


def current_thread(explicit=None):
    """Bind writes to the invoking runtime, never the visible or most recent chat."""
    contextual = [os.environ[key] for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID")
                  if os.environ.get(key)]
    if len(set(contextual)) > 1:
        raise ValueError("运行时提供的会话 ID 不一致；未写入任何计划。")
    if explicit and contextual and explicit != contextual[0]:
        raise ValueError("传入会话与当前运行时会话不一致；未写入任何计划。")
    return thread_id(contextual[0] if contextual else explicit)


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
               activate=False):
        ident = thread_id(ident)
        if plan is not None:
            plan = normalize_plan(plan)
        if title is not None and (not isinstance(title, str) or len(title) > 500):
            raise ValueError("title 必须是最多 500 字的文字。")
        with state_lock(self.directory):
            state = self.get(ident)
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
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).with_name("plan-progress-window.exe")
        if not executable.is_file():
            raise FileNotFoundError("缺少配套的 plan-progress-window.exe。")
        command = [str(executable)]
    else:
        script = Path(__file__).with_name("overlay.py")
        executable = Path(sys.executable)
        if os.name == "nt" and executable.with_name("pythonw.exe").exists():
            executable = executable.with_name("pythonw.exe")
        command = [str(executable), str(script)]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "overlay.log").open("ab") as output:
        subprocess.Popen(command + ["--data-dir", str(directory)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
            start_new_session=os.name != "nt", close_fds=True)


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Codex 当前会话计划进度")
    parser.add_argument("command", choices=["set-plan", "set-state", "show", "get"])
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--thread-id", help="Only the current chat ID supplied by the runtime")
    parser.add_argument("--json", dest="json_value")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--run-state", choices=["running", "idle", "interrupted"])
    args = parser.parse_args()
    if args.command == "set-state" and args.run_state is None:
        parser.error("set-state requires --run-state.")
    if args.command != "set-state" and args.run_state is not None:
        parser.error("--run-state is only valid with set-state.")
    store = Store(args.data_dir)
    ident = current_thread(args.thread_id)
    if args.command == "set-plan":
        data = (read_json(args.file) if args.file else json.loads(args.json_value)
                if args.json_value else json.load(sys.stdin))
        if isinstance(data, list):
            data = {"plan": data}
        if not isinstance(data, dict) or "plan" not in data:
            raise ValueError("JSON 必须包含 plan 数组。")
        state = store.update(ident, plan=data["plan"], title=data.get("title"), run_state="running", source="local-cli")
        launch_overlay(store.directory)
    elif args.command == "set-state":
        state = store.update(ident, run_state=args.run_state, source="local-cli")
    elif args.command == "show":
        state = store.get(ident)
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
