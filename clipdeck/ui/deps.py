"""Guided installation of what Clipdeck needs: a dialog explains the problem, the app
installs the fix with the system's own password prompt, and a command is shown only
when the system offers no way to install from an app.

Adw.AlertDialog closes itself before it emits "response", so each stage is its own
dialog: offer -> progress -> (failure with the command)."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GLib, Gtk

from .. import decoders, installer
from ..media import have_tools


class DependencyGuide:
    def __init__(self, win):
        self.win = win
        self.dialog: Adw.AlertDialog | None = None     # whichever stage is on screen
        self.installer: installer.Installer | None = None

    # ------------------------------------------------------------------ entry points
    def offer_decoders(self, missing: list[str] | None = None):
        missing = missing or decoders.common_missing() or ["h264", "aac"]
        what = decoders.describe(missing)
        self._offer(
            heading="Video previews need a decoder",
            body=(f"ffmpeg is installed, so fixing and converting work. But previews play "
                  f"through GStreamer, and this system has no GStreamer decoder for {what}."
                  "\n\nClipdeck can install it now. Your system will ask for your password."),
            need=installer.decoder_need(missing),
            success="Decoders installed. Previews play now.",
            after=self._after_decoders)

    def offer_ffmpeg(self):
        self._offer(
            heading="ffmpeg is not installed",
            body=("Clipdeck uses ffmpeg and ffprobe for everything: inspecting, fixing and "
                  "converting. Nothing works until they are installed."
                  "\n\nClipdeck can install them now. Your system will ask for your password."),
            need=installer.ffmpeg_need(),
            success="ffmpeg installed. You can start adding videos.",
            after=self._after_ffmpeg)

    # ------------------------------------------------------------------ stage: offer
    def _offer(self, heading: str, body: str, need: installer.Need, success: str,
               after: Callable[[], None]):
        if self.dialog is not None or self.installer is not None:
            return          # already on screen (startup check + player button)
        self.heading, self.need, self.success, self.after = heading, need, success, after
        self.manual = installer.manual_command(need)
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("later", "Not now")
        dlg.add_response("install", "Install now")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("install")
        dlg.set_close_response("later")
        dlg.connect("response", lambda _d, resp: resp == "install" and self._install())
        self._present(dlg)

    def _present(self, dlg: Adw.AlertDialog):
        self.dialog = dlg
        dlg.connect("closed", self._closed)
        dlg.present(self.win)

    def _closed(self, dlg):
        if self.dialog is dlg:
            self.dialog = None

    # --------------------------------------------------------------- stage: progress
    def _install(self):
        dlg = Adw.AlertDialog(heading=self.heading,
                              body="Installing. This can take a minute on a slow connection.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6)
        self.progress = Gtk.ProgressBar(show_text=False)
        self.status = Gtk.Label(xalign=0.5, wrap=True, label="Starting…")
        self.status.add_css_class("dim-label")
        box.append(self.progress)
        box.append(self.status)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._cancel)
        self._present(dlg)
        self.installer = installer.Installer(self.need, self._status, self._done)
        self.installer.start()
        GLib.timeout_add(150, self._tick)

    def _tick(self):
        if self.installer is None or self.dialog is None:
            return False
        if self.progress.get_fraction() == 0:
            self.progress.pulse()
        return True

    def _cancel(self, _dlg, _resp):
        if self.installer is not None:
            self.installer.cancel()
            self.win.toast("Installation cancelled.")

    def _status(self, text: str, percent: int):
        if self.dialog is None or self.installer is None:
            return
        self.status.set_label(text)
        self.progress.set_fraction(percent / 100 if percent >= 0 else 0)

    def _done(self, ok: bool, detail: str):
        cancelled_by_user = self.installer is None or detail == "cancelled"
        self.installer = None
        if self.dialog is not None:
            self.dialog.force_close()
        if ok:
            self.win.toast(self.success)
            self.after()
        elif cancelled_by_user:
            self._failure("Installation was cancelled. You can try again, or run this "
                          "command in a terminal and restart Clipdeck:")
        else:
            self._failure(f"Clipdeck could not install it automatically ({detail}).\n\n"
                          "Run this command in a terminal, then restart Clipdeck:")

    # ---------------------------------------------------------------- stage: failure
    def _failure(self, body: str):
        dlg = Adw.AlertDialog(heading=self.heading, body=body)
        cmd = Gtk.Label(label=self.manual, selectable=True, wrap=True, xalign=0.5,
                        margin_top=6)
        cmd.add_css_class("monospace")
        dlg.set_extra_child(cmd)
        dlg.add_response("close", "Close")
        dlg.add_response("copy", "Copy command")
        dlg.add_response("retry", "Try again")
        dlg.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("retry")
        dlg.set_close_response("close")

        def on_response(_d, resp):
            if resp == "copy":
                self.win.get_clipboard().set(self.manual)
                self.win.toast("Command copied. Paste it into a terminal, then restart Clipdeck.")
            elif resp == "retry":
                self._install()
        dlg.connect("response", on_response)
        self._present(dlg)

    # -------------------------------------------------------------- after a success
    def _after_decoders(self):
        if decoders.common_missing():
            # the registry did not pick the plugin up live: say so instead of pretending
            self.win.toast("Decoders installed. Restart Clipdeck to play previews.")
            return
        for p in self.win.pages:
            player = getattr(p, "player", None)
            if player is not None and player.path:
                player.reload()

    def _after_ffmpeg(self):
        if not have_tools():
            self.win.toast("ffmpeg was installed but is not on PATH yet. Restart Clipdeck.")
            return
        for it in self.win.library.items():
            self.win.library.refresh(it)
        import threading
        threading.Thread(target=self.win._probe_encoders, daemon=True).start()
