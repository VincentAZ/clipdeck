"""Quick Fix: fixrot in a window. Diagnose the selection and repair it in one click."""

from __future__ import annotations

import os

from gi.repository import Adw, Gtk

from .. import ops
from ..ops import PlanError, Recipe
from .library import MediaItem
from .widgets import button_row, clear_group, plain_row


def describe(info, recipe: Recipe) -> str:
    bits = []
    if info.rotation:
        bits.append(f"bake {abs(info.rotation)}° rotation")
    if info.container not in ("mp4", "mov", "m4v"):
        bits.append(f".{info.container} → .mp4")
    if info.vcodec not in ("h264", "hevc", "av1"):
        bits.append(f"{info.vcodec} → H.264")
    if info.has_audio and any(a.codec not in ("aac", "mp3") for a in info.audio) \
            and info.container != "mov":
        bits.append(f"{info.acodec} → AAC")
    if info.width % 2 or info.height % 2:
        bits.append("even dimensions")
    if recipe.cfr:
        bits.append("constant frame rate")
    return ", ".join(bits) if bits else "re-encode"


def mov_recipe() -> Recipe:
    return Recipe(fmt="mov", vcodec="auto", acodec="auto", bake_rotation=True,
                  quality="lossless", speed_preset="best")


class FixPage(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.items: list[MediaItem] = []
        self.rows: list = []

        self.stack = Gtk.Stack(vexpand=True)
        self.append(self.stack)
        self.stack.add_named(Adw.StatusPage(
            icon_name="applications-engineering-symbolic", title="Quick Fix",
            description="Select clips in the library. Sideways phone videos, MKV/WebM/AVI "
                        "containers and incompatible audio are repaired in one click, and every "
                        "result is verified before it is saved."), "empty")

        page = Adw.PreferencesPage()
        self.stack.add_named(page, "main")

        self.list_group = Adw.PreferencesGroup(title="Diagnosis")
        page.add(self.list_group)

        opts = Adw.PreferencesGroup(title="Options")
        page.add(opts)
        self.cfr = Adw.SwitchRow(
            title="Constant frame rate",
            subtitle="Convert variable-frame-rate phone footage so editors keep audio in sync")
        self.cfr.set_active(ctx.settings.autofix_cfr)
        self.mov = Adw.SwitchRow(
            title="Also make a Resolve MOV",
            subtitle="Upright MOV with PCM audio next to each clip (video copied when possible)")
        self.mov.set_active(ctx.settings.autofix_mov)
        self.replace = Adw.SwitchRow(
            title="Replace originals",
            subtitle="After the fixed file verifies, the original goes to the Trash and the "
                     "fixed file takes its name when the format is unchanged")
        for r in (self.cfr, self.mov, self.replace):
            opts.add(r)
            r.connect("notify::active", lambda *_: self._options_changed())

        foot, self.button = button_row("Fix")
        self.button.connect("clicked", lambda *_: self.run())
        page_box = Adw.PreferencesGroup()
        page_box.add(foot)
        page.add(page_box)
        self.plans: list = []

    def _options_changed(self):
        self.ctx.settings.autofix_cfr = self.cfr.get_active()
        self.ctx.settings.autofix_mov = self.mov.get_active()
        self.ctx.settings.save()
        self.refresh()

    def set_items(self, items: list[MediaItem]):
        self.items = items
        self.refresh()

    # ------------------------------------------------------------------ planning
    def _plan_all(self):
        """[(item, fix_plan | None, mov_plan | None, error | None)]"""
        out = []
        taken = self.ctx.taken()
        for it in self.items:
            if not it.ready:
                out.append((it, None, None, "still reading" if it.state == "probing" else it.error))
                continue
            info = it.info
            fix = mov = None
            err = None
            try:
                r = ops.autofix_recipe(info, cfr=self.cfr.get_active())
                if r:
                    r.replace_original = self.replace.get_active()
                    fix = ops.build(info, r, self.ctx.settings.out_dir, self.ctx.encoders, taken)
                    taken.add(fix.output)
                if self.mov.get_active() and info.has_video and info.container != "mov":
                    mov = ops.build(info, mov_recipe(), self.ctx.settings.out_dir,
                                    self.ctx.encoders, taken)
                    taken.add(mov.output)
            except PlanError as e:
                err = str(e)
            out.append((it, fix, mov, err))
        return out

    def refresh(self):
        if not self.items:
            self.stack.set_visible_child_name("empty")
            return
        self.stack.set_visible_child_name("main")
        clear_group(self.list_group, self.rows)
        planned = self._plan_all()
        self.plans = planned
        jobs = 0
        for it, fix, mov, err in planned:
            row = plain_row(it.name)
            row.set_title_lines(1)
            row.set_subtitle_lines(3)
            if err:
                row.set_subtitle(err)
                icon, cls = "dialog-error-symbolic", "error"
            elif not it.info.has_video:
                row.set_subtitle("Audio file, nothing to fix")
                icon, cls = "object-select-symbolic", "dim-label"
            elif fix is None and mov is None:
                row.set_subtitle("Already fine, nothing to fix")
                icon, cls = "object-select-symbolic", "success"
            else:
                lines = []
                if fix:
                    r = ops.autofix_recipe(it.info, cfr=self.cfr.get_active())
                    what = describe(it.info, r)
                    lines.append(f"{what[:1].upper()}{what[1:]} → {os.path.basename(fix.output)}")
                    jobs += 1
                if mov:
                    lines.append(f"Resolve MOV → {os.path.basename(mov.output)}")
                    jobs += 1
                row.set_subtitle("\n".join(lines))
                icon, cls = ("object-select-symbolic", "success") if fix is None else \
                    ("dialog-warning-symbolic", "warning")
            img = Gtk.Image(icon_name=icon)
            img.add_css_class(cls)
            row.add_prefix(img)
            self.list_group.add(row)
            self.rows.append(row)
        n = len(self.items)
        self.list_group.set_description(f"{n} clip{'s' if n != 1 else ''} selected")
        self.button.set_sensitive(jobs > 0)
        self.button.set_label(f"Fix {jobs} file{'s' if jobs != 1 else ''}" if jobs
                              else "Nothing to fix")

    def run(self):
        planned = self._plan_all()   # re-plan: names may have changed since display
        jobs = []
        for it, fix, mov, err in planned:
            first = None
            if mov:
                first = self.ctx.submit(mov)
                jobs.append(first)
            if fix:
                # replacing the original must wait until the MOV has finished reading it
                jobs.append(self.ctx.submit(fix, after=first if fix.replace_original else None))
        if jobs:
            self.ctx.toast(f"Queued {len(jobs)} job{'s' if len(jobs) != 1 else ''}")
            self.ctx.show_queue()
        self.refresh()

