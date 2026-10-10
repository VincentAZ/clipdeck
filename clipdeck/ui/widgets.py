"""Small reusable widgets."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk


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
    """Gtk.Video wrapper that knows its file and current position."""

    def __init__(self, height: int = 280):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("player")
        self.video = Gtk.Video(autoplay=False, loop=False)
        self.video.set_size_request(-1, height)
        self.video.set_vexpand(True)
        self.append(self.video)
        self.path: str | None = None

    def load(self, path: str | None):
        if path == self.path:
            return
        self.path = path
        stream = self.video.get_media_stream()
        if stream is not None:
            try:
                stream.pause()
            except Exception:  # noqa: BLE001 - stream may already be gone
                pass
        if path:
            self.video.set_file(Gio.File.new_for_path(path))
        else:
            self.video.set_media_stream(None)

    def reload(self):
        """Open the file again (it was replaced on disk under the same name)."""
        path, self.path = self.path, None
        self.video.set_media_stream(None)
        self.load(path)

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
