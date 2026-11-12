"""Recording from a camera and microphone with ffmpeg.

One ffmpeg process does everything for the recorder window: it opens the camera and the
microphone, sends small preview frames down stdout and microphone levels down stderr, and
while recording also encodes the full-size picture and sound into a hidden work file.
Stopping lets ffmpeg finish the file; it is then checked and published under its final
name (never over an existing file), exactly like a job's output."""

from __future__ import annotations

import glob
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import runner
from .media import ProbeError, probe
from .ops import temp_for

# the preview feed: small, letterboxed into a fixed box so its size is always known
PREVIEW_W, PREVIEW_H, PREVIEW_FPS = 640, 360, 15
FRAME_BYTES = PREVIEW_W * PREVIEW_H * 3          # rgb24
RATE = 30                                         # what we ask the camera for
SILENT_DB = -120.0                                # what a level of "-inf" is reported as

# what a camera sends, best first: MJPEG reaches full size at full rate over USB 2
_FORMAT_ORDER = ("mjpeg", "yuyv422", "nv12", "yuv420p", "h264")
PREFERRED_SIZES = ((1920, 1080), (1280, 720))

GROUP_FIX = "sudo usermod -aG video $USER"


@dataclass(frozen=True)
class Camera:
    name: str
    device: str
    modes: tuple = ()           # ((width, height, pixel format), ...), largest first
    problem: str = ""           # why it cannot be used, in plain words
    fix: str = ""               # the command that fixes `problem`, if there is one
    fake: str = ""              # a lavfi source instead of a device (tests, demos)

    @property
    def sizes(self) -> list[tuple[int, int]]:
        return [(w, h) for w, h, _ in self.modes]

    def default_size(self) -> tuple[int, int] | None:
        for s in PREFERRED_SIZES:
            if s in self.sizes:
                return s
        return self.sizes[0] if self.sizes else None

    @property
    def absolute(self) -> bool:
        """Its timestamps are wall-clock time (see `command`)."""
        return not self.fake

    def input_args(self, size: tuple[int, int] | None) -> list[str]:
        if self.fake:
            w, h = size or (1280, 720)
            return ["-re", "-f", "lavfi", "-i", self.fake.format(w=w, h=h, rate=RATE)]
        # mono2abs: kernel frame times converted to wall-clock time, the clock the
        # microphone is stamped with, so picture and sound line up
        a = ["-f", "v4l2", "-ts", "mono2abs", "-thread_queue_size", "512"]
        fmt = next((f for w, h, f in self.modes if (w, h) == size), "")
        if fmt:
            a += ["-input_format", fmt]
        if size:
            a += ["-video_size", f"{size[0]}x{size[1]}"]
        return a + ["-framerate", str(RATE), "-i", self.device]


@dataclass(frozen=True)
class Microphone:
    name: str
    device: str
    backend: str = "pulse"      # pulse | alsa | lavfi
    default: bool = False

    @property
    def absolute(self) -> bool:
        return self.backend != "lavfi"

    def input_args(self) -> list[str]:
        if self.backend == "lavfi":
            return ["-re", "-f", "lavfi", "-i", self.device]
        return ["-f", self.backend, "-thread_queue_size", "512", "-i", self.device]


# ----------------------------------------------------------------------------- discovery

def _run(argv: list[str], timeout: float = 10) -> str:
    try:
        res = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                             timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout + res.stderr


def parse_formats(text: str) -> tuple:
    """`ffmpeg -f v4l2 -list_formats all` output -> modes, largest first, each size with
    the best pixel format the camera offers for it."""
    best: dict[tuple[int, int], str] = {}
    for line in text.splitlines():
        m = re.search(r"\]\s*(?:Compressed|Raw)\s*:\s*(\S+)\s*:(.*)$", line)
        if not m:
            continue
        fmt = m.group(1)
        rank = _FORMAT_ORDER.index(fmt) if fmt in _FORMAT_ORDER else len(_FORMAT_ORDER)
        for w, h in re.findall(r"\b(\d+)x(\d+)\b", m.group(2)):
            size = (int(w), int(h))
            old = best.get(size)
            old_rank = (_FORMAT_ORDER.index(old) if old in _FORMAT_ORDER
                        else len(_FORMAT_ORDER)) if old else None
            if old is None or rank < old_rank:
                best[size] = fmt
    return tuple((w, h, f) for (w, h), f in
                 sorted(best.items(), key=lambda kv: (kv[0][0] * kv[0][1], kv[0][0]),
                        reverse=True))


def _device_name(dev: str) -> str:
    try:
        with open(f"/sys/class/video4linux/{os.path.basename(dev)}/name",
                  encoding="utf-8", errors="replace") as f:
            name = f.read().strip()
    except OSError:
        name = ""
    # "USB 2.0 Camera: USB Camera" -> keep it, but drop an exact repeat ("Cam: Cam")
    head, sep, tail = name.partition(": ")
    if sep and head == tail:
        name = head
    return name or os.path.basename(dev)


