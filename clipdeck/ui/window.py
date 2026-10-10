"""Main window: library sidebar + Inspect / Quick Fix / Edit / Tools / Queue."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import ops
from ..jobs import DONE, FAILED, Job, JobQueue
from ..ops import Plan
from ..settings import Settings
from .edit import EditPage
from .fix import FixPage
from .inspect import InspectPage
from .library import Library, MediaItem, MediaRow, is_media_path
from .prefs import PreferencesDialog
from .queue import QueuePage
from .tools import ToolsPage

# what we can assume before the real (slower) encoder probe finishes
BASE_ENCODERS = {"libx264", "libx265", "libsvtav1", "libvpx-vp9", "prores_ks", "dnxhd"}


class Context:
    """What pages need from the window, without depending on it directly."""

    def __init__(self, win: "MainWindow"):
        self.win = win
        self.settings: Settings = win.settings
        self.encoders: set[str] = set(BASE_ENCODERS)
        self.queue: JobQueue = win.queue
        self.library: Library = win.library
        self._warned_missing = False

    def taken(self) -> set[str]:
        return self.queue.reserved_outputs()

    @property
    def out_dir(self) -> str | None:
        """Where outputs go. A chosen folder that has gone missing (an unplugged drive)
        falls back to saving beside the originals, and the user is told once."""
        s = self.settings
        missing = s.output_mode == "folder" and s.output_folder and not s.out_dir
        if missing and not self._warned_missing:
            self._warned_missing = True
            GLib.idle_add(lambda: self.toast(f"Output folder {s.output_folder} is missing: "
                                             "saving next to the originals") and False)
        elif not missing:
            self._warned_missing = False
        return s.out_dir

    def submit(self, plan: Plan, after: Job | None = None) -> Job:
        return self.queue.submit(plan, after=after)

    def toast(self, text: str):
        self.win.toast(text)

    def show_queue(self):
        self.win.stack.set_visible_child_name("queue")

    def queue_badge(self, n: int):
        page = getattr(self.win, "queue_page_ref", None)
        if page is not None:          # the queue page updates once before it is added
            page.set_badge_number(n)
            page.set_needs_attention(n > 0)

    def add_paths(self, paths, select=False):
        return self.win.add_paths(paths, select=select)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, settings: Settings):
        super().__init__(application=app, title="Clipdeck", default_width=1320,
                         default_height=860)
        self.set_size_request(500, 480)
        self.settings = settings
        self._quit_confirmed = False
        self._announced: set[int] = set()
        self._closed = False
        self._batch: list[Job] = []          # finished since the queue last drained
        self._last_toast: Adw.Toast | None = None
        self.library = Library(on_item_ready=self._item_ready)
        self.queue = JobQueue(self._job_changed_threadsafe, workers=settings.workers,
                              thorough=lambda: self.settings.thorough)
        self.ctx = Context(self)

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.OverlaySplitView(min_sidebar_width=320, max_sidebar_width=420,
                                          sidebar_width_fraction=0.3)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        self._build_sidebar()
        self._build_content()

        # only one breakpoint applies at a time (the last matching), so the narrow one
        # repeats the medium one's setters
        # (the sidebar plus Edit's two columns need about 1180 px side by side). Short
        # windows get smaller players so the forms keep room.
        for cond, collapse, short in (("max-width: 1200sp", False, False),
                                      ("max-width: 860sp", True, False),
                                      ("max-width: 1200sp and max-height: 720sp", False, True),
                                      ("max-width: 860sp and max-height: 720sp", True, True)):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(cond))
            bp.add_setter(self.switcher, "visible", False)
            bp.add_setter(self.switcher_bar, "reveal", True)
            self.edit.narrow_setters(bp, short)
            if short:
                bp.add_setter(self.inspect.player.video, "height-request", 160)
            if collapse:
                bp.add_setter(self.split, "collapsed", True)
            self.add_breakpoint(bp)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.connect("close-request", self._on_close)
        threading.Thread(target=self._probe_encoders, daemon=True).start()

    # ================================================================== layout
    def _build_sidebar(self):
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title="Library"))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add videos (Ctrl+O)")
        add.connect("clicked", lambda *_: self.open_files())
        hb.pack_start(add)
        menu = Gio.Menu()
        menu.append("Add Folder…", "win.add-folder")
        menu.append("Select All", "win.select-all")
        menu.append("Remove Selected", "win.remove-selected")
        menu.append("Clear Library", "win.clear-library")
        sec = Gio.Menu()
        sec.append("Preferences", "app.preferences")
        sec.append("About Clipdeck", "app.about")
        menu.append_section(None, sec)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                   tooltip_text="Main menu", primary=True))
        tv.add_top_bar(hb)

        self.side_stack = Gtk.Stack()
        empty = Adw.StatusPage(icon_name="folder-videos-symbolic", title="Drop videos here",
                               description="Or add files to fix, rotate and convert them.")
        b = Gtk.Button(label="Add Videos…", halign=Gtk.Align.CENTER)
        b.add_css_class("pill")
        b.add_css_class("suggested-action")
        b.connect("clicked", lambda *_: self.open_files())
        empty.set_child(b)
        empty.add_css_class("compact")
        self.side_stack.add_named(empty, "empty")

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(MediaRow()))
        factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        factory.connect("unbind", lambda _f, li: li.get_child().unbind())
        self.selection = Gtk.MultiSelection.new(self.library.store)
        self.selection.connect("selection-changed", lambda *_: self._selection_changed())
        self.selection.connect("items-changed", lambda *_: self._library_changed())
        self._refresh_pending = 0
        self.listview = Gtk.ListView(model=self.selection, factory=factory)
        self.listview.add_css_class("navigation-sidebar")
        # Delete removes clips only while the list has focus: as a window-wide shortcut it
        # would fire before text fields see the key and empty the library while typing
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Delete"),
                                           Gtk.NamedAction.new("win.remove-selected")))
        self.listview.add_controller(keys)
        self.side_stack.add_named(Gtk.ScrolledWindow(child=self.listview, vexpand=True,
                                                     hscrollbar_policy=Gtk.PolicyType.NEVER),
                                  "list")
        tv.set_content(self.side_stack)

        foot = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.sel_label = Gtk.Label(xalign=0, hexpand=True)
        self.sel_label.add_css_class("caption")
        self.sel_label.add_css_class("dim-label")
        rm = Gtk.Button(icon_name="user-trash-symbolic",
                        tooltip_text="Remove selected from library (files are not touched)")
        rm.add_css_class("flat")
        rm.update_property([Gtk.AccessibleProperty.LABEL], ["Remove selected from library"])
        rm.connect("clicked", lambda *_: self.remove_selected())
        self.rm_btn = rm
        foot.append(self.sel_label)
        foot.append(rm)
        tv.add_bottom_bar(foot)
        self.split.set_sidebar(tv)

        for name, cb in (("add-folder", lambda *_: self.open_folder()),
                         ("select-all", lambda *_: self.selection.select_all()),
                         ("remove-selected", lambda *_: self.remove_selected()),
                         ("clear-library", lambda *_: self.clear_library()),
                         ("open", lambda *_: self.open_files()),
                         ("show-sidebar", lambda *_: self.split.set_show_sidebar(True))):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

    def _build_content(self):
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        hb.set_title_widget(self.switcher)
        side_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Library")
        # the split view owns the state; the button mirrors it
        self.split.bind_property("show-sidebar", side_btn, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.split.bind_property("collapsed", side_btn, "visible", GObject.BindingFlags.SYNC_CREATE)
        hb.pack_start(side_btn)
        tv.add_top_bar(hb)

        self.inspect = InspectPage(self.ctx)
        self.fix = FixPage(self.ctx)
        self.edit = EditPage(self.ctx)
        self.tools = ToolsPage(self.ctx)
        self.queue_page = QueuePage(self.ctx)
        self.stack.add_titled_with_icon(self.inspect, "inspect", "Inspect",
                                        "dialog-information-symbolic")
        self.stack.add_titled_with_icon(self.fix, "fix", "Quick Fix",
                                        "applications-engineering-symbolic")
        self.stack.add_titled_with_icon(self.edit, "edit", "Edit & Convert",
                                        "document-edit-symbolic")
        self.stack.add_titled_with_icon(self.tools, "tools", "Tools",
                                        "applications-utilities-symbolic")
        self.queue_page_ref = self.stack.add_titled_with_icon(self.queue_page, "queue", "Queue",
                                                              "view-list-symbolic")
        self.pages = (self.inspect, self.fix, self.edit, self.tools)
        tv.set_content(self.stack)
        # Alt+1…5 switch pages
        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        for n, name in enumerate(("inspect", "fix", "edit", "tools", "queue"), 1):
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(f"<Alt>{n}"),
                Gtk.CallbackAction.new(lambda *_a, nm=name: self.stack.set_visible_child_name(nm)
                                       or True)))
        self.add_controller(keys)

        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        tv.add_bottom_bar(self.switcher_bar)
        self.split.set_content(tv)

    # ================================================================== library
    def add_paths(self, paths, select=False) -> list[MediaItem]:
        added = self.library.add_paths(list(paths))
        if not added:
            existing = [self.library.find(p) for p in paths]
            existing = [e for e in existing if e]
            if select and existing:
                self._select_items(existing)
            elif not existing:
                self.toast("No video or audio files found")
            return []
        if select or self.selection.get_selection().get_size() == 0:
            self._select_items(added)
        return added

    def _select_items(self, items):
        n = self.library.store.get_n_items()
        wanted = set(id(i) for i in items)
        bits = Gtk.Bitset.new_empty()
        for pos in range(n):
            if id(self.library.store.get_item(pos)) in wanted:
                bits.add(pos)
        # one change for the whole set (selecting row by row re-plans every page each time)
        self.selection.set_selection(bits, Gtk.Bitset.new_range(0, n))

    def selected_items(self) -> list[MediaItem]:
        bits = self.selection.get_selection()
        return [self.library.store.get_item(bits.get_nth(i)) for i in range(bits.get_size())]

    def remove_selected(self):
        items = self.selected_items()
        self.library.remove(items)
        self._selection_changed()

    def clear_library(self):
        self.library.store.remove_all()
        self._selection_changed()

    def _library_changed(self):
        n = self.library.store.get_n_items()
        self.side_stack.set_visible_child_name("list" if n else "empty")
        self._selection_changed()

    def _selection_changed(self):
        """Refresh the pages once the current burst of changes is over (adding a folder
        or probing finishing emits one change per clip)."""
        if not self._refresh_pending:
            self._refresh_pending = GLib.idle_add(self._refresh_pages)

    def _refresh_pages(self):
        self._refresh_pending = 0
        if self._closed:
            return False
        items = self.selected_items()
        n = len(self.library)
        self.sel_label.set_label(f"{len(items)} of {n} selected" if n else "")
        self.rm_btn.set_sensitive(bool(items))
        for p in self.pages:
            p.set_items(items)
        return False

    def _item_ready(self, item: MediaItem):
        if item in self.selected_items():
            self._selection_changed()

    # ================================================================== dialogs
    def open_files(self):
        dlg = Gtk.FileDialog(title="Add Videos", modal=True)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="Video and audio")
        f.add_mime_type("video/*")
        f.add_mime_type("audio/*")
        f.add_mime_type("image/gif")
        filters.append(f)
        allf = Gtk.FileFilter(name="All files")
        allf.add_pattern("*")
        filters.append(allf)
        dlg.set_filters(filters)
        if self.settings.last_dir and os.path.isdir(self.settings.last_dir):
            dlg.set_initial_folder(Gio.File.new_for_path(self.settings.last_dir))

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            paths = [p for p in paths if p]
            if paths:
                self.settings.last_dir = os.path.dirname(paths[0])
                self.settings.save()
                self.add_paths(paths)
        dlg.open_multiple(self, None, done)

    def open_folder(self):
        dlg = Gtk.FileDialog(title="Add Folder", modal=True)

        def done(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.add_paths([folder.get_path()])
        dlg.select_folder(self, None, done)

    def _on_drop(self, _target, value, _x, _y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        files = [p for p in paths if os.path.isdir(p) or is_media_path(p)]
        if not files:
            self.toast("No video or audio files in what was dropped")
            return False
        self.add_paths(files)
        return True

    def show_preferences(self):
        PreferencesDialog(self).present(self)

    def toast(self, text: str):
        # a new message replaces the old one instead of queueing behind it
        if self._last_toast is not None:
            self._last_toast.dismiss()
        self._last_toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=3)
        self.toasts.add_toast(self._last_toast)

    # ================================================================== jobs
    def _job_changed_threadsafe(self, job: Job):
        GLib.idle_add(self._job_changed, job)

    def _job_changed(self, job: Job):
        if self._closed:
            return False         # late callbacks after the window was torn down
        self.queue_page.job_changed(job)
        if not job.finished or job.id in self._announced:
            return False
        self._announced.add(job.id)
        self._batch.append(job)
        if job.state == DONE and job.result and job.result.replaced_original:
            gone = self.library.find(job.result.replaced_original)
            if gone and os.path.exists(gone.path):
                self.library.refresh(gone)    # the fixed file took the original's name
                for p in self.pages:          # players still hold the trashed original
                    if hasattr(p, "player") and p.player.path == gone.path:
                        p.player.reload()
            elif gone:
                # new name (format changed): the fixed file takes the original's place
                was_selected = gone in self.selected_items()
                self.library.remove([gone])
                added = self.add_paths([job.result.output])
                if was_selected and added:
                    keep = [i for i in self.selected_items()] + added
                    self._select_items(keep)
        if self.queue.active_count() == 0:
            self._announce_batch()
        return False

    def _announce_batch(self):
        """One message per drained queue, not one per job."""
        batch, self._batch = self._batch, []
        done = [j for j in batch if j.state == DONE]
        failed = [j for j in batch if j.state == FAILED]
        if not done and not failed:
            return        # only cancellations: the user already knows
        if len(done) + len(failed) == 1:
            j = (done or failed)[0]
            name = os.path.basename(j.result.output or j.plan.output)
            text = f"Saved {name}" if done else f"Failed: {name}"
        else:
            text = f"{len(done)} file{'s' if len(done) != 1 else ''} saved"
            if failed:
                text += f", {len(failed)} failed"
        self.toast(text)
        app = self.get_application()
        if app and not self.is_active():
            n = Gio.Notification.new("Clipdeck: all jobs finished")
            n.set_body(f"{text}. Open the Queue for details." if failed
                       else f"{text}. Every output was verified.")
            app.send_notification("all-done", n)

    def _quit_now(self):
        self._shutdown()
        self.destroy()
        return False

    def _probe_encoders(self):
        enc = ops.available_encoders()
        GLib.idle_add(self._encoders_ready, enc)

    def _encoders_ready(self, enc):
        if enc:
            self.ctx.encoders = enc
        self._selection_changed()
        return False

    def _shutdown(self):
        """Cancel everything (partial files are removed by the workers) and stop players."""
        self._closed = True
        self.queue.shutdown(wait=8)
        for p in self.pages:
            if hasattr(p, "player"):
                p.player.load(None)

    def _on_close(self, *_):
        active = self.queue.active_count()
        if active == 0 or self._quit_confirmed:
            self._shutdown()
            return False
        dlg = Adw.AlertDialog(heading="Cancel running jobs?",
                              body=f"{active} job{'s are' if active != 1 else ' is'} still "
                                   "running or waiting. Quitting cancels them; finished files "
                                   "are kept and partial files are removed.")
        dlg.add_response("keep", "Keep Working")
        dlg.add_response("quit", "Cancel Jobs and Quit")
        dlg.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")
        dlg.set_close_response("keep")

        def on_resp(_d, resp):
            if resp == "quit" and not self._quit_confirmed:
                self._quit_confirmed = True
                # close() would be swallowed while the dialog is still open, so shut down
                # explicitly once the response handler has returned
                GLib.idle_add(self._quit_now)
        dlg.connect("response", on_resp)
        dlg.present(self)
        return True

