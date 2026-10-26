"""Small, deterministic status animations for a Tk task list.

``size`` is a logical 96-DPI size. The canvas scales it using Tk's DPI setting;
use the existing icon-slot size when embedding to preserve the row height.
Animation never changes the supplied task status. Hidden/unmapped icons and
icons disabled with set_active(False) do not keep an animation timer running.
"""
from __future__ import annotations

import math
import time
import tkinter as tk


class AnimatedStatusIcon(tk.Canvas):
    STATUSES = frozenset({"pending", "in_progress", "completed", "blocked"})
    FRAME_MS = 34  # At most approximately 30 frames per second.
    COMPLETION_SECONDS = 0.4

    def __init__(self, parent, status, size=22, bg="#FFFFFF", animate_completion=False):
        if status not in self.STATUSES:
            raise ValueError(f"Unsupported task status: {status!r}")
        if not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
            raise ValueError("size must be a positive number")
        scale = max(0.5, float(parent.tk.call("tk", "scaling")) / (96 / 72))
        self.logical_size = float(size)
        self.pixel_size = max(1, round(size * scale))
        super().__init__(parent, width=self.pixel_size, height=self.pixel_size, bg=bg,
                         borderwidth=0, highlightthickness=0, relief="flat", takefocus=False)
        self.status = status
        self._background = bg
        self._active = True
        self._mapped = False
        self._destroyed = False
        self._timer = None
        self._elapsed = 0.0
        self._last_tick = time.monotonic()
        self._completion_animating = status == "completed" and bool(animate_completion)
        self._disc = self.create_oval(0, 0, 1, 1, outline="")
        self._ring = self.create_oval(0, 0, 1, 1, fill="")
        self._arc = self.create_arc(0, 0, 1, 1, style="arc", extent=115)
        self._core = self.create_oval(0, 0, 1, 1, outline="")
        self._check = self.create_line(0, 0, 0, 0, capstyle="round", joinstyle="round")
        self._stem = self.create_line(0, 0, 0, 0, capstyle="round")
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Unmap>", self._on_unmap, add="+")
        self.bind("<Configure>", lambda _: self._draw(), add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw()

    def set_status(self, status, animate=True):
        """Set the real status; animate controls the completed entrance only.

        Reapplying the same status never restarts a completion or motion cycle.
        Ongoing/blocked movement runs only while active and mapped.
        """
        if status not in self.STATUSES:
            raise ValueError(f"Unsupported task status: {status!r}")
        if self._destroyed or status == self.status:
            return
        self._cancel_timer()
        self.status = status
        self._elapsed = 0.0
        self._last_tick = time.monotonic()
        self._completion_animating = status == "completed" and bool(animate) and self._active
        self._draw()
        self._ensure_timer()

    def set_background(self, color):
        """Match a row's background, including its brief completion highlight."""
        if self._destroyed:
            return
        self.configure(background=color)
        self._background = color
        self._draw()

    def set_active(self, active):
        """Pause animation when the card is hidden/collapsed; resume ongoing work."""
        if self._destroyed or bool(active) == self._active:
            return
        self._active = bool(active)
        if not self._active:
            self._cancel_timer()
            # A hidden completion is shown finished later, without replaying it.
            self._completion_animating = False
            self._draw()
        else:
            self._last_tick = time.monotonic()
            self._ensure_timer()

    def _on_map(self, event):
        if event.widget is self and not self._destroyed:
            self._mapped = True
            self._last_tick = time.monotonic()
            self._ensure_timer()

    def _on_unmap(self, event):
        if event.widget is self:
            self._mapped = False
            self._cancel_timer()
            self._completion_animating = False
            self._draw()

    def _has_animation(self):
        return self.status in ("in_progress", "blocked") or self._completion_animating

    def _ensure_timer(self):
        if not self._destroyed and self._active and self._mapped and self._has_animation() and self._timer is None:
            self._timer = self.after(self.FRAME_MS, self._tick)

    def _cancel_timer(self):
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None

    def _tick(self):
        self._timer = None
        if self._destroyed or not self._active or not self._mapped:
            return
        now = time.monotonic()
        self._elapsed += max(0.0, now - self._last_tick)
        self._last_tick = now
        if self._completion_animating and self._elapsed >= self.COMPLETION_SECONDS:
            self._completion_animating = False
        self._draw()
        self._ensure_timer()

    def _blend(self, foreground, amount):
        front = self.winfo_rgb(foreground)
        back = self.winfo_rgb(self._background)
        return "#" + "".join(f"{round((b + (f - b) * amount) / 257):02x}"
                              for f, b in zip(front, back))

    def _draw(self):
        if self._destroyed:
            return
        width = self.winfo_width() if self.winfo_width() > 1 else self.pixel_size
        height = self.winfo_height() if self.winfo_height() > 1 else self.pixel_size
        unit = min(width, height) / 22
        cx, cy = width / 2, height / 2
        self.itemconfigure("all", state="hidden")

        def circle(item, radius, **style):
            radius *= unit
            self.coords(item, cx - radius, cy - radius, cx + radius, cy + radius)
            self.itemconfigure(item, state="normal", **style)

        if self.status == "pending":
            circle(self._ring, 4.0, outline="#C6CBD2", width=1.15 * unit)
        elif self.status == "in_progress":
            breath = (1 - math.cos(self._elapsed * math.tau / 2.4)) / 2
            circle(self._ring, 6.3, outline=self._blend("#A5ABB5", 0.2), width=1.15 * unit)
            circle(self._arc, 6.3, outline="#858C97", width=1.5 * unit,
                   start=90 - (self._elapsed / 1.8 * 360) % 360, extent=112 + 12 * breath)
            circle(self._core, 1.25 + 0.22 * breath, fill=self._blend("#858C97", 0.34 + 0.13 * breath))
        elif self.status == "completed":
            progress = min(1.0, self._elapsed / self.COMPLETION_SECONDS) if self._completion_animating else 1.0
            if progress < 0.65:
                bounce = 0.9 + 0.125 * (1 - (1 - progress / 0.65) ** 3)
            else:
                bounce = 1.025 - 0.025 * ((progress - 0.65) / 0.35)
            circle(self._disc, 7.6 * bounce, fill=self._blend("#CFE9DA", min(0.55, progress * 2.8)))
            draw_progress = 1 - (1 - min(1.0, progress / 0.94)) ** 3
            points = [(-3.4, 0.0), (-1.0, 2.45), (4.05, -3.0)]
            lengths = [math.dist(points[i], points[i + 1]) for i in range(2)]
            remaining = sum(lengths) * draw_progress
            line = [cx + points[0][0] * unit * bounce, cy + points[0][1] * unit * bounce]
            for index, length in enumerate(lengths):
                fraction = min(1.0, remaining / length)
                x = points[index][0] + (points[index + 1][0] - points[index][0]) * fraction
                y = points[index][1] + (points[index + 1][1] - points[index][1]) * fraction
                line.extend((cx + x * unit * bounce, cy + y * unit * bounce))
                remaining -= length
                if remaining <= 0:
                    break
            if draw_progress > 0:
                self.coords(self._check, *line)
                self.itemconfigure(self._check, state="normal", fill="#3A9770", width=1.65 * unit)
        elif self.status == "blocked":
            breath = (1 - math.cos(self._elapsed * math.tau / 3.2)) / 2
            circle(self._disc, 7.1, fill=self._blend("#C79B58", 0.07 + 0.035 * breath))
            circle(self._ring, 6.0, outline=self._blend("#B88743", 0.48 + 0.12 * breath), width=1.15 * unit)
            self.coords(self._stem, cx, cy - 2.8 * unit, cx, cy + 0.5 * unit)
            self.itemconfigure(self._stem, state="normal", fill="#B88743", width=1.35 * unit)
            radius = 0.72 * unit
            self.coords(self._core, cx - radius, cy + 2.6 * unit - radius,
                        cx + radius, cy + 2.6 * unit + radius)
            self.itemconfigure(self._core, state="normal", fill="#B88743")

    def _on_destroy(self, event):
        if event.widget is self:
            self._cancel_timer()
            self._destroyed = True

    def destroy(self):
        if not self._destroyed:
            self._cancel_timer()
            self._destroyed = True
        super().destroy()