def cameras() -> list[Camera]:
    """Cameras that can capture. Metadata nodes (a second /dev/video per webcam) are left
    out; a camera this user may not open is kept, with the reason and the fix."""
    def num(p):
        m = re.search(r"(\d+)$", p)
        return int(m.group(1)) if m else 0
    found = []
    for dev in sorted(glob.glob("/dev/video*"), key=num):
        text = _run(["ffmpeg", "-hide_banner", "-f", "v4l2", "-list_formats", "all",
                     "-i", dev])
        modes = parse_formats(text)
        name = _device_name(dev)
        if modes:
            found.append(Camera(name, dev, modes))
        elif "Permission denied" in text or not os.access(dev, os.R_OK | os.W_OK):
            found.append(Camera(name, dev, problem=(
                "Clipdeck is not allowed to use this camera. Add yourself to the “video” "
                "group, then log out and back in."), fix=GROUP_FIX))
    names = [c.name for c in found]
    return [Camera(f"{c.name} ({os.path.basename(c.device)})", c.device, c.modes, c.problem,
                   c.fix) if names.count(c.name) > 1 else c for c in found]


def parse_sources(text: str) -> list[Microphone]:
    """`ffmpeg -sources pulse` output -> microphones, the system default first.
    Monitors (what the speakers play) are not microphones."""
    mics = []
    for line in text.splitlines():
        m = re.match(r"^(\*)?\s+(\S+)\s+\[(.*)\]", line)
        if not m or m.group(2).endswith(".monitor"):
            continue
        mics.append(Microphone(m.group(3) or m.group(2), m.group(2), "pulse",
                               default=bool(m.group(1))))
    mics.sort(key=lambda mic: not mic.default)
    return mics


def microphones() -> list[Microphone]:
    mics = parse_sources(_run(["ffmpeg", "-hide_banner", "-sources", "pulse"]))
    if mics:
        return mics
    # no PipeWire/PulseAudio server to ask: ALSA's default device still works
    return [Microphone("Default microphone", "default", "alsa", default=True)]


# ----------------------------------------------------------------------------- command

def video_encoder_args(encoders: set[str]) -> list[str]:
    """Fast enough to keep up live; H.264 so the file plays and edits everywhere."""
    if "libx264" in encoders:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    if "h264_nvenc" in encoders:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23"]
    return ["-c:v", "mpeg4", "-q:v", "3"]       # built into every ffmpeg


def command(camera: Camera, size: tuple[int, int] | None, mic: Microphone | None,
            output: str = "", encoders: set[str] = frozenset({"libx264"}),
            origin: float | None = None) -> list[str]:
    """Preview (frames on stdout, levels on stderr), and with `output` also record.

    Camera and microphone stamp their data with wall-clock time; -copyts keeps those
    stamps and both streams are shifted by the same `origin`, so they stay in sync
    however long each device takes to start (ffmpeg would otherwise start each input
    at zero on its own)."""
    origin = time.time() if origin is None else origin
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error"]
    argv += camera.input_args(size)
    if mic is not None:
        argv += mic.input_args()
    argv += ["-copyts"]
    shift = f"setpts=PTS-{origin:.6f}/TB," if camera.absolute else ""
    graph = [f"[0:v]{shift}split=2[v][pv]" if output else f"[0:v]{shift}null[pv]",
             # mirrored, like looking in a mirror; the recording is not
             f"[pv]fps={PREVIEW_FPS},hflip,scale={PREVIEW_W}:{PREVIEW_H}:"
             f"force_original_aspect_ratio=decrease,pad={PREVIEW_W}:{PREVIEW_H}:-1:-1,"
             "format=rgb24[pvo]"]
    if output:
        graph.append(f"[v]fps={RATE},format=yuv420p[rec]")
    if mic is not None:
        # the meter sits in the sound's own path: ffmpeg only runs filters an output reads
        ashift = f"asetpts=PTS-{origin:.6f}/TB," if mic.absolute else ""
        graph.append(f"[1:a]{ashift}astats=metadata=1:reset=1,ametadata=mode=print:"
                     r"key=lavfi.astats.Overall.Peak_level:file=pipe\\:2:direct=1[a]")
    argv += ["-filter_complex", ";".join(graph)]
    if output:
        argv += ["-map", "[rec]"] + video_encoder_args(set(encoders))
        if mic is not None:
            argv += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        argv += ["-avoid_negative_ts", "make_zero", "-f", "mp4", output]
    elif mic is not None:
        argv += ["-map", "[a]", "-f", "null", "-"]
    return argv + ["-map", "[pvo]", "-f", "rawvideo", "pipe:1"]


# ----------------------------------------------------------------------------- running

