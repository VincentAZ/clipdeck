"""Preferences: where outputs go, verification depth, parallelism."""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, GLib, Gtk


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferences")
        self.win = win
        s = win.settings
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(page)

        g = Adw.PreferencesGroup(title="Output",
                                 description="Existing files are never overwritten: a new name "
                                             "like “clip (2).mp4” is used instead.")
        page.add(g)
        self.beside = Adw.SwitchRow(title="Save next to the original",
                                    subtitle="Off: save everything into one folder")
        self.beside.set_active(s.output_mode != "folder")
        g.add(self.beside)
        self.folder_row = Adw.ActionRow(title="Output folder")
        self.folder_row.set_subtitle(s.output_folder or "Not chosen")
        btn = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._choose())
        self.folder_row.add_suffix(btn)
        g.add(self.folder_row)
        self.beside.connect("notify::active", lambda *_: self._beside_changed())

        g = Adw.PreferencesGroup(title="Processing")
        page.add(g)
        self.thorough = Adw.SwitchRow(
            title="Thorough verification",
            subtitle="Decode every frame of each output and compare frame counts with the "
                     "source. Slower, but catches corrupt files.")
        self.thorough.set_active(s.thorough)
        self.thorough.connect("notify::active", lambda *_: self._save())
        g.add(self.thorough)
        self.workers = Adw.SpinRow.new_with_range(1, 4, 1)
        self.workers.set_title("Jobs at once")
        self.workers.set_subtitle("ffmpeg already uses every CPU core; more helps mostly with "
                                  "GPU or copy jobs")
        self.workers.set_value(s.workers)
        self.workers.connect("notify::value", lambda *_: self._save())
        g.add(self.workers)
        self._sync()

    def _sync(self):
        self.folder_row.set_sensitive(not self.beside.get_active())
        f = self.win.settings.output_folder
        if f and not os.path.isdir(f):
            self.folder_row.set_subtitle(f"{f} (missing: saving next to the originals)")

    def _beside_changed(self):
        # "one folder" without a folder would silently keep saving beside the originals
        if not self.beside.get_active() and not self.win.settings.output_folder:
            self._choose(revert=True)
            return
        self._save()

    def _choose(self, revert: bool = False):
        dlg = Gtk.FileDialog(title="Output Folder", modal=True)
        if self.win.settings.output_folder and os.path.isdir(self.win.settings.output_folder):
            dlg.set_initial_folder(Gio.File.new_for_path(self.win.settings.output_folder))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                f = None
            if not (f and f.get_path()):
                if revert:
                    self.beside.set_active(True)
                return
            if f and f.get_path():
                self.win.settings.output_folder = f.get_path()
                self.folder_row.set_subtitle(f.get_path())
                self.beside.set_active(False)
                self._save()
        dlg.select_folder(self.win, None, done)

    def _save(self):
        s = self.win.settings
        s.output_mode = "beside" if self.beside.get_active() or not s.output_folder else "folder"
        s.thorough = self.thorough.get_active()
        s.workers = int(self.workers.get_value())
        s.save()
        self.win.queue.set_workers(s.workers)
        self._sync()
        self.win._selection_changed()   # output names may have moved
