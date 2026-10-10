"""Queue: live progress of every job, with cancel, open, show-in-folder and logs."""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, Gtk

from ..jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING, Job

STATE_ICON = {
    QUEUED: "content-loading-symbolic",
    RUNNING: "media-playback-start-symbolic",
    DONE: "object-select-symbolic",
    FAILED: "dialog-error-symbolic",
    CANCELLED: "process-stop-symbolic",
}
STATE_CLASS = {DONE: "success", FAILED: "error", CANCELLED: "dim-label"}


def _fmt_elapsed(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


class JobRow(Gtk.ListBoxRow):
    def __init__(self, page: "QueuePage", job: Job):
        super().__init__(activatable=False, selectable=False)
        self.page = page
        self.job = job
        box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=10, margin_bottom=10)
        self.set_child(box)
        self.icon = Gtk.Image(icon_name=STATE_ICON[QUEUED], valign=Gtk.Align.CENTER)
        box.append(self.icon)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=2)
        self.title.add_css_class("heading")
        self.detail = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.detail.add_css_class("caption")
        self.detail.add_css_class("dim-label")
        self.bar = Gtk.ProgressBar(show_text=False)
        col.append(self.title)
        col.append(self.detail)
        col.append(self.bar)
        box.append(col)

        btns = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.b_cancel = self._btn("process-stop-symbolic", "Cancel", self._cancel)
        self.b_open = self._btn("media-playback-start-symbolic", "Open", self._open)
        self.b_folder = self._btn("folder-open-symbolic", "Show in folder", self._folder)
        self.b_add = self._btn("list-add-symbolic", "Add result to library", self._add)
        self.b_log = self._btn("utilities-terminal-symbolic", "Show log", self._log)
        for b in (self.b_cancel, self.b_open, self.b_folder, self.b_add, self.b_log):
            btns.append(b)
        box.append(btns)
        self.update()

    @staticmethod
    def _btn(icon, tip, cb):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip)
        b.add_css_class("flat")
        b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
        b.connect("clicked", lambda *_: cb())
        return b

    def update(self):
        j = self.job
        # state first: a worker stores the result before the state, so a finished state
        # read here always comes with its result
        state = j.state
        r = j.result
        out = r.output if (r and r.output) else j.plan.output
        self.title.set_label(os.path.basename(out))
        self.set_tooltip_text(out)
        self.icon.set_from_icon_name(STATE_ICON[state])
        for c in STATE_CLASS.values():
            self.icon.remove_css_class(c)
        if state in STATE_CLASS:
            self.icon.add_css_class(STATE_CLASS[state])
        src = ", ".join(os.path.basename(i.path) for i in j.plan.inputs[:3])
        if len(j.plan.inputs) > 3:
            src += f" +{len(j.plan.inputs) - 3}"
        if state == QUEUED:
            text = f"Waiting · from {src}"
            if j.after is not None and not j.after.finished:
                text = f"Waiting for {os.path.basename(j.after.plan.output)} · from {src}"
        elif state == RUNNING:
            text = f"{j.status} · {int(j.progress * 100)}%"
        elif state == DONE:
            text = f"Verified and saved in {_fmt_elapsed(r.elapsed)} · from {src}"
            if r.replaced_original:
                text += " · original moved to Trash"
            if r.warnings:
                text += "\n⚠ " + "; ".join(r.warnings)
        elif state == FAILED:
            text = "Failed: " + "; ".join(r.failures if r else ["unknown error"])
            if r and r.output:
                text += f"\nBroken output kept for inspection: {os.path.basename(r.output)}"
        else:
            text = "Cancelled"
        self.detail.set_label(text)
        self.bar.set_visible(state in (QUEUED, RUNNING))
        self.bar.set_fraction(j.progress)
        if state == RUNNING and j.progress <= 0:
            self.bar.pulse()
        self.b_cancel.set_visible(state in (QUEUED, RUNNING))
        ok = state == DONE and r and os.path.exists(r.output)
        self.b_open.set_visible(bool(ok))
        self.b_folder.set_visible(bool(ok) or bool(state == FAILED and r and r.output
                                                     and os.path.exists(r.output)))
        self.b_add.set_visible(bool(ok))
        self.b_log.set_visible(state not in (QUEUED, RUNNING) and r is not None)

    # ------------------------------------------------------------------ actions
    def _out(self):
        r = self.job.result
        return r.output if r else ""

    def _cancel(self):
        self.page.ctx.queue.cancel(self.job)

    def _open(self):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self._out()))
        launcher.launch(self.get_root(), None, None, None)

    def _folder(self):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self._out()))
        launcher.open_containing_folder(self.get_root(), None, None, None)

    def _add(self):
        self.page.ctx.add_paths([self._out()], select=True)

    def _log(self):
        r = self.job.result
        dlg = Adw.Dialog(title="Job log", content_width=760, content_height=480)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        lines = []
        if self.job.plan.summary:
            lines += ["Plan:"] + ["  " + s for s in self.job.plan.summary] + [""]
        if r and r.failures:
            lines += ["Failures:"] + ["  " + f for f in r.failures] + [""]
        if r and r.warnings:
            lines += ["Warnings:"] + ["  " + w for w in r.warnings] + [""]
        lines.append(r.log if r else "")
        view.get_buffer().set_text("\n".join(lines))
        tv.set_content(Gtk.ScrolledWindow(child=view, vexpand=True))
        dlg.set_child(tv)
        dlg.present(self.get_root())


