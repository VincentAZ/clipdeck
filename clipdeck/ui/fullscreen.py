"""Full-screen playback: one clip fills the screen and Left/Right step through every
playable clip in the library."""

from __future__ import annotations

from gi.repository import Adw, Gdk, GLib, Gtk

from .library import MediaItem
from .widgets import Player

HIDE_AFTER_MS = 2500      # the toolbar and the pointer hide after this much stillness


class FullscreenViewer(Adw.Window):
    """A black window that fills the screen with the Player.

    `clips` returns the playable library items in library order each time it is asked,
    so clips added or replaced while the viewer is open are picked up. `on_close` gets
    the clip that was showing when the viewer closed."""

    def __init__(self, win, clips, item: MediaItem, position: float = 0.0, on_close=None,
                 on_install=None):
        super().__init__(transient_for=win, title=item.name, default_width=960,
                         default_height=540)
        self.add_css_class("viewer")
        self.clips = clips
        self.current: MediaItem | None = None
        self.on_close = on_close
        self._hide_id = 0
        self._seek_to = 0.0
        self._prepared = None       # (stream, handler id) while waiting to seek

        overlay = Gtk.Overlay()
        self.player = Player(height=200)
        self.player.add_css_class("fullscreen")
        self.player.video.set_autoplay(True)
        self.player.on_install = on_install
        overlay.set_child(self.player)

        self.bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                                valign=Gtk.Align.START, reveal_child=True)
        box = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=12,
                      margin_bottom=12)
        box.add_css_class("toolbar")
        box.add_css_class("osd")
        box.add_css_class("viewer-bar")
        self.prev_btn = self._button("go-previous-symbolic", "Previous clip (←)",
                                     lambda *_: self.step(-1))
        box.append(self.prev_btn)
        self.title_label = Gtk.Label(hexpand=True, ellipsize=3, xalign=0.5)  # END
        self.title_label.add_css_class("heading")
        box.append(self.title_label)
        self.next_btn = self._button("go-next-symbolic", "Next clip (→)",
                                     lambda *_: self.step(1))
        box.append(self.next_btn)
        box.append(self._button("view-restore-symbolic", "Exit full screen (Esc)",
                                lambda *_: self.close()))
        self.bar.set_child(box)
        overlay.add_overlay(self.bar)
        self.set_content(overlay)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda *_: self.show_bar())
        overlay.add_controller(motion)
        self._bar_motion = Gtk.EventControllerMotion()
        box.add_controller(self._bar_motion)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("close-request", self._on_close_request)

        self.show(item, position)
        self.fullscreen()
        self.present()

    @staticmethod
    def _button(icon: str, tip: str, cb) -> Gtk.Button:
        b = Gtk.Button(icon_name=icon, tooltip_text=tip)
        b.add_css_class("flat")
        b.add_css_class("circular")
        b.update_property([Gtk.AccessibleProperty.LABEL], [tip.split(" (")[0]])
        b.connect("clicked", cb)
        return b

    # ------------------------------------------------------------------ playback
    def show(self, item: MediaItem, position: float = 0.0):
        """Play `item`, from `position` seconds in."""
        self.current = item
        self._seek_to = position
        self.player.load(item.path, item.info)
        self._arm_seek()
        clips = self.clips()
        n = len(clips)
        i = clips.index(item) + 1 if item in clips else 0
        self.title_label.set_label(f"{item.name}  ·  {i} of {n}" if n > 1 else item.name)
        self.set_title(item.name)
        self.prev_btn.set_sensitive(n > 1)
        self.next_btn.set_sensitive(n > 1)
        self.show_bar()

    def _arm_seek(self):
        """Seek to the hand-over position once the stream knows its duration."""
        if self._prepared is not None:
            stream, hid = self._prepared
            stream.disconnect(hid)
            self._prepared = None
        stream = self.player.video.get_media_stream()
        if stream is None or self._seek_to < 0.05:
            return
        if stream.is_prepared():
            self.player.seek(self._seek_to)
            return
        self._prepared = (stream, stream.connect("notify::prepared", self._on_prepared))

    def _on_prepared(self, stream, _pspec):
        if not stream.is_prepared():
            return
        if self._prepared is not None and self._prepared[0] is stream:
            stream.disconnect(self._prepared[1])
            self._prepared = None
        if stream is self.player.video.get_media_stream():
            self.player.seek(self._seek_to)

    def step(self, delta: int):
        """Move `delta` clips through the library, wrapping around at the ends."""
        clips = self.clips()
        if not clips:
            self.close()
            return
        if self.current in clips:
            i = clips.index(self.current)
        else:
            i = -1 if delta > 0 else 0   # the clip was replaced: carry on from an end
        self.show(clips[(i + delta) % len(clips)])

    def toggle_play(self):
        stream = self.player.video.get_media_stream()
        if stream is None:
            return
        if stream.get_playing():
            stream.pause()
        else:
            stream.play()

    # ------------------------------------------------------------------ chrome
    def show_bar(self):
        self.bar.set_reveal_child(True)
        self.set_cursor_from_name("default")
        if self._hide_id:
            GLib.source_remove(self._hide_id)
        self._hide_id = GLib.timeout_add(HIDE_AFTER_MS, self._hide_bar)

    def _hide_bar(self):
        if self._bar_motion.contains_pointer():
            return True          # hovering the toolbar: keep it
        self._hide_id = 0
        self.bar.set_reveal_child(False)
        self.set_cursor_from_name("none")
        return False

    def _key(self, _ctrl, keyval, _code, _state):
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down):
            self.step(1)
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up):
            self.step(-1)
        elif keyval == Gdk.KEY_Home:
            clips = self.clips()
            if clips:
                self.show(clips[0])
        elif keyval == Gdk.KEY_End:
            clips = self.clips()
            if clips:
                self.show(clips[-1])
        elif keyval == Gdk.KEY_space:
            self.toggle_play()
        elif keyval in (Gdk.KEY_Escape, Gdk.KEY_F11):
            self.close()
        else:
            return False
        return True

    def _on_close_request(self, *_):
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = 0
        if self._prepared is not None:
            stream, hid = self._prepared
            stream.disconnect(hid)
            self._prepared = None
        self.player.load(None)
        if self.on_close is not None:
            self.on_close(self.current)
        return False
