"""Probing media files with ffprobe and diagnosing what is wrong with them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction

# containers that editors and phones all agree on
FRIENDLY_CONTAINERS = {"mp4", "mov", "m4v"}
# video/audio codecs with broad player support inside MP4
FRIENDLY_VCODECS = {"h264", "hevc", "av1"}
FRIENDLY_ACODECS = {"aac", "mp3"}

VIDEO_EXTS = {
    "mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "mts", "m2ts",
    "mpg", "mpeg", "3gp", "ogv", "gif", "vob", "mxf", "dv",
}
AUDIO_EXTS = {"mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "wma", "aiff", "aif"}


class ProbeError(Exception):
    pass


def have_tools() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rate(text) -> float:
    """'30000/1001' -> 29.97; '0/0' or junk -> 0."""
    try:
        f = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return float(f) if f > 0 else 0.0


def _rotation(stream: dict) -> int:
    """Counter-clockwise display rotation in degrees, normalized to -90/90/180/0."""
    rot = 0.0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = _num(sd["rotation"])
            break
    else:
        rot = _num((stream.get("tags") or {}).get("rotate"), 0.0)
        rot = -rot  # legacy 'rotate' tag is clockwise
    return normalize_rotation(int(round(rot)))


def normalize_rotation(deg: int) -> int:
    """Map any multiple of 90 to one of 0, 90, 180, -90."""
    d = int(deg) % 360
    return {0: 0, 90: 90, 180: 180, 270: -90}.get(d, 0)


@dataclass
class AudioStream:
    index: int
    codec: str
    channels: int
    sample_rate: int
    bitrate: int
    language: str = ""


@dataclass
class MediaInfo:
    path: str
    container: str            # lowercase file extension
    format_name: str
    duration: float
    size: int
    bitrate: int
    vcodec: str = ""
    vprofile: str = ""
    width: int = 0            # stored pixels
    height: int = 0
    sar: Fraction = Fraction(1)
    pix_fmt: str = ""
    fps: float = 0.0          # average frame rate
    r_fps: float = 0.0        # container's nominal rate
    nb_frames: int = 0        # 0 when unknown
    rotation: int = 0         # ccw degrees from the display matrix
    field_order: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    bit_depth: int = 8
    audio: list[AudioStream] = field(default_factory=list)
    subtitle_count: int = 0
    is_image_seq: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)

    @property
    def acodec(self) -> str:
        return self.audio[0].codec if self.audio else ""

    @property
    def display_size(self) -> tuple[int, int]:
        """Upright size as players show it (rotation and pixel aspect applied)."""
        w = self.width
        if self.sar and self.sar != 1:
            w = int(round(self.width * self.sar))
        h = self.height
        if self.rotation in (90, -90):
            return h, w
        return w, h

    @property
    def is_vfr(self) -> bool:
        if not (self.fps and self.r_fps):
            return False
        if self.is_interlaced and abs(self.r_fps - 2 * self.fps) / self.r_fps < 0.01:
            return False      # field rate vs frame rate, not variable
        return abs(self.fps - self.r_fps) / self.r_fps > 0.01

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in ("smpte2084", "arib-std-b67")

    @property
    def is_interlaced(self) -> bool:
        return self.field_order in ("tt", "bb", "tb", "bt")

    def issues(self) -> list["Issue"]:
        return diagnose(self)


@dataclass
class Issue:
    key: str
    title: str
    detail: str
    fixable: bool = True       # handled by Auto-fix


def diagnose(info: MediaInfo) -> list[Issue]:
    out: list[Issue] = []
    if not info.has_video:
        return out
    if info.rotation:
        out.append(Issue(
            "sideways", "Stored sideways",
            f"Pixels are stored {info.width}×{info.height} with a {abs(info.rotation)}° "
            "rotation flag. Players that honor the flag look fine; editors like "
            "DaVinci Resolve ignore it and show the clip on its side."))
    if info.container not in FRIENDLY_CONTAINERS:
        out.append(Issue(
            "container", f".{info.container} container",
            "Many editors and phones cannot open this container. Converting to MP4 is "
            "usually lossless (the streams are copied)."))
    if info.vcodec not in FRIENDLY_VCODECS:
        out.append(Issue(
            "vcodec", f"{info.vcodec} video",
            "This video codec is poorly supported outside desktop players and is a "
            "common cause of green or garbled frames. It will be re-encoded to H.264."))
    odd_audio = [a.codec for a in info.audio if a.codec not in FRIENDLY_ACODECS]
    if odd_audio and info.container != "mov":
        out.append(Issue(
            "acodec", f"{odd_audio[0]} audio",
            "This audio codec is often silent or rejected in MP4 players. It will be "
            "converted to AAC."))
    if info.is_vfr:
        out.append(Issue(
            "vfr", "Variable frame rate",
            f"Average {info.fps:.3f} fps vs nominal {info.r_fps:.3f} fps. Editors can "
            "drift audio out of sync on VFR clips; enable “Constant frame rate” in "
            "Auto-fix to resolve.", fixable=False))
    if info.is_interlaced:
        out.append(Issue(
            "interlaced", "Interlaced",
            "Shows combing on progressive screens. Use Edit → Deinterlace.",
            fixable=False))
    w, h = info.width, info.height
    if w % 2 or h % 2:
        out.append(Issue(
            "odd", "Odd dimensions",
            f"{w}×{h} cannot be encoded to H.264 4:2:0 as-is; Quick Fix rounds it to even."))
    return out


def probe(path: str, timeout: float = 30) -> MediaInfo:
    if not os.path.isfile(path):
        raise ProbeError(f"Not a file: {path}")
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError("ffprobe timed out")
    if res.returncode != 0:
        msg = (res.stderr or "").strip().splitlines()
        text = msg[-1] if msg else "ffprobe failed"
        text = text.removeprefix(path + ": ").removeprefix(os.path.abspath(path) + ": ")
        raise ProbeError(text[:1].upper() + text[1:])
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        raise ProbeError("ffprobe returned unreadable output")

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    info = MediaInfo(
        path=os.path.abspath(path),
        container=ext,
        format_name=fmt.get("format_name", ""),
        duration=_num(fmt.get("duration")),
        size=int(_num(fmt.get("size"), os.path.getsize(path))),
        bitrate=int(_num(fmt.get("bit_rate"))),
    )
    for s in streams:
        kind = s.get("codec_type")
        disp = s.get("disposition") or {}
        if kind == "video" and not info.vcodec and not disp.get("attached_pic"):
            info.vcodec = s.get("codec_name", "") or ""
            info.vprofile = s.get("profile", "") or ""
            info.width = int(_num(s.get("width")))
            info.height = int(_num(s.get("height")))
            sar = s.get("sample_aspect_ratio", "1:1")
            try:
                num, den = (int(x) for x in sar.split(":"))
                info.sar = Fraction(num, den) if num > 0 and den > 0 else Fraction(1)
            except (ValueError, AttributeError):
                info.sar = Fraction(1)
            info.pix_fmt = s.get("pix_fmt", "") or ""
            info.fps = _rate(s.get("avg_frame_rate"))
            info.r_fps = _rate(s.get("r_frame_rate"))
            info.nb_frames = int(_num(s.get("nb_frames")))
            info.rotation = _rotation(s)
            info.field_order = s.get("field_order", "") or ""
            info.color_primaries = s.get("color_primaries", "") or ""
            info.color_transfer = s.get("color_transfer", "") or ""
            info.color_space = s.get("color_space", "") or ""
            info.color_range = s.get("color_range", "") or ""
            depth = s.get("bits_per_raw_sample")
            if depth:
                info.bit_depth = int(_num(depth, 8))
            elif "10" in info.pix_fmt:
                info.bit_depth = 10
            if not info.duration:
                info.duration = _num(s.get("duration"))
        elif kind == "audio":
            info.audio.append(AudioStream(
                index=int(s.get("index", 0)),
                codec=s.get("codec_name", "") or "",
                channels=int(_num(s.get("channels"))),
                sample_rate=int(_num(s.get("sample_rate"))),
                bitrate=int(_num(s.get("bit_rate"))),
                language=(s.get("tags") or {}).get("language", ""),
            ))
        elif kind == "subtitle":
            info.subtitle_count += 1
    if not info.vcodec and not info.audio:
        raise ProbeError("No audio or video streams found")
    if not info.duration and not (info.vcodec in ("png", "mjpeg", "bmp", "webp", "tiff")
                                  and len(streams) == 1):
        # live/streamed recordings (OBS, browser MediaRecorder) carry no duration:
        # measure it by reading the packets (fast, nothing is decoded)
        info.duration = _scan_duration(path, timeout)
    if info.vcodec in ("png", "mjpeg", "bmp", "webp", "tiff") and info.duration == 0:
        info.is_image_seq = True
    return info


def _scan_duration(path: str, timeout: float) -> float:
    try:
        res = subprocess.run(["ffmpeg", "-v", "quiet", "-nostdin", "-i", path, "-map", "0",
                              "-c", "copy", "-f", "null", "-progress", "pipe:1", "-"],
                             capture_output=True, text=True, timeout=max(timeout, 120))
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    us = [line.split("=", 1)[1] for line in res.stdout.splitlines()
          if line.startswith("out_time_us=")]
    try:
        return max(0.0, int(us[-1]) / 1e6) if us else 0.0
    except ValueError:
        return 0.0


def rotation_flag(path: str) -> int:
    """Just the rotation flag of the first video stream (0 when absent)."""
    try:
        return probe(path).rotation
    except ProbeError:
        return 0


def fmt_duration(sec: float) -> str:
    if sec <= 0:
        return "0:00"
    sec_i = int(sec)
    h, rem = divmod(sec_i, 3600)
    m, s = divmod(rem, 60)
    frac = sec - sec_i
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    if sec < 10:
        return f"{m}:{s:02d}.{int(frac * 10)}"
    return f"{m}:{s:02d}"


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_bitrate(bps: int) -> str:
    if bps <= 0:
        return "—"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mb/s"
    return f"{bps / 1000:.0f} kb/s"


def parse_time(text: str) -> float:
    """'90', '1:30', '0:01:30.5', '1m30s' -> seconds. Raises ValueError on junk."""
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("empty time")
    if any(c in t for c in "hms") and ":" not in t:
        total, num = 0.0, ""
        for c in t:
            if c.isdigit() or c == ".":
                num += c
            elif c in "hms" and num:
                total += float(num) * {"h": 3600, "m": 60, "s": 1}[c]
                num = ""
            elif not c.isspace():
                raise ValueError(f"bad time: {text}")
        if num:
            total += float(num)
        return total
    parts = t.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad time: {text}")
    total = 0.0
    for p in parts:
        if p == "" or not p.replace(".", "", 1).isdigit():
            raise ValueError(f"bad time: {text}")
        total = total * 60 + float(p)
    return total
