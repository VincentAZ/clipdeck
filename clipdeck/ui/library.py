"""The media library: items the user added, probed and thumbnailed in the background."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ..media import AUDIO_EXTS, VIDEO_EXTS, MediaInfo, ProbeError, fmt_duration, probe
from ..thumbs import thumbnail

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clipdeck-probe")

PROBING, READY, ERROR = "probing", "ready", "error"


class MediaItem(GObject.Object):
    __gtype_name__ = "ClipdeckMediaItem"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, path: str):
        super().__init__()
        self.path = os.path.abspath(path)
        self.info: MediaInfo | None = None
        self.state = PROBING
        self.error = ""
        self.texture: Gdk.Texture | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ready(self) -> bool:
        return self.state == READY and self.info is not None


def is_media_path(path: str) -> bool:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in VIDEO_EXTS or ext in AUDIO_EXTS


class Library:
    """Owns the list store and background probing."""

    def __init__(self, on_item_ready=None):
        self.store = Gio.ListStore.new(MediaItem)
        self.on_item_ready = on_item_ready

    def __len__(self):
        return self.store.get_n_items()

    def items(self) -> list[MediaItem]:
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def find(self, path: str) -> MediaItem | None:
        ap = os.path.abspath(path)
        for it in self.items():
            if it.path == ap:
                return it
        return None

    def expand(self, paths: list[str]) -> list[str]:
        """Files as given; folders contribute the media files directly inside them."""
        out = []
        for p in paths:
            if os.path.isdir(p):
                try:
                    names = sorted(os.listdir(p))
                except OSError:
                    continue
                out += [os.path.join(p, n) for n in names
                        if not n.startswith(".") and is_media_path(n)
                        and os.path.isfile(os.path.join(p, n))]
            elif os.path.isfile(p):
                out.append(p)
        return out

    def add_paths(self, paths: list[str]) -> list[MediaItem]:
        known = {it.path for it in self.items()}
        added = []
        for p in self.expand(paths):
            ap = os.path.abspath(p)
            if ap in known:
                continue
            known.add(ap)
            added.append(MediaItem(ap))
        if added:
            # one store change for the whole batch, not one per file
            n = self.store.get_n_items()
            self.store.splice(n, 0, added)
            for item in added:
                _pool.submit(self._probe, item)
        return added

    def remove(self, items: list[MediaItem]):
        for it in items:
            found, pos = self.store.find(it)
            if found:
                self.store.remove(pos)

    def refresh(self, item: MediaItem):
        item.state = PROBING
        item.emit("changed")
        _pool.submit(self._probe, item)

    # runs on a worker thread
    def _probe(self, item: MediaItem):
        try:
            info = probe(item.path)
            err = ""
        except ProbeError as e:
            info, err = None, str(e)
        except Exception as e:   # noqa: BLE001 - surface anything as a row error
            info, err = None, f"{type(e).__name__}: {e}"
        GLib.idle_add(self._probed, item, info, err)
        if info is not None:
            thumb = thumbnail(info)
            if thumb:
                GLib.idle_add(self._thumbed, item, thumb)

    def _probed(self, item: MediaItem, info, err):
        item.info = info
        item.error = err
        item.state = READY if info else ERROR
        item.emit("changed")
        if self.on_item_ready:
            self.on_item_ready(item)
        return False

    def _thumbed(self, item: MediaItem, path: str):
        try:
            item.texture = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            item.texture = None
        item.emit("changed")
        return False


def summary_line(info: MediaInfo) -> str:
    parts = []
    if info.has_video:
        w, h = info.display_size
        parts.append(f"{w}×{h}")
    parts.append(fmt_duration(info.duration))
    codecs = "/".join(c for c in (info.vcodec, info.acodec) if c)
    if codecs:
        parts.append(codecs)
    return " · ".join(parts)


class MediaRow(Gtk.Box):
    """One library entry: thumbnail, name, summary and issue badges."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("media-row")
        self.item: MediaItem | None = None
        self._handler = 0

        frame = Gtk.Overlay()
        frame.set_size_request(96, 54)
        frame.set_valign(Gtk.Align.CENTER)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.picture.set_size_request(96, 54)
        self.picture.add_css_class("thumb")
        frame.set_child(self.picture)
        self.placeholder = Gtk.Image(icon_name="video-x-generic-symbolic", pixel_size=24)
        self.placeholder.add_css_class("dim-label")
        frame.add_overlay(self.placeholder)
        self.spinner = Adw.Spinner()
        self.spinner.set_size_request(20, 20)
        self.spinner.set_halign(Gtk.Align.CENTER)
        self.spinner.set_valign(Gtk.Align.CENTER)
        frame.add_overlay(self.spinner)
        self.append(frame)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                      valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=3)  # Pango.EllipsizeMode.END
        self.title.add_css_class("heading")
        self.subtitle = Gtk.Label(xalign=0, ellipsize=3)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        self.badges = Adw.WrapBox(child_spacing=4, line_spacing=4)
        col.append(self.title)
        col.append(self.subtitle)
        col.append(self.badges)
        self.append(col)

    def bind(self, item: MediaItem):
        self.unbind()
        self.item = item
        self._handler = item.connect("changed", lambda *_: self.refresh())
        self.refresh()

    def unbind(self):
        if self.item is not None and self._handler:
            self.item.disconnect(self._handler)
        self.item = None
        self._handler = 0

    def refresh(self):
        it = self.item
        if it is None:
            return
        self.title.set_label(it.name)
        self.set_tooltip_text(it.path)
        self.spinner.set_visible(it.state == PROBING)
        self.picture.set_paintable(it.texture)
        self.placeholder.set_visible(it.texture is None and it.state != PROBING)
        audio_only = it.info is not None and not it.info.has_video
        self.placeholder.set_from_icon_name("audio-x-generic-symbolic" if audio_only
                                            else "video-x-generic-symbolic")
        child = self.badges.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.badges.remove(child)
            child = nxt
        if it.state == PROBING:
            self.subtitle.set_label("Reading…")
        elif it.state == ERROR:
            self.subtitle.set_label(it.error or "Unreadable")
            self._badge("Unreadable", "error")
        else:
            self.subtitle.set_label(summary_line(it.info))
            issues = it.info.issues()
            for iss in issues[:3]:
                self._badge(iss.title, "warning")
            if len(issues) > 3:
                self._badge(f"+{len(issues) - 3}", "warning")
            if not issues:
                self._badge("Ready to edit", "success")

    def _badge(self, text: str, kind: str):
        lab = Gtk.Label(label=text)
        lab.add_css_class("badge")
        lab.add_css_class(kind)
        self.badges.append(lab)
