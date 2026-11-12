"""Recorder engine: device listing parsers, error explanations, and real recordings from
a synthetic camera and microphone (lavfi), checked with ffprobe."""

import os
import tempfile
import threading
import time
import unittest

# keep the in-progress journal away from a real Clipdeck running beside us
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="clipdeck-test-config-")

from clipdeck import recorder, runner
from clipdeck.media import probe
from clipdeck.recorder import Camera, Microphone, Recording, Session

FORMATS = """\
[video4linux2,v4l2 @ 0x6] Compressed:       mjpeg :          Motion-JPEG : 1280x720 640x480 1920x1080
[video4linux2,v4l2 @ 0x6] Raw       :     yuyv422 :           YUYV 4:2:2 : 640x480 1280x720 320x240
/dev/video0: Immediate exit requested
"""

SOURCES = """\
Auto-detected sources for pulse:
  alsa_input.usb-Cam-02.mono-fallback [USB 2.0 Camera Mono] (none)
  alsa_output.usb-Head-01.analog-stereo.monitor [Monitor of Headset] (none)
* alsa_input.usb-Head-01.mono-fallback [Headset Mono] (none)
"""

FAKE_CAM = Camera("Test pattern", "fake", ((1280, 720, ""), (640, 360, "")),
                  fake="testsrc2=size={w}x{h}:rate={rate}")
FAKE_MIC = Microphone("Test tone", "sine=frequency=440:sample_rate=48000", "lavfi")


def record(seconds: float, mic=FAKE_MIC, cam=FAKE_CAM, folder=None, size=(640, 360)):
    """Record for `seconds` after the first frame; returns (Saved, frames, levels, rc)."""
    folder = folder or tempfile.mkdtemp(prefix="clipdeck-rec-")
    rec = Recording(folder, mic)
    frames, levels, done, out = [], [], threading.Event(), {}
    s = Session(recorder.command(cam, size, mic, rec.tmp), frames.append, levels.append,
                lambda rc, log: (out.update(rc=rc, log=log), done.set()))
    s.start()
    end = time.monotonic() + 15
    while not frames and time.monotonic() < end and not done.is_set():
        time.sleep(0.02)
    time.sleep(seconds)
    s.stop()
    assert done.wait(20), "ffmpeg did not stop"
    return rec, rec.finish(out["log"]), frames, levels, out


class ParseTest(unittest.TestCase):
    def test_formats_prefer_mjpeg_and_sort_largest_first(self):
        modes = recorder.parse_formats(FORMATS)
        self.assertEqual(modes, ((1920, 1080, "mjpeg"), (1280, 720, "mjpeg"),
                                 (640, 480, "mjpeg"), (320, 240, "yuyv422")))
        self.assertEqual(Camera("c", "/dev/video0", modes).default_size(), (1920, 1080))
        self.assertEqual(Camera("c", "/dev/video0", modes[2:]).default_size(), (640, 480))

    def test_metadata_node_has_no_formats(self):
        self.assertEqual(recorder.parse_formats(
            "[video4linux2,v4l2 @ 0x6] ioctl(VIDIOC_G_INPUT): Inappropriate ioctl for device"),
            ())

    def test_sources_skip_monitors_default_first(self):
        mics = recorder.parse_sources(SOURCES)
        self.assertEqual([(m.name, m.default) for m in mics],
                         [("Headset Mono", True), ("USB 2.0 Camera Mono", False)])
        self.assertEqual(mics[0].input_args()[-1], "alsa_input.usb-Head-01.mono-fallback")

    def test_camera_args(self):
        cam = Camera("c", "/dev/video0", recorder.parse_formats(FORMATS))
        args = cam.input_args((320, 240))
        self.assertEqual(args[args.index("-input_format") + 1], "yuyv422")
        self.assertEqual(args[args.index("-video_size") + 1], "320x240")
        self.assertIn("mono2abs", args)
        self.assertEqual(args[-1], "/dev/video0")

    def test_explain(self):
        text, fix = recorder.explain(["[video4linux2,v4l2 @ 0x1] ioctl(VIDIOC_STREAMON): "
                                      "Device or resource busy"])
        self.assertIn("in use by another app", text)
        self.assertEqual(fix, "")
        text, fix = recorder.explain(["Cannot open video device /dev/video0: "
                                      "Permission denied"])
        self.assertIn("video", text)
        self.assertEqual(fix, recorder.GROUP_FIX)
        self.assertEqual(recorder.explain([])[0], "ffmpeg stopped unexpectedly.")

    def test_encoder_fallbacks(self):
        self.assertIn("libx264", recorder.video_encoder_args({"libx264", "h264_nvenc"}))
        self.assertIn("h264_nvenc", recorder.video_encoder_args({"h264_nvenc"}))
        self.assertIn("mpeg4", recorder.video_encoder_args(set()))


