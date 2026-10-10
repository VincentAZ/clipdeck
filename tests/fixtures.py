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
    # ---- review regressions
    # long GOP (one keyframe per 10 s), like many phone/screen recordings
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=12", "-f", "lavfi", "-i", "sine=d=12",
        "-c:v", "libx264", "-g", "300", "-c:a", "aac", "-shortest", j("longgop.mp4"))
    # keyframe-trimmed copy: edit list hides 5 s of pre-roll
    run("-ss", "5", "-t", "2", "-i", j("longgop.mp4"), "-c", "copy", j("fasttrim.mp4"))
    # stereo + mono audio tracks
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi", "-i", "sine=d=2",
        "-f", "lavfi", "-i", "sine=f=900:d=2", "-map", "0", "-map", "1", "-map", "2",
        "-c:v", "libx264", "-c:a", "aac", "-ac:a:0", "2", "-ac:a:1", "1", "-shortest",
        j("stereo_mono.mkv"))
    # matroska with a subtitle track
    with open(j("subs.srt"), "w") as f:
        f.write("1\n00:00:00,000 --> 00:00:01,500\nHello\n")
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-i", j("subs.srt"),
        "-c:v", "libx264", "-c:s", "srt", j("subs.mkv"))
    # Xvid-style AVI with B-frames, and H.264 in AVI
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=3", "-f", "lavfi", "-i", "sine=d=3",
        "-c:v", "mpeg4", "-bf", "2", "-c:a", "libmp3lame", "-shortest", j("xvid.avi"))
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=3", "-c:v", "libx264", j("h264.avi"))
    # sideways MOV with PCM audio (an edit deliverable)
    run("-i", j("stored.mp4"), "-c:v", "copy", "-c:a", "pcm_s24le", j("stored_pcm.mov"))
    run("-display_rotation:v:0", "-90", "-i", j("stored_pcm.mov"), "-c", "copy", j("rot.mov"))
    # live WebM with no duration in the header
    live = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                           "testsrc2=s=320x180:r=30:d=3", "-f", "lavfi", "-i", "sine=d=3",
                           "-c:v", "libvpx", "-c:a", "libopus", "-shortest", "-live", "1",
                           "-f", "webm", "pipe:1"], capture_output=True, check=True).stdout
    with open(j("live.webm"), "wb") as f:
        f.write(live)
    # two identical h264+opus mkvs
    for n in ("o1.mkv", "o2.mkv"):
        run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-f", "lavfi", "-i", "sine=d=1",
            "-c:v", "libx264", "-c:a", "libopus", "-shortest", j(n))
    # Pixel-style VFR: HEVC on a 90 kHz time base, 30 fps dropping to 24 fps mid-clip
    # with jitter, stored sideways (reproduces the real-footage MOV frame drops)
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=4",
        "-vf", "setpts='if(lt(N\\,60)\\,N/30\\,2+(N-60)/24)/TB+0.003*sin(N)/TB'",
        "-fps_mode", "passthrough", "-video_track_timescale", "90000", "-c:v", "libx265",
        "-x265-params", "log-level=none", "-tag:v", "hvc1", j("pixelvfr_stored.mp4"))
    run("-display_rotation:v:0", "-90", "-i", j("pixelvfr_stored.mp4"), "-c", "copy",
        j("jitter.mp4"))
    # Pixel frame cadence: mostly 41.8 ms intervals with every 4th at 33.3 ms
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=6", "-frames:v", "140",
        "-vf", "setpts='(N*0.0418-floor(N/4)*0.0085)/TB'", "-fps_mode", "passthrough",
        "-video_track_timescale", "90000", "-c:v", "libx265", "-x265-params", "log-level=none",
        "-tag:v", "hvc1", j("cadence.mp4"))
    # ---- second review
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-c:v", "libx264",
        "-video_track_timescale", "30", j("ts30.mp4"))
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-c:v", "libx265",
        "-x265-params", "log-level=none:transfer=smpte2084", "-pix_fmt", "yuv420p10le",
        "-tag:v", "hvc1", j("hdr_trc_only.mp4"))
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=20", "-f", "lavfi", "-i", "sine=d=20",
        "-f", "lavfi", "-i", "sine=f=700:d=20", "-map", "0", "-map", "1", "-map", "2",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "192k", "-shortest",
        j("two20.mkv"))
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi", "-i", "sine=d=2",
        "-f", "lavfi", "-i", "sine=f=500:d=2", "-map", "0", "-map", "1", "-map", "2",
        "-c:v", "libx264", "-c:a:0", "aac", "-c:a:1", "ac3", "-shortest", j("aac_ac3.mkv"))
    run("-f", "lavfi", "-i", "testsrc2=s=64x64", "-frames:v", "1", j("cover.jpg"))
    run("-i", j("subs.mkv"), "-attach", j("cover.jpg"), "-metadata:s:t", "mimetype=image/jpeg",
        "-metadata:s:t", "filename=cover.jpg", "-map", "0", "-c", "copy", j("cover.mkv"))
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30000/1001:d=2", "-c:v", "mpeg4",
        j("ntsc.mkv"))
    # ---- third review
    # streams of different lengths (screen recorders): audio outlasts video and vice versa
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3", "-f", "lavfi", "-i", "sine=d=5",
        "-c:v", "libx264", "-c:a", "aac", j("alonger.mp4"))
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=5", "-f", "lavfi", "-i", "sine=d=3",
        "-c:v", "libx264", "-c:a", "aac", j("vlonger.mp4"))
    # 2.1 and 4.0 audio (libopus rejects ffmpeg's default layouts for 3 and 4 channels)
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi",
        "-i", "sine=d=2,aformat=channel_layouts=2.1", "-c:v", "libx264", "-c:a", "pcm_s16le",
        "-shortest", j("ch3.mkv"))
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi",
        "-i", "sine=d=2,aformat=channel_layouts=4.0", "-c:v", "libx264", "-c:a", "pcm_s16le",
        "-shortest", j("ch4.mkv"))
    # a sample rate neither AAC nor MP3 supports
    run("-f", "lavfi", "-i", "sine=d=2:sample_rate=37800", "-c:a", "pcm_s16le", j("odd_rate.wav"))
    # smaller than any NVENC encoder accepts
    run("-f", "lavfi", "-i", "testsrc2=s=96x64:r=30:d=1", "-c:v", "libx264", j("tiny.mp4"))
    # 20 s of 48 kHz stereo, for size targets with uncompressed audio
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=20", "-f", "lavfi",
        "-i", "sine=d=20:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-ac", "2", "-shortest", j("st48.mp4"))
    # a still image
    run("-f", "lavfi", "-i", "testsrc2=s=320x180", "-frames:v", "1", j("still.png"))
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
