"""Synthetic test media, generated once per test run with ffmpeg's lavfi sources."""

import os
import shutil
import subprocess
import tempfile

_DIR = None


def run(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args], check=True)


def media_dir() -> str:
    """Directory of source clips (built lazily, shared by all tests in the process)."""
    global _DIR
    if _DIR:
        return _DIR
    d = tempfile.mkdtemp(prefix="clipdeck-media-")
    j = lambda n: os.path.join(d, n)
    # upright marker clip: blue frame, red square in the top-left corner
    marker = "color=blue:s=320x180:d=3:r=30,drawbox=x=0:y=0:w=40:h=40:color=red:t=fill"
    run("-f", "lavfi", "-i", marker, "-f", "lavfi", "-i", "sine=f=440:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", j("upright.mp4"))
    # phone-style: pixels stored sideways, flag says rotate 90 clockwise for display
    run("-i", j("upright.mp4"), "-vf", "transpose=cclock", "-c:v", "libx264", "-c:a", "copy",
        j("stored.mp4"))
    run("-display_rotation:v:0", "-90", "-i", j("stored.mp4"), "-c", "copy",
        "-metadata", "location=+40.6892-074.0445/", j("phone.mp4"))
    # guvcview-style mkv: mpeg4 + mp2
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi", "-i", "sine=d=2",
        "-c:v", "mpeg4", "-c:a", "mp2", "-shortest", j("guv.mkv"))
    # mp4 with flac audio
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi", "-i", "sine=d=2",
        "-c:v", "libx264", "-c:a", "flac", "-strict", "-2", "-shortest", j("flac.mp4"))
    # silent 60fps
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=60:d=2", "-c:v", "libx264", j("silent60.mp4"))
    # hevc mp4
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi", "-i", "sine=d=2",
        "-c:v", "libx265", "-x265-params", "log-level=none", "-tag:v", "hvc1", "-c:a", "aac",
        "-shortest", j("hevc.mp4"))
    # letterboxed: 320x180 picture with 20px black bars top/bottom
    run("-f", "lavfi", "-i", "testsrc2=s=320x140:r=30:d=2", "-vf", "pad=320:180:0:20:black",
        "-c:v", "libx264", j("letterbox.mp4"))
    # odd dimensions in an avi
    run("-f", "lavfi", "-i", "testsrc2=s=321x181:r=25:d=2", "-c:v", "mpeg4", j("odd.avi"))
    # non-square pixels (DV-style 720x480 with 32:27 SAR)
    run("-f", "lavfi", "-i", "testsrc2=s=720x480:r=30:d=1", "-vf", "setsar=32/27",
        "-c:v", "libx264", j("anamorphic.mp4"))
    # variable frame rate: drop frames irregularly
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3",
        "-vf", "select='not(mod(n\\,3))+lt(t\\,1)'", "-fps_mode", "vfr", "-c:v", "libx264",
        j("vfr.mp4"))
    # two audio tracks
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi", "-i", "sine=f=300:d=2",
        "-f", "lavfi", "-i", "sine=f=600:d=2", "-map", "0", "-map", "1", "-map", "2",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", j("twoaudio.mkv"))
    # 10-bit HLG hevc
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-c:v", "libx265",
        "-x265-params", "log-level=none:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
        "-pix_fmt", "yuv420p10le",
        "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
        "-tag:v", "hvc1", j("hdr.mp4"))
    # merge material: two identical-format clips, a different-size silent one
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1.5", "-f", "lavfi", "-i", "sine=d=1.5",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", j("part a.mp4"))
    run("-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=1", "-f", "lavfi", "-i", "sine=d=1",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", j("part 'b'.mp4"))
    run("-f", "lavfi", "-i", "testsrc=s=640x480:r=25:d=1", "-c:v", "libx264", j("öther.mov"))
    # long-ish clip for cancellation / progress
    run("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=20", "-f", "lavfi", "-i", "sine=d=20",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", j("long.mp4"))
    # garbage that only pretends to be a video
    with open(j("fake.mp4"), "wb") as f:
        f.write(b"\x00" * 1000)
    _DIR = d
    return d


def cleanup():
    global _DIR
    if _DIR:
        shutil.rmtree(_DIR, ignore_errors=True)
        _DIR = None


def frame(path, at=0.0):
    """First frame (after autorotation) as (w, h, rgb bytes)."""
    d = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", path, "-frames:v", "1",
                        "-f", "image2pipe", "-c:v", "ppm", "-"], capture_output=True).stdout
    magic, dims, maxv, data = d.split(b"\n", 3)
    w, h = map(int, dims.split())
    return w, h, data


def red_corner(path) -> str:
    """Which corner holds the red marker: TL/TR/BL/BR, or '' if none/several."""
    w, h, p = frame(path)

    def red(x, y):
        o = (y * w + x) * 3
        r, g, b = p[o], p[o + 1], p[o + 2]
        return r > 180 and b < 80 and g < 80
    hits = [k for k, (x, y) in {"TL": (5, 5), "TR": (w - 6, 5), "BL": (5, h - 6),
                                 "BR": (w - 6, h - 6)}.items() if red(x, y)]
    return hits[0] if len(hits) == 1 else ""
