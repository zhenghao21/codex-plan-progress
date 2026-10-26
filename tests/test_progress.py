"""Behavioral checks for plan events, thread isolation and the real stdio transport."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "plugin" / "scripts" / "progress.py"
spec = importlib.util.spec_from_file_location("progress_under_test", SCRIPT)
progress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress)


def plan(*statuses):
    return [{"step": f"中文任务 {index + 1}", "status": status}
            for index, status in enumerate(statuses)]


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-plan-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = progress.Store(self.root)
        self.env = patch.dict(os.environ, {"CODEX_PLAN_PROGRESS_NO_UI": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def hook(self, event, ident="chat-a", turn="turn-a", **extra):
        return progress.handle_hook({"hook_event_name": event, "session_id": ident,
                                     "turn_id": turn, **extra}, self.store)

    def tool(self, name="set_plan", ident="chat-a", turn="turn-a", **arguments):
        return progress.call_tool({"name": name, "arguments": arguments,
            "_meta": {"x-codex-turn-metadata": json.dumps({"thread_id": ident, "turn_id": turn})}},
            self.store)["structuredContent"]

    def current(self):
        return json.loads((self.root / "current.json").read_text(encoding="utf-8"))

    def test_no_plan_is_inferred_from_conversation_or_ordinary_tools(self):
        self.hook("UserPromptSubmit", prompt="请完成：1、读取文件。2、生成结果。")
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "echo 完成"},
                  tool_response={"exit_code": 0})
        self.hook("Stop")
        self.assertEqual(self.current()["plan"], [])

    def test_structured_updates_replace_entire_plan_and_keep_unicode(self):
        self.hook("UserPromptSubmit", prompt="插件开发进度")
        self.hook("PostToolUse", tool_name="functions.update_plan",
                  tool_input={"plan": plan("in_progress", "pending")}, tool_response="Plan updated")
        self.assertEqual(self.current()["plan"], plan("in_progress", "pending"))
        self.hook("PostToolUse", tool_name="update_plan",
                  tool_input=json.dumps({"plan": plan("completed")}), tool_response="Plan updated")
        self.assertEqual(self.current()["plan"], plan("completed"))
        self.assertEqual(self.current()["title"], "插件开发进度")

    def test_failed_tool_never_marks_work_completed(self):
        self.store.update("chat-a", plan=plan("in_progress"), turn_id="turn-a")
        self.hook("PostToolUse", tool_name="update_plan",
                  tool_input={"plan": plan("completed")}, tool_response={"isError": True})
        self.assertEqual(self.current()["plan"], plan("in_progress"))

    def test_unknown_or_text_error_from_plan_tool_is_not_success(self):
        self.store.update("chat-a", plan=plan("in_progress"), turn_id="turn-a")
        for response in ("Error: invalid plan", "Tool failed", None, {}):
            self.hook("PostToolUse", tool_name="update_plan",
                      tool_input={"plan": plan("completed")}, tool_response=response)
            self.assertEqual(self.current()["plan"], plan("in_progress"))

    def test_stop_and_interrupt_preserve_unfinished_tasks(self):
        original = plan("completed", "in_progress", "pending", "blocked")
        self.store.update("chat-a", plan=original, turn_id="turn-a")
        self.hook("Interrupt")
        self.assertEqual(self.current()["run_state"], "interrupted")
        self.assertEqual(self.current()["plan"], original)
        self.hook("Stop")
        self.assertEqual(self.current()["run_state"], "idle")
        self.assertEqual(self.current()["plan"], original)

    def test_background_thread_cannot_steal_selected_plan(self):
        self.hook("UserPromptSubmit", ident="chat-a", prompt="A")
        self.hook("UserPromptSubmit", ident="chat-b", turn="turn-b", prompt="B")
        self.tool(ident="chat-b", turn="turn-b", plan=plan("in_progress"))
        self.tool(ident="chat-a", plan=plan("completed"))
        self.hook("SessionStart", ident="chat-a")
        self.assertEqual(self.current()["thread_id"], "chat-b")
        self.assertEqual(self.current()["plan"], plan("in_progress"))
        self.tool("show_progress", ident="chat-a")
        self.assertEqual(self.current()["thread_id"], "chat-a")
        self.assertEqual(self.current()["plan"], plan("completed"))

    def test_late_stop_does_not_pause_a_new_turn(self):
        self.hook("UserPromptSubmit", turn="old-turn")
        self.hook("UserPromptSubmit", turn="new-turn")
        self.tool(turn="new-turn", plan=plan("in_progress"))
        self.hook("Stop", turn="old-turn")
        self.hook("Interrupt", turn="old-turn")
        self.assertEqual(self.current()["turn_id"], "new-turn")
        self.assertEqual(self.current()["run_state"], "running")

    def test_mcp_only_fallback_can_update_plan_in_next_turn(self):
        self.tool(turn="first-turn", plan=plan("pending"))
        self.tool(turn="second-turn", plan=plan("completed"))
        self.assertEqual(self.current()["plan"], plan("completed"))
        self.assertEqual(self.current()["turn_id"], "second-turn")

    def test_requesting_progress_does_not_delete_the_existing_plan(self):
        existing = plan("completed", "pending")
        self.tool(plan=existing)
        self.hook("UserPromptSubmit", turn="next-turn", prompt="显示进度")
        result = self.tool("show_progress", turn="next-turn")
        self.assertEqual(result["plan"], existing)

    def test_empty_plan_explicitly_hides_progress(self):
        self.tool(plan=plan("completed"))
        self.tool(plan=[])
        self.assertEqual(self.current()["plan"], [])

    def test_subagent_events_do_not_overwrite_parent(self):
        self.tool(plan=plan("in_progress"))
        self.hook("PostToolUse", agent_id="child-1", tool_name="update_plan",
                  tool_input={"plan": plan("completed")})
        transcript = self.root / "child.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload":
            {"id": "child-1", "thread_source": "agent", "source": {"subagent": "review"}}}) + "\n",
            encoding="utf-8")
        self.hook("PostToolUse", transcript_path=str(transcript), tool_name="update_plan",
                  tool_input={"plan": plan("completed")})
        self.assertEqual(self.current()["plan"], plan("in_progress"))

    def test_invalid_plan_leaves_previous_state_intact(self):
        self.tool(plan=plan("pending"))
        for invalid in ["text plan", [{"step": "x", "status": "done"}],
                        [{"step": "", "status": "pending"}], [{}]]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.tool(plan=invalid)
                self.assertEqual(self.current()["plan"], plan("pending"))

    def test_thread_path_traversal_and_metadata_mismatch_are_rejected(self):
        for bad in ["../escape", "C:\\outside", "a/b", "", None]:
            with self.subTest(thread=bad), self.assertRaises(ValueError):
                self.store.update(bad, plan=[])
        with self.assertRaises(ValueError):
            progress.call_tool({"name": "set_plan", "arguments": {"thread_id": "other", "plan": []},
                "_meta": {"openai/threadId": "actual"}}, self.store)

    def run_stdio(self, requests):
        raw = "\n".join(json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
                        for x in requests) + "\n"
        result = subprocess.run([sys.executable, str(SCRIPT), "serve", "--data-dir", str(self.root)],
            input=raw, text=True, encoding="utf-8", capture_output=True, timeout=15,
            env={**os.environ, "CODEX_PLAN_PROGRESS_NO_UI": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_real_stdio_round_trip(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params":
                {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        for ident, name, args in [(3, "set_plan", {"title": "实时任务 ✓", "plan": plan("completed", "blocked")}),
                                  (4, "get_progress", {})]:
            requests.append({"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params":
                {"name": name, "arguments": args, "_meta": {"x-codex-turn-metadata":
                    json.dumps({"thread_id": "wire-chat", "turn_id": "wire-turn"})}}})
        answers = self.run_stdio(requests)
        self.assertEqual([answer["id"] for answer in answers], [1, 2, 3, 4])
        self.assertEqual(answers[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual({tool["name"] for tool in answers[1]["result"]["tools"]},
                         {"set_plan", "get_progress", "show_progress"})
        state = answers[-1]["result"]["structuredContent"]
        self.assertEqual((state["thread_id"], state["title"], state["completed"], state["total"]),
                         ("wire-chat", "实时任务 ✓", 1, 2))

    def test_bad_stdio_input_is_reported_and_server_keeps_running(self):
        answers = self.run_stdio(["{broken", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "set_plan", "arguments": {"thread_id": "safe", "plan": "not a list"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}])
        self.assertEqual(answers[0]["error"]["code"], -32700)
        self.assertTrue(answers[1]["result"]["isError"])
        self.assertEqual(answers[2]["result"], {})

    def test_concurrent_process_updates_keep_complete_json_and_selection(self):
        self.store.update("selected", plan=plan("pending"), activate=True)
        def writer(index):
            requests = [{"jsonrpc": "2.0", "id": index * 10 + j, "method": "tools/call", "params":
                {"name": "set_plan", "arguments": {"thread_id": f"background-{index}",
                    "plan": [{"step": f"worker {index} update {j}", "status": "completed"}]}}}
                for j in range(5)]
            return self.run_stdio(requests)
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(writer, range(4)))
        self.assertTrue(all(not answer["result"].get("isError") for batch in responses for answer in batch))
        self.assertEqual(self.current()["thread_id"], "selected")
        for index in range(4):
            state = self.store.get(f"background-{index}")
            self.assertEqual(state["plan"][0]["step"], f"worker {index} update 4")
        self.assertEqual(list(self.root.rglob(".write-*")), [])


if __name__ == "__main__":
    unittest.main()
