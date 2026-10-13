"""Tools: join clips, grab a frame, build a contact sheet, tag videos for Civitai."""

from __future__ import annotations

import os

from gi.repository import Adw, Gdk, GLib, Gtk

from .. import ops
from ..media import fmt_duration, parse_time
from ..ops import PlanError, Recipe
from .library import PROBING, MediaItem, is_media_path
from .widgets import KeyCombo, clear_group, plain_row


class ToolsPage(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.items: list[MediaItem] = []
        self.order: list[MediaItem] = []
        self.merge_rows: list = []
        self._forced = False
        self._lossless_key: tuple = ()
        self._lossless = False

        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)
        self._build_civitai(page)

        # ---------------- merge
        self.merge = Adw.PreferencesGroup(
            title="Join clips",
            description="Select two or more clips in the library, put them in order, join.")
        page.add(self.merge)
        self.merge_list = Adw.PreferencesGroup()
        page.add(self.merge_list)
        mopts = Adw.PreferencesGroup()
        page.add(mopts)
        self.m_reencode = Adw.SwitchRow(
            title="Re-encode",
            subtitle="Needed when clips differ in size, codec or frame rate; off joins losslessly")
        self.m_codec = KeyCombo("Codec", [("h264", "H.264"), ("hevc", "HEVC (H.265)"),
                                          ("av1", "AV1")])
        self.m_gpu = Adw.SwitchRow(title="Use GPU (NVENC)")
        self.m_gpu.set_active(ctx.settings.gpu)
        self.m_gpu.connect("notify::active", lambda *_: self._remember_gpu())
        self.m_res = KeyCombo("Size", [("0", "Same as first clip"), ("2160", "2160p"),
                                       ("1080", "1080p"), ("720", "720p")])
        for w in (self.m_reencode, self.m_codec, self.m_gpu, self.m_res):
            mopts.add(w)
        self.m_reencode.connect("notify::active", lambda *_: self._sync_merge())
        self.m_codec.connect("notify::selected", lambda *_: self._sync_merge())
        self.m_go = Adw.ButtonRow(title="Join clips")
        self.m_go.add_css_class("suggested-action")
        self.m_go.connect("activated", lambda *_: self._merge())
        mopts.add(self.m_go)

        # ---------------- snapshot
        g = Adw.PreferencesGroup(title="Save a frame",
                                 description="Full-resolution still from the first selected clip")
        page.add(g)
        self.s_time = Adw.EntryRow(title="Time (e.g. 0:03.5)")
        self.s_time.set_text("0:00")
        self.s_fmt = KeyCombo("Format", [("png", "PNG (lossless)"), ("jpg", "JPEG")])
        self.s_go = Adw.ButtonRow(title="Save frame")
        self.s_go.connect("activated", lambda *_: self._snapshot())
        for w in (self.s_time, self.s_fmt, self.s_go):
            g.add(w)

        # ---------------- contact sheet
        g = Adw.PreferencesGroup(title="Contact sheet",
                                 description="A grid of evenly spaced thumbnails, one JPEG per clip")
        page.add(g)
        self.c_cols = Adw.SpinRow.new_with_range(1, 12, 1)
        self.c_cols.set_title("Columns")
        self.c_cols.set_value(4)
        self.c_rows = Adw.SpinRow.new_with_range(1, 12, 1)
        self.c_rows.set_title("Rows")
        self.c_rows.set_value(4)
        self.c_w = Adw.SpinRow.new_with_range(80, 1920, 10)
        self.c_w.set_title("Thumbnail width (px)")
        self.c_w.set_value(320)
        self.c_go = Adw.ButtonRow(title="Build contact sheet")
        self.c_go.connect("activated", lambda *_: self._sheet())
        for w in (self.c_cols, self.c_rows, self.c_w, self.c_go):
            g.add(w)
        self.set_items([])

    # ---------------- civitai
    def _build_civitai(self, page):
        st = self.ctx.settings
        self._tag_waiting: list[MediaItem] = []   # dropped, queued once read
        g = Adw.PreferencesGroup(
            title="Tag for Civitai",
            description="Writes which model made the video, the way Civitai reads it (some "
                        "uploads and competitions require it). Every other tag goes, a "
                        "ComfyUI prompt included. Video and sound are copied, not re-encoded.")
        page.add(g)
        drop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       height_request=110, valign=Gtk.Align.CENTER, margin_top=12)
        drop.add_css_class("card")
        drop.append(Gtk.Image(icon_name="folder-videos-symbolic", pixel_size=32,
                              margin_top=18, css_classes=["dim-label"]))
        self.t_drop_label = Gtk.Label(label="Drop videos here to tag them",
                                      margin_bottom=18, wrap=True,
                                      justify=Gtk.Justification.CENTER)
        drop.append(self.t_drop_label)
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_tag_drop)
        target.connect("enter", lambda *_: (drop.add_css_class("accent"), Gdk.DragAction.COPY)[1])
        target.connect("leave", lambda *_: drop.remove_css_class("accent"))
        drop.add_controller(target)
        self.t_drop = drop
        self.t_model = KeyCombo("Model", [(k, lab) for k, lab, _a, _n in ops.CIVITAI_MODELS] +
                                [("other", "Another model…")])
        self.t_model.set_key(st.civitai_model)
        self.t_air = Adw.EntryRow(title="Its AIR, from the model's Civitai page (urn:air:…)")
        self.t_air.set_text(st.civitai_air)
        self.t_replace = Adw.SwitchRow(
            title="Replace originals",
            subtitle="The tagged copy takes the original's name; the original goes to the Trash")
        self.t_replace.set_active(st.civitai_replace)
        self.t_go = Adw.ButtonRow(title="Tag selected clips")
        self.t_go.add_css_class("suggested-action")
        self.t_go.connect("activated", lambda *_: self._tag(self._tag_clips()))
        # a group shows its rows first, then other widgets: model, then the drop box
        g.add(self.t_model)
        g.add(self.t_air)
        g.add(drop)
        opts = Adw.PreferencesGroup()
        page.add(opts)
        opts.add(self.t_replace)
        opts.add(self.t_go)
        self.t_model.connect("notify::selected", lambda *_: self._sync_tag(save=True))
        self.t_air.connect("changed", lambda *_: self._sync_tag(save=True))
        self.t_replace.connect("notify::active", lambda *_: self._sync_tag(save=True))
        self._sync_tag()

    def _tag_clips(self):
        return [i for i in self.items if i.ready and i.info.has_video]

    def _sync_tag(self, save=False):
        other = self.t_model.get_key() == "other"
        self.t_air.set_visible(other)
        st = self.ctx.settings
        if save:
            st.civitai_model = self.t_model.get_key()
            st.civitai_air = self.t_air.get_text().strip()
            st.civitai_replace = self.t_replace.get_active()
            st.save()
        n = len(self._tag_clips())
        self.t_go.set_sensitive(n > 0)
        self.t_go.set_title(f"Tag {n} selected clip{'s' if n != 1 else ''}" if n
                            else "Select clips in the library, or drop them above")

    def _tag_tags(self):
        try:
            return ops.civitai_tags(self.t_model.get_key(), self.t_air.get_text())
        except PlanError as e:
            self.t_air.add_css_class("error")
            self.ctx.toast(str(e))
            return None

    def _tag(self, clips):
        tags = self._tag_tags()
        if tags is None or not clips:
            return
        self.t_air.remove_css_class("error")
        taken = self.ctx.taken()
        n = 0
        for it in clips:
            try:
                plan = ops.build_civitai_tag(it.info, tags, self.ctx.out_dir, taken,
                                             replace_original=self.t_replace.get_active())
            except PlanError as e:
                self.ctx.toast(str(e))
                continue
            taken.add(plan.output)
            self.ctx.submit(plan)
            n += 1
        if n:
            self.ctx.toast(f"Tagging {n} clip{'s' if n != 1 else ''} for Civitai")
            self.ctx.show_queue()

    def _on_tag_drop(self, _target, value, _x, _y):
        self.t_drop.remove_css_class("accent")
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        files = [p for p in self.ctx.library.expand(
            [p for p in paths if os.path.isdir(p) or is_media_path(p)])]
        if not files:
            self.ctx.toast("No videos in what was dropped")
            return True        # handled here: the window must not add them as well
        if self._tag_tags() is None:
            return True
        self.ctx.add_paths(files, select=True)
        items = [self.ctx.library.find(p) for p in files]
        self._tag_waiting += [i for i in items if i and i not in self._tag_waiting]
        if self._tag_waiting:
            GLib.timeout_add(200, self._drain_drops)
        return True

    def _drain_drops(self):
        """Queue dropped clips as soon as they have been read (probing runs in the
        background); the ones that can't be read or have no video are reported."""
        ready = [i for i in self._tag_waiting if i.state != PROBING]
        self._tag_waiting = [i for i in self._tag_waiting if i.state == PROBING]
        good = [i for i in ready if i.ready and i.info.has_video]
        bad = [i for i in ready if i not in good]
        if good:
            self._tag(good)
        for i in bad:
            self.ctx.toast(f"{i.name}: {i.error or 'no video'}")
        return bool(self._tag_waiting)       # keep polling while some are being read

    # ------------------------------------------------------------------ selection
    def set_items(self, items: list[MediaItem]):
        self.items = items
        # keep the user's ordering for clips that stay selected, append new ones
        keep = [i for i in self.order if i in items]
        self.order = keep + [i for i in items if i not in keep]
        self._sync_merge()
        videos = [i for i in items if i.ready and i.info.has_video]
        self.s_go.set_sensitive(bool(videos))
        self.c_go.set_sensitive(bool(videos))
        self.c_go.set_title(f"Build {len(videos)} contact sheets" if len(videos) > 1
                            else "Build contact sheet")
        if hasattr(self, "t_go"):
            self._sync_tag()

    def _ready_order(self):
        return [i for i in self.order if i.ready and i.info.has_video]

    def _sync_merge(self):
        clear_group(self.merge_list, self.merge_rows)
        clips = self._ready_order()
        for n, it in enumerate(clips):
            row = plain_row(f"{n + 1}. {it.name}",
                            f"{it.info.display_size[0]}×{it.info.display_size[1]} · "
                            f"{fmt_duration(it.info.duration)} · {it.info.vcodec}")
            row.set_title_lines(1)
            up = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Move up", sensitive=n > 0)
            down = Gtk.Button(icon_name="go-down-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Move down", sensitive=n < len(clips) - 1)
            for b, what in ((up, "up"), (down, "down")):
                b.add_css_class("flat")
                b.update_property([Gtk.AccessibleProperty.LABEL], [f"Move {it.name} {what}"])
            up.connect("clicked", lambda _b, i=it: self._move(i, -1))
            down.connect("clicked", lambda _b, i=it: self._move(i, 1))
            row.add_suffix(up)
            row.add_suffix(down)
            self.merge_list.add(row)
            self.merge_rows.append(row)
        self.merge_list.set_visible(bool(clips))
        key = tuple(i.path for i in clips)
        if key != self._lossless_key:      # probing for pre-roll reads files: cache it
            self._lossless_key = key
            self._lossless = ops.can_concat_losslessly([i.info for i in clips])
        lossless = self._lossless
        if len(clips) >= 2 and not lossless:
            if not self.m_reencode.get_active():
                self._forced = True            # remember that we, not the user, turned it on
                self.m_reencode.set_active(True)
            self.m_reencode.set_sensitive(False)
            self.m_reencode.set_subtitle("Clips differ in format, so they must be re-encoded")
        else:
            if self._forced:               # undo a switch we flipped, not one the user did
                self._forced = False
                self.m_reencode.set_active(False)
            self.m_reencode.set_sensitive(len(clips) >= 2)
            self.m_reencode.set_subtitle(
                "Clips match: off joins them losslessly in seconds" if lossless else
                "Needed when clips differ in size, codec or frame rate")
        enc = self.m_reencode.get_active()
        self.m_codec.set_sensitive(enc)
        self.m_res.set_sensitive(enc)
        gpu_ok = any(e.endswith("_nvenc") for e in self.ctx.encoders)
        self.m_gpu.set_visible(gpu_ok)
        self.m_gpu.set_sensitive(enc)
        total = sum(i.info.duration for i in clips)
        self.m_go.set_sensitive(len(clips) >= 2)
        self.m_go.set_title(f"Join {len(clips)} clips ({fmt_duration(total)})"
                            if len(clips) >= 2 else "Select at least two clips")

    def _remember_gpu(self):
        if self.ctx.settings.gpu != self.m_gpu.get_active():
            self.ctx.settings.gpu = self.m_gpu.get_active()
            self.ctx.settings.save()

    def _move(self, item, delta):
        clips = self._ready_order()
        if item not in clips:      # re-probing after a change; the list refreshes shortly
            return
        i = clips.index(item)
        j = i + delta
        if 0 <= j < len(clips):
            clips[i], clips[j] = clips[j], clips[i]
            rest = [x for x in self.order if x not in clips]
            self.order = clips + rest
            self._sync_merge()

    # ------------------------------------------------------------------ actions
    def _merge(self):
        clips = self._ready_order()
        r = Recipe(vcodec=self.m_codec.get_key(), gpu=self.m_gpu.get_visible() and
                   self.m_gpu.get_active(), scale_height=int(self.m_res.get_key()))
        try:
            plan = ops.build_merge([i.info for i in clips], r, self.ctx.out_dir,
                                   self.ctx.encoders, force_reencode=self.m_reencode.get_active(),
                                   taken=self.ctx.taken())
        except PlanError as e:
            self.ctx.toast(str(e))
            return
        self.ctx.submit(plan)
        self.ctx.toast(f"Joining {len(clips)} clips → {os.path.basename(plan.output)}")
        self.ctx.show_queue()

    def _snapshot(self):
        self.s_time.remove_css_class("error")
        try:
            at = parse_time(self.s_time.get_text())
        except ValueError:
            self.s_time.add_css_class("error")
            self.ctx.toast("Enter a time like 0:03.5")
            return
        clips = [i for i in self.items if i.ready and i.info.has_video]
        if not clips:
            return
        try:
            plan = ops.build_snapshot(clips[0].info, at, self.s_fmt.get_key(),
                                      self.ctx.out_dir, self.ctx.taken())
        except PlanError as e:
            self.ctx.toast(str(e))
            return
        self.ctx.submit(plan)
        self.ctx.toast(f"Saving frame → {os.path.basename(plan.output)}")

    def _sheet(self):
        taken = self.ctx.taken()
        n = 0
        for it in self.items:
            if not (it.ready and it.info.has_video):
                continue
            try:
                plan = ops.build_contact_sheet(it.info, int(self.c_cols.get_value()),
                                               int(self.c_rows.get_value()),
                                               int(self.c_w.get_value()),
                                               self.ctx.out_dir, taken)
            except PlanError as e:
                self.ctx.toast(f"{it.name}: {e}")
                continue
            taken.add(plan.output)
            self.ctx.submit(plan)
            n += 1
        if n:
            self.ctx.toast(f"Queued {n} contact sheet{'s' if n != 1 else ''}")
            self.ctx.show_queue()