class RecordTest(unittest.TestCase):
    def test_record_with_sound(self):
        rec, saved, frames, levels, out = record(2.5)
        self.assertTrue(saved.ok, saved.error)
        self.assertEqual(os.path.basename(saved.path), os.path.basename(rec.final))
        self.assertFalse(os.path.exists(rec.tmp), "work file left behind")
        self.assertEqual(runner._journal_read(), [])
        info = probe(saved.path)
        self.assertEqual((info.vcodec, info.acodec), ("h264", "aac"))
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertAlmostEqual(info.fps, recorder.RATE, delta=0.5)
        self.assertGreater(info.duration, 2.0)
        self.assertLess(abs(info.video_duration - info.audio_duration), 0.3)
        self.assertEqual(info.issues(), [], "a recording needs no fixing")
        # preview: whole rgb frames, about PREVIEW_FPS a second
        self.assertGreater(len(frames), 20)
        self.assertTrue(all(len(f) == recorder.FRAME_BYTES for f in frames))
        # a sine at amplitude 1/8 peaks at -18 dBFS
        self.assertGreater(len(levels), 20)
        self.assertAlmostEqual(sorted(levels)[len(levels) // 2], -18.06, delta=1.0)

    def test_preview_is_live(self):
        """Frames and levels arrive while previewing, not in bursts or only at exit
        (ffmpeg buffers metadata output and skips filters no output reads)."""
        frames, levels = [], []
        s = Session(recorder.command(FAKE_CAM, (640, 360), FAKE_MIC), frames.append,
                    levels.append, lambda rc, log: None)
        s.start()
        try:
            end = time.monotonic() + 5
            while (len(frames) < 10 or len(levels) < 10) and time.monotonic() < end:
                time.sleep(0.05)
            self.assertGreaterEqual(len(frames), 10)
            self.assertGreaterEqual(len(levels), 10)
            self.assertIsNone(s.proc.poll(), "still running")
        finally:
            s.kill()
            s.wait(5)

    def test_record_without_microphone(self):
        _rec, saved, _f, levels, _o = record(1.2, mic=None)
        self.assertTrue(saved.ok, saved.error)
        info = probe(saved.path)
        self.assertTrue(info.has_video)
        self.assertFalse(info.has_audio)
        self.assertEqual(levels, [])
        self.assertEqual(saved.warnings, [])

    def test_never_overwrites(self):
        folder = tempfile.mkdtemp(prefix="clipdeck-rec-")
        name = recorder.recording_name()
        with open(os.path.join(folder, name), "w") as f:
            f.write("keep me")
        rec = Recording(folder, None, name=name)
        frames, done, out = [], threading.Event(), {}
        s = Session(recorder.command(FAKE_CAM, (640, 360), None, rec.tmp), frames.append,
                    lambda _db: None, lambda rc, log: (out.update(log=log), done.set()))
        s.start()
        time.sleep(1.5)
        s.stop()
        done.wait(20)
        saved = rec.finish(out["log"])
        self.assertTrue(saved.ok, saved.error)
        self.assertTrue(saved.path.endswith(" (2).mp4"), saved.path)
        with open(os.path.join(folder, name)) as f:
            self.assertEqual(f.read(), "keep me")

    def test_broken_camera_saves_nothing(self):
        broken = Camera("Broken", "fake", fake="nosuchsource={w}x{h}")
        folder = tempfile.mkdtemp(prefix="clipdeck-rec-")
        rec = Recording(folder, None)
        done, out = threading.Event(), {}
        s = Session(recorder.command(broken, None, None, rec.tmp), lambda _b: None,
                    lambda _db: None, lambda rc, log: (out.update(rc=rc, log=log), done.set()))
        s.start()
        self.assertTrue(done.wait(20))
        self.assertNotEqual(out["rc"], 0)
        saved = rec.finish(out["log"])
        self.assertFalse(saved.ok)
        self.assertTrue(saved.error)
        self.assertEqual(os.listdir(folder), [], "no partial file left behind")
        self.assertEqual(runner._journal_read(), [])


if __name__ == "__main__":
    unittest.main()
