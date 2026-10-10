"""Interactive crop overlay in upright source-pixel coordinates."""
from gi.repository import Gdk, Gtk


class CropOverlay(Gtk.DrawingArea):
    def __init__(self, changed):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Video crop selection"])
        self.changed = changed
        self.source = (0, 0)
        self.crop = (0, 0, 0, 0)
        self.target = None
        self.set_tooltip_text("Drag to draw a crop; drag inside to move it or corners to resize. "
                              "Arrow keys move the crop; Shift moves ten pixels.")
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._drag)
        self.add_controller(drag)
        keys = Gtk.EventControllerKey.new()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    def frame(self):
        w, h = self.source
        scale = min(self.get_width() / max(1, w), self.get_height() / max(1, h))
        return ((self.get_width() - w * scale) / 2,
                (self.get_height() - h * scale) / 2, max(scale, 0.0001))

    def _point(self, x, y):
        ox, oy, scale = self.frame()
        w, h = self.source
        return max(0, min(w, (x - ox) / scale)), max(0, min(h, (y - oy) / scale))

    def _begin(self, gesture, x, y):
        self.target = None
        sw, sh = self.source
        ox, oy, scale = self.frame()
        if sw < 2 or sh < 2 or not (ox <= x <= ox + sw * scale and
                                    oy <= y <= oy + sh * scale):
            return
        self.grab_focus()
        self.origin = (x, y)
        self.anchor = self._point(x, y)
        self.initial = self.crop
        w, h, cx, cy = self.crop
        for name, px, py in (("nw", cx, cy), ("ne", cx + w, cy),
                             ("sw", cx, cy + h), ("se", cx + w, cy + h)):
            if abs(x - (ox + px * scale)) <= 12 and abs(y - (oy + py * scale)) <= 12:
                self.target = name
                return
        self.target = "move" if cx < self.anchor[0] < cx + w and cy < self.anchor[1] < cy + h else "draw"

    def _drag(self, gesture, dx, dy):
        if self.target is None:
            return
        w, h, x, y = self.initial
        px, py = self._point(self.origin[0] + dx, self.origin[1] + dy)
        if self.target == "move":
            self._publish(w, h, x + px - self.anchor[0], y + py - self.anchor[1])
            return
        if self.target == "draw":
            ax, ay = self.anchor
        else:
            ax = x + w if "w" in self.target else x
            ay = y + h if "n" in self.target else y
        self._publish(abs(px - ax), abs(py - ay), min(px, ax), min(py, ay))

    def _publish(self, w, h, x, y):
        sw, sh = self.source
        # Even dimensions work with the app's 4:2:0 output codecs.
        w = max(2, min(sw - sw % 2, round(w / 2) * 2))
        h = max(2, min(sh - sh % 2, round(h / 2) * 2))
        x, y = max(0, min(sw - w, round(x))), max(0, min(sh - h, round(y)))
        self.crop = (w, h, x, y)
        self.changed(w, h, x, y)
        self.queue_draw()

    def _key(self, controller, key, code, state):
        moves = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0),
                 Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}
        if key not in moves or min(self.source) < 2:
            return False
        dx, dy = moves[key]
        step = 10 if state & Gdk.ModifierType.SHIFT_MASK else 1
        w, h, x, y = self.crop
        self._publish(w, h, x + dx * step, y + dy * step)
        return True

    def _draw(self, area, cr, width, height):
        sw, sh = self.source
        if min(sw, sh) < 2:
            return
        ox, oy, scale = self.frame()
        w, h, x, y = self.crop
        if w <= 0 or h <= 0 or x + w > sw or y + h > sh:
            return
        x, y, w, h = ox + x * scale, oy + y * scale, w * scale, h * scale
        cr.set_source_rgba(0, 0, 0, 0.55)
        for rx, ry, rw, rh in ((ox, oy, sw * scale, y - oy),
                              (ox, y + h, sw * scale, oy + sh * scale - y - h),
                              (ox, y, x - ox, h), (x + w, y, ox + sw * scale - x - w, h)):
            cr.rectangle(rx, ry, rw, rh)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.45)
        cr.set_line_width(1)
        for f in (1 / 3, 2 / 3):
            cr.move_to(x + w * f, y)
            cr.line_to(x + w * f, y + h)
            cr.move_to(x, y + h * f)
            cr.line_to(x + w, y + h * f)
        cr.stroke()
        cr.set_source_rgb(0.38, 0.70, 0.30)
        cr.set_line_width(2)
        cr.rectangle(x, y, w, h)
        cr.stroke()
        for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
            cr.rectangle(px - 5, py - 5, 10, 10)
        cr.fill()
