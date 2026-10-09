"""Turning a Recipe (what the user asked for) into a Plan (exact ffmpeg commands plus
what the output must look like for verification to pass)."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace

from .media import MediaInfo, normalize_rotation

# ----------------------------------------------------------------------------- formats


@dataclass(frozen=True)
class Format:
    key: str
    label: str
    ext: str
    kind: str                    # video | audio | gif
    vcodecs: tuple = ()          # selectable encoders, first is the default
    acodecs: tuple = ()
    copy_v: frozenset = frozenset()   # source codecs that may be stream-copied
    copy_a: frozenset = frozenset()
    faststart: bool = False


ANY = frozenset({"*"})

FORMATS: dict[str, Format] = {f.key: f for f in (
    Format("mp4", "MP4", "mp4", "video", ("h264", "hevc", "av1"), ("aac", "mp3"),
           frozenset({"h264", "hevc", "av1"}), frozenset({"aac", "mp3"}), True),
    Format("mov", "MOV", "mov", "video", ("h264", "hevc", "prores", "dnxhr"),
           ("pcm24", "pcm16", "aac"),
           frozenset({"h264", "hevc", "prores", "dnxhd", "mjpeg"}),
           # MOV is the edit format: Resolve on Linux cannot decode AAC, so audio -> PCM
           frozenset({"pcm_s16le", "pcm_s24le"}), True),
    Format("mkv", "MKV", "mkv", "video", ("h264", "hevc", "av1", "vp9"),
           ("aac", "opus", "flac", "mp3"), ANY, ANY),
    Format("webm", "WebM", "webm", "video", ("vp9", "av1"), ("opus",),
           frozenset({"vp8", "vp9", "av1"}), frozenset({"opus", "vorbis"})),
    Format("gif", "GIF", "gif", "gif"),
    Format("mp3", "MP3 audio", "mp3", "audio", (), ("mp3",), frozenset(), frozenset({"mp3"})),
    Format("m4a", "M4A (AAC) audio", "m4a", "audio", (), ("aac",), frozenset(),
           frozenset({"aac"}), True),
    Format("wav", "WAV audio", "wav", "audio", (), ("pcm24", "pcm16"), frozenset(),
           frozenset({"pcm_s16le", "pcm_s24le"})),
    Format("flac", "FLAC audio", "flac", "audio", (), ("flac",), frozenset(), frozenset({"flac"})),
    Format("opus", "Opus audio", "opus", "audio", (), ("opus",), frozenset(), frozenset({"opus"})),
)}

VCODEC_LABELS = {
    "auto": "Automatic", "copy": "Copy (lossless)", "h264": "H.264", "hevc": "HEVC (H.265)",
    "av1": "AV1", "vp9": "VP9", "prores": "ProRes 422 HQ", "dnxhr": "DNxHR HQ",
}
ACODEC_LABELS = {
    "auto": "Automatic", "copy": "Copy (lossless)", "aac": "AAC", "mp3": "MP3",
    "opus": "Opus", "flac": "FLAC", "pcm16": "PCM 16-bit", "pcm24": "PCM 24-bit",
}
# ffprobe codec_name each encoder choice produces
VCODEC_NAME = {"h264": "h264", "hevc": "hevc", "av1": "av1", "vp9": "vp9",
               "prores": "prores", "dnxhr": "dnxhd", "gif": "gif"}
ACODEC_NAME = {"aac": "aac", "mp3": "mp3", "opus": "opus", "flac": "flac",
               "pcm16": "pcm_s16le", "pcm24": "pcm_s24le"}
ACODEC_ENCODER = {"aac": "aac", "mp3": "libmp3lame", "opus": "libopus", "flac": "flac",
                  "pcm16": "pcm_s16le", "pcm24": "pcm_s24le"}
LOSSY_AUDIO = {"aac", "mp3", "opus"}

QUALITY_LEVELS = ("lossless", "high", "balanced", "small")
QUALITY_LABELS = {"lossless": "Visually lossless", "high": "High",
                  "balanced": "Balanced", "small": "Small file"}
# per-encoder rate factor for each quality level
_QMAP = {
    "libx264":    (16, 20, 23, 28),
    "libx265":    (18, 22, 26, 30),
    "libsvtav1":  (22, 28, 34, 42),
    "libvpx-vp9": (20, 28, 33, 40),
    "h264_nvenc": (19, 23, 26, 31),
    "hevc_nvenc": (21, 25, 28, 32),
    "av1_nvenc":  (24, 30, 35, 42),
}
SPEEDS = ("fast", "balanced", "best")
SPEED_LABELS = {"fast": "Fast", "balanced": "Balanced", "best": "Best compression"}

ROTATIONS = (0, 90, 180, 270)     # clockwise, as users think of it


# ----------------------------------------------------------------------------- recipe


@dataclass
class Recipe:
    fmt: str = "mp4"
    vcodec: str = "auto"
    acodec: str = "auto"
    gpu: bool = False
    quality: str = "high"
    crf: int | None = None          # overrides quality when set
    speed_preset: str = "balanced"
    target_mb: float = 0.0          # >0: aim for this file size instead of quality
    audio_kbps: int = 192
    # edits
    trim_start: float | None = None
    trim_end: float | None = None
    trim_exact: bool = True
    bake_rotation: bool = True      # make the pixels upright instead of keeping the flag
    rotate: int = 0                 # extra clockwise rotation 0/90/180/270
    hflip: bool = False
    vflip: bool = False
    rotate_lossless: bool = False   # only change the rotation flag, copy streams
    crop: tuple[int, int, int, int] | None = None   # w, h, x, y in upright pixels
    scale_height: int = 0
    scale_width: int = 0
    fps: float = 0.0                # 0 keeps source rate
    cfr: bool = False               # force constant frame rate at source rate
    speed: float = 1.0
    deinterlace: bool = False
    stabilize: bool = False
    denoise: bool = False
    remove_audio: bool = False
    normalize_audio: bool = False
    volume_db: float = 0.0
    strip_metadata: bool = False
    gif_fps: float = 15.0           # 0 = source rate
    gif_width: int = 640            # 0 = source width
    gif_per_frame_palette: bool = True
    suffix: str = ""
    replace_original: bool = False

    def has_video_filters(self) -> bool:
        return bool(self.rotate or self.hflip or self.vflip or self.crop or self.scale_height
                    or self.scale_width or self.fps or self.cfr or self.speed != 1.0
                    or self.deinterlace or self.stabilize or self.denoise)

    def has_audio_filters(self) -> bool:
        return bool(self.speed != 1.0 or self.normalize_audio or self.volume_db)

    def trimmed(self) -> bool:
        return self.trim_start is not None or self.trim_end is not None


class PlanError(ValueError):
    """The recipe cannot be applied to this file; message is user-facing."""


# ----------------------------------------------------------------------------- plan


@dataclass
class Expect:
    kind: str                         # video | audio | gif | image
    vcodec: str = ""
    width: int = 0
    height: int = 0
    rotation: int | None = 0          # None: do not check (flipped display matrices)
    acodec: str = ""                  # "" means no audio track expected
    audio_tracks: list = field(default_factory=list)   # [(codec, rate, channels)] per track
    audio_count: int = 0
    duration: float = 0.0
    dur_tol: float = 0.1
    min_duration: float | None = None
    frames: int | None = None         # exact frame count, None = do not check
    frames_from: str = ""             # count frames of this file and compare
    video_md5_from: str = ""
    audio_md5_from: str = ""
    max_bytes: int = 0                # soft limit, warns only


@dataclass
class Step:
    argv: list[str]
    label: str
    duration: float                   # media seconds this step will process, for progress
    weight: float = 1.0


@dataclass
class Plan:
    title: str
    inputs: list[MediaInfo]
    output: str
    temp: str
    steps: list[Step]
    expect: Expect
    summary: list[str] = field(default_factory=list)
    cleanup: list[str] = field(default_factory=list)
    replace_original: bool = False
    files: dict[str, str] = field(default_factory=dict)   # written before the first step


# ----------------------------------------------------------------------------- helpers


def even(n: float) -> int:
    """Nearest even integer >= 2 (halves round up, unlike Python's round)."""
    return max(2, int(n / 2.0 + 0.5) * 2)


def even_down(n: int) -> int:
    return max(2, n - (n % 2))


def unique_path(path: str, taken: set[str] | None = None) -> str:
    """path, or 'name (2).ext', 'name (3).ext'... whichever does not exist yet."""
    taken = taken or set()
    base, ext = os.path.splitext(path)
    cand, n = path, 2
    while os.path.exists(cand) or os.path.abspath(cand) in taken:
        cand = f"{base} ({n}){ext}"
        n += 1
    return cand


def output_path(info: MediaInfo, ext: str, suffix: str, out_dir: str | None,
                taken: set[str] | None = None) -> str:
    stem = os.path.splitext(os.path.basename(info.path))[0]
    d = out_dir or os.path.dirname(info.path)
    name = f"{stem}{suffix}.{ext}"
    path = os.path.join(d, name)
    if os.path.abspath(path) == os.path.abspath(info.path) and not suffix:
        path = os.path.join(d, f"{stem}_converted.{ext}")
    return unique_path(path, taken)


def temp_for(final: str) -> str:
    d, name = os.path.split(final)
    stem, ext = os.path.splitext(name)
    return os.path.join(d, f".{stem}.clipdeck-partial{ext}")


STANDARD_RATES = (23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 100, 119.88, 120)


def rate_expr(rate: float) -> str:
    """fps filter argument; NTSC rates become exact fractions (29.97 -> 30000/1001)."""
    for n in (24, 30, 48, 60, 120):
        if abs(rate - n * 1000 / 1001) < 0.005:
            return f"{n * 1000}/1001"
    return f"{rate:.6g}"


def snap_rate(fps: float) -> float:
    """Nearest standard frame rate within 3%, for turning VFR into CFR."""
    if fps <= 0:
        return 30.0
    best = min(STANDARD_RATES, key=lambda r: abs(r - fps))
    return best if abs(best - fps) / fps < 0.03 else round(fps, 3)


# containers that store H.264/HEVC as Annex B byte streams; copying to/from these
# legitimately rewrites the packets, so bit-exact comparison does not apply
ANNEXB_CONTAINERS = {"ts", "mts", "m2ts", "avi", "mpg", "mpeg", "vob", "h264", "264", "hevc",
                     "265"}


def same_framing(a: str, b: str) -> bool:
    return (a in ANNEXB_CONTAINERS) == (b in ANNEXB_CONTAINERS)


_TONEMAP_CORE = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                 "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def tonemap_chain(info: MediaInfo) -> str:
    """HDR -> SDR. Phones often tag only the transfer function, and zscale cannot convert
    from 'unknown' primaries/matrix, so declare the missing ones (BT.2020) first."""
    known = lambda v: v and v not in ("unknown", "reserved", "unspecified")  # noqa: E731
    prim = info.color_primaries if known(info.color_primaries) else "bt2020"
    space = info.color_space if known(info.color_space) else "bt2020nc"
    rng = "pc" if info.color_range == "pc" else "tv"
    return (f"setparams=color_primaries={prim}:color_trc={info.color_transfer}:"
            f"colorspace={space}:range={rng},{_TONEMAP_CORE}")
SDR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]


