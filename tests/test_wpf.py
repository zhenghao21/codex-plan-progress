"""Exercise the shipped WPF model and bridge without opening a desktop window."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "plugin" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import overlay
import overlay_wpf as bridge


def state(ident="chat-a"):
    return {"thread_id": ident, "title": "中文计划", "run_state": "running", "plan": [
        {"step": "父任务", "status": "pending", "children": [
            {"step": "完成子项", "status": "completed"},
            {"step": "正在执行", "status": "in_progress"},
        ]},
    ]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-wpf-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def write_view(self, ident, now, available=True):
        (self.directory / "active-view.json").write_text(json.dumps({
            "thread_id": ident, "title": "当前窗口", "available": available,
            "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        }), encoding="utf-8")

    def write_plan(self, value):
        folder = self.directory / "threads"
        folder.mkdir(exist_ok=True)
        (folder / (value["thread_id"] + ".json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_actual_reader_gates_bridge_by_current_chat_and_independent_expiry(self):
        now = 1800000000.0
        reader = bridge.ActiveThreadState(self.directory, observed_not_before=now)
        self.write_plan(state())
        (self.directory / "current.json").write_text(json.dumps(state()), encoding="utf-8")
        placement = bridge.read_placement(self.directory)
        rect = (0, 0, 1200, 900)
        def visible(moment):
            return bridge.build_view(reader.read(moment), rect, placement, moment)
        self.write_view("chat-a", now - 1)
        self.assertFalse(visible(now)["visible"], "A restarted viewer must wait for a new observation")
        self.write_view("chat-a", now)
        active = visible(now)
        self.assertTrue(active["visible"])
        self.assertEqual(active["state"]["title"], "当前窗口")
        self.write_view("no-plan", now)
        self.assertFalse(visible(now)["visible"], "Never fall back to current.json")
        self.write_view("chat-a", now)
        self.assertTrue(visible(now)["visible"])
        (self.directory / "threads" / "chat-a.json").write_text("{", encoding="utf-8")
        self.write_view("chat-a", now + 3)
        self.assertTrue(visible(now + 3)["visible"])
        self.write_view("chat-a", now + 4.1)
        self.assertFalse(visible(now + 4.1)["visible"], "New observations cannot renew a broken plan")
        self.write_plan(state())
        self.write_view("chat-a", now + 5)
        self.assertTrue(visible(now + 5)["visible"])
        (self.directory / "active-view.json").write_text("{", encoding="utf-8")
        self.assertFalse(visible(now + 9.1)["visible"], "A broken observation must expire independently")
        for ident, available in [("chat-a", False), (None, True), ("../current", True)]:
            self.write_view(ident, now + 10, available)
            self.assertFalse(visible(now + 10)["visible"])

    def test_build_view_preserves_tree_and_rejects_invalid_host_or_plan(self):
        source = state()
        original = copy.deepcopy(source)
        placement = bridge.read_placement(self.directory)
        view = bridge.build_view(source, (-1000, -100, 0, 800), placement, 123)
        self.assertEqual((view["schema"], view["emitted_at"], view["visible"]), (1, 123, True))
        self.assertEqual(view["host_rect"], [-1000, -100, 0, 800])
        self.assertEqual(view["state"]["plan"], original["plan"])
        self.assertEqual(source, original)
        for rect in (None, [0, 0, 0, 500], [0, 0, 500, -1], [True, 0, 500, 500], [0, 0, float("nan"), 500]):
            self.assertFalse(bridge.build_view(source, rect, placement, 123)["visible"])
        invalid = state()
        invalid["plan"][0]["children"][0]["status"] = "success"
        self.assertFalse(bridge.build_view(invalid, (0, 0, 1000, 800), placement, 123)["visible"])
        self.assertFalse(bridge.build_view({"plan": []}, (0, 0, 1000, 800), placement, 123)["visible"])

    def test_placement_keeps_legacy_logical_coordinates_and_rejects_bad_values(self):
        filename = self.directory / "placement.json"
        legacy = {"width": 300, "top": 432, "right": 12, "offset_x": -184.181, "offset_y": 20.020}
        filename.write_text(json.dumps(legacy), encoding="utf-8-sig")
        self.assertEqual(bridge.read_placement(self.directory), legacy)
        filename.write_text(json.dumps({**legacy, "width": 950}), encoding="utf-8")
        self.assertEqual(bridge.read_placement(self.directory)["width"], 420)
        for bad in (float("nan"), float("inf"), True, "950", 10000):
            filename.write_text(json.dumps({**legacy, "offset_x": bad}), encoding="utf-8")
            self.assertEqual(bridge.read_placement(self.directory)["offset_x"], 0)

    def test_publisher_heartbeat_revision_and_atomic_file(self):
        publisher = bridge.ViewPublisher(self.directory)
        source, rect, placement = state(), (0, 0, 1200, 900), bridge.read_placement(self.directory)
        first = publisher.publish(source, rect, placement, now=100)
        observed_replaces = []
        real_replace = os.replace
        def replace(temporary, target):
            self.assertEqual(json.loads(Path(target).read_text(encoding="utf-8")), first)
            staged = json.loads(Path(temporary).read_text(encoding="utf-8"))
            self.assertEqual(staged["emitted_at"], 101)
            observed_replaces.append((Path(temporary), Path(target)))
            real_replace(temporary, target)
        with mock.patch.object(bridge.os, "replace", side_effect=replace):
            heartbeat = publisher.publish(source, rect, placement, now=101)
        self.assertEqual(first["revision"], heartbeat["revision"])
        self.assertEqual(len(observed_replaces), 1)
        self.assertFalse(observed_replaces[0][0].exists())
        source["plan"][0]["status"] = "completed"
        changed = publisher.publish(source, rect, placement, now=102)
        self.assertEqual(changed["revision"], first["revision"] + 1)
        stopped = publisher.publish(source, rect, placement, now=103, terminate=True)
        self.assertTrue(stopped["terminate"])
        self.assertEqual(stopped["revision"], changed["revision"] + 1)
        self.assertEqual(json.loads(publisher.path.read_text(encoding="utf-8")), stopped)

    @staticmethod
    def locator():
        locator = bridge.HostLocator.__new__(bridge.HostLocator)
        locator.host, locator.last_host = mock.Mock(), None
        locator.host.enabled = True
        locator.host.process_path.side_effect = lambda handle: {
            100: r"C:\Program Files\WindowsApps\OpenAI.Codex_1\app\ChatGPT.exe",
            200: r"C:\Program Files\ChatGPT\ChatGPT.exe",
        }.get(handle, "other.exe")
        locator.host.user.GetForegroundWindow.return_value = 100
        locator.host.user.IsWindowVisible.return_value = True
        locator.host.user.IsIconic.return_value = False
        def get_pid(handle, pointer):
            pointer._obj.value = 789 if handle == 300 else 123
        def get_rect(handle, pointer):
            rect = pointer._obj
            rect.left, rect.top, rect.right, rect.bottom = -1000, -100, 0, 800
            return True
        locator.host.user.GetWindowThreadProcessId.side_effect = get_pid
        locator.host.user.GetWindowRect.side_effect = get_rect
        return locator

    def test_host_gate_only_codex_or_own_viewer_over_last_codex(self):
        locator = self.locator()
        expected = (-1000, -100, 0, 800)
        self.assertEqual(locator.foreground_rect(789), expected)
        locator.host.user.GetForegroundWindow.return_value = 300
        self.assertEqual(locator.foreground_rect(789), expected)
        locator.host.user.GetForegroundWindow.return_value = 200
        self.assertIsNone(locator.foreground_rect(789), "Regular ChatGPT is not Codex")
        locator.host.user.GetForegroundWindow.return_value = 100
        locator.host.user.IsIconic.return_value = True
        self.assertIsNone(locator.foreground_rect(789))
        locator.host.user.IsIconic.return_value = False
        locator.host.user.IsWindowVisible.return_value = False
        self.assertIsNone(locator.foreground_rect(789))
        locator.host.user.GetSystemMetrics.side_effect = lambda index: {76: -1000, 77: -100, 78: 3000, 79: 1200}[index]
        self.assertEqual(locator.desktop_rect(), (-1000, -100, 2000, 1100))

    @unittest.skipUnless(sys.platform == "win32", "Windows defaults to WPF")
    def test_default_entrypoint_selects_wpf_and_does_not_create_tk(self):
        with mock.patch.object(overlay.sys, "argv", ["overlay.py", "--data-dir", str(self.directory)]), \
             mock.patch.object(overlay, "enable_dpi_awareness"), \
             mock.patch.object(bridge, "run_native", return_value=0) as run, \
             mock.patch.object(overlay.tk, "Tk") as tk_root:
            self.assertEqual(overlay.main(), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0].renderer, "wpf")
        tk_root.assert_not_called()

    def test_controller_keeps_lock_and_observer_until_renderer_exits(self):
        args = argparse.Namespace(data_dir=self.directory, standalone=False, preview=False, demo=False,
                                  quit_after=0, diagnostics=None)
        lock, observer, viewer = mock.Mock(), mock.Mock(), mock.Mock()
        viewer.pid, viewer.returncode = 789, 0
        events = []
        polls = iter([None, 0, 0])
        def poll():
            self.assertNotIn("unlock", events)
            return next(polls)
        viewer.poll.side_effect = poll
        lock.close.side_effect = lambda: events.append("unlock")
        reader, locator = mock.Mock(), mock.Mock()
        reader.read.return_value = state()
        locator.foreground_rect.return_value = (0, 0, 1200, 900)
        with mock.patch.object(bridge, "acquire_lock", return_value=lock), \
             mock.patch.object(bridge, "compile_renderer", return_value=self.directory / "renderer.exe"), \
             mock.patch.object(bridge, "ActiveThreadState", return_value=reader), \
             mock.patch.object(bridge, "HostLocator", return_value=locator), \
             mock.patch.object(bridge, "start_observer", return_value=observer) as start, \
             mock.patch.object(bridge, "stop_observer", side_effect=lambda process: events.append("stop-observer")) as stop, \
             mock.patch.object(bridge, "preview_state") as preview, \
             mock.patch.object(bridge.subprocess, "Popen", return_value=viewer) as launch, \
             mock.patch.object(bridge.time, "sleep"):
            self.assertEqual(bridge.run_native(args), 0)
        start.assert_called_once()
        stop.assert_called_once_with(observer)
        reader.read.assert_called_once()
        locator.foreground_rect.assert_called_once_with(789)
        preview.assert_not_called()
        self.assertIn("--parent-pid", launch.call_args.args[0])
        self.assertEqual(events, ["stop-observer", "unlock"])
        final = json.loads((self.directory / "renderer-view.json").read_text(encoding="utf-8"))
        self.assertTrue(final["terminate"])
        self.assertFalse(final["visible"])

    def test_controller_does_not_spawn_a_second_viewer(self):
        args = argparse.Namespace(data_dir=self.directory)
        with mock.patch.object(bridge, "acquire_lock", return_value=None), \
             mock.patch.object(bridge, "compile_renderer") as compile_renderer, \
             mock.patch.object(bridge, "start_observer") as start:
            self.assertEqual(bridge.run_native(args), 0)
        compile_renderer.assert_not_called()
        start.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "WPF is a Windows renderer")
class NativeModelTests(unittest.TestCase):
    def test_actual_wpf_model_without_opening_a_window(self):
        framework = bridge.framework_directory()
        references = [framework / "WPF" / name for name in
                      ("WindowsBase.dll", "PresentationCore.dll", "PresentationFramework.dll")]
        references += [framework / name for name in ("System.Xaml.dll", "System.Web.Extensions.dll")]
        with tempfile.TemporaryDirectory(prefix="codex-wpf-model-") as directory:
            executable = Path(directory) / "WpfTests.exe"
            command = [str(framework / "csc.exe"), "/nologo", "/target:exe", "/main:WpfTests",
                       "/codepage:65001", "/utf8output", "/out:" + str(executable)]
            command += ["/reference:" + str(path) for path in references]
            command += [str(SCRIPTS / "overlay_wpf.cs"), str(PROJECT / "tests" / "WpfTests.cs")]
            compiled = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace", timeout=60,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(compiled.returncode, 0, compiled.stdout)
            checked = subprocess.run([str(executable)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", timeout=30,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(checked.returncode, 0, checked.stdout)
            self.assertEqual(checked.stdout.count("PASS "), 6, checked.stdout)


if __name__ == "__main__":
    unittest.main()
