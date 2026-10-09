"""Tools: join clips, grab a frame, build a contact sheet."""

from __future__ import annotations

import os

from gi.repository import Adw, Gtk

from .. import ops
from ..media import fmt_duration, parse_time
from ..ops import PlanError, Recipe
from .library import MediaItem
from .widgets import KeyCombo, clear_group, plain_row


class ToolsPage(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.items: list[MediaItem] = []
        self.order: list[MediaItem] = []
        self.merge_rows: list = []

        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)

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
            for b in (up, down):
                b.add_css_class("flat")
            up.connect("clicked", lambda _b, i=it: self._move(i, -1))
            down.connect("clicked", lambda _b, i=it: self._move(i, 1))
            row.add_suffix(up)
            row.add_suffix(down)
            self.merge_list.add(row)
            self.merge_rows.append(row)
        self.merge_list.set_visible(bool(clips))
        lossless = ops.can_concat_losslessly([i.info for i in clips])
        if len(clips) >= 2 and not lossless:
            self.m_reencode.set_active(True)
            self.m_reencode.set_sensitive(False)
            self.m_reencode.set_subtitle("Clips differ in format, so they must be re-encoded")
        else:
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

    def _move(self, item, delta):
        clips = self._ready_order()
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
            plan = ops.build_merge([i.info for i in clips], r, self.ctx.settings.out_dir,
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
                                      self.ctx.settings.out_dir, self.ctx.taken())
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
                                               self.ctx.settings.out_dir, taken)
            except PlanError as e:
                self.ctx.toast(f"{it.name}: {e}")
                continue
            taken.add(plan.output)
            self.ctx.submit(plan)
            n += 1
        if n:
            self.ctx.toast(f"Queued {n} contact sheet{'s' if n != 1 else ''}")
            self.ctx.show_queue()
