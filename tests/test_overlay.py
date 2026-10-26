import importlib.util
import json
import copy
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
import sys
from unittest import mock
from datetime import datetime, timezone

MODULE = Path(__file__).resolve().parents[1] / "plugin" / "scripts" / "overlay.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("overlay", MODULE)
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


class OverlayTests(unittest.TestCase):
    @staticmethod
    def pump(root, milliseconds=35):
        done = tk.BooleanVar(root, False)
        root.after(milliseconds, lambda: done.set(True))
        root.wait_variable(done)

    @staticmethod
    def visible_rows(app):
        return [row for row in app.rows.winfo_children() if row.winfo_manager()]

    def assert_single_reveal_boundary(self, rows):
        """A group reveals a prefix, not a thin strip from every text row."""
        partial = [row for row in rows if 0 < row.clip_height < row.natural_height]
        self.assertLessEqual(len(partial), 1)
        boundary_seen = False
        for row in rows:
            self.assertGreater(row.natural_height, 1)
            self.assertGreaterEqual(row.clip_height, 0)
            self.assertLessEqual(row.clip_height, row.natural_height)
            if boundary_seen:
                self.assertEqual(row.clip_height, 0, "A later row appeared below the reveal boundary")
            if row.clip_height < row.natural_height:
                boundary_seen = True
            if row.clip_height == row.natural_height:
                self.assertEqual(row.winfo_height(), row.natural_height)
            if row.clip_height:
                self.assertEqual(row.content.winfo_height(), row.natural_height)
        return bool(partial)

    def assert_natural_row_spacing(self, rows):
        for previous, following in zip(rows, rows[1:]):
            self.assertAlmostEqual(following.layout_y - previous.layout_y,
                                   previous.natural_height + 2, delta=1)

    def test_twenty_children_reveal_one_boundary_without_text_stripes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-alpha", 0.0)
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            data = {"thread_id": "single-boundary", "run_state": "running", "plan": [
                {"step": "主任务", "status": "in_progress", "children": [
                    {"step": f"子步骤 {i} " + ("保持自然文字高度和行间距，" * (3 if i % 3 == 0 else 1)),
                     "status": "completed" if i % 4 == 0 else "pending"}
                    for i in range(20)
                ]},
                {"step": "后续任务", "status": "blocked"},
            ]}
            original = copy.deepcopy(data)
            try:
                app.set_state(data)
                app.poll(schedule=False)
                root.update_idletasks()
                parent, sibling = app.root_rows
                icon, badge = parent.status_icon, app.badge["text"]
                cap = round(256 * app.scale)
                clock = [100.0]
                saw_partial = False
                with mock.patch.object(overlay.time, "monotonic", side_effect=lambda: clock[0]):
                    for opening in (True, False):
                        parent.disclosure.invoke()
                        children = list(parent.child_rows)
                        self.assertEqual(len(children), 20)
                        previous_y = sibling.layout_y
                        for advance in (0.025, 0.025, 0.04, 0.06, 0.12, 0.3):
                            clock[0] += advance
                            self.pump(root)
                            saw_partial |= self.assert_single_reveal_boundary(children)
                            self.assert_natural_row_spacing(children)
                            if opening:
                                self.assertGreaterEqual(sibling.layout_y, previous_y)
                            else:
                                self.assertLessEqual(sibling.layout_y, previous_y)
                            previous_y = sibling.layout_y
                            self.assertLessEqual(root.winfo_height(), cap)
                            self.assertEqual(app.badge["text"], badge)
                            self.assertEqual(app.state["plan"], original["plan"])
                            self.assertIs(parent.status_icon, icon)
                        self.assertIsNone(app.task_animation)
                        self.assertTrue(all(row.clip_height == (row.natural_height if opening else 0)
                                            for row in children))
                self.assertTrue(saw_partial, "The test must inspect an actual partial reveal")
                self.assertEqual(len(self.visible_rows(app)), 2)
                self.assertEqual(data, original)
            finally:
                root.destroy()

    def test_nested_group_preserves_inner_expansion_during_ancestor_reversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-alpha", 0.0)
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            data = {"thread_id": "nested-boundary", "run_state": "running", "plan": [
                {"step": "外层主任务", "status": "in_progress", "children": [
                    {"step": "前置步骤", "status": "completed"},
                    {"step": "内层分支", "status": "in_progress", "children": [
                        {"step": f"内层步骤 {i}，自然高度与间距", "status": "pending"} for i in range(8)
                    ]},
                    {"step": "末尾步骤", "status": "blocked"},
                ]},
                {"step": "其他任务", "status": "pending"},
            ]}
            original = copy.deepcopy(data)
            try:
                app.set_state(data)
                app.poll(schedule=False)
                root.update_idletasks()
                outer, sibling = app.root_rows
                badge, outer_icon = app.badge["text"], outer.status_icon
                cap = round(256 * app.scale)
                clock = [100.0]
                with mock.patch.object(overlay.time, "monotonic", side_effect=lambda: clock[0]):
                    outer.disclosure.invoke()
                    clock[0] += 0.5
                    self.pump(root)
                    inner = outer.child_rows[1]
                    inner_icon = inner.status_icon
                    inner.disclosure.invoke()
                    for advance in (0.03, 0.04, 0.5):
                        clock[0] += advance
                        self.pump(root)
                        self.assert_single_reveal_boundary(inner.child_rows)
                        self.assert_natural_row_spacing(inner.child_rows)
                        self.assertLessEqual(root.winfo_height(), cap)
                    self.assertEqual(inner.expansion, 1.0)
                    descendants = [row for key, row in app.row_cache.items()
                                   if len(key) > len(outer.plan_key) and key[:len(outer.plan_key)] == outer.plan_key]
                    outer.disclosure.invoke()
                    clock[0] += 0.035
                    self.pump(root)
                    self.assert_single_reveal_boundary(descendants)
                    self.assert_natural_row_spacing(descendants)
                    position, velocity, sibling_y = outer.expansion, outer.expansion_velocity, sibling.layout_y
                    self.assertLess(velocity, 0)
                    outer.disclosure.invoke()
                    self.assertAlmostEqual(outer.expansion, position)
                    self.assertAlmostEqual(outer.expansion_velocity, velocity)
                    self.assertAlmostEqual(sibling.layout_y, sibling_y, delta=1)
                    for advance in (0.004, 0.03, 0.06, 0.5):
                        clock[0] += advance
                        self.pump(root)
                        self.assert_single_reveal_boundary(descendants)
                        self.assert_natural_row_spacing(descendants)
                        self.assertEqual(inner.expansion, 1.0)
                        self.assertIn(inner.plan_key, app.expanded_tasks)
                        self.assertLessEqual(root.winfo_height(), cap)
                        self.assertEqual(app.badge["text"], badge)
                    self.assertIsNone(app.task_animation)
                    self.assertEqual(outer.expansion, 1.0)
                    self.assertTrue(all(row.clip_height == row.natural_height for row in descendants))
                    outer.disclosure.invoke()
                    clock[0] += 0.5
                    self.pump(root)
                    self.assertTrue(all(row.clip_height == 0 for row in descendants))
                    self.assertEqual(inner.expansion, 1.0)
                    self.assertIn(inner.plan_key, app.expanded_tasks)
                    outer.disclosure.invoke()
                    clock[0] += 0.5
                    self.pump(root)
                    self.assertTrue(all(row.clip_height == row.natural_height for row in descendants))
                self.assertIs(outer.status_icon, outer_icon)
                self.assertIs(inner.status_icon, inner_icon)
                self.assertEqual(app.state["plan"], original["plan"])
                self.assertEqual(data, original)
                self.assertEqual(app.badge["text"], badge)
            finally:
                root.destroy()

    def test_sublist_moves_smoothly_in_both_directions_without_changing_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True)
            root.attributes("-alpha", 0.0)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            data = {"thread_id": "motion-a", "run_state": "running", "plan": [
                {"step": "展开的主任务", "status": "in_progress", "children": [
                    {"step": f"子步骤 {i}", "status": "completed" if i == 0 else "pending"}
                    for i in range(3)
                ]},
                {"step": "后续任务", "status": "pending"},
            ]}
            try:
                app.set_state(data)
                app.poll(schedule=False)
                root.update_idletasks()
                parent, sibling = app.rows.winfo_children()
                initial_y, initial_height = sibling.winfo_y(), root.winfo_height()
                parent_icon = parent.status_icon
                count = app.badge["text"]
                clock = [100.0]
                with mock.patch.object(overlay.time, "monotonic", side_effect=lambda: clock[0]):
                    parent.disclosure.invoke()
                    root.update_idletasks()
                    self.assertLessEqual(abs(sibling.winfo_y() - initial_y), 2)
                    self.assertLessEqual(abs(root.winfo_height() - initial_height), 2)
                    clock[0] += 0.08
                    self.pump(root)
                    middle_y = sibling.winfo_y()
                    self.assertGreater(middle_y, initial_y)
                    self.assertIsNotNone(app.task_animation)
                    clock[0] += 0.25
                    self.pump(root)
                    final_y = sibling.winfo_y()
                    self.assertGreater(final_y, middle_y)
                    self.assertIsNone(app.task_animation)
                    self.assertIs(parent.status_icon, parent_icon)
                    self.assertEqual(app.badge["text"], count)
                    parent.disclosure.invoke()
                    root.update_idletasks()
                    self.assertLessEqual(abs(sibling.winfo_y() - final_y), 2)
                    clock[0] += 0.08
                    self.pump(root)
                    self.assertGreater(sibling.winfo_y(), initial_y)
                    self.assertLess(sibling.winfo_y(), final_y)
                    clock[0] += 0.25
                    self.pump(root)
                    self.assertAlmostEqual(sibling.winfo_y(), initial_y, delta=2)
                    self.assertIsNone(app.task_animation_job)
                self.assertEqual(len(self.visible_rows(app)), 2)
                self.assertEqual(app.badge["text"], count)
                self.assertEqual(app.state["plan"][0]["children"][1]["status"], "pending")
            finally:
                root.destroy()

    def test_reversal_keeps_velocity_and_reuses_hidden_rows_without_frame_flushes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True)
            root.attributes("-alpha", 0.0)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            try:
                app.set_state({"thread_id": "motion-velocity", "plan": [
                    {"step": "主任务", "status": "in_progress", "children": [
                        {"step": "子任务", "status": "in_progress"},
                    ]},
                ]})
                app.poll(schedule=False)
                root.update_idletasks()
                parent = app.ordered_rows[0]
                clock = [100.0]
                with mock.patch.object(overlay.time, "monotonic", side_effect=lambda: clock[0]):
                    parent.disclosure.invoke()
                    root.update_idletasks()
                    child = parent.child_rows[0]
                    icon = child.status_icon
                    with mock.patch.object(root, "update_idletasks", wraps=root.update_idletasks) as flush:
                        clock[0] += 0.05
                        self.pump(root)
                        self.assertEqual(flush.call_count, 0)
                    position, velocity = parent.expansion, parent.expansion_velocity
                    self.assertGreater(velocity, 0)
                    parent.disclosure.invoke()
                    self.assertAlmostEqual(parent.expansion, position)
                    self.assertAlmostEqual(parent.expansion_velocity, velocity)
                    clock[0] += 0.004
                    self.pump(root)
                    self.assertGreater(parent.expansion_velocity, 0)
                    self.assertLess(parent.expansion_velocity, velocity)
                    clock[0] += 0.025
                    self.pump(root)
                    self.assertLess(parent.expansion_velocity, 0)
                    clock[0] += 0.4
                    self.pump(root)
                    self.assertIsNone(app.task_animation_job)
                    self.assertEqual(child.clip_height, 0)
                    self.assertFalse(child.winfo_manager())
                    parent.disclosure.invoke()
                    self.assertIs(parent.child_rows[0].status_icon, icon)
            finally:
                root.destroy()

    def test_rapid_sublist_reversal_stays_compact_and_old_chat_animation_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True)
            root.attributes("-alpha", 0.0)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            data = {"thread_id": "motion-long", "run_state": "running", "plan": [
                {"step": "有很多子步骤的任务", "status": "in_progress", "children": [
                    {"step": f"子步骤 {i}，保持自然文字大小", "status": "pending"}
                    for i in range(20)
                ]},
                {"step": "后续任务", "status": "pending"},
            ]}
            errors = []
            root.report_callback_exception = lambda *error: errors.append(error)
            try:
                app.set_state(data)
                app.poll(schedule=False)
                root.update_idletasks()
                parent, sibling = app.rows.winfo_children()
                clock = [100.0]
                cap = round(256 * app.scale)
                with mock.patch.object(overlay.time, "monotonic", side_effect=lambda: clock[0]):
                    parent.disclosure.invoke()
                    clock[0] += 0.06
                    self.pump(root)
                    before_reverse = sibling.winfo_y()
                    parent.disclosure.invoke()
                    root.update_idletasks()
                    self.assertLessEqual(abs(sibling.winfo_y() - before_reverse), 2)
                    clock[0] += 0.04
                    self.pump(root)
                    before_reopen = sibling.winfo_y()
                    parent.disclosure.invoke()
                    root.update_idletasks()
                    self.assertLessEqual(abs(sibling.winfo_y() - before_reopen), 2)
                    for advance in (0.05, 0.06, 0.39):
                        clock[0] += advance
                        self.pump(root)
                        self.assertLessEqual(root.winfo_height(), cap)
                    self.assertIsNone(app.task_animation)
                    self.assertTrue(app.scrollbar.winfo_ismapped())
                    self.assertEqual(len(app.rows.winfo_children()), 22)
                    parent.disclosure.invoke()
                    clock[0] += 0.04
                    self.pump(root)
                    app.set_state({"thread_id": "new-chat", "plan": [
                        {"step": "新会话任务", "status": "pending"},
                    ]})
                    clock[0] += 0.4
                    self.pump(root)
                    self.assertIsNone(app.task_animation_job)
                    self.assertIsNone(app.task_animation)
                self.assertEqual(app.state["thread_id"], "new-chat")
                self.assertEqual([row.plan_step for row in app.rows.winfo_children()], ["新会话任务"])
                self.assertEqual(errors, [])
            finally:
                root.destroy()

    def test_nested_tasks_start_closed_expand_and_keep_real_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            app = overlay.PlanOverlay(root, Path(directory), standalone=True)
            data = {"thread_id": "nested-a", "run_state": "running", "plan": [
                {"step": "主任务", "status": "in_progress", "children": [
                    {"step": "完成的子步骤", "status": "completed"},
                    {"step": "继续细分", "status": "in_progress", "children": [
                        {"step": "更细的步骤", "status": "pending"},
                    ]},
                ]},
                {"step": "其他任务", "status": "blocked"},
            ]}
            original = copy.deepcopy(data)
            try:
                app.set_state(data)
                root.update_idletasks()
                rows = app.rows.winfo_children()
                parent_icon = rows[0].status_icon
                self.assertEqual(len(rows), 2)
                self.assertEqual(app.badge["text"], "1/5")
                self.assertIn("1/3", rows[0].disclosure["text"])
                rows[0].disclosure.invoke()
                root.update_idletasks()
                self.assertEqual(len(app.rows.winfo_children()), 4)
                self.assertIs(app.rows.winfo_children()[0].status_icon, parent_icon)
                branch = next(row for row in app.rows.winfo_children() if row.plan_step == "继续细分")
                branch.disclosure.invoke()
                root.update_idletasks()
                self.assertEqual(len(app.rows.winfo_children()), 5)
                self.assertEqual(app.badge["text"], "1/5")
                self.assertEqual(data, original)
                app.set_state({**data, "thread_id": "nested-b"})
                root.update_idletasks()
                self.assertEqual(len(app.rows.winfo_children()), 2)
                app.set_state(data)
                root.update_idletasks()
                self.assertEqual(len(app.rows.winfo_children()), 5)
                parent = next(row for row in app.rows.winfo_children() if row.plan_step == "主任务")
                parent.disclosure.invoke()
                root.update_idletasks()
                self.assertEqual(len(self.visible_rows(app)), 2)
                self.assertEqual(app.badge["text"], "1/5")
                self.assertEqual(app.state["plan"][0]["children"][1]["status"], "in_progress")
            finally:
                root.destroy()

    def test_invalid_nested_status_is_rejected(self):
        with self.assertRaises(ValueError):
            overlay.normalize_state({"plan": [{"step": "父", "status": "pending", "children": [
                {"step": "子", "status": "finished"},
            ]}]})

    def test_precision_wheel_accumulates_small_deltas(self):
        app = overlay.PlanOverlay.__new__(overlay.PlanOverlay)
        app.collapsed, app.wheel_delta = False, 0
        app.canvas = mock.Mock()
        app._sync_icon_activity = mock.Mock()
        for _ in range(3):
            app._scroll(mock.Mock(delta=30))
        app.canvas.yview_scroll.assert_not_called()
        app._scroll(mock.Mock(delta=30))
        app.canvas.yview_scroll.assert_called_once_with(-1, "units")
        app._scroll(mock.Mock(delta=-240))
        self.assertEqual(app.canvas.yview_scroll.call_args.args, (2, "units"))

    def test_whole_card_crops_natural_content_and_reverses_without_a_jump(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-alpha", 0.0)
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            data = {"thread_id": "whole-card-motion", "run_state": "running", "plan": [
                {"step": f"保持自然排版的任务 {i}", "status": "completed" if i == 0 else "pending"}
                for i in range(5)
            ]}
            original = copy.deepcopy(data)
            try:
                app.set_state(data)
                app.poll(schedule=False)
                self.pump(root)
                expanded = root.winfo_height()
                collapsed = app.header.winfo_reqheight() + 13
                badge = app.badge["text"]
                icons = [row.status_icon for row in app.root_rows]
                natural_rows = [(row.layout_y, row.natural_height) for row in app.root_rows]
                self.assertGreater(expanded, collapsed)
                clock = [100.0]
                with mock.patch.object(overlay.time, "monotonic", side_effect=lambda: clock[0]):
                    app.toggle_collapse()
                    root.update_idletasks()
                    self.assertTrue(app.details.winfo_ismapped())
                    self.assertEqual(root.winfo_height(), expanded)
                    self.assertEqual(app.card_content_height, expanded)
                    clock[0] += 0.04
                    self.pump(root)
                    middle = root.winfo_height()
                    self.assertGreater(middle, collapsed)
                    self.assertLess(middle, expanded)
                    self.assertEqual(app.card_content_height, expanded)
                    self.assertEqual(app.card.winfo_height(), expanded - 2)
                    self.assertEqual([(row.layout_y, row.natural_height) for row in app.root_rows], natural_rows)
                    position = app.height_animation["value"]
                    velocity = app.height_animation["velocity"]
                    self.assertLess(velocity, 0)
                    app.toggle_collapse()
                    self.assertAlmostEqual(app.height_animation["value"], position)
                    self.assertAlmostEqual(app.height_animation["velocity"], velocity)
                    self.assertEqual(root.winfo_height(), middle)
                    clock[0] += 0.004
                    self.pump(root)
                    self.assertLess(app.height_animation["velocity"], 0)
                    self.assertGreater(app.height_animation["velocity"], velocity)
                    clock[0] += 0.03
                    self.pump(root)
                    self.assertGreater(app.height_animation["velocity"], 0)
                    self.assertEqual(app.card_content_height, expanded)
                    clock[0] += 0.466
                    self.pump(root)
                    self.assertEqual(root.winfo_height(), expanded)
                    self.assertIsNone(app.height_animation)
                    self.assertIsNone(app.height_job)
                    self.assertIsNone(app.card_content_height)
                    app.toggle_collapse()
                    clock[0] += 0.5
                    self.pump(root)
                    self.assertEqual(root.winfo_height(), collapsed)
                    self.assertFalse(app.details.winfo_manager())
                    self.assertFalse(app.body.winfo_ismapped())
                    self.assertFalse(app.canvas.winfo_ismapped())
                    self.assertIsNone(app.height_animation)
                    self.assertIsNone(app.height_job)
                    self.assertIsNone(app.card_content_height)
                    app.toggle_collapse()
                    root.update_idletasks()
                    self.assertEqual(root.winfo_height(), collapsed)
                    self.assertTrue(app.details.winfo_ismapped())
                    self.assertEqual(app.card_content_height, expanded)
                    clock[0] += 0.05
                    self.pump(root)
                    self.assertGreater(root.winfo_height(), collapsed)
                    self.assertLess(root.winfo_height(), expanded)
                    self.assertEqual(app.card_content_height, expanded)
                    clock[0] += 0.45
                    self.pump(root)
                    self.assertEqual(root.winfo_height(), expanded)
                    self.assertIsNone(app.height_animation)
                    self.assertIsNone(app.height_job)
                self.assertEqual([row.status_icon for row in app.root_rows], icons)
                self.assertEqual(app.badge["text"], badge)
                self.assertEqual(app.state["plan"], original["plan"])
                self.assertEqual(data, original)
            finally:
                root.destroy()

    @staticmethod
    def drag_overlay(directory, offset=(0, 0)):
        """Model applied native coordinates without moving any desktop window."""
        app = overlay.PlanOverlay.__new__(overlay.PlanOverlay)
        app.data_dir = Path(directory)
        app.last_rect = (0, 0, 1000, 800)
        app.width, app.scale = 280, 1.0
        app.placement = {"top": 100, "right": 16, "offset_x": offset[0], "offset_y": offset[1]}
        app.placement_signature = None
        app.offset, app.drag, app.window_position = offset, None, None
        app.root = mock.Mock()
        app.root.winfo_height.return_value = 200
        app.root.winfo_screenwidth.return_value = 1000
        app.root.winfo_screenheight.return_value = 800
        applied = {"x": 0, "y": 0, "grab": False}
        app.root.winfo_x.side_effect = lambda: applied["x"]
        app.root.winfo_y.side_effect = lambda: applied["y"]
        app.root.grab_set.side_effect = lambda: applied.update(grab=True)
        app.root.grab_release.side_effect = lambda: applied.update(grab=False)
        app.root.grab_current.side_effect = lambda: app.root if applied["grab"] else None
        app.host = mock.Mock()
        app.host.move.side_effect = lambda root, x, y: applied.update(x=x, y=y)
        app._queue_decoration, app._cancel_tip, app._resize = mock.Mock(), mock.Mock(), mock.Mock()
        return app, applied

    def test_old_oversized_offset_leaves_edge_on_first_reverse_pixel(self):
        with tempfile.TemporaryDirectory() as directory:
            app, applied = self.drag_overlay(directory, offset=(950, 0))
            self.assertEqual(app._position(), (712, 100))
            app.start_drag(mock.Mock(x_root=732, y_root=110))
            app.move_drag(mock.Mock(x_root=731, y_root=110))
            self.assertEqual((applied["x"], applied["y"]), (711, 100))
            self.assertEqual(app.window_position, (711, 100))
            self.assertTrue(applied["grab"])
            app.end_drag()
            self.assertFalse(applied["grab"])

    def test_edge_overdrag_reverses_immediately_and_saved_position_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            app, applied = self.drag_overlay(directory)
            self.assertEqual(app._position(), (704, 100))
            app.start_drag(mock.Mock(x_root=724, y_root=110))
            app.move_drag(mock.Mock(x_root=1324, y_root=110))
            self.assertEqual(applied["x"], 712)
            app.move_drag(mock.Mock(x_root=1314, y_root=110))
            self.assertEqual(applied["x"], 702)
            app.end_drag()
            self.assertIsNone(app.drag)
            self.assertFalse(applied["grab"])
            saved = json.loads((Path(directory) / "placement.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["offset_x"], -2)
            self.assertEqual(saved["offset_y"], 0)
            restored, restored_position = self.drag_overlay(directory)
            restored._read_placement()
            self.assertEqual(restored._position(), (702, 100))
            self.assertEqual((restored_position["x"], restored_position["y"]), (702, 100))
            # The fix removes accumulated dead-zone, not the Codex-window bounds.
            restored.start_drag(mock.Mock(x_root=722, y_root=110))
            restored.move_drag(mock.Mock(x_root=-1000, y_root=-1000))
            self.assertEqual(restored.window_position, (8, 8))
            restored.end_drag()

    def test_position_cache_does_not_hide_a_native_coordinate_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            app, applied = self.drag_overlay(directory)
            expected = app._position()
            app.host.move.reset_mock()
            applied.update(x=0, y=0)
            self.assertEqual(app._position(), expected)
            app.host.move.assert_called_once_with(app.root, *expected)
            self.assertEqual((applied["x"], applied["y"]), expected)

    def test_title_drag_bindings_apply_release_with_or_without_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            host = overlay.WindowsHost()
            host.capture_initial_foreground()
            root = tk.Tk()
            root.withdraw()
            root.attributes("-alpha", 0.0)
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True, host=host)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            try:
                app.placement = {"top": 70, "right": 30}
                app.set_state({"thread_id": "title-drag-events", "plan": [
                    {"step": "通过真实标题绑定拖动", "status": "pending"},
                ]})
                app.poll(schedule=False)
                self.pump(root)
                motions = []
                root.bind("<B1-Motion>", lambda event: motions.append(event.state), add="+")
                for with_motion in (False, True):
                    with self.subTest(with_motion=with_motion):
                        motions.clear()
                        initial = (root.winfo_x(), root.winfo_y())
                        initial_offset = app.offset
                        px, py = app.title.winfo_rootx() + 12, app.title.winfo_rooty() + 8
                        expected = (initial[0] - 80, initial[1] + 20)
                        app.title.event_generate("<ButtonPress-1>", x=12, y=8,
                                                 rootx=px, rooty=py, when="now")
                        self.assertIsNotNone(app.drag)
                        self.assertIs(root.grab_current(), root)
                        if with_motion:
                            app.title.event_generate("<B1-Motion>", x=12, y=8,
                                                     rootx=px - 80, rooty=py + 20,
                                                     state=256, when="now")
                            root.update_idletasks()
                            self.assertEqual((root.winfo_x(), root.winfo_y()), expected)
                        app.title.event_generate("<ButtonRelease-1>", x=12, y=8,
                                                 rootx=px - 80, rooty=py + 20,
                                                 state=256, when="now")
                        self.pump(root)
                        self.assertEqual((root.winfo_x(), root.winfo_y()), expected)
                        self.assertEqual(app.window_position, expected)
                        self.assertEqual(len(motions), int(with_motion))
                        self.assertTrue(all(state & 256 for state in motions))
                        self.assertIsNone(app.drag)
                        self.assertIsNone(root.grab_current())
                        saved = json.loads((Path(directory) / "placement.json").read_text(encoding="utf-8"))
                        self.assertAlmostEqual(saved["offset_x"] * app.scale, initial_offset[0] - 80)
                        self.assertAlmostEqual(saved["offset_y"] * app.scale, initial_offset[1] + 20)
            finally:
                root.destroy()

    def test_first_map_synchronizes_cached_tk_and_native_position(self):
        with tempfile.TemporaryDirectory() as directory:
            host = overlay.WindowsHost()
            host.capture_initial_foreground()
            root = tk.Tk()
            root.withdraw()
            root.attributes("-alpha", 0.0)
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True, host=host)
            for shadow, _ in app.shadows:
                shadow.attributes("-alpha", 0.0)
            try:
                app.placement = {"top": 70, "right": 30}
                app.set_state({"thread_id": "first-map-position", "plan": [
                    {"step": "需要显示的任务", "status": "pending"},
                ]})
                app.poll(schedule=False)
                self.pump(root)
                expected = app.window_position
                self.assertIsNotNone(expected)
                self.assertGreater(expected[0], 0)
                self.assertEqual((root.winfo_x(), root.winfo_y()), expected)
                self.assertEqual(app._position(), expected)
                if host.enabled:
                    rect = overlay.wintypes.RECT()
                    self.assertTrue(host.user.GetWindowRect(host.hwnd(root), overlay.ctypes.byref(rect)))
                    self.assertEqual((rect.left, rect.top), expected)
            finally:
                root.destroy()

    def test_only_icons_inside_the_scroll_view_animate(self):
        app = overlay.PlanOverlay.__new__(overlay.PlanOverlay)
        app.visible, app.collapsed = True, False
        app.state = {"run_state": "running"}
        app.canvas = mock.Mock()
        app.canvas.canvasy.return_value = 100
        app.canvas.winfo_height.return_value = 100
        rows = []
        for position in (0, 90, 150, 220):
            row = mock.Mock()
            row.winfo_y.return_value = position
            row.status_icon.winfo_y.return_value = 2
            row.status_icon.winfo_height.return_value = 22
            rows.append(row)
        app.rows = mock.Mock()
        app.rows.winfo_children.return_value = rows
        app._sync_icon_activity()
        self.assertEqual([row.status_icon.set_active.call_args.args[0] for row in rows],
                         [False, True, True, False])
        app.collapsed = True
        app._sync_icon_activity()
        self.assertTrue(all(not row.status_icon.set_active.call_args.args[0] for row in rows))

    def test_chat_keeps_fold_and_scroll_and_running_footer_is_accurate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            app = overlay.PlanOverlay(root, Path(directory), standalone=True)
            first = {"thread_id": "chat-a", "run_state": "running", "plan": [
                {"step": "执行任务", "status": "in_progress"},
                {"step": "等待用户", "status": "blocked"},
            ] + [{"step": f"后续任务 {index}", "status": "pending"} for index in range(20)]}
            try:
                app.set_state(first)
                root.update_idletasks()
                self.assertEqual(app.footer["text"], "正在执行 · 1 项待处理")
                app.canvas.yview_moveto(0.5)
                offset = app.canvas.yview()[0]
                self.assertGreater(offset, 0.2)
                app.toggle_collapse()
                app.set_state({})
                root.update_idletasks()
                app.set_state(first)
                root.update_idletasks()
                self.assertTrue(app.collapsed)
                self.assertEqual(app.fold["text"], "+")
                self.assertAlmostEqual(app.canvas.yview()[0], offset, delta=0.03)
                second = {"thread_id": "chat-b", "plan": [{"step": "另一任务", "status": "pending"}]}
                app.set_state(second)
                root.update_idletasks()
                self.assertFalse(app.collapsed)
                self.assertEqual(app.canvas.yview()[0], 0)
                app.set_state(first)
                root.update_idletasks()
                self.assertTrue(app.collapsed)
                app.set_state({**first, "run_state": "idle"})
                self.assertEqual(app.footer["text"], "等待处理 · 1 项受阻")
            finally:
                root.destroy()

    def test_main_captures_host_before_tk_and_passes_it_to_overlay(self):
        order = []
        host, root, lock = mock.Mock(), mock.Mock(), mock.Mock()
        host.capture_initial_foreground.side_effect = lambda: order.append("capture")
        def make_root():
            order.append("tk")
            return root
        with mock.patch.object(overlay, "enable_dpi_awareness"), \
             mock.patch.object(overlay.sys, "argv", ["overlay.py", "--data-dir", "test-data", "--renderer", "tk"]), \
             mock.patch.object(overlay, "acquire_lock", return_value=lock), \
             mock.patch.object(overlay, "WindowsHost", return_value=host), \
             mock.patch.object(overlay, "start_observer", return_value=None), \
             mock.patch.object(overlay.tk, "Tk", side_effect=make_root), \
             mock.patch.object(overlay, "PlanOverlay") as window:
            self.assertEqual(overlay.main(), 0)
            self.assertEqual(order, ["capture", "tk"])
            self.assertIs(window.call_args.kwargs["host"], host)

    def test_focus_restore_preserves_user_switch_to_another_app(self):
        host = overlay.WindowsHost.__new__(overlay.WindowsHost)
        host.enabled, host.initial_foreground = True, 100
        host.user = mock.Mock()
        host.hwnd = lambda root: 200
        host.user.IsWindow.return_value = True
        host.user.GetForegroundWindow.return_value = 200
        host.restore_focus_if_owned(None)
        host.user.SetForegroundWindow.assert_called_once_with(100)
        host.user.SetForegroundWindow.reset_mock()
        host.user.GetForegroundWindow.return_value = 300
        host.restore_focus_if_owned(None)
        host.user.SetForegroundWindow.assert_not_called()

    def test_store_codex_is_recognized_without_matching_regular_chatgpt(self):
        self.assertTrue(overlay.is_codex_process_path(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"))
        self.assertTrue(overlay.is_codex_process_path(r"C:\Apps\Codex\codex.exe"))
        self.assertTrue(overlay.is_codex_process_path(r"C:\Apps\Codex++\codex-plus-plus.exe"))
        self.assertFalse(overlay.is_codex_process_path(r"C:\Apps\ChatGPT\ChatGPT.exe"))
        self.assertFalse(overlay.is_codex_process_path(
            r"C:\Program Files\WindowsApps\OpenAI.ChatGPT_1.0_x64\app\ChatGPT.exe"))

    def test_invalid_status_is_not_completed(self):
        with self.assertRaises(ValueError):
            overlay.normalize_state({"plan": [{"step": "Check result", "status": "done-ish"}]})

    def test_live_snapshot_renders_real_completions_and_hides_empty_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            app = overlay.PlanOverlay(root, Path(directory), standalone=True)
            try:
                app.poll(schedule=False)
                self.assertFalse(app.visible)
                data = {"thread_id": "test-thread", "title": "测试会话", "run_state": "running", "plan": [
                    {"step": "第一项", "status": "completed"},
                    {"step": "第二项", "status": "in_progress"},
                    {"step": "第三项", "status": "pending"},
                ]}
                path = Path(directory) / "current.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                app.poll(schedule=False)
                root.update_idletasks()
                self.assertTrue(app.visible)
                self.assertEqual(app.summary["text"], "已完成 1 / 3 项")
                self.assertEqual(app.percent["text"], "33%")
                self.assertEqual(app.title["text"], "测试会话")
                self.assertEqual(len(app.rows.winfo_children()), 3)
                app.toggle_collapse()
                self.assertFalse(app.body.winfo_ismapped())
                app.toggle_collapse()
                done = tk.BooleanVar(root, False)
                root.after(220, lambda: done.set(True))
                root.wait_variable(done)
                self.assertTrue(app.body.winfo_ismapped())
                data["plan"][1]["status"] = "completed"
                path.write_text(json.dumps(data), encoding="utf-8")
                app.poll(schedule=False)
                self.assertEqual(app.summary["text"], "已完成 2 / 3 项")
                path.write_text("{", encoding="utf-8")
                app.poll(schedule=False)
                self.assertEqual(app.summary["text"], "已完成 2 / 3 项")
                path.write_text('{"plan": []}', encoding="utf-8")
                app.poll(schedule=False)
                self.assertFalse(app.visible)
            finally:
                root.destroy()


class ActiveThreadStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / "threads").mkdir()
        self.reader = overlay.ActiveThreadState(self.directory)
        self.now = 1800000000.0

    def view(self, ident, when=None, available=True):
        value = {"thread_id": ident, "title": "当前 " + str(ident), "available": available,
                 "observed_at": datetime.fromtimestamp(self.now if when is None else when, timezone.utc).isoformat()}
        (self.directory / "active-view.json").write_text(json.dumps(value), encoding="utf-8")

    def plan(self, ident, rows=None):
        value = {"thread_id": ident, "title": ident, "plan": rows if rows is not None else
                 [{"step": ident + " 的任务", "status": "pending"}]}
        (self.directory / "threads" / (ident + ".json")).write_text(json.dumps(value), encoding="utf-8")

    def test_switches_a_b_and_hides_chat_without_plan(self):
        self.plan("chat-a")
        self.plan("chat-b")
        self.view("chat-a")
        self.assertEqual(self.reader.read(self.now)["thread_id"], "chat-a")
        self.view("chat-b")
        self.assertEqual(self.reader.read(self.now)["thread_id"], "chat-b")
        self.view("no-plan")
        self.assertEqual(self.reader.read(self.now), {})
        self.plan("empty-plan", [])
        self.view("empty-plan")
        self.assertEqual(self.reader.read(self.now)["plan"], [])

    def test_unavailable_null_and_unsafe_ids_never_use_current_json(self):
        self.plan("chat-a")
        (self.directory / "current.json").write_text(
            (self.directory / "threads" / "chat-a.json").read_text(), encoding="utf-8")
        for ident, available in [("chat-a", False), (None, True), ("../current", True), ("C:\\current", True)]:
            self.view(ident, available=available)
            self.assertEqual(self.reader.read(self.now), {})

    def test_broken_observation_expires_in_four_seconds(self):
        self.plan("chat-a")
        self.view("chat-a")
        self.assertEqual(self.reader.read(self.now)["thread_id"], "chat-a")
        (self.directory / "active-view.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.reader.read(self.now + 3)["thread_id"], "chat-a")
        self.assertEqual(self.reader.read(self.now + 4.1), {})

    def test_broken_plan_cannot_live_forever_with_fresh_observations(self):
        self.plan("chat-a")
        self.view("chat-a")
        self.reader.read(self.now)
        (self.directory / "threads" / "chat-a.json").write_text("{", encoding="utf-8")
        self.view("chat-a", when=self.now + 3)
        self.assertEqual(self.reader.read(self.now + 3)["thread_id"], "chat-a")
        self.view("chat-a", when=self.now + 5)
        self.assertEqual(self.reader.read(self.now + 5), {})

    def test_many_long_tasks_can_scroll(self):
        root = tk.Tk()
        with tempfile.TemporaryDirectory() as directory:
            app = overlay.PlanOverlay(root, Path(directory), standalone=True, demo=True)
            try:
                app.set_state({"plan": [{"step": "很长的中文任务说明，用来检查换行后的列表仍然可以滚动查看。" * 3,
                                         "status": "pending"} for _ in range(25)]})
                app.poll(schedule=False)
                root.update()
                self.assertTrue(app.scrollbar.winfo_ismapped())
                self.assertLess(app.root.winfo_height(), root.winfo_screenheight())
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
