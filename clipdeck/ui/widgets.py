"""Small reusable widgets."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from .. import decoders


class KeyCombo(Adw.ComboRow):
    """ComboRow whose entries are (key, label) pairs; read and write by key."""

    def __init__(self, title: str, entries: list[tuple[str, str]], subtitle: str = ""):
        super().__init__(title=title)
        if subtitle:
            self.set_subtitle(subtitle)
        self.keys: list[str] = []
        self.set_entries(entries)

    def set_entries(self, entries: list[tuple[str, str]], keep: bool = True):
        old = self.get_key() if keep and self.keys else None
        self.keys = [k for k, _ in entries]
        self.set_model(Gtk.StringList.new([lab for _, lab in entries]))
        if old in self.keys:
            self.set_selected(self.keys.index(old))
        elif self.keys:
            self.set_selected(0)

    def get_key(self) -> str:
        i = self.get_selected()
        if 0 <= i < len(self.keys):
            return self.keys[i]
        return self.keys[0] if self.keys else ""

    def set_key(self, key) -> bool:
        key = str(key)
        if key in self.keys:
            self.set_selected(self.keys.index(key))
            return True
        return False


class Player(Gtk.Box):
    """Gtk.Video wrapper that knows its file and current position.

    When the file uses a codec GStreamer cannot decode, or playback fails for any other
    reason, the black box is replaced by a notice that says what is wrong and what to
    install, with the command one click away."""

    def __init__(self, height: int = 280):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("player")
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.set_size_request(-1, height)
        self.append(self.stack)
        self.video = Gtk.Video(autoplay=False, loop=False)
        self.video.set_vexpand(True)
        self.stack.add_named(self.video, "video")

        notice = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                         margin_start=18, margin_end=18, margin_top=12, margin_bottom=12)
        notice.add_css_class("player-notice")
        self.notice_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.notice_title.add_css_class("heading")
        notice.append(self.notice_title)
        self.notice_body = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                     selectable=True)
        self.notice_body.add_css_class("caption")
        notice.append(self.notice_body)
        self.fix_btn = Gtk.Button(label="Install decoders…", halign=Gtk.Align.CENTER,
                                  margin_top=4)
        self.fix_btn.add_css_class("pill")
        self.fix_btn.add_css_class("suggested-action")
        self.fix_btn.connect("clicked", self._fix)
        notice.append(self.fix_btn)
        self.stack.add_named(notice, "notice")

        self.path: str | None = None
        self.command: str | None = None
        # set by the page: opens the guided installer (see ui/deps.py)
        self.on_install = None
        self._stream = None
        self._error_handler = None

    def load(self, path: str | None, info=None):
        if path == self.path:
            return
        self.path = path
        self._watch(None)
        stream = self.video.get_media_stream()
        if stream is not None:
            try:
                stream.pause()
            except Exception:  # noqa: BLE001 - stream may already be gone
                pass
        if not path:
            self.video.set_media_stream(None)
            self.stack.set_visible_child_name("video")
            return
        missing = decoders.missing_for(info) if info is not None else []
        if missing:
            # don't even hand the file to GStreamer: it would fail with a bare error icon
            self.video.set_media_stream(None)
            self._show_notice(
                f"Preview can't play {decoders.describe(missing)}",
                "The video converts fine, but previews need a GStreamer decoder this "
                "system does not have. Clipdeck can install it for you.",
                decoders.INSTALL_COMMAND)
            return
        self.stack.set_visible_child_name("video")
        self.video.set_file(Gio.File.new_for_path(path))
        self._watch(self.video.get_media_stream())

    def _watch(self, stream):
        """Turn a playback error into a readable notice instead of the ⊗ icon."""
        if self._stream is not None and self._error_handler is not None:
            self._stream.disconnect(self._error_handler)
        self._stream, self._error_handler = stream, None
        if stream is not None:
            self._error_handler = stream.connect("notify::error", self._on_stream_error)
            if stream.get_error() is not None:
                self._on_stream_error(stream, None)

    def _on_stream_error(self, stream, _pspec):
        if stream is not self.video.get_media_stream():
            return
        err = stream.get_error()
        if err is None:
            return
        text = err.message or "unknown error"
        decoder_problem = any(w in text.lower() for w in ("plugin", "decoder", "codec",
                                                           "no suitable", "not supported"))
        hint = ("This usually means a GStreamer decoder is missing. Clipdeck can install "
                "the decoders for you." if decoder_problem
                else "Processing with ffmpeg is unaffected; only the preview failed.")
        self._show_notice("Preview can't play this file", f"{text}\n{hint}",
                          decoders.INSTALL_COMMAND if decoder_problem else None)

    def _show_notice(self, title: str, body: str, command: str | None):
        self.command = command
        self.notice_title.set_label(title)
        self.notice_body.set_label(body)
        self.fix_btn.set_visible(command is not None)
        self.fix_btn.set_label("Install decoders…" if self.on_install else "Copy install command")
        self.stack.set_visible_child_name("notice")

    def _fix(self, *_):
        if self.on_install is not None:
            self.on_install()
        elif self.command:
            self.get_clipboard().set(self.command)
            self.fix_btn.set_label("Copied")
            GLib.timeout_add(1500, lambda: self.fix_btn.set_label("Copy install command")
                             or False)

    def reload(self):
        """Open the file again (it was replaced on disk under the same name)."""
        path, self.path = self.path, None
        self._watch(None)
        self.video.set_media_stream(None)
        self.load(path)   # re-probed later; a playback error still shows a notice

    def pause(self):
        stream = self.video.get_media_stream()
        if stream is not None and stream.get_playing():
            stream.pause()

    def position(self) -> float:
        stream = self.video.get_media_stream()
        if stream is None:
            return 0.0
        return stream.get_timestamp() / 1_000_000

    def seek(self, seconds: float):
        stream = self.video.get_media_stream()
        if stream is not None and stream.is_seekable():
            stream.seek(int(max(0.0, seconds) * 1_000_000))


def plain_row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """ActionRow showing text literally. use-markup must be off before any text is set,
    or names like “Tom & Jerry <1>.mp4” are parsed as markup and vanish."""
    row = Adw.ActionRow()
    row.set_use_markup(False)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def button_row(label: str, style: str = "suggested-action") -> tuple[Gtk.Box, Gtk.Button]:
    """A centered pill button in its own box (for page footers)."""
    box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=12)
    btn = Gtk.Button(label=label)
    btn.add_css_class("pill")
    if style:
        btn.add_css_class(style)
    box.append(btn)
    return box, btn


def clear_box(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def clear_group(group: Adw.PreferencesGroup, rows: list):
    for r in rows:
        group.remove(r)
    rows.clear()


def fmt_time_entry(sec: float) -> str:
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    if h:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"


def idle(fn, *args):
    def run():
        fn(*args)
        return False
    GLib.idle_add(run)
