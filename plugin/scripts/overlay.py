"""Native, read-only plan card. No third-party packages are required.

Run with pythonw overlay.py --data-dir <plugin data folder>.
The producer owns current.json; this viewer never modifies plan completion.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk

from status_icon import AnimatedStatusIcon


STATUSES = {"pending", "in_progress", "completed", "blocked"}
BG = "#FFFFFF"
PANEL = "#F7F7F8"
TEXT = "#303035"
MUTED = "#98989E"
BLUE = "#71717A"
GREEN = "#24986B"
AMBER = "#B98129"
MOTION_OMEGA = 24.0


def normalize_state(value: object) -> dict:
    """Reject malformed snapshots rather than inventing task statuses."""
    if not isinstance(value, dict) or not isinstance(value.get("plan", []), list):
        raise ValueError("Invalid plan snapshot")
    count = 0
    def normalize_rows(items, depth=1):
        nonlocal count
        if not isinstance(items, list) or depth > 6 and items:
            raise ValueError("Invalid task hierarchy")
        rows = []
        for item in items:
            count += 1
            if count > 100 or not isinstance(item, dict):
                raise ValueError("Invalid plan item")
            text, status = item.get("step"), item.get("status")
            if not isinstance(text, str) or not text.strip() or len(text) > 2000 or status not in STATUSES:
                raise ValueError("Invalid step or status")
            row = {"step": text.strip(), "status": status}
            if "children" in item:
                children = normalize_rows(item["children"], depth + 1)
                if children:
                    row["children"] = children
            rows.append(row)
        return rows
    rows = normalize_rows(value.get("plan", []))
    return {
        "thread_id": str(value.get("thread_id", "")),
        "title": str(value.get("title") or "当前会话"),
        "plan": rows,
        "run_state": str(value.get("run_state", "idle")),
        "updated_at": value.get("updated_at"),
        "source": str(value.get("source", "")),
    }


def walk_plan(plan, prefix=()):
    for index, item in enumerate(plan):
        key = prefix + ((index, item["step"]),)
        yield key, item
        yield from walk_plan(item.get("children", []), key)


def completion(plan: list[dict]) -> tuple[int, int]:
    nodes = [item for _, item in walk_plan(plan)]
    return sum(item["status"] == "completed" for item in nodes), len(nodes)


class ActiveThreadState:
    """Select only the observed foreground chat; stale observation fails closed."""
    def __init__(self, data_dir, observed_not_before=None):
        self.data_dir = Path(data_dir)
        self.observed_not_before = observed_not_before
        self.observation = None
        self.selected = None
        self.cached_plan = None
        self.plan_read_at = 0.0

    def read(self, now=None):
        now = time.time() if now is None else now
        try:
            observed = json.loads((self.data_dir / "active-view.json").read_text(encoding="utf-8-sig"))
            # A complete invalid/unavailable observation hides immediately. A transient
            # missing/half-written file may use the preceding observation for <=4s.
            self.observation = observed if isinstance(observed, dict) else None
        except (OSError, ValueError, TypeError):
            pass
        view = self.observation or {}
        ident = view.get("thread_id")
        try:
            stamp = view.get("observed_at")
            if not isinstance(stamp, str):
                raise ValueError("Missing observation timestamp")
            date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("Observation must include timezone")
            timestamp = date.timestamp()
            fresh = -1 <= now - timestamp <= 4 and (
                self.observed_not_before is None or timestamp >= self.observed_not_before
            )
        except (ValueError, TypeError, OverflowError):
            fresh = False
        safe = isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", ident)
        if view.get("available") is not True or not fresh or not safe:
            self.selected, self.cached_plan = None, None
            return {}
        if ident != self.selected:
            self.selected, self.cached_plan = ident, None
        path = self.data_dir / "threads" / (ident + ".json")
        try:
            plan = normalize_state(json.loads(path.read_text(encoding="utf-8-sig")))
            if plan["thread_id"] != ident:
                self.cached_plan = None
                return {}
            if isinstance(view.get("title"), str) and view["title"].strip():
                plan["title"] = view["title"].strip()
            self.cached_plan, self.plan_read_at = plan, now
        except (OSError, ValueError, TypeError):
            pass
        if self.cached_plan is not None and now - self.plan_read_at <= 4:
            return self.cached_plan
        self.cached_plan = None
        return {}


def start_observer(data_dir, codex_home):
    plugin_root = Path(__file__).resolve().parents[1]
    script = plugin_root / "scripts" / "active_thread.mjs"
    node = None
    try:
        runtime = json.loads((plugin_root / "runtime.json").read_text(encoding="utf-8-sig"))
        configured = runtime.get("node") if isinstance(runtime, dict) else None
        if isinstance(configured, str) and Path(configured).is_file():
            node = configured
    except (OSError, ValueError, TypeError):
        pass
    node = node or shutil.which("node")
    if not node or not script.is_file():
        return None
    try:
        return subprocess.Popen(
            [node, str(script), "--data-dir", str(Path(data_dir).resolve()), "--codex-home", str(codex_home),
             "--parent-pid", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError:
        return None


def stop_observer(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def is_codex_process_path(path: str) -> bool:
    """Store builds use ChatGPT.exe; require the actual Codex package directory."""
    executable = PureWindowsPath(path.lower())
    if executable.name in {"codex.exe", "codex-plus-plus.exe"}:
        return True
    return executable.name == "chatgpt.exe" and any(
        component.startswith("openai.codex_") for component in executable.parts[:-1]
    )


def enable_dpi_awareness():
    """Let Tk draw at monitor resolution rather than Windows scaling a bitmap."""
    if sys.platform != "win32":
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    try:
        user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        user.SetProcessDPIAware()
    except AttributeError:
        pass


class WindowsHost:
    """Locate only the foreground Codex window; never inspect other app content."""

    def __init__(self, preview=False):
        self.preview = preview
        self.enabled = sys.platform == "win32"
        self.last_host = None
        self.initial_foreground = None
        self._regions = {}
        if not self.enabled:
            return
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetParent.argtypes = [wintypes.HWND]
        self.user.GetParent.restype = wintypes.HWND
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.get_style = self.user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.user.GetWindowLongW
        self.set_style = self.user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.user.SetWindowLongW
        self.get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_style.restype = ctypes.c_ssize_t
        self.set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.set_style.restype = ctypes.c_ssize_t
        self.get_class_style = self.user.GetClassLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.user.GetClassLongW
        self.set_class_style = self.user.SetClassLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.user.SetClassLongW
        self.get_class_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_class_style.restype = ctypes.c_size_t
        self.set_class_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_size_t]
        self.set_class_style.restype = ctypes.c_size_t

    def hwnd(self, root):
        child = root.winfo_id()
        return self.user.GetParent(child) or child if self.enabled else child

    def prepare(self, root):
        if self.enabled and not self.preview:
            handle = self.hwnd(root)
            # Remove all Win32 frame variants, including styles Tk may restore on map.
            frame_styles = 0x00800000 | 0x00400000 | 0x00040000
            edge_styles = 0x00000100 | 0x00000200 | 0x00020000 | 0x00000001
            self.set_style(handle, -16, self.get_style(handle, -16) & ~frame_styles)
            # TOOLWINDOW + NOACTIVATE: stays out of Alt-Tab and does not steal typing focus.
            self.set_style(handle, -20, (self.get_style(handle, -20) & ~edge_styles) | 0x80 | 0x08000000)
            self.set_class_style(handle, -26, self.get_class_style(handle, -26) & ~0x00020000)
            self._remove_dwm_frame(handle)
            # Flush cached nonclient styles without moving, resizing, or activating.
            self.user.SetWindowPos(handle, None, 0, 0, 0, 0, 0x0037)

    def _remove_dwm_frame(self, handle):
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            # Windows 11 draws a DWM border even when WS_BORDER is absent.
            # DWMWA_BORDER_COLOR=34, DWMWA_COLOR_NONE=0xFFFFFFFE (Microsoft Learn).
            border_color = wintypes.DWORD(0xFFFFFFFE)
            dwm.DwmSetWindowAttribute(handle, 34, ctypes.byref(border_color), ctypes.sizeof(border_color))
            # The application-owned region supplies round corners. Do not request
            # another DWM frame around it, which can add a system outline/shadow.
            corner_preference = ctypes.c_int(1)  # DWMWCP_DONOTROUND
            dwm.DwmSetWindowAttribute(handle, 33, ctypes.byref(corner_preference), ctypes.sizeof(corner_preference))
            nonclient_policy = ctypes.c_int(1)  # DWMNCRP_DISABLED
            dwm.DwmSetWindowAttribute(handle, 2, ctypes.byref(nonclient_policy), ctypes.sizeof(nonclient_policy))
        except (OSError, AttributeError):
            pass

    def round_window(self, root, width, height, radius, redraw=True):
        if not self.enabled or self.preview:
            return
        self._round_region(self.hwnd(root), width, height, radius, redraw=redraw)

    def _round_region(self, handle, width, height, radius, redraw=True):
        geometry = (width, height, radius)
        if self._regions.get(handle) == geometry:
            return
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi.CreateRoundRectRgn.restype = wintypes.HANDLE
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        self.user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        region = gdi.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        if region:
            if self.user.SetWindowRgn(handle, region, redraw):
                self._regions[handle] = geometry
            else:
                gdi.DeleteObject(region)

    def round_child(self, widget, width, height, radius):
        if not self.enabled:
            return
        self._round_region(widget.winfo_id(), width, height, radius)

    def process_path(self, handle):
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            return ""
        try:
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if self.kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                return name.value
            return ""
        finally:
            self.kernel.CloseHandle(process)

    def process_name(self, handle):
        return PureWindowsPath(self.process_path(handle)).name.lower()

    def capture_initial_foreground(self):
        """Called before creating Tk, which may briefly activate a hidden wrapper."""
        if not self.enabled:
            return
        self.initial_foreground = self.user.GetForegroundWindow()
        if self.initial_foreground and is_codex_process_path(self.process_path(self.initial_foreground)):
            self.last_host = self.initial_foreground

    def restore_focus_if_owned(self, root, previous=None):
        if not self.enabled:
            return
        previous = previous if previous is not None else self.initial_foreground
        own = self.hwnd(root)
        # Do not override a user switching apps while this window initializes/shows.
        if previous and previous != own and self.user.IsWindow(previous) and self.user.GetForegroundWindow() == own:
            self.user.SetForegroundWindow(previous)

    def foreground_rect(self, root):
        if not self.enabled:
            return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        foreground = self.user.GetForegroundWindow()
        own = self.hwnd(root)
        if foreground == own:
            foreground = self.last_host
        elif is_codex_process_path(self.process_path(foreground)):
            self.last_host = foreground
        else:
            return None
        if not foreground or not self.user.IsWindowVisible(foreground) or self.user.IsIconic(foreground):
            return None
        rect = wintypes.RECT()
        if self.user.GetWindowRect(foreground, ctypes.byref(rect)):
            return rect.left, rect.top, rect.right, rect.bottom
        return None

    def show(self, root):
        previous = self.user.GetForegroundWindow() if self.enabled else None
        self.prepare(root)
        root.deiconify()
        if self.enabled:
            self.prepare(root)
            self.user.ShowWindow(self.hwnd(root), 4)  # SW_SHOWNOACTIVATE
            self.user.SetWindowPos(self.hwnd(root), wintypes.HWND(-1), 0, 0, 0, 0, 0x0013)
            self.restore_focus_if_owned(root, previous)

    def move(self, root, x, y):
        if self.enabled:
            # Native coordinates support monitors to the left/above the primary display.
            self.user.SetWindowPos(self.hwnd(root), None, x, y, 0, 0, 0x0015)
        else:
            root.geometry(f"+{max(0, x)}+{max(0, y)}")


class PlanOverlay:
    def __init__(self, root: tk.Tk, data_dir: Path, standalone=False, demo=False, preview=False, host=None):
        self.root, self.data_dir = root, Path(data_dir)
        self.standalone, self.demo, self.preview = standalone, demo, preview
        self.host = host if host is not None else WindowsHost(preview=preview)
        self.state = normalize_state({})
        self.shadows = []
        self.shadow_host = WindowsHost(preview=False)
        self.signature = None
        self.active_state = ActiveThreadState(self.data_dir, observed_not_before=time.time())
        self.collapsed = False
        self.view_states = {}
        self.expanded_tasks = set()
        self.expanded_by_thread = {}
        self.wheel_delta = 0
        self.visible = False
        self.drag = None
        self.offset = (0, 0)
        self.last_rect = None
        self.resizing = False
        self.resize_pending = False
        self.height_job = None
        self.height_animation = None
        self.height_animation_generation = 0
        self.card_content_height = None
        self.expanded_height = None
        self.task_animation_job = None
        self.task_animation = None
        self.task_animation_generation = 0
        self.ordered_rows = []
        self.row_cache = {}
        self.root_rows = []
        self.rows_height = 1.0
        self.laying_rows = False
        self.rows_layout_job = None
        self.rows_width = 0
        self.measuring_rows = False
        self.chrome_height = None
        self.viewport_height = None
        self.window_size = None
        self.window_position = None
        self.rows_geometry = None
        self.outline_geometry = None
        self.shadow_geometry = None
        self.decoration_job = None
        self.icon_activity_job = None
        self.progress_job = None
        self.tip_job = None
        self.tooltip = None
        self.ratio = 0.0
        self.placement = {}
        self.placement_signature = None
        root.withdraw()
        root.title("Codex · 任务进度")
        root.overrideredirect(not preview)
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        self.scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
        self.width = round(280 * self.scale)
        root.geometry(f"{self.width}x100")
        self._build()
        root.bind("<Destroy>", self._on_destroy, add="+")
        root.update_idletasks()
        self.host.prepare(root)
        self.host.restore_focus_if_owned(root)
        self._create_shadows()
        self.host.restore_focus_if_owned(root)
        if demo:
            self.set_state({"thread_id": "demo-38fc8d1a", "title": "优化网站首页", "run_state": "running", "plan": [
                {"step": "确认页面结构", "status": "completed"},
                {"step": "调整布局和配色", "status": "completed"},
                {"step": "完善交互细节", "status": "in_progress"},
                {"step": "检查不同屏幕效果", "status": "pending"},
                {"step": "整理结果并交付", "status": "pending"},
            ]})

    def _label(self, parent, text="", **kwargs):
        opts = dict(text=text, bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 9), anchor="w", bd=0)
        opts.update(kwargs)
        return tk.Label(parent, **opts)

    def _on_destroy(self, event):
        if event.widget is self.root:
            self._cancel_task_animation(settle=False)
            self._cancel_height_animation(settle=False)
            for callback in self.root.tk.call("after", "info"):
                try:
                    self.root.after_cancel(callback)
                except tk.TclError:
                    pass

    def _create_shadows(self):
        if self.preview:
            return
        # Two narrow translucent layers supply a small controlled shadow without
        # enabling the Windows frame/shadow that also drew an unwanted grey line.
        for spread, opacity in ((4, 0.025), (2, 0.035)):
            previous = self.shadow_host.user.GetForegroundWindow() if self.shadow_host.enabled else None
            shadow = tk.Toplevel(self.root)
            shadow.withdraw()
            shadow.overrideredirect(True)
            shadow.configure(bg="#2A2B30")
            shadow.attributes("-topmost", True)
            shadow.attributes("-alpha", opacity)
            shadow.update_idletasks()
            self.shadow_host.prepare(shadow)
            if self.shadow_host.enabled:
                handle = self.shadow_host.hwnd(shadow)
                self.shadow_host.set_style(handle, -20, self.shadow_host.get_style(handle, -20) | 0x00000020 | 0x00080000)
                self.shadow_host.restore_focus_if_owned(shadow, previous)
            self.shadows.append((shadow, spread))

    def _sync_shadows(self):
        if not self.shadows:
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        geometry = (x, y, width, height)
        if geometry == self.shadow_geometry:
            return
        self.shadow_geometry = geometry
        after = self.host.hwnd(self.root) if self.host.enabled else None
        for shadow, logical_spread in reversed(self.shadows):
            spread = round(logical_spread * self.scale)
            offset = round(2 * self.scale)
            w, h = width + 2 * spread, height + 2 * spread
            self.shadow_host.round_window(shadow, w, h, round(16 * self.scale) + spread, redraw=False)
            if self.shadow_host.enabled:
                handle = self.shadow_host.hwnd(shadow)
                # Place both transparent, click-through layers directly behind card.
                self.shadow_host.user.SetWindowPos(handle, after, x - spread, y - spread + offset, w, h, 0x0010)
                after = handle
            else:
                shadow.geometry(f"{w}x{h}+{max(0, x-spread)}+{max(0, y-spread+offset)}")

    def _button(self, parent, text, tip, action):
        button = self._label(parent, text, fg=MUTED, font=("Segoe UI", 11), cursor="hand2", width=2)
        button.pack(side="right", padx=(2, 0))
        def enter(event):
            button.configure(bg="#F0F0F2", fg=TEXT)
            self._cancel_tip()
            self.tip_job = self.root.after(650, lambda: self._show_tip(button, tip))
        def leave(event):
            button.configure(bg=BG, fg=MUTED)
            self._cancel_tip()
        def click(event):
            self._cancel_tip()
            action()
        button.bind("<Enter>", enter)
        button.bind("<Leave>", leave)
        button.bind("<Button-1>", click)
        return button

    def _show_tip(self, widget, text):
        self.tip_job = None
        if not widget.winfo_exists():
            return
        tip = tk.Toplevel(self.root)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, bg="#27334A", fg="#FFFFFF", padx=7, pady=4,
                 font=("Microsoft YaHei UI", 8)).pack()
        tip.update_idletasks()
        tip.geometry(f"+{max(0, widget.winfo_rootx() - 16)}+{widget.winfo_rooty() + widget.winfo_height() + 5}")
        self.host.prepare(tip)
        tip.deiconify()
        self.tooltip = tip

    def _cancel_tip(self):
        if self.tip_job:
            self.root.after_cancel(self.tip_job)
            self.tip_job = None
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

    def _build(self):
        self.outline = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        self.outline.pack(fill="both", expand=True)
        self.card = tk.Frame(self.outline, bg=BG)
        self.card_window = self.outline.create_window(1, 1, window=self.card, anchor="nw")
        self.outline.bind("<Configure>", self._outline_configure)
        header = tk.Frame(self.card, bg=BG)
        self.header = header
        header.pack(fill="x", padx=12, pady=(7, 4))
        self.heading = self._label(header, "任务进度", font=("Microsoft YaHei UI", 9), fg="#77777D")
        self.heading.pack(side="left")
        self.badge = self._label(header, fg=BLUE, font=("Segoe UI", 8, "bold"), padx=5)
        self.badge.pack(side="left", padx=(5, 0))
        self.close = self._button(header, "×", "关闭小窗", self.root.destroy)
        self.fold = self._button(header, "−", "收起 / 展开", self.toggle_collapse)
        self.badge.bind("<Enter>", lambda _: self._progress_tip())
        self.badge.bind("<Leave>", lambda _: self._cancel_tip())
        self.details = tk.Frame(self.card, bg=BG)
        self.details.pack(fill="both", expand=True)
        self.title = self._label(self.details, fg=MUTED, font=("Microsoft YaHei UI", 8))
        self.title.pack(fill="x", padx=14, pady=(0, 5))
        self.title.bind("<Enter>", lambda _: self._title_tip())
        self.title.bind("<Leave>", lambda _: self._cancel_tip())
        for item in [header, self.heading, self.badge, self.title, self.card, self.outline]:
            item.configure(cursor="fleur")
            item.bind("<ButtonPress-1>", self.start_drag)
        # The toplevel binding also receives release/motion outside the header
        # while the drag owns the pointer, including over another child widget.
        self.root.bind("<B1-Motion>", self.move_drag, add="+")
        self.root.bind("<ButtonRelease-1>", self.end_drag, add="+")
        metrics = tk.Frame(self.details, bg=BG)
        # Keep accessible summary text available without duplicating the header count.
        self.summary = self._label(metrics, fg=MUTED, font=("Microsoft YaHei UI", 8))
        self.summary.pack(side="left")
        self.percent = self._label(metrics, fg=BLUE, font=("Segoe UI", 8, "bold"))
        self.percent.pack(side="right")
        self.progress = tk.Canvas(self.details, height=3, bg="#EFF3F8", highlightthickness=0)
        self.progress.pack(fill="x", padx=14, pady=(5, 7))
        self.progress.bind("<Configure>", lambda _: self._draw_progress())
        self.body = tk.Frame(self.details, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.body, bg=BG, highlightthickness=0, width=1, height=1)
        # A fixed narrow gutter avoids text rewrapping when scrolling becomes needed.
        self.canvas.pack(side="left", fill="both", expand=True, padx=(9, round(12 * self.scale)))
        style = ttk.Style(self.root)
        style.configure("Plan.Vertical.TScrollbar", arrowsize=9, width=8)
        self.scrollbar = ttk.Scrollbar(self.body, orient="vertical", command=self.canvas.yview,
                                      style="Plan.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._view_changed)
        self.rows = tk.Frame(self.canvas, bg=BG)
        self.rows_window = self.canvas.create_window(0, 0, window=self.rows, anchor="nw",
                                                     width=self.width - round(22 * self.scale))
        self.canvas.bind("<Configure>", self._canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._scroll)
        self.footer = self._label(self.details, fg=MUTED, font=("Microsoft YaHei UI", 8))
        self.footer.pack(fill="x", padx=14, pady=(4, 7))

    def _outline_configure(self, event):
        self._set_card_geometry(event.width, event.height)
        self._queue_decoration()

    def _set_card_geometry(self, width, height):
        # Keep natural content geometry while the outer window reveals/crops it.
        geometry = (width, self.card_content_height or height)
        if geometry != self.outline_geometry:
            self.outline_geometry = geometry
            self.outline.itemconfigure(self.card_window, width=max(1, geometry[0] - 2), height=max(1, geometry[1] - 2))

    def _queue_decoration(self):
        if self.decoration_job is None:
            self.decoration_job = self.root.after_idle(self._decorate_window)

    def _decorate_window(self):
        self.decoration_job = None
        width, height = self.root.winfo_width(), self.root.winfo_height()
        radius = round(16 * self.scale)
        self.host.round_window(self.root, width, height, radius)
        # The rounded top-level clips all descendants. A second changing region
        # on card duplicated redraws and needlessly cropped its natural layout.
        self._sync_shadows()

    def _scroll(self, event):
        if self.collapsed:
            self.wheel_delta = 0
            return
        self.wheel_delta += event.delta
        units = -int(self.wheel_delta / 120)
        if units:
            self.wheel_delta += units * 120
            self.canvas.yview_scroll(units, "units")
            self._sync_icon_activity()

    def _view_changed(self, first, last):
        self.scrollbar.set(first, last)
        self._queue_icon_activity()

    def _canvas_configure(self, event):
        if event.width != self.rows_width:
            self.rows_width = event.width
            for widget in self.rows.winfo_children():
                self._configure_row_width(widget, event.width)
            if not self.laying_rows and not self.measuring_rows and self.rows_layout_job is None:
                self.rows_layout_job = self.root.after_idle(self._measure_and_layout_rows)

    def _row_layout_width(self):
        width = self.canvas.winfo_width()
        # Tk reports 1px until its first Map event. Use the exact packed viewport
        # width in that interval, not a 1px label wrap or a different approximation.
        return width if width > 1 else max(1, self.width - 2 - 9 - round(12 * self.scale))

    def _measure_rows(self):
        if self.measuring_rows:
            return
        self.measuring_rows = True
        try:
            width = self._row_layout_width()
            self.rows_width = width
            for row in self.ordered_rows:
                self._configure_row_width(row, width)
            dirty = [row for row in self.ordered_rows if row.measure_dirty]
            if not dirty:
                return
            # Resolve new/wrapped text once before motion, never on every frame.
            self.root.update_idletasks()
            for row in dirty:
                row.natural_height = max(1, row.content.winfo_reqheight())
                row.measure_dirty = False
        finally:
            self.measuring_rows = False

    def _measure_and_layout_rows(self):
        self.rows_layout_job = None
        self._measure_rows()
        self._layout_rows()

    def _configure_row_width(self, row, width):
        reserved = round((38 + 12 * row.depth) * self.scale)
        if row.plan_node.get("children"):
            reserved += row.disclosure.winfo_reqwidth() + 6
        wrap = max(round(70 * self.scale), width - reserved)
        content_width = max(1, width - round(12 * row.depth * self.scale) - 8)
        if row.measure_width != (wrap, content_width):
            row.measure_width = (wrap, content_width)
            row.measure_dirty = True
            row.step_label.configure(wraplength=wrap)
            row.content.place_configure(width=content_width)

    def set_state(self, state):
        state = normalize_state(state)
        if state == self.state:
            return
        old = {key: item["status"] for key, item in walk_plan(self.state["plan"])}
        switched = state["thread_id"] != self.state["thread_id"]
        restored_scroll = 0.0
        if switched:
            self._cancel_task_animation(settle=False)
            self._cancel_height_animation(settle=False)
            self._cancel_tip()
            previous_id = self.state["thread_id"]
            if previous_id:
                self.view_states[previous_id] = (self.collapsed, self.canvas.yview()[0])
                self.expanded_by_thread[previous_id] = set(self.expanded_tasks)
            old = {}
            self.expanded_tasks = set(self.expanded_by_thread.get(state["thread_id"], set()))
            self.collapsed, restored_scroll = self.view_states.get(state["thread_id"], (False, 0.0))
            self.fold.configure(text="+" if self.collapsed else "−")
            if self.collapsed:
                self.details.pack_forget()
            else:
                self.details.pack(fill="both", expand=True)
            self.wheel_delta = 0
        self.state = state
        label = state["title"].replace("\n", " ").strip()
        title = label[:13] + ("…" if len(label) > 13 else "")
        self.title.configure(text=title)
        done, total = completion(state["plan"])
        percent = round(100 * done / total) if total else 0
        self.summary.configure(text=f"已完成 {done} / {total} 项")
        self.badge.configure(text=f"{done}/{total}", fg=GREEN if total and done == total else BLUE)
        self.percent.configure(text=f"{percent}%", fg=GREEN if total and done == total else BLUE)
        valid_branches = {key for key, item in walk_plan(state["plan"]) if item.get("children")}
        self.expanded_tasks.intersection_update(valid_branches)
        self._render_rows(old, rebuild=switched, animate=bool(self.task_animation) and not switched)
        if total and done == total:
            footer, color = "✓  全部完成", GREEN
        elif state["run_state"] == "interrupted":
            footer, color = "暂停中 · 等待继续", AMBER
        else:
            nodes = [item for _, item in walk_plan(state["plan"])]
            blocked = sum(item["status"] == "blocked" for item in nodes)
            working = any(item["status"] == "in_progress" for item in nodes)
            if state["run_state"] == "running" and working:
                footer = "正在执行" + (f" · {blocked} 项待处理" if blocked else "")
                color = BLUE
            elif blocked:
                footer, color = f"等待处理 · {blocked} 项受阻", AMBER
            else:
                footer, color = "等待继续", MUTED
        self.footer.configure(text=footer, fg=color)
        self._animate_progress(done / max(total, 1))
        self._sync_icon_activity()
        self._resize()
        if switched:
            ident = state["thread_id"]
            self.root.after_idle(lambda: self._restore_scroll(ident, restored_scroll))

    def _restore_scroll(self, ident, offset):
        if self.state["thread_id"] == ident:
            self.canvas.yview_moveto(offset)
            self._sync_icon_activity()

    def _render_rows(self, old=None, rebuild=False, animate=False):
        old = old or {}
        existing = {row.plan_key: row for row in self.rows.winfo_children()}
        if rebuild:
            for row in existing.values():
                self._drop_row(row)
            existing = {}
        needed_keys = set()
        def include(items, prefix=()):
            for index, item in enumerate(items):
                key = prefix + ((index, item["step"]),)
                needed_keys.add(key)
                previous = existing.get(key)
                if key in self.expanded_tasks or animate and previous is not None and previous.expansion > 0:
                    include(item.get("children", []), key)
        include(self.state["plan"])
        cached, targets = {}, {}
        for key, item in walk_plan(self.state["plan"]):
            row = existing.pop(key, None)
            if key not in needed_keys and row is None:
                continue
            highlight = key in needed_keys and item["status"] == "completed" and old.get(key) not in (None, "completed")
            if row is None:
                row = self._add_row(item, key, highlight)
            elif row.plan_status != item["status"]:
                self._update_row(row, item, highlight=highlight)
            row.plan_node = item
            self._update_disclosure(row)
            row.child_rows = []
            cached[key] = row
            if item.get("children"):
                targets[key] = 1.0 if key in self.expanded_tasks else 0.0
            else:
                row.expansion = row.expansion_velocity = 0.0
        for row in existing.values():
            self._drop_row(row)
        self.row_cache = cached
        self.root_rows = []
        for key, row in cached.items():
            parent = cached.get(key[:-1])
            if parent is None:
                self.root_rows.append(row)
            else:
                parent.child_rows.append(row)
        self.ordered_rows = list(cached.values())
        self._measure_rows()
        self._measure_branch_extents()
        if animate:
            # Status-only updates retain both the branch motion and its velocity.
            if self.task_animation is None or self.task_animation["to"] != targets:
                self._begin_task_animation(targets)
            else:
                self._layout_rows()
        else:
            self._cancel_task_animation(settle=False)
            for row in cached.values():
                row.expansion = targets.get(row.plan_key, 0.0)
                row.expansion_velocity = 0.0
            self._layout_rows(resize=False)

    def _begin_task_animation(self, targets):
        self._cancel_task_animation(settle=False)
        if self.chrome_height is None:
            self._cache_chrome_height()
        now = time.monotonic()
        self.task_animation = {
            "started": now, "last_sample": now, "to": targets,
        }
        self._task_animation_frame(self.task_animation_generation)

    def _sample_task_animation(self, now):
        if self.task_animation is None:
            return False
        animation = self.task_animation
        elapsed = max(0.0, now - animation["last_sample"])
        animation["last_sample"] = now
        omega = MOTION_OMEGA
        decay = math.exp(-omega * elapsed)
        error_pixels = speed_pixels = 0.0
        for key, target in animation["to"].items():
            row = self.row_cache.get(key)
            if row is None:
                continue
            error = row.expansion - target
            coefficient = row.expansion_velocity + omega * error
            value = target + (error + coefficient * elapsed) * decay
            velocity = (row.expansion_velocity - omega * coefficient * elapsed) * decay
            # Preserve velocity when retargeting; clamp only numerical/boundary
            # excursions, never allow content or the card to overshoot its extent.
            row.expansion = min(1.0, max(0.0, value))
            row.expansion_velocity = 0.0 if value <= 0 and velocity < 0 or value >= 1 and velocity > 0 else velocity
            extent = max(1.0, row.child_content_height)
            error_pixels += abs(row.expansion - target) * extent
            speed_pixels += abs(row.expansion_velocity) * extent
        finished = error_pixels < 0.5 and speed_pixels < 10.0
        if finished:
            for key, target in animation["to"].items():
                row = self.row_cache.get(key)
                if row is not None:
                    row.expansion, row.expansion_velocity = target, 0.0
        return finished

    def _task_animation_frame(self, generation):
        self.task_animation_job = None
        if generation != self.task_animation_generation or self.task_animation is None:
            return
        animation = self.task_animation
        finished = self._sample_task_animation(time.monotonic())
        if finished:
            self.task_animation = None
        self._layout_rows(fast=True)
        if not finished:
            # Schedule against an absolute cadence, subtracting rendering time.
            # An 8ms request gives Windows room to coalesce timers without adding
            # another full 16ms after each layout; no system timer is changed.
            delay = self._motion_delay(animation["started"])
            self.task_animation_job = self.root.after(delay, lambda: self._task_animation_frame(generation))

    @staticmethod
    def _motion_delay(started):
        now = time.monotonic()
        tick = math.floor((now - started) / 0.008 + 1e-6) + 1
        return max(1, math.ceil((started + tick * 0.008 - now) * 1000))

    def _cancel_task_animation(self, settle=True, resize=True):
        self.task_animation_generation += 1
        if self.rows_layout_job:
            self.root.after_cancel(self.rows_layout_job)
            self.rows_layout_job = None
        if self.task_animation_job:
            self.root.after_cancel(self.task_animation_job)
            self.task_animation_job = None
        animation, self.task_animation = self.task_animation, None
        if settle and animation is not None:
            for key, target in animation["to"].items():
                row = self.row_cache.get(key)
                if row is not None:
                    row.expansion, row.expansion_velocity = target, 0.0
            self._layout_rows(resize=resize)

    def _measure_branch_extents(self):
        def measure(row):
            row.child_content_height = sum(measure(child) for child in row.child_rows)
            row.total_height = row.natural_height + 2 + row.child_content_height * row.expansion
            return row.total_height
        return sum(measure(row) for row in self.root_rows)

    def _layout_rows(self, resize=True, fast=False):
        self.rows_layout_job = None
        if self.laying_rows:
            return
        self.laying_rows = True
        try:
            width = self._row_layout_width()
            height = self._measure_branch_extents()
            visible = []
            def place(row, top, clip_end):
                indent = round(12 * row.depth * self.scale)
                row_width = max(1, width - indent - 8)
                natural = row.natural_height
                content_geometry = (row_width, natural)
                if content_geometry != row.content_geometry:
                    row.content_geometry = content_geometry
                    row.content.place_configure(x=0, y=0, width=row_width, height=natural)
                # A branch has one moving lower boundary. Rows keep their full
                # natural spacing; only the row crossed by that boundary clips.
                row.layout_y = round(top + 1)
                row.clip_height = max(0, min(natural, round(clip_end) - row.layout_y)) if math.isfinite(clip_end) else natural
                row.reveal = row.clip_height / natural
                if row.clip_height:
                    visible.append(row)
                    geometry = (indent, row.layout_y, row_width, row.clip_height)
                    if geometry != row.layout_geometry:
                        row.layout_geometry = geometry
                        row.place(x=indent, y=row.layout_y, width=row_width, height=row.clip_height)
                else:
                    self._hide_row(row)
                child_top = top + natural + 2
                child_end = min(clip_end, child_top + row.child_content_height * row.expansion)
                for child in row.child_rows:
                    place(child, child_top, child_end)
                    child_top += child.total_height
            top = 0.0
            for row in self.root_rows:
                place(row, top, math.inf)
                top += row.total_height
            self.ordered_rows = visible
            self.rows_height = max(1.0, height)
            geometry = (width, round(self.rows_height))
            if geometry != self.rows_geometry:
                self.rows_geometry = geometry
                self.rows.configure(height=geometry[1])
                self.canvas.itemconfigure(self.rows_window, width=width, height=geometry[1])
                self.canvas.configure(scrollregion=(0, 0, width, geometry[1]))
            if resize:
                self._resize(fast=fast)
            self._queue_icon_activity()
        finally:
            self.laying_rows = False

    def _drop_row(self, row):
        if row.highlight_job:
            self.root.after_cancel(row.highlight_job)
        row.destroy()

    def _hide_row(self, row):
        # Cache only nodes still present in this chat (at most 100). Reopening
        # reuses their text and icons; a chat switch/deleted node destroys them.
        row.reveal, row.reveal_velocity, row.clip_height = 0.0, 0.0, 0
        if row.layout_geometry is not None:
            row.place_forget()
            row.layout_geometry = None
        row.status_icon.set_active(False)
        if row.highlight_job:
            self.root.after_cancel(row.highlight_job)
            row.highlight_job = None
            self._update_row(row, row.plan_node)

    def _update_disclosure(self, row):
        children = row.plan_node.get("children", [])
        signature = (*completion(children), row.plan_key in self.expanded_tasks)
        if row.disclosure_signature == signature:
            return
        row.disclosure_signature = signature
        if children:
            done, total = completion(children)
            arrow = "▾" if row.plan_key in self.expanded_tasks else "▸"
            row.disclosure.configure(text=f"{done}/{total} {arrow}")
            row.disclosure.pack(side="right", before=row.step_label, anchor="n", padx=(2, 3), pady=2)
            row.step_label.configure(cursor="hand2")
            row.step_label.bind("<Button-1>", lambda _, key=row.plan_key: self.toggle_task(key))
        else:
            row.disclosure.pack_forget()
            row.step_label.configure(cursor="")
            row.step_label.unbind("<Button-1>")
        self._configure_row_width(row, self._row_layout_width())

    def toggle_task(self, key):
        if key in self.expanded_tasks:
            self.expanded_tasks.remove(key)
        else:
            self.expanded_tasks.add(key)
        self._cancel_tip()
        self._render_rows(animate=self.visible and not self.collapsed)
        if self.task_animation is None:
            self._resize()
        self._sync_icon_activity()

    def _draw_progress(self):
        self.progress.delete("all")
        width = max(0, self.progress.winfo_width() * self.ratio)
        if width:
            done, total = completion(self.state["plan"])
            self.progress.create_line(1, 1.5, width, 1.5, width=3, capstyle="round",
                                      fill=GREEN if total and done == total else BLUE)

    def _animate_progress(self, target):
        if self.progress_job:
            self.root.after_cancel(self.progress_job)
            self.progress_job = None
        start = self.ratio
        def frame(index=0):
            fraction = min(1, index / 12)
            self.ratio = start + (target - start) * (1 - (1 - fraction) ** 3)
            self._draw_progress()
            self.progress_job = self.root.after(16, lambda: frame(index + 1)) if fraction < 1 else None
        frame()

    def _add_row(self, item, key, highlight=False):
        status = item["status"]
        row = tk.Frame(self.rows, bg=PANEL if status == "in_progress" else BG)
        row.depth = len(key) - 1
        row.reveal = 1.0
        row.reveal_velocity = 0.0
        row.expansion = 1.0 if key in self.expanded_tasks else 0.0
        row.expansion_velocity = 0.0
        row.child_rows = []
        row.child_content_height = 0.0
        row.total_height = 0.0
        row.natural_height, row.clip_height = 1, 1
        row.measure_width = None
        row.measure_dirty = True
        row.content_geometry = None
        row.layout_geometry = None
        row.layout_y = 0
        row.disclosure_signature = None
        row.content = tk.Frame(row, bg=row["bg"])
        row.content.place(x=0, y=0, width=self.width, height=1)
        icon = AnimatedStatusIcon(row.content, status, size=18, bg=row["bg"],
                                  animate_completion=highlight)
        icon.pack(side="left", anchor="n", padx=(3, 2), pady=2)
        row.disclosure = tk.Button(row.content, text="", bg=row["bg"], fg=MUTED, activebackground="#F0F0F2",
                                   activeforeground=TEXT, font=("Segoe UI", 8), relief="flat", overrelief="flat",
                                   bd=0, highlightthickness=0, padx=1, pady=0, cursor="hand2",
                                   command=lambda: self.toggle_task(key))
        step = self._label(row.content, item["step"], fg="#8A95A5" if status == "completed" else TEXT,
                           bg=row["bg"], justify="left", wraplength=self.width - round(55 * self.scale),
                           font=("Microsoft YaHei UI", 9))
        step.pack(side="left", fill="x", expand=True, padx=(0, 5), pady=2)
        row.plan_step, row.plan_status = item["step"], status
        row.plan_key, row.plan_node = key, item
        row.status_icon = icon
        row.step_label = step
        row.highlight_job = None
        if highlight:
            self._fade_completed_row(row)
        return row

    def _update_row(self, row, item, highlight=False):
        if row.highlight_job:
            self.root.after_cancel(row.highlight_job)
            row.highlight_job = None
        status = item["status"]
        row.plan_status = status
        background = PANEL if status == "in_progress" else BG
        row.configure(bg=background)
        row.content.configure(bg=background)
        row.disclosure.configure(bg=background)
        row.step_label.configure(bg=background, fg="#8A95A5" if status == "completed" else TEXT)
        row.status_icon.set_background(background)
        row.status_icon.set_status(status, animate=highlight)
        if highlight:
            self._fade_completed_row(row)

    def _fade_completed_row(self, row, index=0):
        row.highlight_job = None
        if not row.winfo_exists():
            return
        ratio = min(1, index / 24)
        initial = (234, 248, 240)
        color = "#" + "".join(f"{round(v + (255-v)*ratio):02x}" for v in initial)
        row.configure(bg=color)
        row.content.configure(bg=color)
        row.disclosure.configure(bg=color)
        row.step_label.configure(bg=color)
        row.status_icon.set_background(color)
        if ratio < 1:
            row.highlight_job = self.root.after(20, lambda: self._fade_completed_row(row, index + 1))

    def _queue_icon_activity(self):
        if self.icon_activity_job is None:
            self.icon_activity_job = self.root.after_idle(self._sync_icon_activity)

    def _sync_icon_activity(self):
        if getattr(self, "icon_activity_job", None) is not None:
            self.root.after_cancel(self.icon_activity_job)
            self.icon_activity_job = None
        active = self.visible and not self.collapsed and self.state["run_state"] == "running"
        top = self.canvas.canvasy(0) if active else 0
        bottom = top + self.canvas.winfo_height() if active else 0
        for row in self.rows.winfo_children():
            icon = row.status_icon
            clipped = getattr(row, "clip_height", None)
            if not active or isinstance(clipped, (int, float)) and clipped <= 0:
                icon.set_active(False)
                continue
            row_top = getattr(row, "layout_y", None)
            row_top = row_top if isinstance(row_top, (int, float)) else row.winfo_y()
            icon_top = row_top + icon.winfo_y()
            in_view = active and icon_top < bottom and icon_top + icon.winfo_height() > top
            if isinstance(clipped, (int, float)):
                in_view = in_view and clipped > icon.winfo_y()
            icon.set_active(in_view)

    def _cache_chrome_height(self):
        if not self.collapsed:
            self.chrome_height = max(0, self.card.winfo_reqheight() - self.canvas.winfo_reqheight() + 2)

    def _resize(self, animate=False, fast=False):
        self.resize_pending = False
        if self.resizing:
            return
        self.resizing = True
        fast = fast or self.task_animation is not None or self.height_animation is not None
        if not fast or self.chrome_height is None:
            self.root.update_idletasks()
            self._cache_chrome_height()
        max_height = min(round(256 * self.scale),
                         (self.last_rect[3] - self.last_rect[1] - 90) if self.last_rect else self.root.winfo_screenheight() - 90)
        row_height = self.rows_height
        available = max(50, max_height - round(116 * self.scale))
        viewport = max(1, round(min(row_height, available)))
        scrolling = row_height > available and (not self.collapsed or animate or self.height_animation is not None)
        if self.viewport_height != (viewport, scrolling):
            self.viewport_height = (viewport, scrolling)
            self.canvas.configure(height=viewport)
            if scrolling:
                self.scrollbar.place(relx=1, x=-3, y=0, relheight=1, width=round(8 * self.scale), anchor="ne")
            else:
                self.scrollbar.place_forget()
        expanded = min(max_height, (self.chrome_height or round(116 * self.scale)) + viewport)
        self.expanded_height = expanded
        target = expanded
        if self.collapsed:
            # Stop exactly below the header's natural height and its 7/4px
            # padding, so no title fragment vanishes on the final frame.
            target = self.header.winfo_reqheight() + 13
        self.resizing = False
        if animate or self.height_animation is not None and self.visible:
            self._animate_height(target, expanded)
        else:
            self._cancel_height_animation(settle=False)
            self._commit_height(target)
            self._finish_height_content(target)

    def _commit_height(self, value):
        height = max(1, round(value))
        size = (self.width, height)
        if size != self.window_size:
            self.window_size = size
            self.root.geometry(f"{self.width}x{height}")
        self._position(height=height)

    def _finish_height_content(self, height):
        self.card_content_height = None
        if self.collapsed:
            self.details.pack_forget()
        elif not self.details.winfo_manager():
            self.details.pack(fill="both", expand=True)
        self._set_card_geometry(self.width, round(height))
        self._queue_decoration()

    def _animate_height(self, target, expanded):
        previous = self.height_animation
        value = previous["value"] if previous is not None else float(self.root.winfo_height())
        self.card_content_height = max(round(value), round(expanded))
        self._set_card_geometry(self.width, round(value))
        if previous is not None and previous["target"] == target:
            return
        if self.height_job:
            self.root.after_cancel(self.height_job)
            self.height_job = None
        self.height_animation_generation += 1
        now = time.monotonic()
        self.height_animation = {
            "started": now, "last_sample": now, "target": float(target),
            "value": value, "velocity": previous["velocity"] if previous is not None else 0.0,
            "minimum": float(self.header.winfo_reqheight() + 13),
            "maximum": float(max(expanded, value)),
        }
        self._height_animation_frame(self.height_animation_generation)

    def _height_animation_frame(self, generation):
        self.height_job = None
        animation = self.height_animation
        if generation != self.height_animation_generation or animation is None:
            return
        now = time.monotonic()
        elapsed = max(0.0, now - animation["last_sample"])
        animation["last_sample"] = now
        target = animation["target"]
        error = animation["value"] - target
        coefficient = animation["velocity"] + MOTION_OMEGA * error
        decay = math.exp(-MOTION_OMEGA * elapsed)
        value = target + (error + coefficient * elapsed) * decay
        velocity = (animation["velocity"] - MOTION_OMEGA * coefficient * elapsed) * decay
        lower, upper = animation["minimum"], animation["maximum"]
        animation["value"] = min(upper, max(lower, value))
        animation["velocity"] = 0.0 if value <= lower and velocity < 0 or value >= upper and velocity > 0 else velocity
        finished = abs(animation["value"] - target) < 0.35 and abs(animation["velocity"]) < 8.0
        if finished:
            self.height_animation = None
            self._commit_height(target)
            self._finish_height_content(target)
        else:
            self._commit_height(animation["value"])
            self.height_job = self.root.after(self._motion_delay(animation["started"]),
                                              lambda: self._height_animation_frame(generation))

    def _cancel_height_animation(self, settle=True):
        self.height_animation_generation += 1
        if self.height_job:
            self.root.after_cancel(self.height_job)
            self.height_job = None
        animation, self.height_animation = self.height_animation, None
        self.card_content_height = None
        if settle and animation is not None:
            self._commit_height(animation["target"])
            self._finish_height_content(animation["target"])

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        self.fold.configure(text="+" if self.collapsed else "−")
        if not self.collapsed:
            self.details.pack(fill="both", expand=True)
        # The body stays at natural size until the outer crop reaches the header.
        if self.visible:
            self.card_content_height = self.expanded_height or self.root.winfo_height()
            self._set_card_geometry(self.width, self.root.winfo_height())
        self._cancel_task_animation(settle=True, resize=False)
        self._sync_icon_activity()
        self._resize(animate=self.visible)

    def start_drag(self, event):
        self._cancel_tip()
        # Rebase on the position the user can actually see, not an old offset
        # that may have accumulated beyond the edge of the chat window.
        self.offset = self._offset_for_position(self.root.winfo_x(), self.root.winfo_y())
        self.drag = (event.x_root, event.y_root, self.offset)
        try:
            self.root.grab_set()
        except tk.TclError:
            pass

    def move_drag(self, event):
        if self.drag:
            x, y, original = self.drag
            self.offset = (original[0] + event.x_root - x, original[1] + event.y_root - y)
            applied = self._position()
            self.offset = self._offset_for_position(*applied)
            # Throw away unapplied movement at an edge. Reversing the pointer
            # then moves the card immediately instead of crossing a dead zone.
            self.drag = (event.x_root, event.y_root, self.offset)

    def _title_tip(self):
        self._cancel_tip()
        text = self.state["title"]
        if self.state["thread_id"]:
            text += " · " + self.state["thread_id"][-8:]
        self.tip_job = self.root.after(650, lambda: self._show_tip(self.title, text))

    def _progress_tip(self):
        self._cancel_tip()
        done, total = completion(self.state["plan"])
        text = f"已完成 {done} / {total} 项 · {round(100 * done / max(1, total))}%"
        self.tip_job = self.root.after(650, lambda: self._show_tip(self.badge, text))

    def end_drag(self, event=None):
        if self.drag is None:
            return
        if event is not None:
            # Pointer movement may be coalesced before Tk handles a fast drag.
            # Always apply the release point so the final movement is retained.
            self.move_drag(event)
        applied = self.window_position or (self.root.winfo_x(), self.root.winfo_y())
        self.offset = self._offset_for_position(*applied)
        self.drag = None
        try:
            if self.root.grab_current() is self.root:
                self.root.grab_release()
        except tk.TclError:
            pass
        self.placement["offset_x"] = self.offset[0] / self.scale
        self.placement["offset_y"] = self.offset[1] / self.scale
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            target = self.data_dir / "placement.json"
            temporary = self.data_dir / "placement.overlay.tmp"
            temporary.write_text(json.dumps(self.placement, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            pass

    def _read_placement(self):
        try:
            path = self.data_dir / "placement.json"
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self.placement_signature:
                return
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                return
            for key in ("width", "top", "right", "offset_x", "offset_y"):
                if key in value and (not isinstance(value[key], (int, float)) or not -10000 < value[key] < 10000):
                    return
            self.placement = value
            self.width = round(max(220, min(420, value.get("width", 280))) * self.scale)
            if not self.drag:
                self.offset = (round(value.get("offset_x", 0) * self.scale), round(value.get("offset_y", 0) * self.scale))
            self.placement_signature = signature
            self._resize()
        except (OSError, ValueError, TypeError):
            pass

    def _offset_for_position(self, x, y):
        left, top, right, bottom = self.last_rect or (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        anchor_x = right - self.width - round(self.placement.get("right", 16) * self.scale)
        anchor_y = top + round(self.placement.get("top", 440) * self.scale)
        return x - anchor_x, y - anchor_y

    def _position(self, height=None):
        left, top, right, bottom = self.last_rect or (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        x = max(left + 8, min(right - self.width - 8, right - self.width - round(self.placement.get("right", 16) * self.scale) + self.offset[0]))
        height = self.root.winfo_height() if height is None else height
        y = max(top + 8, min(bottom - height - 8, top + round(self.placement.get("top", 440) * self.scale) + self.offset[1]))
        actual = (self.root.winfo_x(), self.root.winfo_y())
        if self.window_position != (x, y) or actual != (x, y):
            self.window_position = (x, y)
            self.host.move(self.root, x, y)
            self._queue_decoration()
        return x, y

    def poll(self, schedule=True):
        self._read_placement()
        if not self.demo and not (self.standalone or self.preview):
            self.set_state(self.active_state.read())
        elif not self.demo:
            path = self.data_dir / "current.json"
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if signature != self.signature:
                    self.set_state(json.loads(path.read_text(encoding="utf-8-sig")))
                    self.signature = signature
            except FileNotFoundError:
                self.set_state({})
                self.signature = None
            except (OSError, ValueError, TypeError):
                pass
        rect = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()) if self.standalone else self.host.foreground_rect(self.root)
        should_show = bool(self.state["plan"]) and rect is not None
        if should_show:
            if rect != self.last_rect:
                self.last_rect = rect
                self._resize()
            if not self.visible:
                for shadow, _ in self.shadows:
                    self.shadow_host.show(shadow)
                self.host.show(self.root)
                # Mapping can restore Tk's initial location after a move made
                # while withdrawn. Reapply placement on the mapped window.
                self.window_position = None
                self._position()
                self.shadow_geometry = None
                self._sync_shadows()
                self.visible = True
        elif self.visible:
            self._cancel_tip()
            if self.drag is not None:
                self.end_drag()
            self.root.withdraw()
            for shadow, _ in self.shadows:
                shadow.withdraw()
            self.visible = False
            self._cancel_task_animation(settle=True)
            self._cancel_height_animation(settle=True)
        self._sync_icon_activity()
        if schedule:
            self.root.after(500, self.poll)


def acquire_lock(data_dir):
    """Keep one viewer per data directory; OS releases this lock after crashes."""
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = (data_dir / "overlay.lock").open("a+b")
    if sys.platform == "win32":
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return None
    return handle


def main():
    enable_dpi_awareness()
    parser = argparse.ArgumentParser(description="Codex plan progress overlay")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--standalone", action="store_true", help="Show without a foreground Codex window")
    parser.add_argument("--demo", action="store_true", help="Show an in-memory example plan")
    parser.add_argument("--preview", action="store_true", help="Use a normal window for visual QA")
    parser.add_argument("--quit-after", type=float, default=0, help="Automatically close after N seconds")
    parser.add_argument("--renderer", choices=("wpf", "tk"), default="wpf" if sys.platform == "win32" else "tk",
                        help="Native composition renderer, or the legacy Tk viewer")
    parser.add_argument("--diagnostics", type=Path, help="Write native animation timing for a local preview")
    args = parser.parse_args()
    if args.renderer == "wpf":
        from overlay_wpf import run_native
        return run_native(args)
    lock = acquire_lock(args.data_dir)
    if lock is None:
        return 0
    observer = None
    try:
        if not (args.standalone or args.demo or args.preview):
            observer = start_observer(args.data_dir, Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
        host = WindowsHost(preview=args.preview)
        host.capture_initial_foreground()
        root = tk.Tk()
        app = PlanOverlay(root, args.data_dir, standalone=args.standalone, demo=args.demo, preview=args.preview, host=host)
        app.poll()
        if args.quit_after > 0:
            root.after(round(args.quit_after * 1000), root.destroy)
        root.mainloop()
    finally:
        stop_observer(observer)
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
