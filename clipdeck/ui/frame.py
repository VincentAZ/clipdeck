"""Save the preview's current frame and describe the resulting image."""

import os
import time
from math import gcd

from gi.repository import GLib, Gtk

from .. import ops
from .widgets import fmt_time_entry


class FrameActions(Gtk.Box):
    def __init__(self, ctx, player):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.ctx, self.player = ctx, player
        self.info = None
        self.crop = None
        self._busy = False
        self.save = Gtk.Button(label="Save frame (PNG)", halign=Gtk.Align.START,
                               tooltip_text="Save the current preview frame, including any crop")
        self.save.connect("clicked", self._save)
        self.append(self.save)
        self.dimensions = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.dimensions.add_css_class("caption")
        self.append(self.dimensions)
        self.set_media(None)

    def set_media(self, info, crop=None):
        self.info, self.crop = info, crop
        valid = info is not None and info.has_video
        self.set_visible(valid)
        if valid:
            w, h = crop[:2] if crop else info.display_size
            divisor = gcd(w, h) if w > 0 and h > 0 else 1
            label = "Cropped image" if crop else "Image"
            self.dimensions.set_label(f"{label}: {w} × {h} px · {w // divisor}:{h // divisor}")
        else:
            self.dimensions.set_label("")
        self.save.set_sensitive(valid and not self._busy)

    def _save(self, *_):
        info, crop = self.info, self.crop
        stream = self.player.video.get_media_stream()
        if self._busy or info is None or self.player.path != info.path or stream is None:
            return
        self.player.pause()
        self._busy = True
        self.save.set_sensitive(False)
        deadline = time.monotonic() + 5

        def capture():
            current = self.player.video.get_media_stream()
            if current is not stream or self.player.path != info.path:
                self._finish()
                return False
            # Scrubbing seeks asynchronously: wait for the displayed frame's timestamp.
            if stream.is_seeking() or not stream.is_prepared():
                if time.monotonic() < deadline:
                    return True
                self.ctx.toast("The frame is still loading. Try saving again in a moment.")
                self._finish()
                return False
            at = self.player.position()
            try:
                plan = ops.build_snapshot(info, at, out_dir=self.ctx.out_dir,
                                          taken=self.ctx.taken(), crop=crop)
            except ops.PlanError as error:
                self.ctx.toast(str(error))
            else:
                self.ctx.submit(plan)
                self.ctx.toast(f"Saving frame at {fmt_time_entry(at)} → {os.path.basename(plan.output)}")
            self._finish()
            return False

        if capture():
            GLib.timeout_add(40, capture)

    def _finish(self):
        self._busy = False
        self.set_media(self.info, self.crop)
