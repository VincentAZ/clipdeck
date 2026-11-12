"""Record Video: a live camera preview with a microphone meter and a record button.
The finished recording is checked, saved, and added to the library (see recorder.py)."""

from __future__ import annotations

import os
import threading
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import recorder
from ..recorder import (PREVIEW_H, PREVIEW_W, SILENT_DB, Camera, Microphone, Recording,
                        Session)
from .widgets import KeyCombo

NO_MIC = "none"
METER_FLOOR = -60.0        # dBFS at the left end of the meter
HEARD = -100.0             # quieter than any real microphone's own noise: muted or dead
SILENCE_HINT_AFTER = 3.0   # seconds of that before saying so


def videos_dir() -> str:
    return (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
            or os.path.expanduser("~/Videos"))


def _clock(sec: float) -> str:
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


class RecordDialog(Adw.Dialog):
    """States: looking (finding devices), preview, starting (camera reopening to record),
    recording, saving, notice (something to tell the user instead of a picture)."""

    def __init__(self, win):
        super().__init__(title="Record Video", content_width=720)
        self.win = win
        self.settings = win.settings
        self.cams: list[Camera] = []
        self.mics: list[Microphone] = []
        self.state = "looking"
        self.session: Session | None = None
        self.recording: Recording | None = None
        self._gen = 0                   # bumps with every session; late callbacks are dropped
        self._closed = False
        self._frame: bytes | None = None
        self._frame_pending = False
        self._peak = SILENT_DB          # loudest since the meter last updated
        self._shown = 0.0
        self._heard_at = 0.0
        self._started_at = 0.0          # when the current session was launched
        self._rec_since = 0.0           # when the first recorded frame arrived
        self._notice_command = ""
        self._notice_retry = None
        self._filling = False           # combos being refilled: not a user's choice
        self._saving: threading.Thread | None = None
        self._switching = threading.Lock()

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18,
                       margin_end=18, margin_top=6, margin_bottom=12)

        # ---- preview
        self.view = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.view.set_size_request(-1, 200)
        # an empty 16:9 picture until frames arrive keeps the box the camera's shape
        # (and the same size) whatever it shows
        self.blank = Gdk.Paintable.new_empty(PREVIEW_W, PREVIEW_H)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                   paintable=self.blank)
        self.view.add_named(self.picture, "video")
        wait = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        wait.add_css_class("player-notice")
        wait.append(Adw.Spinner(width_request=32, height_request=32))
        self.wait_label = Gtk.Label(label="Looking for cameras…")
        wait.append(self.wait_label)
        self.view.add_named(wait, "wait")
        notice = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                         margin_start=24, margin_end=24)
        notice.add_css_class("player-notice")
        self.notice_icon = Gtk.Image(pixel_size=48, margin_bottom=6)
        self.notice_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.notice_title.add_css_class("title-4")
        self.notice_body = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                     selectable=True, max_width_chars=60)
        self.notice_body.add_css_class("caption")
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=8)
        self.copy_btn = Gtk.Button(label="Copy Command")
        self.copy_btn.add_css_class("pill")
        self.copy_btn.connect("clicked", lambda *_: self._copy())
        self.retry_btn = Gtk.Button(label="Try Again")
        self.retry_btn.add_css_class("pill")
        self.retry_btn.add_css_class("suggested-action")
        self.retry_btn.connect("clicked", lambda *_: self._notice_retry and self._notice_retry())
        btns.append(self.copy_btn)
        btns.append(self.retry_btn)
        for w in (self.notice_icon, self.notice_title, self.notice_body, btns):
            notice.append(w)
        self.view.add_named(notice, "notice")

        frame = Gtk.Overlay(child=self.view, overflow=Gtk.Overflow.HIDDEN)
        frame.add_css_class("player")
        self.pill = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.START,
                            margin_start=10, margin_top=10, visible=False)
        self.pill.add_css_class("rec-pill")
        dot = Gtk.Image(icon_name="media-record-symbolic")
        dot.add_css_class("rec-dot")
        self.pill.append(dot)
        self.clock = Gtk.Label(label="0:00")
        self.clock.add_css_class("numeric")
        self.pill.append(self.clock)
        frame.add_overlay(self.pill)
        body.append(frame)

        # ---- microphone meter
        meter = Gtk.Box(spacing=10)
        self.mic_icon = Gtk.Image(icon_name="audio-input-microphone-symbolic",
                                  tooltip_text="Microphone level")
        meter.append(self.mic_icon)
        self.meter = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True,
                                  valign=Gtk.Align.CENTER)
        # one colour for the whole range (the default offsets colour a low level as a warning)
        for name in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_FULL):
            self.meter.remove_offset_value(name)
        self.meter.add_offset_value(Gtk.LEVEL_BAR_OFFSET_HIGH, 1.0)
        self.meter.update_property([Gtk.AccessibleProperty.LABEL], ["Microphone level"])
        meter.append(self.meter)
        body.append(meter)
        self.hint = Gtk.Label(wrap=True, xalign=0, visible=False,
                              label="No sound is coming from this microphone. It may be "
                                    "muted in your sound settings, or choose another one.")
        self.hint.add_css_class("caption")
        self.hint.add_css_class("warning")
        body.append(self.hint)

        # ---- devices and destination
        group = Adw.PreferencesGroup()
        self.cam_row = KeyCombo("Camera", [])
        self.size_row = KeyCombo("Resolution", [])
        self.mic_row = KeyCombo("Microphone", [])
        self.folder_row = Adw.ActionRow(title="Save to")
        self.folder_row.set_use_markup(False)
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose_folder())
        self.folder_row.add_suffix(choose)
        for row in (self.cam_row, self.size_row, self.mic_row, self.folder_row):
            group.add(row)
        self.cam_row.connect("notify::selected", lambda *_: self._camera_changed())
        self.size_row.connect("notify::selected", lambda *_: self._devices_changed())
        self.mic_row.connect("notify::selected", lambda *_: self._devices_changed())
        body.append(group)
        self._show_folder()

        tv.set_content(Gtk.ScrolledWindow(child=body, propagate_natural_height=True,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
        foot = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=12)
        self.rec_btn = Gtk.Button()
        self.rec_btn.add_css_class("pill")
        self.rec_btn.add_css_class("destructive-action")
        self.rec_content = Adw.ButtonContent(icon_name="media-record-symbolic", label="Record")
        self.rec_btn.set_child(self.rec_content)
        self.rec_btn.connect("clicked", lambda *_: self._toggle())
        foot.append(self.rec_btn)
        tv.add_bottom_bar(foot)
        self.set_child(tv)
        self.set_default_widget(self.rec_btn)
        self.set_focus(self.rec_btn)

        self.connect("close-attempt", lambda *_: self._close_attempt())
        self.connect("closed", lambda *_: self._on_closed())
        self._ticker = GLib.timeout_add(80, self._tick)
        self._sync()
        self._discover()

    # ================================================================== devices
    def _discover(self):
        self.state = "looking"
        self.wait_label.set_label("Looking for cameras…")
        self.view.set_visible_child_name("wait")
        self._sync()

        def work():
            cams, mics = recorder.cameras(), recorder.microphones()
            GLib.idle_add(self._discovered, cams, mics)
        threading.Thread(target=work, daemon=True).start()

    def _discovered(self, cams, mics):
        if self._closed:
            return False
        self.cams, self.mics = cams, mics
        if not cams:
            self._notice("camera-disabled-symbolic", "No camera found",
                         "Connect a webcam, then try again.", retry=self._discover)
            return False
        self._filling = True
        self.cam_row.set_entries([(c.device, c.name) for c in cams], keep=False)
        self.cam_row.set_key(self.settings.record_camera)
        self.mic_row.set_entries([(m.device, m.name + (" (default)" if m.default else ""))
                                  for m in mics] + [(NO_MIC, "None (video only)")], keep=False)
        if not self.mic_row.set_key(self.settings.record_mic or "") and mics:
            self.mic_row.set_key(mics[0].device)
        self._fill_sizes()
        self._filling = False
        self._preview()
        return False

    def _fill_sizes(self):
        cam = self._camera()
        sizes = cam.sizes if cam else []
        self.size_row.set_entries([(f"{w}x{h}", f"{w} × {h}") for w, h in sizes]
                                  or [("", "Camera default")], keep=False)
        if not self.size_row.set_key(self.settings.record_size) and cam and cam.default_size():
            w, h = cam.default_size()
            self.size_row.set_key(f"{w}x{h}")

    def _camera(self) -> Camera | None:
        key = self.cam_row.get_key()
        return next((c for c in self.cams if c.device == key), None)

    def _mic(self) -> Microphone | None:
        key = self.mic_row.get_key()
        return next((m for m in self.mics if m.device == key), None)

    def _size(self) -> tuple[int, int] | None:
        w, _, h = self.size_row.get_key().partition("x")
        return (int(w), int(h)) if w.isdigit() and h.isdigit() else None

    def _camera_changed(self):
        if self._filling or not self.cams:
            return
        self._filling = True
        self._fill_sizes()
        self._filling = False
        self._devices_changed()

    def _devices_changed(self):
        if self._filling or not self.cams:
            return
        s = self.settings
        s.record_camera = self.cam_row.get_key()
        s.record_size = self.size_row.get_key()
        s.record_mic = self.mic_row.get_key()
        s.save()
        if self.state in ("preview", "notice", "wait"):
            self._preview()

    # ================================================================== folder
    def folder(self) -> str:
        f = self.settings.record_folder
        return f if f and os.path.isdir(f) else videos_dir()

    def _show_folder(self):
        f = self.settings.record_folder
        sub = _home(self.folder())
        if f and not os.path.isdir(f):
            sub += f" ({_home(f)} is missing)"
        self.folder_row.set_subtitle(sub)

    def _choose_folder(self):
        dlg = Gtk.FileDialog(title="Save Recordings To", modal=True)
        if os.path.isdir(self.folder()):
            dlg.set_initial_folder(Gio.File.new_for_path(self.folder()))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path() and not self._closed:
                self.settings.record_folder = f.get_path()
                self.settings.save()
                self._show_folder()
        dlg.select_folder(self.win, None, done)

    # ================================================================== sessions
    def _preview(self):
        cam = self._camera()
        if cam is None:
            return
        if cam.problem:
            self._replace(None)
            self._notice("camera-disabled-symbolic", "Can't use this camera", cam.problem,
                         command=cam.fix, retry=self._discover)
            return
        self.state = "wait"
        self.wait_label.set_label("Starting the camera…")
        if self.view.get_visible_child_name() != "video" or not self.has_frame:
            self.view.set_visible_child_name("wait")
        self._sync()
        self._replace(recorder.command(cam, self._size(), self._mic()))

    def _replace(self, argv: list[str] | None):
        """Stop the running session (the camera can only be opened once), then start
        `argv`, unless another change came in meanwhile."""
        old, self.session = self.session, None
        self._gen += 1
        gen = self._gen

        def work():
            # one switch at a time: a quick second change must not open the camera
            # while the first change is still waiting for the old session to let go
            with self._switching:
                if old is not None:
                    old.kill()
                    old.wait(5)
                if argv is not None:
                    GLib.idle_add(self._launch, gen, argv)
        threading.Thread(target=work, daemon=True).start()

    def _launch(self, gen: int, argv: list[str]):
        if gen != self._gen or self._closed:
            return False
        self._started_at = self._heard_at = time.monotonic()
        self.session = Session(
            argv,
            on_frame=lambda buf: self._frame_arrived(gen, buf),
            on_level=lambda db: self._level_arrived(gen, db),
            on_exit=lambda rc, log: GLib.idle_add(self._exited, gen, rc, log))
        try:
            self.session.start()
        except OSError as e:
            self.session = None
            self._fail_recording()
            self._notice("dialog-error-symbolic", "Could not start ffmpeg", str(e),
                         retry=self._preview)
        return False

    # worker threads
    def _frame_arrived(self, gen: int, buf: bytes):
        if gen != self._gen:
            return
        self._frame = buf
        if not self._frame_pending:
            self._frame_pending = True
            GLib.idle_add(self._show_frame, gen)

    def _level_arrived(self, gen: int, db: float):
        if gen == self._gen:
            self._peak = max(self._peak, db)

    @property
    def has_frame(self) -> bool:
        return self.picture.get_paintable() is not self.blank

    # main loop
    def _show_frame(self, gen: int):
        self._frame_pending = False
        buf, self._frame = self._frame, None
        if gen != self._gen or self._closed or buf is None:
            return False
        tex = Gdk.MemoryTexture.new(PREVIEW_W, PREVIEW_H, Gdk.MemoryFormat.R8G8B8,
                                    GLib.Bytes.new(buf), PREVIEW_W * 3)
        self.picture.set_paintable(tex)
        if self.state == "wait":
            self.state = "preview"
        elif self.state == "starting":
            self.state = "recording"
            self._rec_since = time.monotonic()
        if self.view.get_visible_child_name() != "video":
            self.view.set_visible_child_name("video")
        self._sync()
        return False

    def _exited(self, gen: int, rc: int, log: list[str]):
        if gen != self._gen or self._closed:
            return False
        self.session = None
        if self.recording is not None:
            # stopped by the user, or the camera went away mid-take: either way, keep
            # whatever was recorded
            self.state = "saving"
            self._sync()
            rec, self.recording = self.recording, None
            self._saving = threading.Thread(
                target=lambda: GLib.idle_add(self._saved, rec.finish(log)), daemon=True)
            self._saving.start()
            return False
        text, fix = recorder.explain(log)
        self._notice("camera-disabled-symbolic", "The camera stopped", text, command=fix,
                     retry=self._preview)
        return False

    def _saved(self, saved: recorder.Saved):
        if self._closed:
            return False
        if not saved.ok:
            self._notice("dialog-error-symbolic", "Nothing was saved", saved.error,
                         retry=self._preview)
            return False
        self.win.add_recording(saved.path, saved.warnings)
        self.state = "done"
        self.force_close()
        return False

    # ================================================================== record / stop
    def _toggle(self):
        if self.state == "preview":
            self._start_recording()
        elif self.state in ("starting", "recording"):
            self._stop_recording()

    def _start_recording(self):
        cam, mic = self._camera(), self._mic()
        try:
            self.recording = Recording(self.folder(), mic)
        except OSError as e:
            self._notice("folder-symbolic", "Can't save to this folder",
                         f"{self.folder()}: {e.strerror or e}. Choose another folder.",
                         retry=self._preview)
            return
        self.state = "starting"
        self._sync()
        self._replace(recorder.command(cam, self._size(), mic, self.recording.tmp,
                                       self.win.ctx.encoders))

    def _stop_recording(self):
        self.state = "saving"
        self._sync()
        if self.session is not None:
            self.session.stop()        # on_exit saves the recording
        elif self.recording is not None:
            # still reopening the camera: nothing was recorded yet
            self._fail_recording()
            self._preview()

    def _fail_recording(self):
        if self.recording is not None:
            self.recording.discard()
            self.recording = None

    def _close_attempt(self):
        # closing mid-take keeps the take: stop, save, and close once it is in the library
        if self.state in ("starting", "recording"):
            self._stop_recording()

    def _on_closed(self):
        self._closed = True
        if self._ticker:
            GLib.source_remove(self._ticker)
            self._ticker = 0
        self._replace(None)
        self._fail_recording()

    def shutdown(self):
        """The app is quitting: finish and keep a recording in progress (synchronously),
        stop the preview."""
        self._closed = True
        self._gen += 1
        session, self.session = self.session, None
        rec, self.recording = self.recording, None
        if session is not None:
            if rec is not None:
                session.stop()
                session.wait(20)
            else:
                session.kill()
        if rec is not None:
            rec.finish(list(session.log) if session else [])
        if self._saving is not None:
            self._saving.join(20)     # a stopped take being checked and published

    # ================================================================== display
    def _notice(self, icon: str, title: str, body: str, command: str = "", retry=None):
        self.state = "notice"
        self.notice_icon.set_from_icon_name(icon)
        self.notice_title.set_label(title)
        self.notice_body.set_label(body + (f"\n\n{command}" if command else ""))
        self._notice_command = command
        self._notice_retry = retry
        self.copy_btn.set_label("Copy Command")
        self.copy_btn.set_visible(bool(command))
        self.retry_btn.set_visible(retry is not None)
        self.view.set_visible_child_name("notice")
        self.picture.set_paintable(self.blank)
        self._sync()

    def _copy(self):
        if self._notice_command:
            self.get_clipboard().set(self._notice_command)
            self.copy_btn.set_label("Copied")

    def _sync(self):
        st = self.state
        busy = st in ("starting", "recording", "saving")
        self.set_can_close(not busy)
        for row in (self.cam_row, self.size_row, self.mic_row, self.folder_row):
            row.set_sensitive(not busy and st != "looking")
        if st in ("starting", "recording"):
            self.rec_content.set_icon_name("media-playback-stop-symbolic")
            self.rec_content.set_label("Stop")
        elif st == "saving":
            self.rec_content.set_icon_name("document-save-symbolic")
            self.rec_content.set_label("Saving…")
        else:
            self.rec_content.set_icon_name("media-record-symbolic")
            self.rec_content.set_label("Record")
        self.rec_btn.set_sensitive(st in ("preview", "starting", "recording"))
        self.rec_btn.set_tooltip_text(
            "Stop and save the recording" if st in ("starting", "recording")
            else "Start recording")
        self.pill.set_visible(st in ("starting", "recording", "saving"))
        if st == "starting":
            self.clock.set_label("Starting…")
        has_mic = self._mic() is not None
        self.meter.set_sensitive(has_mic)
        self.mic_icon.set_from_icon_name("audio-input-microphone-symbolic" if has_mic
                                         else "microphone-disabled-symbolic")

    def _tick(self):
        if self._closed:
            return False
        now = time.monotonic()
        peak, self._peak = self._peak, SILENT_DB
        live = self.session is not None and self.state in ("preview", "recording")
        if live and peak > HEARD:
            self._heard_at = now
        target = max(0.0, min(1.0, (peak - METER_FLOOR) / -METER_FLOOR)) if live else 0.0
        # rise at once, fall gently, like a real meter
        self._shown = target if target > self._shown else max(target, self._shown - 0.04)
        self.meter.set_value(self._shown)
        silent = (live and self._mic() is not None
                  and now - max(self._heard_at, self._started_at) > SILENCE_HINT_AFTER)
        self.hint.set_visible(silent)
        if self.state == "recording":
            self.clock.set_label(_clock(now - self._rec_since))
        return True