def explain(log: list[str]) -> tuple[str, str]:
    """ffmpeg's last words -> (what went wrong in plain words, a command that fixes it)."""
    text = "\n".join(log)
    if "Device or resource busy" in text:
        return ("The camera is in use by another app. Close that app (a video call, "
                "another recorder), then try again.", "")
    if "Permission denied" in text and "/dev/video" in text:
        return ("Clipdeck is not allowed to use the camera. Add yourself to the “video” "
                "group, then log out and back in.", GROUP_FIX)
    if "No such file or directory" in text and "/dev/video" in text:
        return "The camera was disconnected. Plug it back in and refresh.", ""
    if "pa_context_connect" in text or "Connection refused" in text:
        return ("The sound server (PipeWire or PulseAudio) is not responding. Choose another "
                "microphone, or record without one.", "")
    if "No such entity" in text or ("Invalid argument" in text and "pulse" in text):
        return "That microphone is gone. Refresh and choose another one.", ""
    lines = [ln for ln in log if ln.strip()]
    return (lines[-1] if lines else "ffmpeg stopped unexpectedly."), ""


class Session:
    """One running ffmpeg. Callbacks arrive on worker threads:
    on_frame(bytes) with one rgb24 PREVIEW_W x PREVIEW_H picture, on_level(dBFS peak),
    on_exit(returncode, log lines)."""

    def __init__(self, argv: list[str], on_frame: Callable[[bytes], None],
                 on_level: Callable[[float], None], on_exit: Callable[[int, list[str]], None]):
        self.argv = argv
        self.on_frame, self.on_level, self.on_exit = on_frame, on_level, on_exit
        self.log: deque[str] = deque(maxlen=40)
        self.frames = 0
        self.proc: subprocess.Popen | None = None
        self.stopping = False

    def start(self):
        self.proc = runner.popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL)
        self._err = threading.Thread(target=self._read_err, daemon=True)
        self._err.start()
        threading.Thread(target=self._read_frames, daemon=True).start()

    def _read_frames(self):
        out = self.proc.stdout
        while True:
            buf = out.read(FRAME_BYTES)
            if not buf or len(buf) < FRAME_BYTES:
                break
            self.frames += 1
            self.on_frame(buf)
        rc = self.proc.wait()
        self._err.join(2)
        runner._close(self.proc)
        self.on_exit(rc, list(self.log))

    def _read_err(self):
        key = "lavfi.astats.Overall.Peak_level="
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith(key):
                try:
                    db = float(line[len(key):])
                except ValueError:
                    continue
                self.on_level(db if db > SILENT_DB else SILENT_DB)   # "-inf": silence
            elif line and not line.startswith("frame:"):
                self.log.append(line)

    def stop(self):
        """Let ffmpeg finish: it writes the end of the file and exits (on_exit follows)."""
        if self.proc is not None and self.proc.poll() is None and not self.stopping:
            self.stopping = True
            self.proc.send_signal(signal.SIGINT)
            # a stuck device can ignore it; a hard stop still beats hanging forever
            t = threading.Timer(15, self._force)
            t.daemon = True
            t.start()

    def _force(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    def kill(self):
        """Stop at once (a preview: there is nothing to finish)."""
        self.stopping = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def wait(self, timeout: float | None = None) -> bool:
        if self.proc is None:
            return True
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False


# ----------------------------------------------------------------------------- saving

def recording_name(when: datetime | None = None) -> str:
    return f"Recording {(when or datetime.now()):%Y-%m-%d %H-%M-%S}.mp4"


@dataclass
class Saved:
    ok: bool
    path: str = ""                 # where the recording now is
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    duration: float = 0.0


class Recording:
    """A recording's work file: listed in the in-progress journal while it is written
    (a crash cannot leave it behind), checked, then published under its final name."""

    def __init__(self, folder: str, mic: Microphone | None, name: str | None = None):
        os.makedirs(folder, exist_ok=True)
        self.final = os.path.join(folder, name or recording_name())
        self.tmp = temp_for(self.final)
        self.mic = mic
        runner._journal(add=[self.tmp])

    def finish(self, log: list[str]) -> Saved:
        """Check the work file and publish it. Footage is never thrown away because a
        detail is off: a recording without sound is still saved, with a warning."""
        try:
            try:
                info = probe(self.tmp)
            except (ProbeError, OSError):
                info = None
            if info is None or not info.has_video or info.duration <= 0:
                reason = explain(log)[0] if log else "No picture was recorded."
                return Saved(False, error=reason)
            warnings = []
            if self.mic is not None and not info.has_audio:
                warnings.append("Saved without sound: the microphone sent no audio.")
            try:
                path = runner.publish(self.tmp, self.final)
            except OSError as e:
                return Saved(False, error=f"Could not save the recording: {e.strerror or e}")
            return Saved(True, path, warnings=warnings, duration=info.duration)
        finally:
            runner._remove(self.tmp)
            runner._journal(remove=[self.tmp])

    def discard(self):
        runner._remove(self.tmp)
        runner._journal(remove=[self.tmp])