def _color_args(info: MediaInfo) -> list[str]:
    args = []
    for attr, opt in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"),
                      ("color_space", "-colorspace"), ("color_range", "-color_range")):
        v = getattr(info, attr)
        if v and v not in ("unknown", "reserved", "unspecified"):
            args += [opt, v]
    return args


def atempo_chain(speed: float) -> list[str]:
    """atempo only accepts 0.5..2.0 per instance, so chain them."""
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.6g}")
    return parts


def _quality_value(encoder: str, r: Recipe) -> int:
    if r.crf is not None:
        return int(r.crf)
    idx = QUALITY_LEVELS.index(r.quality) if r.quality in QUALITY_LEVELS else 1
    return _QMAP[encoder][idx]


def resolve_encoder(vcodec: str, gpu: bool, available: set[str] | None = None) -> str:
    if vcodec == "h264":
        enc = "h264_nvenc" if gpu else "libx264"
    elif vcodec == "hevc":
        enc = "hevc_nvenc" if gpu else "libx265"
    elif vcodec == "av1":
        enc = "av1_nvenc" if gpu else "libsvtav1"
    elif vcodec == "vp9":
        enc = "libvpx-vp9"
    elif vcodec == "prores":
        enc = "prores_ks"
    elif vcodec == "dnxhr":
        enc = "dnxhd"
    else:
        raise PlanError(f"Unknown video codec {vcodec}")
    if available is not None and enc not in available:
        raise PlanError(f"The {enc} encoder is not available on this system")
    return enc


