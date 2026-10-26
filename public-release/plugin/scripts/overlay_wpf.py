"""Controller for the fixed-size, composition-driven native Windows renderer.

The existing observer and ActiveThreadState remain the source of chat identity.
This process owns the existing viewer lock and the two child process lifetimes.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from overlay import (ActiveThreadState, WindowsHost, acquire_lock,
                     is_codex_process_path, normalize_state, start_observer,
                     stop_observer)


def script_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "scripts"
    return Path(__file__).resolve().parent


def renderer_version():
    try:
        value = json.loads((script_directory().parent / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8-sig"))
        version = value.get("version", "1.3.0")
        if isinstance(version, str) and version and len(version) <= 64 and all(character.isalnum() or character in ".-_" for character in version):
            return version
    except (OSError, ValueError, TypeError):
        pass
    return "1.3.0"


def framework_directory():
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for architecture in ("Framework64", "Framework"):
        candidate = windows / "Microsoft.NET" / architecture / "v4.0.30319"
        if (candidate / "csc.exe").is_file() and (candidate / "WPF" / "PresentationFramework.dll").is_file():
            return candidate
    raise RuntimeError("Windows .NET Framework with WPF is unavailable.")


def compile_renderer(data_dir):
    source = script_directory() / "overlay_wpf.cs"
    manifest = script_directory() / "overlay_wpf.manifest"
    output = Path(data_dir) / "runtime" / ("wpf-" + renderer_version()) / "PlanProgress.exe"
    if output.is_file() and output.stat().st_mtime_ns >= max(source.stat().st_mtime_ns, manifest.stat().st_mtime_ns):
        return output
    framework = framework_directory()
    references = [framework / "WPF" / name for name in
                  ("PresentationCore.dll", "PresentationFramework.dll", "WindowsBase.dll")]
    references.extend(framework / name for name in ("System.Xaml.dll", "System.Web.Extensions.dll"))
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(framework / "csc.exe"), "/nologo", "/target:winexe", "/optimize+",
               "/codepage:65001", "/utf8output", "/out:" + str(output),
               "/win32manifest:" + str(manifest)]
    command.extend("/reference:" + str(path) for path in references)
    command.append(str(source))
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace", timeout=60,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode or not output.is_file():
        raise RuntimeError("Native progress window could not be built:\n" + result.stdout[-6000:])
    return output


def read_placement(data_dir):
    """Keep the legacy logical-pixel placement and reject malformed coordinates."""
    defaults = {"width": 280, "top": 440, "right": 16, "offset_x": 0, "offset_y": 0}
    try:
        value = json.loads((Path(data_dir) / "placement.json").read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            return defaults
        for key in defaults:
            number = value.get(key, defaults[key])
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not -10000 < number < 10000:
                return defaults
        value = {**defaults, **value}
        value["width"] = max(220, min(420, value["width"]))
        return value
    except (OSError, ValueError, TypeError):
        return defaults


def build_view(state, host_rect, placement, now=None):
    now = time.time() if now is None else now
    try:
        state = normalize_state(state)
    except (ValueError, TypeError):
        state = normalize_state({})
    rect = None
    if isinstance(host_rect, (tuple, list)) and len(host_rect) == 4:
        if all(isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) for number in host_rect):
            if host_rect[2] > host_rect[0] and host_rect[3] > host_rect[1]:
                rect = list(host_rect)
    return {"schema": 1, "emitted_at": now, "visible": bool(state["plan"]) and rect is not None,
            "host_rect": rect or [], "state": state, "placement": placement}


class ViewPublisher:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "renderer-view.json"
        self.revision = 0
        self.previous = None

    def publish(self, state, host_rect, placement, now=None, terminate=False):
        view = build_view(state, host_rect, placement, now)
        meaning = {key: value for key, value in view.items() if key != "emitted_at"}
        meaning["terminate"] = bool(terminate)
        if meaning != self.previous:
            self.revision += 1
            # Values belong to this read, never to the producer's mutable dict.
            self.previous = json.loads(json.dumps(meaning, ensure_ascii=False))
        view["revision"] = self.revision
        view["terminate"] = bool(terminate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name("renderer-view." + str(os.getpid()) + ".tmp")
        temporary.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)
        return view


class HostLocator:
    """Observe foreground process identity only; never inspect other app content."""
    def __init__(self):
        self.host = WindowsHost(preview=True)
        self.last_host = None

    def desktop_rect(self):
        if not self.host.enabled:
            return None
        user = self.host.user
        left, top = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
        return left, top, left + user.GetSystemMetrics(78), top + user.GetSystemMetrics(79)

    def foreground_rect(self, viewer_pid):
        if not self.host.enabled:
            return None
        user = self.host.user
        foreground = user.GetForegroundWindow()
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
        if pid.value == viewer_pid:
            foreground = self.last_host
        elif foreground and is_codex_process_path(self.host.process_path(foreground)):
            self.last_host = foreground
        else:
            return None
        if not foreground or not user.IsWindowVisible(foreground) or user.IsIconic(foreground):
            return None
        rect = wintypes.RECT()
        if user.GetWindowRect(foreground, ctypes.byref(rect)):
            return rect.left, rect.top, rect.right, rect.bottom
        return None


def preview_state(data_dir, demo=False):
    if demo:
        return {"thread_id": "native-preview", "title": "原生动效预览", "run_state": "running", "plan": [
            {"step": "确认界面结构", "status": "completed"},
            {"step": "优化交互与绘制", "status": "in_progress", "children": [
                {"step": "保持文字和行距", "status": "completed"},
                {"step": "平滑展开与收起", "status": "in_progress"},
                {"step": "检查自由拖动", "status": "pending"}]},
            {"step": "验证后更新安装", "status": "pending"}]}
    try:
        return json.loads((Path(data_dir) / "current.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}


def run_native(args):
    directory = Path(args.data_dir)
    lock = acquire_lock(directory)
    if lock is None:
        return 0
    observer = viewer = None
    publisher = ViewPublisher(directory)
    try:
        executable = compile_renderer(directory)
        reader = ActiveThreadState(directory, observed_not_before=time.time())
        locator = HostLocator()
        standalone = args.standalone or args.demo or args.preview
        if not standalone:
            observer = start_observer(directory, Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
        publisher.publish({}, None, read_placement(directory))
        command = [str(executable), "--view-file", str(publisher.path), "--data-dir", str(directory),
                   "--parent-pid", str(os.getpid())]
        if args.preview:
            command.append("--preview")
        diagnostics = getattr(args, "diagnostics", None)
        if diagnostics:
            command.extend(["--diagnostics", str(diagnostics)])
        viewer = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + args.quit_after if args.quit_after > 0 else None
        next_publish = 0.0
        while viewer.poll() is None:
            clock = time.monotonic()
            if deadline is not None and clock >= deadline:
                break
            if clock >= next_publish:
                state = preview_state(directory, args.demo) if standalone else reader.read()
                rect = locator.desktop_rect() if standalone else locator.foreground_rect(viewer.pid)
                try:
                    publisher.publish(state, rect, read_placement(directory))
                except OSError:
                    # A brief sharing violation must not tear down the viewer.
                    # Its heartbeat guard still hides a persistently stale view.
                    pass
                next_publish = clock + 0.25
            time.sleep(0.05)
        return viewer.returncode or 0
    finally:
        try:
            publisher.publish({}, None, read_placement(directory), terminate=True)
        except OSError:
            pass
        if viewer is not None and viewer.poll() is None:
            try:
                viewer.wait(timeout=2)
            except subprocess.TimeoutExpired:
                viewer.terminate()
                viewer.wait(timeout=2)
        stop_observer(observer)
        lock.close()


if __name__ == "__main__":
    # The public launcher always goes through overlay.py for a shared CLI.
    raise SystemExit("Run overlay.py to start the progress window.")