class QueuePage(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.rows: dict[int, JobRow] = {}

        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=12)
        self.counts = Gtk.Label(xalign=0, hexpand=True)
        self.counts.add_css_class("dim-label")
        self.b_clear = Gtk.Button(label="Clear finished")
        self.b_clear.connect("clicked", lambda *_: self.clear_finished())
        self.b_cancel = Gtk.Button(label="Cancel all")
        self.b_cancel.add_css_class("destructive-action")
        self.b_cancel.connect("clicked", lambda *_: ctx.queue.cancel_all())
        bar.append(self.counts)
        bar.append(self.b_clear)
        bar.append(self.b_cancel)
        self.append(bar)

        self.stack = Gtk.Stack(vexpand=True)
        self.append(self.stack)
        self.stack.add_named(Adw.StatusPage(
            icon_name="view-list-symbolic", title="No jobs yet",
            description="Fixes, conversions and exports you start appear here with live "
                        "progress. Every output is verified before it is saved, and nothing "
                        "is ever overwritten."), "empty")
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        clamp = Adw.Clamp(maximum_size=900, child=self.listbox, margin_top=12, margin_bottom=24,
                          margin_start=12, margin_end=12)
        self.stack.add_named(Gtk.ScrolledWindow(child=clamp, vexpand=True,
                                                hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self._sync()

    def job_changed(self, job: Job):
        row = self.rows.get(job.id)
        if row is None:
            if job not in self.ctx.queue.jobs:
                return
            row = JobRow(self, job)
            self.rows[job.id] = row
            self.listbox.prepend(row)
        row.update()
        self._sync()

    def clear_finished(self):
        self.ctx.queue.clear_finished()
        for jid, row in list(self.rows.items()):
            if row.job.finished:
                self.listbox.remove(row)
                del self.rows[jid]
        self._sync()

    def _sync(self):
        jobs = [r.job for r in self.rows.values()]
        active = sum(1 for j in jobs if not j.finished)
        done = sum(1 for j in jobs if j.state == DONE)
        failed = sum(1 for j in jobs if j.state == FAILED)
        self.stack.set_visible_child_name("list" if jobs else "empty")
        parts = []
        if active:
            parts.append(f"{active} active")
        if done:
            parts.append(f"{done} done")
        if failed:
            parts.append(f"{failed} failed")
        self.counts.set_label(" · ".join(parts))
        self.b_clear.set_sensitive(any(j.finished for j in jobs))
        self.b_cancel.set_sensitive(active > 0)
        self.ctx.queue_badge(active)