def video_encoder_args(enc: str, r: Recipe, info: MediaInfo, vbitrate: int = 0,
                       pass_no: int = 0, passlog: str = "") -> list[str]:
    """Encoder flags. vbitrate (bits/s) > 0 means target-size mode."""
    hi = info.bit_depth > 8
    sp = SPEEDS.index(r.speed_preset) if r.speed_preset in SPEEDS else 1
    a: list[str] = ["-c:v", enc]
    if enc == "libx264":
        a += ["-preset", ("fast", "medium", "slow")[sp], "-pix_fmt", "yuv420p"]
        if vbitrate:
            a += ["-b:v", str(vbitrate)]
        else:
            a += ["-crf", str(_quality_value(enc, r))]
    elif enc == "libx265":
        a += ["-preset", ("fast", "medium", "slow")[sp],
              "-pix_fmt", "yuv420p10le" if hi else "yuv420p", "-tag:v", "hvc1"]
        params = ["log-level=error"]
        if vbitrate:
            a += ["-b:v", str(vbitrate)]
        else:
            a += ["-crf", str(_quality_value(enc, r))]
        a += ["-x265-params", ":".join(params)]
    elif enc == "libsvtav1":
        a += ["-preset", ("10", "7", "5")[sp],
              "-pix_fmt", "yuv420p10le" if hi else "yuv420p"]
        if vbitrate:
            a += ["-b:v", str(vbitrate)]
        else:
            a += ["-crf", str(_quality_value(enc, r))]
    elif enc == "libvpx-vp9":
        a += ["-deadline", "good", "-cpu-used", ("4", "2", "1")[sp], "-row-mt", "1",
              "-pix_fmt", "yuv420p"]
        if vbitrate:
            a += ["-b:v", str(vbitrate)]
        else:
            a += ["-crf", str(_quality_value(enc, r)), "-b:v", "0"]
    elif enc.endswith("_nvenc"):
        a += ["-preset", ("p3", "p5", "p7")[sp], "-tune", "hq"]
        if enc == "h264_nvenc":
            a += ["-pix_fmt", "yuv420p"]
        else:
            a += ["-pix_fmt", "p010le" if hi else "yuv420p"]
        if enc == "hevc_nvenc":
            a += ["-tag:v", "hvc1"]
        if vbitrate:
            a += ["-rc", "vbr", "-b:v", str(vbitrate), "-maxrate", str(int(vbitrate * 1.5)),
                  "-multipass", "fullres"]
        else:
            a += ["-rc", "vbr", "-cq", str(_quality_value(enc, r)), "-b:v", "0"]
    elif enc == "prores_ks":
        a += ["-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    elif enc == "dnxhd":
        a += ["-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"]
    if pass_no:
        a += ["-pass", str(pass_no), "-passlogfile", passlog]
    return a


def available_encoders() -> set[str]:
    """Encoders that exist in this ffmpeg build AND actually initialise (NVENC needs a GPU)."""
    wanted = ["libx264", "libx265", "libsvtav1", "libvpx-vp9", "prores_ks", "dnxhd",
              "h264_nvenc", "hevc_nvenc", "av1_nvenc"]
    try:
        listing = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    present = {e for e in wanted if re.search(rf"\s{re.escape(e)}\s", listing)}
    usable = set()
    for e in present:
        if not e.endswith("_nvenc"):
            usable.add(e)
            continue
        try:
            rc = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1",
                 "-frames:v", "1", "-c:v", e, "-f", "null", "-"],
                capture_output=True, timeout=20).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = 1
        if rc == 0:
            usable.add(e)
    return usable


# ----------------------------------------------------------------------------- geometry


def _geometry(info: MediaInfo, r: Recipe, need_even: bool, upright: bool = True
              ) -> tuple[list[str], int, int]:
    """Video filter chain (excluding fps/speed) and the resulting stored size.
    Works in upright, square-pixel coordinates: ffmpeg autorotates before our filters.
    upright=False keeps the stored orientation (autorotation off, flag preserved)."""
    vf: list[str] = []
    # all sizes are computed in displayed orientation; when the rotation flag is kept
    # (upright=False) the pixels stay sideways, so filters get swapped dimensions
    sideways = not upright and info.rotation in (90, -90)

    def dims(a, b):
        return (b, a) if sideways else (a, b)
    w, h = info.display_size
    if info.sar != 1:
        # normalize to square pixels first so every later number means what it says
        sw, sh = dims(even(w), even(h))
        vf.append(f"scale={sw}:{sh}:flags=lanczos,setsar=1")
        w, h = even(w), even(h)
    if r.deinterlace:
        vf.append("bwdif=mode=send_frame")
    if r.crop:
        cw, ch, cx, cy = (int(v) for v in r.crop)
        if cw <= 0 or ch <= 0 or cx < 0 or cy < 0 or cx + cw > w or cy + ch > h:
            raise PlanError(f"Crop {cw}×{ch} at {cx},{cy} does not fit inside the {w}×{h} frame")
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
        w, h = cw, ch
    if r.stabilize:
        vf.append("deshake")
    rot = r.rotate % 360
    if rot == 90:
        vf.append("transpose=clock")
        w, h = h, w
    elif rot == 270:
        vf.append("transpose=cclock")
        w, h = h, w
    elif rot == 180:
        vf += ["hflip", "vflip"]
    if r.hflip:
        vf.append("hflip")
    if r.vflip:
        vf.append("vflip")
    if r.denoise:
        vf.append("hqdn3d")
    tw, th = w, h
    if r.scale_height and r.scale_width:
        tw, th = r.scale_width, r.scale_height
    elif r.scale_height:
        th = r.scale_height
        tw = w * th / h
    elif r.scale_width:
        tw = r.scale_width
        th = h * tw / w
    if need_even:
        tw, th = even(tw), even(th)
    else:
        tw, th = max(1, int(round(tw))), max(1, int(round(th)))
    if (tw, th) != (w, h):
        sw, sh = dims(tw, th)
        vf.append(f"scale={sw}:{sh}:flags=lanczos,setsar=1")
    return (vf, *dims(tw, th))


def _out_duration(info: MediaInfo, r: Recipe) -> tuple[float, float]:
    """(start, input span) after trimming, and validates the trim."""
    start = r.trim_start or 0.0
    if not info.duration and r.trim_end is None:
        raise PlanError(f"The length of {info.name} cannot be determined")
    end = r.trim_end if r.trim_end is not None else info.duration
    if start < 0 or end < 0:
        raise PlanError("Trim times cannot be negative")
    if info.duration and start >= info.duration:
        raise PlanError(f"Trim start {start:.2f}s is past the end of the clip ({info.duration:.2f}s)")
    if info.duration:
        end = min(end, info.duration)
    if end - start < 0.05:
        raise PlanError("Trim end must be after trim start")
    return start, end - start


def _track_format(acodec: str, src_rate: int, src_ch: int) -> tuple[int, int]:
    """Sample rate and channel count an encoder will use for one source track."""
    rate = src_rate or 48000
    ch = src_ch or 2
    if acodec == "opus":
        rate, ch = 48000, min(ch, 8)
    elif acodec == "mp3":
        rate, ch = min(rate, 48000), min(ch, 2)
    elif acodec == "aac":
        rate = min(rate, 96000)
    return rate, ch


