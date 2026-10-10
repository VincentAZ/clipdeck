"""A trim range and playhead on one timeline, with native precision controls."""
from gi.repository import Gdk, Gtk

from .widgets import fmt_time_entry


class TrimTimeline(Gtk.DrawingArea):
    def __init__(self, changed, seek):
        super().__init__(content_height=76, hexpand=True, focusable=True)
        self.duration = self.start = self.end = self.position = 0.0
        self.step = 1 / 30
        self.changed, self.seek = changed, seek
        self.target = "start"
        self.origin = 0
        self.set_tooltip_text("Drag the end handles to trim. Click inside to seek. "
                              "Arrow keys adjust the selected handle; Shift moves one second.")
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._drag)
        self.add_controller(drag)
        keys = Gtk.EventControllerKey.new()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    def _x(self, seconds):
        return 16 + (self.get_width() - 32) * seconds / max(self.duration, 0.001)

    def _time(self, x):
        return max(0, min(self.duration, (x - 16) / max(1, self.get_width() - 32) * self.duration))

    def _begin(self, gesture, x, y):
        self.grab_focus()
        self.origin = x
        distances = [(abs(x - self._x(self.start)), "start"),
                     (abs(x - self._x(self.end)), "end")]
        distance, target = min(distances)
        self.target = target if distance <= 16 and y >= 22 else "playhead"
        self._move(self._time(x))

    def _drag(self, gesture, dx, dy):
        self._move(self._time(self.origin + dx))

    def _move(self, value):
        if self.duration <= 0:
            return
        gap = min(self.step, self.duration)
        if self.target == "start":
            self.start = max(0, min(value, self.end - gap))
            self.changed(self.start, self.end)
            self.seek(self.start)
        elif self.target == "end":
            self.end = min(self.duration, max(value, self.start + gap))
            self.changed(self.start, self.end)
            self.seek(max(self.start, self.end - gap))
        else:
            self.seek(value)
        self.queue_draw()

    def _key(self, controller, key, code, state):
        if key not in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home, Gdk.KEY_End):
            return False
        value = getattr(self, self.target if self.target != "playhead" else "position")
        step = 1 if state & Gdk.ModifierType.SHIFT_MASK else self.step
        value = (0 if key == Gdk.KEY_Home else self.duration if key == Gdk.KEY_End
                 else value + (step if key == Gdk.KEY_Right else -step))
        self._move(max(0, min(self.duration, value)))
        return True

    def _draw(self, area, cr, width, height):
        def rect(x, y, w, h, color):
            cr.set_source_rgba(*color)
            cr.rectangle(x, y, w, h)
            cr.fill()
        fg = self.get_color()
        ink = (fg.red, fg.green, fg.blue, 0.8)
        accent = (0.38, 0.70, 0.30, 1)
        a, b = self._x(self.start), self._x(self.end)
        rect(16, 28, max(0, width - 32), 32, (*ink[:3], 0.12))
        rect(a, 28, max(0, b - a), 32, (0.38, 0.70, 0.30, 0.3))
        rect(a, 28, max(0, b - a), 2, accent)
        rect(a, 58, max(0, b - a), 2, accent)
        for x, name in ((a, "start"), (b, "end")):
            rect(x - 7, 25, 14, 38, accent)
            rect(x - 2, 35, 1, 17, (0, 0, 0, 0.7))
            rect(x + 2, 35, 1, 17, (0, 0, 0, 0.7))
            if self.has_focus() and self.target == name:
                cr.set_source_rgba(*ink)
                cr.rectangle(x - 9, 23, 18, 42)
                cr.stroke()
        p = self._x(max(0, min(self.duration, self.position)))
        rect(p - 1, 20, 2, 46, ink)
        cr.set_source_rgba(*ink)
        cr.set_font_size(11)
        for i in range(5):
            x = 16 + (width - 32) * i / 4
            rect(x, 18, 1, 5, ink)
            label = fmt_time_entry(self.duration * i / 4)
            label = label[:-2] if self.duration < 60 else label.rsplit(".", 1)[0]
            ext = cr.text_extents(label)
            cr.move_to(max(0, min(width - ext.width, x - ext.width / 2)), 12)
            cr.show_text(label)
