"""Install missing system packages from inside the app.

Nobody should have to leave Clipdeck, open a terminal and type an apt line to get a
preview playing. This module does the install for them and only falls back to showing
a command when the system offers no way to do it:

1. PackageKit (the desktop's own package service; Ubuntu, Fedora, openSUSE, Debian,
   Arch with packagekit). Resolves the packages, asks the desktop for the password with
   the normal system prompt, downloads and installs. GStreamer decoders are found by
   capability ("something that decodes video/x-h264"), so no package names to guess.
2. pkexec + the distro's package manager, when PackageKit is missing.
3. The exact command for the user to run, when neither exists.

All public callbacks run on the GTK main loop."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import Gio, GLib

from . import decoders


@dataclass
class Need:
    """What to install, described in enough ways for every backend."""
    title: str                              # "GStreamer decoders", "ffmpeg"
    provides: list[str] = field(default_factory=list)   # PackageKit capability queries
    names: dict[str, list[str]] = field(default_factory=dict)  # distro family -> packages
    prefer: tuple[str, ...] = ()            # pick these out of capability matches

    def packages_for(self, family: str) -> list[str]:
        return self.names.get(family) or self.names.get("debian") or []


DECODER_NAMES = {
    "debian": ["gstreamer1.0-libav", "gstreamer1.0-plugins-bad"],
    "fedora": ["gstreamer1-plugin-libav", "gstreamer1-plugins-bad-free"],
    "arch": ["gst-libav", "gst-plugins-bad"],
    "suse": ["gstreamer-plugins-libav", "gstreamer-plugins-bad"],
}
FFMPEG_NAMES = {"debian": ["ffmpeg"], "fedora": ["ffmpeg-free"], "arch": ["ffmpeg"],
                "suse": ["ffmpeg"]}


def decoder_need(missing: list[str]) -> Need:
    """The decoders for these ffprobe codec names (H.264 and AAC when nothing is known)."""
    provides = []
    for codec in missing or ["h264", "aac"]:
        caps = decoders.CODEC_CAPS.get(codec)
        if caps:
            media, _, fields = caps.partition(",")
            q = f"gstreamer1(decoder-{media})"
            if fields:
                q += "".join(f"({f})" for f in fields.split(","))
            provides.append(q)
    return Need("GStreamer decoders", provides=provides, names=DECODER_NAMES,
                prefer=("libav", "plugins-bad", "plugins-good", "plugins-ugly"))


def ffmpeg_need() -> Need:
    return Need("ffmpeg", names=FFMPEG_NAMES)


# ------------------------------------------------------------------ distro / fallbacks
def distro_family() -> str:
    ids = []
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k in ("ID", "ID_LIKE"):
                    ids += v.strip('"').split()
    except OSError:
        pass
    for fam, keys in (("debian", ("debian", "ubuntu", "linuxmint", "pop")),
                      ("fedora", ("fedora", "rhel", "centos", "nobara")),
                      ("arch", ("arch", "manjaro", "endeavouros")),
                      ("suse", ("suse", "opensuse", "opensuse-tumbleweed", "opensuse-leap"))):
        if any(i in keys for i in ids):
            return fam
    return ""


def package_manager() -> tuple[str, list[str]] | None:
    """(family, command prefix) of the first package manager found on PATH."""
    for fam, cmd in (("debian", ["apt-get", "install", "-y"]),
                     ("fedora", ["dnf", "install", "-y"]),
                     ("arch", ["pacman", "-S", "--noconfirm", "--needed"]),
                     ("suse", ["zypper", "--non-interactive", "install"])):
        if shutil.which(cmd[0]):
            return fam, cmd
    return None


def manual_command(need: Need) -> str:
    """What to type when the app cannot install it itself."""
    pm = package_manager()
    fam = distro_family() or (pm[0] if pm else "debian")
    pkgs = " ".join(need.packages_for(fam))
    if pm:
        return "sudo " + " ".join(c for c in pm[1] if c not in ("-y", "--noconfirm",
                                                               "--needed", "--non-interactive")) \
            + " " + pkgs
    return f"install these packages with your package manager: {pkgs}"


def refresh_runtime():
    """Make freshly installed plugins visible to this running process."""
    decoders.can_decode.cache_clear()
    Gst = decoders._gst()
    if Gst is not None:
        try:
            Gst.update_registry()
        except Exception:  # noqa: BLE001 - old registry stays, a restart still fixes it
            pass
        decoders.can_decode.cache_clear()


# --------------------------------------------------------------------------- installer
class Installer:
    """Runs one install. on_status(text, percent or -1) during; on_done(ok, detail) after."""

    def __init__(self, need: Need, on_status: Callable[[str, int], None],
                 on_done: Callable[[bool, str], None]):
        self.need = need
        self.on_status = on_status
        self.on_done = on_done
        self.cancellable = Gio.Cancellable()
        self._finished = False

    def cancel(self):
        self.cancellable.cancel()

    def start(self):
        if self._pk_start():
            return
        if self._pkexec_start():
            return
        self._done(False, "This system has neither PackageKit nor a package manager Clipdeck "
                          "knows how to drive.")

    def _done(self, ok: bool, detail: str):
        if self._finished:
            return
        self._finished = True
        if ok:
            refresh_runtime()
        GLib.idle_add(lambda: self.on_done(ok, detail) and False)

    def _status(self, text: str, percent: int = -1):
        GLib.idle_add(lambda: self.on_status(text, percent) and False)

    # ------------------------------------------------------------- PackageKit backend
    # The synchronous PackageKit calls run on a worker thread (PyGObject cannot marshal
    # the progress callback of the async variants). Every UI update goes through idle_add.
    def _pk(self):
        try:
            import gi
            gi.require_version("PackageKitGlib", "1.0")
            from gi.repository import PackageKitGlib as PK
            return PK
        except (ImportError, ValueError):
            return None

    def _pk_start(self) -> bool:
        PK = self._pk()
        if PK is None:
            return False
        self.PK = PK
        self.client = PK.Client()
        self.client.set_interactive(True)
        threading.Thread(target=self._pk_run, daemon=True).start()
        return True

    def _pk_run(self):
        try:
            reason = self._pk_work()
        except Exception as e:  # noqa: BLE001 - never leave the dialog spinning
            reason = f"{type(e).__name__}: {e}"
        if reason == "":
            return
        if reason == "cancelled":
            self._done(False, "cancelled")
        else:
            GLib.idle_add(lambda: self._pk_fallback(reason) and False)

    def _pk_progress(self, progress, kind, _data):
        PK = self.PK
        if kind in (PK.ProgressType.STATUS, PK.ProgressType.PERCENTAGE):
            status = progress.get_status()
            text = {PK.StatusEnum.WAIT: "Waiting for the package service…",
                    PK.StatusEnum.WAITING_FOR_AUTH: "Waiting for your password…",
                    PK.StatusEnum.DEP_RESOLVE: "Working out what to install…",
                    PK.StatusEnum.DOWNLOAD: "Downloading…",
                    PK.StatusEnum.INSTALL: "Installing…",
                    PK.StatusEnum.SETUP: "Preparing…",
                    PK.StatusEnum.RUNNING: "Running…",
                    PK.StatusEnum.FINISHED: "Finishing…",
                    PK.StatusEnum.QUERY: "Looking up packages…",
                    }.get(status, "Working…")
            pct = progress.get_percentage()
            self._status(text, pct if 0 <= pct <= 100 else -1)

    def _pk_check(self, res) -> str:
        """'' when the transaction succeeded, else why not ('cancelled' for the user's No)."""
        PK = self.PK
        if res.get_exit_code() == PK.ExitEnum.CANCELLED or self.cancellable.is_cancelled():
            return "cancelled"
        err = res.get_error_code()
        if err is not None:
            if err.get_code() == PK.ErrorEnum.NOT_AUTHORIZED:
                return "cancelled"
            return err.get_details() or err.get_code().value_nick
        if res.get_exit_code() != PK.ExitEnum.SUCCESS:
            return res.get_exit_code().value_nick
        return ""

    def _pk_query(self, by_capability: bool):
        """Packages that satisfy the need, or (None, reason)."""
        PK = self.PK
        flt = (1 << PK.FilterEnum.NEWEST) | (1 << PK.FilterEnum.ARCH)
        try:
            if by_capability:
                res = self.client.what_provides(flt, self.need.provides, self.cancellable,
                                                self._pk_progress, None)
            else:
                res = self.client.resolve(flt, self.need.packages_for(distro_family()),
                                          self.cancellable, self._pk_progress, None)
        except GLib.Error as e:
            return None, ("cancelled" if self.cancellable.is_cancelled() else e.message)
        reason = self._pk_check(res)
        if reason:
            return None, reason
        pkgs = res.get_package_array()
        if by_capability:
            wanted = [p for p in pkgs if any(k in p.get_name() for k in self.need.prefer)]
            if not wanted:   # unfamiliar packaging: take whatever provides the decoder
                seen, wanted = set(), []
                for p in pkgs:
                    if p.get_name() not in seen:
                        seen.add(p.get_name())
                        wanted.append(p)
            pkgs = wanted
        return pkgs, ""

    def _pk_work(self) -> str:
        """Resolve then install. Returns '' on success, 'cancelled', or a reason."""
        PK = self.PK
        self._status("Looking up packages…")
        pkgs, reason = None, ""
        if self.need.provides:
            pkgs, reason = self._pk_query(True)
            if reason == "cancelled":
                return reason
        if not pkgs:     # no capability search on this backend, or nothing matched
            pkgs, reason = self._pk_query(False)
            if reason:
                return reason
        if not pkgs:
            return "no package in your distribution's repositories provides it"
        ids = [p.get_id() for p in pkgs if p.get_info() != PK.InfoEnum.INSTALLED]
        if not ids:
            self._done(True, "already installed")
            return ""
        self.pending = sorted({i.split(";")[0] for i in ids})
        self._status("Waiting for your password…")
        reason = self._pk_install(ids)
        if reason:
            return reason
        self._done(True, "installed " + ", ".join(self.pending))
        return ""

    def _pk_install(self, ids: list[str]) -> str:
        PK = self.PK
        try:
            res = self.client.install_packages(PK.TransactionFlagEnum.NONE, ids,
                                               self.cancellable, self._pk_progress, None)
        except GLib.Error as e:
            return "cancelled" if self.cancellable.is_cancelled() else e.message
        return self._pk_check(res)

    def _pk_fallback(self, reason: str):
        """PackageKit could not do it: try pkexec, else report with the manual command."""
        if not self._pkexec_start():
            self._done(False, reason)

    # ----------------------------------------------------------------- pkexec backend
    def _pkexec_start(self) -> bool:
        pm = package_manager()
        if not shutil.which("pkexec") or pm is None:
            return False
        fam, cmd = pm
        pkgs = self.need.packages_for(fam)
        if not pkgs:
            return False
        self.pending = pkgs
        self._status("Waiting for your password…")
        threading.Thread(target=self._pkexec_run, args=(["pkexec"] + cmd + pkgs,),
                         daemon=True).start()
        return True

    def _pkexec_run(self, argv):
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env)
        except OSError as e:
            return self._done(False, str(e))
        self._status("Installing…")
        tail = []
        for line in proc.stdout:
            tail = (tail + [line.rstrip()])[-6:]
            if self.cancellable.is_cancelled():
                proc.terminate()
        rc = proc.wait()
        if rc == 0:
            self._done(True, "installed " + ", ".join(self.pending))
        elif rc in (126, 127):   # polkit: dismissed or not authorised
            self._done(False, "cancelled")
        else:
            self._done(False, "\n".join(tail) or f"the package manager exited with code {rc}")