def _audio_codec_args(acodec: str, r: Recipe, tracks) -> tuple[list[str], list[tuple]]:
    """Encoder flags with per-track rate/channels, so a mono commentary track stays mono
    and a 5.1 track stays 5.1. Returns (args, [(codec, rate, ch)] per track)."""
    args = ["-c:a", ACODEC_ENCODER[acodec]]
    if acodec in LOSSY_AUDIO:
        args += ["-b:a", f"{int(r.audio_kbps)}k"]
    expect = []
    for n, t in enumerate(tracks):
        rate, ch = _track_format(acodec, t.sample_rate, t.channels)
        args += [f"-ar:a:{n}", str(rate), f"-ac:a:{n}", str(ch)]
        expect.append((ACODEC_NAME[acodec], rate, ch))
    return args, expect


# ----------------------------------------------------------------------------- builders

COMMON = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error"]


def build(info: MediaInfo, r: Recipe, out_dir: str | None = None,
          encoders: set[str] | None = None, taken: set[str] | None = None,
          title: str = "") -> Plan:
    """Single-input plan for convert / edit / fix / gif / audio extraction."""
    fmt = FORMATS.get(r.fmt)
    if not fmt:
        raise PlanError(f"Unknown output format {r.fmt}")
    if r.speed <= 0 or r.speed > 100:
        raise PlanError("Speed must be between 0.01× and 100×")
    if fmt.kind in ("video", "gif") and not info.has_video:
        raise PlanError(f"{info.name} has no video stream")
    if fmt.kind == "audio" and not info.has_audio:
        raise PlanError(f"{info.name} has no audio track to extract")
    if r.rotate % 90 or r.rotate % 360 not in ROTATIONS:
        raise PlanError("Rotation must be 0, 90, 180 or 270 degrees")
    if fmt.kind in ("video", "gif") and info.is_image_seq:
        raise PlanError(f"{info.name} is a still image, not a video")
    if fmt.kind == "gif":
        return _build_gif(info, r, out_dir, taken, title)

    start, span = _out_duration(info, r)
    out_dur = span / r.speed

    # ---------------- decide video handling
    vmode = "none"
    venc = ""
    vcodec_name = ""
    lossless_rot = None
    if fmt.kind == "video":
        want_v = r.vcodec
        if want_v not in ("auto", "copy") and want_v not in fmt.vcodecs:
            raise PlanError(f"{VCODEC_LABELS.get(want_v, want_v)} cannot go in {fmt.label}")
        copy_ok_codec = "*" in fmt.copy_v or info.vcodec in fmt.copy_v
        # hidden pre-roll (MP4 edit list) only stays hidden in MP4/MOV
        preroll_exposed = info.preroll and fmt.ext not in ("mp4", "mov", "m4v")
        needs_pixels = (r.has_video_filters() or (r.trimmed() and r.trim_exact)
                        or r.target_mb > 0)
        if r.rotate_lossless:
            if any((r.crop, r.scale_height, r.scale_width, r.fps, r.cfr, r.speed != 1.0,
                    r.deinterlace, r.stabilize, r.denoise)):
                raise PlanError("Lossless rotation only changes the rotation flag; turn off "
                                "the other video edits or switch off “Lossless”")
            if not copy_ok_codec:
                raise PlanError(f"{info.vcodec} video cannot be copied into {fmt.label}; "
                                "lossless rotation is not possible")
            if r.trimmed() and r.trim_exact:
                raise PlanError("Lossless rotation cannot be combined with an exact trim; "
                                "use a fast (keyframe) trim")
            if r.target_mb > 0:
                raise PlanError("Lossless rotation copies the video, so it cannot aim for a "
                                "target file size")
            vmode = "copy"
            lossless_rot = normalize_rotation(info.rotation - (r.rotate % 360))
        else:
            if r.bake_rotation and info.rotation:
                needs_pixels = True
            if not r.bake_rotation and info.rotation and (r.crop or r.rotate or r.hflip
                                                          or r.vflip):
                raise PlanError("Turn on “Make upright” to crop, rotate or flip a clip that "
                                "has a rotation flag")
            if want_v == "copy":
                if needs_pixels:
                    reasons = []
                    if r.has_video_filters():
                        reasons.append("video edits")
                    if r.trimmed() and r.trim_exact:
                        reasons.append("an exact trim")
                    if r.bake_rotation and info.rotation:
                        reasons.append("making the clip upright")
                    if r.target_mb > 0:
                        reasons.append("a target file size")
                    raise PlanError("Copying video is impossible with " + " and ".join(reasons)
                                    + "; pick an encoder")
                if not copy_ok_codec:
                    raise PlanError(f"{info.vcodec} video cannot be copied into {fmt.label}")
                if info.container == "avi" and fmt.ext in ("mp4", "mov"):
                    raise PlanError("AVI video has no reliable timestamps and cannot be copied "
                                    "into MP4/MOV; use Automatic (re-encodes)")
                if preroll_exposed:
                    raise PlanError("This clip was cut on a keyframe and hides frames before "
                                    f"its start; {fmt.label} would show them. Use Automatic")
                vmode = "copy"
            elif want_v == "auto":
                # AVI has no real timestamps: copied B-frame video ends up with
                # out-of-order timestamps in any modern container, so re-encode it
                vmode = "copy" if (copy_ok_codec and not needs_pixels and not preroll_exposed
                                   and info.container != "avi") else "encode"
            else:
                vmode = "encode"
        if vmode == "encode":
            choice = fmt.vcodecs[0] if want_v in ("auto", "copy") else want_v
            if r.target_mb > 0 and choice in ("prores", "dnxhr"):
                raise PlanError("ProRes and DNxHR have fixed bitrates; a target file size "
                                "needs H.264, HEVC, AV1 or VP9")
            venc = resolve_encoder(choice, r.gpu and choice in ("h264", "hevc", "av1"), encoders)
            vcodec_name = VCODEC_NAME[choice]
        else:
            vcodec_name = info.vcodec

    # ---------------- decide audio handling
    amode = "none"
    acodec_key = ""
    keep_audio = info.has_audio and not r.remove_audio
    if fmt.kind == "audio" and not keep_audio:
        raise PlanError("Cannot remove audio when extracting audio")
    if keep_audio:
        want_a = r.acodec
        if want_a not in ("auto", "copy") and want_a not in fmt.acodecs:
            raise PlanError(f"{ACODEC_LABELS.get(want_a, want_a)} audio cannot go in {fmt.label}")
        used = info.audio[:1] if fmt.kind == "audio" else info.audio
        acopy_ok = ("*" in fmt.copy_a or all(a.codec in fmt.copy_a for a in used))
        # copied audio can only start on a packet boundary near the keyframe, so an
        # exact cut needs re-encoded audio (MP4 hides the excess, MKV/WebM play it)
        exact_cut = (r.trimmed() and r.trim_exact and vmode != "copy") or \
            (info.preroll and fmt.ext not in ("mp4", "mov", "m4v"))
        if want_a == "copy":
            if r.has_audio_filters():
                raise PlanError("Copying audio is impossible with speed or volume changes")
            if exact_cut:
                raise PlanError("Copying audio is impossible here: the cut must be exact "
                                "(frame-accurate trim, or a keyframe-trimmed source)")
            if not acopy_ok:
                raise PlanError(f"{info.acodec} audio cannot be copied into {fmt.label}")
            amode = "copy"
        elif want_a == "auto":
            amode = "copy" if (acopy_ok and not r.has_audio_filters()
                               and not exact_cut) else "encode"
        else:
            amode = "encode"
        if amode == "encode":
            acodec_key = fmt.acodecs[0] if want_a in ("auto", "copy") else want_a

    # ---------------- output naming
    suffix = r.suffix
    if not suffix and fmt.ext == info.container:
        suffix = "_edited"
    out = output_path(info, fmt.ext, suffix, out_dir, taken)
    tmp = temp_for(out)

    # ---------------- assemble command
    argv = list(COMMON)
    if lossless_rot is not None:
        argv += ["-display_rotation:v:0", str(lossless_rot)]
        if r.hflip:
            argv += ["-display_hflip:v:0"]
        if r.vflip:
            argv += ["-display_vflip:v:0"]
    if info.container == "avi" and vmode == "copy":
        # copied AVI B-frame video has no timestamps; (when re-encoding, genpts would
        # create a duplicate timestamp and lose a frame, so only here)
        argv += ["-fflags", "+genpts"]
    keep_flag = (fmt.kind == "video" and vmode == "encode" and not r.bake_rotation
                 and bool(info.rotation))
    if keep_flag:
        argv += ["-noautorotate"]
    if start:
        argv += ["-ss", f"{start:.6f}"]
    if r.trim_end is not None or start:
        argv += ["-t", f"{span:.6f}"]
    argv += ["-i", info.path]

    expect = Expect(kind=fmt.kind, duration=out_dur)
    if info.is_vfr:
        # VFR sources often report a duration that stops short of their last frame
        expect.dur_tol = max(expect.dur_tol, 0.15 + 3.0 / max(info.fps, 1.0))
    summary: list[str] = []

    if fmt.kind == "video":
        remux_all = (fmt.key == "mkv" and info.container in ("mkv", "webm") and vmode == "copy"
                     and amode in ("copy", "none") and keep_audio == info.has_audio)
        if remux_all:
            # Matroska to Matroska: keep subtitles, extra tracks and attachments too
            # 0:V = video that is not an attached picture (cover art would become a track)
            argv += ["-map", "0", "-map", "-0:v", "-map", "0:V", "-map", "-0:d?",
                     "-c:s", "copy", "-c:t", "copy"]
            if info.has_cover:
                summary.append("Cover art dropped (it would become a video track)")
            if info.subtitle_count:
                summary.append(f"{info.subtitle_count} subtitle track(s) kept")
        else:
            argv += ["-map", "0:v:0"]
            if keep_audio:
                argv += ["-map", "0:a"]
            argv += ["-sn", "-dn"]
            if info.subtitle_count:
                summary.append(f"{info.subtitle_count} subtitle track(s) dropped")
        need_even = vmode == "encode" and venc not in ("prores_ks",)
        if vmode == "encode":
            vf, ow, oh = _geometry(info, r, need_even, upright=not keep_flag)
            if venc == "dnxhd" and (ow < 256 or oh < 120):
                raise PlanError(f"DNxHR needs at least 256×120; this output would be {ow}×{oh}")
            tonemap = info.is_hdr and (venc in ("libx264", "h264_nvenc", "libvpx-vp9", "dnxhd")
                                       or info.bit_depth <= 8)
            if tonemap:
                vf.append(tonemap_chain(info))
                summary.append("HDR tone-mapped to SDR (this codec is 8-bit)")
            # timing: an explicit rate wins; speed changes need a new constant rate (a coarse
            # source time base would otherwise produce duplicate timestamps); VFR sources keep
            # their timestamps; constant-rate sources stay exactly constant
            rate = r.fps or (snap_rate(info.fps) if r.cfr else 0)
            if not rate and r.speed != 1.0:
                rate = snap_rate(min(60.0, (info.r_fps or info.fps or 30) * r.speed))
            if r.speed != 1.0:                   # retime first, then sample at the new rate
                vf.append(f"setpts=PTS/{r.speed:.6g}")
            if rate:
                vf.append(f"fps={rate_expr(rate)}")
            if vf:
                argv += ["-vf", ",".join(vf)]
            if rate:
                argv += ["-fps_mode", "cfr"]
                # frame-rate conversion snaps the end to the new frame grid
                expect.dur_tol = max(expect.dur_tol, 0.15 + 3.0 / rate)
            elif (info.container in ("mkv", "webm") and info.fps and info.r_fps
                  and abs(info.fps - info.r_fps) / info.r_fps < 0.001):
                # Matroska stores millisecond timestamps, so an exactly constant-rate
                # source (29.97 fps) would carry ±1 ms jitter into MOV/MP4: re-time it
                argv += ["-fps_mode", "cfr"]
            else:
                # keep every frame with its original timestamp. MOV would otherwise default
                # to constant rate and silently drop frames (seen on Pixel footage); phone
                # clips that look constant often are not quite, and forcing cfr adds or
                # drops frames. The source's own time base keeps timestamps from colliding.
                argv += ["-fps_mode", "passthrough", "-enc_time_base:v", "demux"]
            expect.width, expect.height = ow, oh
            expect.rotation = info.rotation if keep_flag else 0
            if keep_flag:
                summary.append(f"Keeps the {abs(info.rotation)}° rotation flag (not made upright)")
            elif info.rotation:
                summary.append(f"Made upright (was stored sideways with a {abs(info.rotation)}° "
                               "flag)")
            if r.cfr and not r.fps:
                summary.append(f"Constant frame rate {rate:g} fps")
            summary.append(f"Video: {info.vcodec} → {VCODEC_LABELS[_key_for(venc)]}"
                           f"{' (GPU)' if venc.endswith('_nvenc') else ''}, {ow}×{oh}")
        else:
            argv += ["-c:v", "copy"]
            if info.vcodec == "hevc" and fmt.ext in ("mp4", "mov"):
                argv += ["-tag:v", "hvc1"]
            expect.width, expect.height = info.width, info.height
            expect.rotation = info.rotation if lossless_rot is None else lossless_rot
            if lossless_rot is not None and (r.hflip or r.vflip):
                expect.rotation = None   # ffprobe reports flipped matrices oddly
            if not r.trimmed() and same_framing(info.container, fmt.ext):
                expect.video_md5_from = info.path
            summary.append(f"Video: {info.vcodec} copied (lossless), {info.width}×{info.height}")
            if lossless_rot is not None:
                summary.append(f"Rotation flag set to {abs(lossless_rot)}° "
                               "(players honor it; some editors ignore it)")
            elif info.rotation:
                summary.append(f"Keeps the {abs(info.rotation)}° rotation flag")
        expect.vcodec = vcodec_name
        if r.trimmed() and vmode == "copy":
            # stream copy can only cut on keyframes, so the start moves back to the
            # previous keyframe, which can be anywhere since the beginning of the file
            expect.dur_tol = start + 0.5
            expect.min_duration = span - 0.25
            summary.append("Fast trim: cuts on the nearest keyframe (may start a bit early)")
        frame_preserving = (not r.trimmed() and not r.fps and not r.cfr and r.speed == 1.0)
        if frame_preserving:
            expect.frames_from = info.path
        if vmode == "encode":
            argv += SDR_TAGS if tonemap else _color_args(info)
    else:
        argv += ["-map", "0:a:0", "-vn", "-sn", "-dn"]

    # audio flags
    if keep_audio:
        af = []
        if r.speed != 1.0:
            af += atempo_chain(r.speed)
        if r.volume_db:
            af.append(f"volume={r.volume_db:.6g}dB")
        if r.normalize_audio:
            af.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        tracks = info.audio[:1] if fmt.kind == "audio" else info.audio
        if amode == "copy":
            argv += ["-c:a", "copy"]
            expect.acodec = info.acodec
            expect.audio_tracks = [(t.codec, t.sample_rate, t.channels) for t in tracks]
            if not r.trimmed():
                expect.audio_md5_from = info.path
            summary.append(f"Audio: {info.acodec} copied (bit-identical)")
        else:
            if af:
                argv += ["-af", ",".join(af)]
            aargs, expect.audio_tracks = _audio_codec_args(acodec_key, r, tracks)
            argv += aargs
            expect.acodec = ACODEC_NAME[acodec_key]
            expect.dur_tol = max(expect.dur_tol, 0.35)
            summary.append(f"Audio: {info.acodec} → {ACODEC_LABELS[acodec_key]}")
        expect.audio_count = len(tracks)
    elif fmt.kind == "video":
        argv += ["-an"]
        summary.append("Audio: none" if not info.has_audio else "Audio: removed")

    if r.strip_metadata:
        argv += ["-map_metadata", "-1", "-map_chapters", "-1"]
        summary.append("Metadata (incl. GPS location) stripped")
    if fmt.faststart:
        argv += ["-movflags", "+faststart"]

    if r.trimmed():
        summary.insert(0, f"Trim {start:.2f}s → {start + span:.2f}s ({span:.2f}s)")
    if r.speed != 1.0:
        summary.append(f"Speed {r.speed:g}× → {out_dur:.2f}s")

    steps: list[Step] = []
    cleanup: list[str] = []
    if r.target_mb > 0 and vmode == "encode":
        total_bits = r.target_mb * 8 * 1024 * 1024
        abits = (r.audio_kbps * 1000 * max(1, len(expect.audio_tracks)) if amode == "encode" else
                 sum(a.bitrate or 128000 for a in info.audio) if amode == "copy" else 0)
        vbit = int(total_bits / max(out_dur, 0.1) * 0.97 - abits)
        if vbit < 50_000:
            raise PlanError(f"{r.target_mb:g} MB is too small for {out_dur:.0f}s of video")
        expect.max_bytes = int(r.target_mb * 1024 * 1024 * 1.05)
        summary.append(f"Target size {r.target_mb:g} MB → video {vbit // 1000} kb/s")
        if venc in ("libx264", "libvpx-vp9"):
            passlog = os.path.join(tempfile.gettempdir(),
                                   f"clipdeck-pass-{os.getpid()}-{abs(hash(tmp))}")
            cleanup += [passlog + "-0.log", passlog + "-0.log.mbtree", passlog + "-0.log.temp",
                        passlog + "-0.log.mbtree.temp"]
            first = _replace_vcodec(argv, video_encoder_args(venc, r, info, vbit, 1, passlog))
            first = [a for a in first if a not in ("-movflags", "+faststart")]
            # pass 1 only needs video statistics
            first = _strip_audio(first) + ["-an", "-f", "null", os.devnull]
            second = _replace_vcodec(argv, video_encoder_args(venc, r, info, vbit, 2, passlog))
            steps.append(Step(first, "Analysing (pass 1/2)", out_dur, 0.4))
            steps.append(Step(second + [tmp], "Encoding (pass 2/2)", out_dur, 0.6))
        else:
            argv = _replace_vcodec(argv, video_encoder_args(venc, r, info, vbit))
            steps.append(Step(argv + [tmp], "Encoding", out_dur))
    else:
        if vmode == "encode":
            argv = _replace_vcodec(argv, video_encoder_args(venc, r, info))
        steps.append(Step(argv + [tmp], "Encoding" if (vmode == "encode" or amode == "encode")
                          else "Copying streams", out_dur))

    return Plan(title=title or os.path.basename(out), inputs=[info], output=out, temp=tmp,
                steps=steps, expect=expect, summary=summary, cleanup=cleanup,
                replace_original=r.replace_original)


