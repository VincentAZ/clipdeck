"""Application entry point."""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .media import have_tools  # noqa: E402
from .settings import Settings  # noqa: E402

CSS = """
.thumb { border-radius: 6px; background-color: alpha(currentColor, 0.08); }
.media-row { padding: 6px 2px; }
.badge {
  font-size: 0.75em; font-weight: 700; padding: 1px 7px; border-radius: 999px;
}
.badge.warning { background-color: color-mix(in srgb, var(--warning-bg-color) 25%, transparent);
                 color: var(--warning-fg-color, var(--warning-color)); }
.badge.success { background-color: color-mix(in srgb, var(--success-bg-color) 22%, transparent);
                 color: var(--success-color); }
.badge.error   { background-color: color-mix(in srgb, var(--error-bg-color) 25%, transparent);
                 color: var(--error-color); }
.badge.accent  { background-color: color-mix(in srgb, var(--accent-bg-color) 22%, transparent);
                 color: var(--accent-color); }
.plan-card { padding: 14px; }
.player video, .player { border-radius: 10px; }
.player { background-color: black; }
"""


class ClipdeckApp(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("CLIPDECK_NON_UNIQUE"):   # tests run beside a real instance
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.settings = Settings.load()
        self.win = None
        GLib.set_application_name("Clipdeck")
        for name, cb, accels in (
                ("preferences", lambda *_: self.win and self.win.show_preferences(), ["<Ctrl>comma"]),
                ("about", lambda *_: self._about(), []),
                ("quit", lambda *_: self.win.close() if self.win else self.quit(), ["<Ctrl>q"])):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.open", ["<Ctrl>o"])
        self.set_accels_for_action("win.remove-selected", ["Delete"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _window(self):
        if self.win is None:
            from .ui.window import MainWindow
            self.win = MainWindow(self, self.settings)
            self.win.connect("destroy", lambda *_: setattr(self, "win", None))
            if not have_tools():
                GLib.idle_add(self._missing_tools)
        return self.win

    def do_activate(self):
        self._window().present()

    def do_open(self, files, n_files, hint):
        win = self._window()
        paths = [f.get_path() for f in files if f.get_path()]
        win.present()
        if paths:
            win.add_paths(paths, select=True)

    def _missing_tools(self):
        dlg = Adw.AlertDialog(heading="ffmpeg is not installed",
                              body="Clipdeck uses ffmpeg and ffprobe for everything. Install "
                                   "them with:\n\nsudo apt install ffmpeg")
        dlg.add_response("ok", "OK")
        dlg.present(self.win)
        return False

    def _about(self):
        about = Adw.AboutDialog(
            application_name="Clipdeck", application_icon=APP_ID, version=__version__,
            developer_name="Vincent",
            comments="A command center for fixing, rotating and converting video. Every output "
                     "is verified before it is saved, and nothing is ever overwritten.",
            license_type=Gtk.License.MIT_X11)
        about.present(self.win)


def main(argv=None) -> int:
    app = ClipdeckApp()
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