def _key_for(enc: str) -> str:
    return {"libx264": "h264", "h264_nvenc": "h264", "libx265": "hevc", "hevc_nvenc": "hevc",
            "libsvtav1": "av1", "av1_nvenc": "av1", "libvpx-vp9": "vp9",
            "prores_ks": "prores", "dnxhd": "dnxhr"}[enc]


def _replace_vcodec(argv: list[str], vargs: list[str]) -> list[str]:
    """Insert encoder flags right after the stream maps / filters (before audio flags)."""
    out = list(argv)
    # place before the first audio option or at the end
    for i, a in enumerate(out):
        if a in ("-c:a", "-af", "-an") and i > out.index("-i"):
            return out[:i] + vargs + out[i:]
    return out + vargs


def _strip_audio(argv: list[str]) -> list[str]:
    out = []
    with_val = {"-c:a", "-af", "-b:a", "-ar", "-ac", "-map"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-map" and i + 1 < len(argv) and argv[i + 1].startswith("0:a"):
            i += 2
            continue
        if a in with_val - {"-map"} or a.startswith(("-ar:a:", "-ac:a:")):
            i += 2
            continue
        if a == "-an":
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _build_gif(info: MediaInfo, r: Recipe, out_dir, taken, title) -> Plan:
    start, span = _out_duration(info, r)
    out_dur = span / r.speed
    gr = replace(r, scale_width=0, scale_height=0)
    vf, w, h = _geometry(info, gr, need_even=False)
    if info.is_hdr:
        vf.append(tonemap_chain(info))
    if r.gif_width and r.gif_width < w:
        nh = max(1, int(round(h * r.gif_width / w)))
        vf.append(f"scale={r.gif_width}:{nh}:flags=lanczos")
        w, h = r.gif_width, nh
    rate = r.gif_fps if (r.gif_fps and (not info.fps or r.gif_fps < info.fps)) else 0
    if r.speed != 1.0:
        vf.append(f"setpts=PTS/{r.speed:.6g}")
    if rate:
        vf.append(f"fps={rate_expr(rate)}")
    chain = ",".join(vf) + "," if vf else ""
    if r.gif_per_frame_palette:
        graph = (f"[0:v:0]{chain}split[g0][g1];[g0]palettegen=max_colors=256:stats_mode=single[p];"
                 "[g1][p]paletteuse=dither=sierra2:new=1")
    else:
        graph = (f"[0:v:0]{chain}split[g0][g1];[g0]palettegen=max_colors=256:stats_mode=diff[p];"
                 "[g1][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle")
    suffix = r.suffix
    out = output_path(info, "gif", suffix, out_dir, taken)
    tmp = temp_for(out)
    argv = list(COMMON)
    if start:
        argv += ["-ss", f"{start:.6f}"]
    if r.trim_end is not None or start:
        argv += ["-t", f"{span:.6f}"]
    argv += ["-i", info.path, "-an", "-filter_complex", graph, "-loop", "0", tmp]
    expect = Expect(kind="gif", vcodec="gif", width=w, height=h, duration=out_dur,
                    dur_tol=max(0.15, 2.0 / (rate or info.fps or 10)))
    if not r.trimmed() and not rate and r.speed == 1.0:
        expect.frames_from = info.path
    summary = [f"GIF {w}×{h}, {f'{rate:g} fps' if rate else 'source frame rate'}, "
               f"{'per-frame' if r.gif_per_frame_palette else 'shared'} 256-color palette"]
    if info.has_audio:
        summary.append("GIF cannot contain audio")
    return Plan(title=title or os.path.basename(out), inputs=[info], output=out, temp=tmp,
                steps=[Step(argv, "Rendering GIF", out_dur)], expect=expect, summary=summary)


# ----------------------------------------------------------------------------- auto-fix


def autofix_recipe(info: MediaInfo, cfr: bool = False) -> Recipe | None:
    """fixrot's logic: bake rotation, move to MP4, make audio AAC. None = already fine."""
    if not info.has_video:
        return None
    bake = bool(info.rotation)
    to_mp4 = info.container not in ("mp4", "mov", "m4v")
    fix_v = info.vcodec not in ("h264", "hevc", "av1")
    fix_a = info.has_audio and any(a.codec not in ("aac", "mp3") for a in info.audio) \
        and info.container != "mov"
    odd = bool(info.width % 2 or info.height % 2)
    do_cfr = cfr and info.is_vfr
    if not (bake or to_mp4 or fix_v or fix_a or odd or do_cfr):
        return None
    if info.container == "mov":
        if not (bake or fix_v or odd or do_cfr):
            return None  # .mov is the edit delivery format; leave it alone
        # stay in MOV (PCM audio is copied, not squeezed into AAC)
        return Recipe(fmt="mov", vcodec="h264", acodec="auto", quality="lossless",
                      speed_preset="best", bake_rotation=True, cfr=do_cfr,
                      suffix="_upright" if bake else "_compatible")
    if to_mp4:
        suffix = ""
    elif bake:
        suffix = "_upright"
    else:
        suffix = "_compatible"
    r = Recipe(fmt="mp4", vcodec="auto", acodec="auto", quality="lossless",
               speed_preset="best", bake_rotation=True, cfr=do_cfr, suffix=suffix)
    if odd:
        r.vcodec = "h264"
    return r


# ----------------------------------------------------------------------------- merge


def can_concat_losslessly(infos: list[MediaInfo]) -> bool:
    if len(infos) < 2:
        return False
    a = infos[0]

    def sig(i: MediaInfo):
        return (i.vcodec, i.width, i.height, i.rotation, i.sar, i.color_transfer,
                i.color_primaries, i.pix_fmt, round(i.fps or 0, 2),
                i.vprofile, len(i.audio),
                tuple((s.codec, s.sample_rate, s.channels) for s in i.audio))
    if not a.has_video or a.vcodec not in ("h264", "hevc"):
        return False
    if any(s.codec not in ("aac", "mp3") for s in a.audio):
        return False          # the joined file is MP4
    if not all(sig(i) == sig(a) for i in infos[1:]):
        return False
    return not any(i.preroll for i in infos)


def _concat_escape(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def build_merge(infos: list[MediaInfo], r: Recipe, out_dir: str | None = None,
                encoders: set[str] | None = None, force_reencode: bool = False,
                taken: set[str] | None = None) -> Plan:
    if len(infos) < 2:
        raise PlanError("Select at least two clips to merge")
    for i in infos:
        if not i.has_video:
            raise PlanError(f"{i.name} has no video and cannot be merged")
    first = infos[0]
    total = sum(i.duration for i in infos)
    out = output_path(first, "mp4", r.suffix or "_merged", out_dir, taken)
    tmp = temp_for(out)
    any_audio = any(i.has_audio for i in infos)
    if not force_reencode and can_concat_losslessly(infos):
        listfile = tmp + ".txt"
        argv = list(COMMON) + ["-f", "concat", "-safe", "0", "-i", listfile,
                               "-map", "0:v:0"]
        if any_audio:
            argv += ["-map", "0:a"]
        argv += ["-c", "copy"]
        if first.vcodec == "hevc":
            argv += ["-tag:v", "hvc1"]
        argv += ["-movflags", "+faststart", tmp]
        expect = Expect(kind="video", vcodec=first.vcodec, width=first.width, height=first.height,
                        rotation=first.rotation, duration=total, dur_tol=0.1 * len(infos) + 0.3,
                        acodec=first.acodec if any_audio else "",
                        audio_count=len(first.audio) if any_audio else 0,
                        audio_tracks=[(t.codec, t.sample_rate, t.channels) for t in first.audio]
                        if any_audio else [],
                        frames=sum(i.nb_frames for i in infos) if all(i.nb_frames for i in infos)
                        else None)
        plan = Plan(title=os.path.basename(out), inputs=infos, output=out, temp=tmp,
                    steps=[Step(argv, "Joining (lossless)", total)], expect=expect,
                    summary=[f"Join {len(infos)} clips losslessly (identical formats)",
                             f"Total {total:.1f}s"],
                    cleanup=[listfile],
                    files={listfile: "".join(f"file {_concat_escape(i.path)}\n" for i in infos)})
        return plan

    # re-encode path: normalize every clip to the first clip's upright size and rate
    w, h = first.display_size
    if r.scale_height:
        w, h = w * r.scale_height / h, r.scale_height
    W, H = even(w), even(h)
    F = r.fps or snap_rate(first.fps or 30)
    if F > 240:
        F = 30
    argv = list(COMMON)
    for i in infos:
        argv += ["-i", i.path]
    parts, labels = [], []
    for n, i in enumerate(infos):
        # square pixels first, so anamorphic clips keep their real shape
        tm = tonemap_chain(i) + "," if i.is_hdr else ""   # output is 8-bit SDR
        parts.append(f"[{n}:v:0]{tm}scale=trunc(iw*sar/2)*2:ih,setsar=1,"
                     f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate_expr(F)},format=yuv420p[v{n}]")
        if any_audio:
            if i.has_audio:
                parts.append(f"[{n}:a:0]aresample=48000,aformat=sample_fmts=fltp:"
                             f"channel_layouts=stereo[a{n}]")
            else:
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={i.duration:.6f},"
                             f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
        labels.append(f"[v{n}]" + (f"[a{n}]" if any_audio else ""))
    graph = ";".join(parts) + ";" + "".join(labels) + \
        f"concat=n={len(infos)}:v=1:a={1 if any_audio else 0}" + \
        ("[v][a]" if any_audio else "[v]")
    argv += ["-filter_complex", graph, "-map", "[v]"]
    if any_audio:
        argv += ["-map", "[a]"]
    choice = r.vcodec if r.vcodec in ("h264", "hevc", "av1") else "h264"
    venc = resolve_encoder(choice, r.gpu, encoders)
    fake = replace(first, bit_depth=8)
    argv += video_encoder_args(venc, r, fake) + SDR_TAGS
    if any_audio:
        argv += ["-c:a", "aac", "-b:a", f"{r.audio_kbps}k", "-ar", "48000", "-ac", "2"]
    argv += ["-movflags", "+faststart", tmp]
    expect = Expect(kind="video", vcodec=VCODEC_NAME[choice], width=W, height=H, rotation=0,
                    duration=total, dur_tol=0.15 * len(infos) + 0.4,
                    acodec="aac" if any_audio else "", audio_count=1 if any_audio else 0,
                    audio_tracks=[("aac", 48000, 2)] if any_audio else [])
    return Plan(title=os.path.basename(out), inputs=infos, output=out, temp=tmp,
                steps=[Step(argv, "Merging (re-encode)", total)], expect=expect,
                summary=[f"Join {len(infos)} clips → {W}×{H} @ {F:.3g} fps, "
                         f"{VCODEC_LABELS[choice]}{' (GPU)' if venc.endswith('_nvenc') else ''}",
                         f"Total {total:.1f}s"])


# ----------------------------------------------------------------------------- stills


def build_snapshot(info: MediaInfo, at: float, ext: str = "png", out_dir: str | None = None,
                   taken: set[str] | None = None) -> Plan:
    if not info.has_video:
        raise PlanError(f"{info.name} has no video")
    if ext not in ("png", "jpg"):
        raise PlanError("Snapshot format must be png or jpg")
    if at < 0 or (info.duration and at > info.duration):
        raise PlanError(f"Time {at:.2f}s is outside the clip (0 – {info.duration:.2f}s)")
    if info.duration:
        # the last frame starts one frame before the end; seeking past it yields nothing
        last = info.duration - 1.5 / (info.fps or 25)
        at = max(0.0, min(at, last))
    out = output_path(info, ext, f"_frame_{at:.2f}s".replace(".", "-"), out_dir, taken)
    tmp = temp_for(out)
    w, h = info.display_size
    argv = list(COMMON) + ["-ss", f"{at:.6f}", "-i", info.path, "-map", "0:v:0",
                           "-frames:v", "1", "-update", "1"]
    if info.sar != 1:
        argv += ["-vf", f"scale={w}:{h},setsar=1"]
    if ext == "jpg":
        argv += ["-q:v", "2"]
    argv.append(tmp)
    return Plan(title=os.path.basename(out), inputs=[info], output=out, temp=tmp,
                steps=[Step(argv, "Grabbing frame", 0.0)],
                expect=Expect(kind="image", vcodec="png" if ext == "png" else "mjpeg",
                              width=w, height=h),
                summary=[f"Frame at {at:.2f}s, {w}×{h} {ext.upper()}"])


def build_contact_sheet(info: MediaInfo, cols: int = 4, rows: int = 4, thumb_w: int = 320,
                        out_dir: str | None = None, taken: set[str] | None = None) -> Plan:
    if not info.has_video:
        raise PlanError(f"{info.name} has no video")
    if not (1 <= cols <= 12 and 1 <= rows <= 12):
        raise PlanError("Grid must be between 1×1 and 12×12")
    if info.duration <= 0:
        raise PlanError("Clip has no duration")
    n = cols * rows
    w, h = info.display_size
    tw = even(thumb_w)
    th = even(h * tw / w)
    pad, margin = 4, 8
    W = cols * tw + (cols - 1) * pad + 2 * margin
    H = rows * th + (rows - 1) * pad + 2 * margin
    out = output_path(info, "jpg", "_contact_sheet", out_dir, taken)
    tmp = temp_for(out)
    # pick n evenly spaced frames: sample at n/duration fps, shifted half an interval in
    rate = n / info.duration
    vf = (f"fps={rate:.6f}:start_time={info.duration / n / 2:.6f},"
          f"scale={tw}:{th}:flags=lanczos,setsar=1,"
          f"tile={cols}x{rows}:padding={pad}:margin={margin}:color=0x202020")
    argv = list(COMMON) + ["-i", info.path, "-map", "0:v:0", "-vf", vf,
                           "-frames:v", "1", "-update", "1", "-q:v", "3", tmp]
    return Plan(title=os.path.basename(out), inputs=[info], output=out, temp=tmp,
                steps=[Step(argv, "Building contact sheet", info.duration)],
                expect=Expect(kind="image", vcodec="mjpeg", width=W, height=H),
                summary=[f"{cols}×{rows} grid of {tw}×{th} thumbnails → {W}×{H} JPEG"])


# ----------------------------------------------------------------------------- crop detect


def detect_crop(info: MediaInfo, timeout: float = 60) -> tuple[int, int, int, int] | None:
    """Find black bars. Returns (w, h, x, y) in upright pixels, or None when there are none."""
    if not info.has_video:
        return None
    dur = info.duration or 0
    start = max(0.0, dur * 0.25) if dur > 4 else 0.0
    span = min(10.0, max(1.0, dur - start)) if dur else 10.0
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-i", info.path,
            "-t", f"{span:.3f}", "-map", "0:v:0", "-vf", "cropdetect=limit=24:round=2:reset=0",
            "-f", "null", "-"]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", res.stderr)
    if not found:
        return None
    w, h, x, y = (int(v) for v in Counter(found).most_common(1)[0][0])
    dw, dh = info.display_size
    if w <= 0 or h <= 0 or (w >= dw - 2 and h >= dh - 2):
        return None
    return w, h, x, y
