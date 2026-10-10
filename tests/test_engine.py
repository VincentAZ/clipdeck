"""End-to-end engine tests: every operation runs real ffmpeg on synthetic media and the
result is checked independently (ffprobe + decoded pixels), on top of the app's own
verification."""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

# keep the in-progress journal and settings away from a real Clipdeck running beside us
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="clipdeck-test-config-")

from clipdeck import media, ops, runner
from clipdeck.jobs import CANCELLED, DONE, FAILED, JobQueue
from clipdeck.media import probe
from clipdeck.ops import PlanError, Recipe

from . import fixtures
from .fixtures import red_corner

ENC = ops.available_encoders()
HAVE_NVENC = "h264_nvenc" in ENC


def setUpModule():
    global SRC
    SRC = fixtures.media_dir()


def src(name):
    return os.path.join(SRC, name)


def read(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="clipdeck-out-")

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def run_plan(self, plan, thorough=True, expect_ok=True):
        cancel = threading.Event()
        seen = []
        res = runner.execute(plan, cancel, lambda f, t: seen.append(f), thorough=thorough)
        if expect_ok:
            self.assertTrue(res.ok, f"{plan.title} failed: {res.failures}\n{res.log[-3000:]}")
            self.assertTrue(os.path.isfile(res.output))
            self.assertFalse(os.path.exists(plan.temp), "temp file left behind")
            # progress is monotonic-ish and finishes at 1
            self.assertEqual(seen[-1], 1.0)
        return res

    def build(self, name, **kw):
        return ops.build(probe(src(name)), Recipe(**kw), out_dir=self.out, encoders=ENC)


# ------------------------------------------------------------------------------- probing

class ProbeTests(Base):
    def test_phone_clip(self):
        i = probe(src("phone.mp4"))
        self.assertEqual((i.width, i.height), (180, 320))
        self.assertEqual(i.rotation, -90)
        self.assertEqual(i.display_size, (320, 180))
        self.assertEqual(i.acodec, "aac")
        self.assertIn("sideways", [x.key for x in i.issues()])

    def test_guvcview_mkv_issues(self):
        keys = {x.key for x in probe(src("guv.mkv")).issues()}
        self.assertTrue({"container", "vcodec"} <= keys, keys)

    def test_flac_issue(self):
        self.assertIn("acodec", [x.key for x in probe(src("flac.mp4")).issues()])

    def test_clean_file_has_no_issues(self):
        self.assertEqual(probe(src("upright.mp4")).issues(), [])

    def test_vfr_detected(self):
        i = probe(src("vfr.mp4"))
        self.assertTrue(i.is_vfr, (i.fps, i.r_fps))

    def test_anamorphic_display_size(self):
        i = probe(src("anamorphic.mp4"))
        self.assertEqual(i.display_size, (853, 480))

    def test_two_audio_tracks(self):
        self.assertEqual(len(probe(src("twoaudio.mkv")).audio), 2)

    def test_hdr(self):
        i = probe(src("hdr.mp4"))
        self.assertTrue(i.is_hdr)
        self.assertEqual(i.bit_depth, 10)

    def test_garbage_raises(self):
        with self.assertRaises(media.ProbeError):
            probe(src("fake.mp4"))
        with self.assertRaises(media.ProbeError):
            probe(src("does-not-exist.mp4"))

    def test_parse_time(self):
        cases = {"90": 90, "1:30": 90, "0:01:30.5": 90.5, "1m30s": 90, "2.5": 2.5,
                 "1h": 3600, " 0:05 ": 5}
        for text, want in cases.items():
            self.assertAlmostEqual(media.parse_time(text), want, msg=text)
        for bad in ("", "abc", "1:2:3:4", "1::2", "-5", "1:-3"):
            with self.assertRaises(ValueError, msg=bad):
                media.parse_time(bad)

    def test_formatters(self):
        self.assertEqual(media.fmt_duration(3725), "1:02:05")
        self.assertEqual(media.fmt_duration(65), "1:05")
        self.assertEqual(media.fmt_size(1536), "1.5 KB")
        self.assertEqual(media.fmt_bitrate(2_500_000), "2.5 Mb/s")


# ------------------------------------------------------------------------------- auto-fix

class AutoFixTests(Base):
    def fix(self, name):
        info = probe(src(name))
        r = ops.autofix_recipe(info)
        self.assertIsNotNone(r, f"{name} should need fixing")
        return info, self.run_plan(ops.build(info, r, out_dir=self.out, encoders=ENC))

    def test_phone_clip_baked_upright(self):
        _, res = self.fix("phone.mp4")
        self.assertTrue(res.output.endswith("phone_upright.mp4"))
        o = probe(res.output)
        self.assertEqual((o.width, o.height, o.rotation), (320, 180, 0))
        self.assertEqual(red_corner(res.output), "TL")
        self.assertEqual(o.acodec, "aac")

    def test_mkv_to_mp4(self):
        _, res = self.fix("guv.mkv")
        self.assertTrue(res.output.endswith("guv.mp4"))
        o = probe(res.output)
        self.assertEqual((o.vcodec, o.acodec), ("h264", "aac"))

    def test_flac_audio_only_reencoded(self):
        _, res = self.fix("flac.mp4")
        self.assertTrue(res.output.endswith("flac_compatible.mp4"))
        o = probe(res.output)
        self.assertEqual((o.vcodec, o.acodec), ("h264", "aac"))

    def test_flac_plan_copies_video(self):
        info = probe(src("flac.mp4"))
        plan = ops.build(info, ops.autofix_recipe(info), out_dir=self.out, encoders=ENC)
        self.assertEqual(plan.expect.video_md5_from, info.path)

    def test_clean_needs_nothing(self):
        self.assertIsNone(ops.autofix_recipe(probe(src("upright.mp4"))))
        self.assertIsNone(ops.autofix_recipe(probe(src("öther.mov"))))

    def test_odd_dims_avi(self):
        _, res = self.fix("odd.avi")
        o = probe(res.output)
        self.assertEqual((o.width % 2, o.height % 2), (0, 0))
        self.assertEqual(o.vcodec, "h264")

    def test_vfr_to_cfr(self):
        info = probe(src("vfr.mp4"))
        self.assertIsNone(ops.autofix_recipe(info))
        r = ops.autofix_recipe(info, cfr=True)
        res = self.run_plan(ops.build(info, r, out_dir=self.out, encoders=ENC))
        self.assertFalse(probe(res.output).is_vfr)

    def test_replace_original_uses_trash(self):
        work = os.path.join(self.out, "w")
        os.makedirs(work)
        shutil.copy(src("phone.mp4"), os.path.join(work, "clip.mp4"))
        trashed = []
        orig = runner.trash

        def fake_trash(p):
            dest = os.path.join(self.out, "trash-" + os.path.basename(p))
            shutil.move(p, dest)
            trashed.append(dest)
            return ""
        runner.trash = fake_trash
        try:
            info = probe(os.path.join(work, "clip.mp4"))
            r = ops.autofix_recipe(info)
            r.replace_original = True
            res = self.run_plan(ops.build(info, r, encoders=ENC))
        finally:
            runner.trash = orig
        self.assertEqual(res.output, os.path.join(work, "clip.mp4"))
        self.assertEqual(probe(res.output).rotation, 0)
        self.assertEqual(len(trashed), 1)
        self.assertEqual(os.listdir(work), ["clip.mp4"])


# ------------------------------------------------------------------------------- geometry

class GeometryTests(Base):
    def corner(self, **kw):
        res = self.run_plan(self.build("upright.mp4", **kw))
        return res, red_corner(res.output)

    def test_rotate_cw(self):
        res, c = self.corner(rotate=90)
        self.assertEqual(c, "TR")
        self.assertEqual((probe(res.output).width, probe(res.output).height), (180, 320))

    def test_rotate_180(self):
        self.assertEqual(self.corner(rotate=180)[1], "BR")

    def test_rotate_ccw(self):
        self.assertEqual(self.corner(rotate=270)[1], "BL")

    def test_flips(self):
        self.assertEqual(self.corner(hflip=True)[1], "TR")
        self.assertEqual(self.corner(vflip=True)[1], "BL")

    def test_rotation_applies_after_baking(self):
        # phone clip displays upright (TL); rotating it 90 CW must put the marker TR
        res = self.run_plan(self.build("phone.mp4", rotate=90))
        self.assertEqual(red_corner(res.output), "TR")
        self.assertEqual(probe(res.output).rotation, 0)

    def test_lossless_rotate(self):
        res = self.run_plan(self.build("upright.mp4", rotate=90, rotate_lossless=True))
        o = probe(res.output)
        self.assertEqual((o.width, o.height), (320, 180))     # pixels untouched
        self.assertEqual(o.rotation, -90)
        self.assertEqual(red_corner(res.output), "TR")         # but players show it rotated

    def test_lossless_unrotate_phone(self):
        # rotating the phone clip by 270 CW (= 90 CCW) cancels its flag entirely
        res = self.run_plan(self.build("phone.mp4", rotate=270, rotate_lossless=True))
        self.assertEqual(probe(res.output).rotation, 0)
        self.assertEqual(red_corner(res.output), "BL")

    def test_lossless_rejects_other_edits(self):
        with self.assertRaises(PlanError):
            self.build("upright.mp4", rotate=90, rotate_lossless=True, scale_height=90)

    def test_crop(self):
        res = self.run_plan(self.build("upright.mp4", crop=(100, 60, 0, 0)))
        o = probe(res.output)
        self.assertEqual((o.width, o.height), (100, 60))
        self.assertEqual(red_corner(res.output), "TL")

    def test_crop_out_of_bounds(self):
        with self.assertRaises(PlanError):
            self.build("upright.mp4", crop=(400, 60, 0, 0))

    def test_scale(self):
        res = self.run_plan(self.build("upright.mp4", scale_height=90))
        self.assertEqual((probe(res.output).width, probe(res.output).height), (160, 90))
        res = self.run_plan(self.build("upright.mp4", scale_width=101))
        self.assertEqual((probe(res.output).width, probe(res.output).height), (102, 56))

    def test_anamorphic_to_square(self):
        res = self.run_plan(self.build("anamorphic.mp4", vcodec="h264"))
        o = probe(res.output)
        self.assertEqual((o.width, o.height, o.sar), (854, 480, 1))

    def test_cropdetect(self):
        c = ops.detect_crop(probe(src("letterbox.mp4")))
        self.assertEqual(c, (320, 140, 0, 20))
        self.assertIsNone(ops.detect_crop(probe(src("upright.mp4"))))


# ------------------------------------------------------------------------------- time

class TimeTests(Base):
    def test_exact_trim(self):
        res = self.run_plan(self.build("upright.mp4", trim_start=0.5, trim_end=2.0))
        self.assertAlmostEqual(probe(res.output).duration, 1.5, delta=0.1)

    def test_fast_trim_copies(self):
        plan = self.build("upright.mp4", trim_start=1.0, trim_exact=False)
        self.assertIn("copy", plan.steps[0].argv)
        self.run_plan(plan)

    def test_bad_trims(self):
        for kw in ({"trim_start": 5}, {"trim_start": 2, "trim_end": 1}, {"trim_start": -1}):
            with self.assertRaises(PlanError, msg=kw):
                self.build("upright.mp4", **kw)

    def test_speed_up_and_down(self):
        for speed, want in ((2.0, 1.5), (0.5, 6.0), (4.0, 0.75)):
            res = self.run_plan(self.build("upright.mp4", speed=speed))
            o = probe(res.output)
            self.assertAlmostEqual(o.duration, want, delta=0.2, msg=speed)
            self.assertTrue(o.has_audio)

    def test_atempo_chain(self):
        self.assertEqual(ops.atempo_chain(4.0), ["atempo=2.0", "atempo=2"])
        self.assertEqual(ops.atempo_chain(0.25), ["atempo=0.5", "atempo=0.5"])
        self.assertEqual(ops.atempo_chain(1.5), ["atempo=1.5"])

    def test_fps_change(self):
        res = self.run_plan(self.build("upright.mp4", fps=15))
        o = probe(res.output)
        self.assertAlmostEqual(o.fps, 15, delta=0.01)
        self.assertEqual(o.nb_frames, 45)


# ------------------------------------------------------------------------------- formats

class FormatTests(Base):
    def check(self, name, want_v=None, want_a=None, **kw):
        res = self.run_plan(self.build(name, **kw))
        o = probe(res.output)
        if want_v is not None:
            self.assertEqual(o.vcodec, want_v)
        if want_a is not None:
            self.assertEqual(o.acodec, want_a)
        return o

    def test_mp4_variants(self):
        self.check("guv.mkv", "h264", "aac", fmt="mp4")
        self.check("upright.mp4", "hevc", "aac", fmt="mp4", vcodec="hevc")
        self.check("upright.mp4", "av1", "aac", fmt="mp4", vcodec="av1")

    def test_mov_for_resolve(self):
        o = self.check("upright.mp4", "h264", "pcm_s24le", fmt="mov")
        self.assertEqual(o.container, "mov")
        self.check("hevc.mp4", "hevc", "pcm_s24le", fmt="mov")
        self.check("silent60.mp4", "h264", "", fmt="mov")

    def test_mov_intermediates(self):
        self.check("upright.mp4", "prores", "pcm_s24le", fmt="mov", vcodec="prores")
        self.check("upright.mp4", "dnxhd", "pcm_s24le", fmt="mov", vcodec="dnxhr")

    def test_mkv_webm(self):
        self.check("upright.mp4", "h264", "aac", fmt="mkv")
        self.check("upright.mp4", "vp9", "opus", fmt="webm")

    def test_multi_audio_kept(self):
        o = self.check("twoaudio.mkv", "h264", "aac", fmt="mp4")
        self.assertEqual(len(o.audio), 2)

    def test_audio_only(self):
        for fmt, codec in (("mp3", "mp3"), ("m4a", "aac"), ("wav", "pcm_s24le"),
                           ("flac", "flac"), ("opus", "opus")):
            o = self.check("upright.mp4", "", codec, fmt=fmt)
            self.assertFalse(o.has_video, fmt)

    def test_audio_from_silent_fails(self):
        with self.assertRaises(PlanError):
            self.build("silent60.mp4", fmt="mp3")

    def test_gif(self):
        res = self.run_plan(self.build("upright.mp4", fmt="gif", gif_width=160, gif_fps=10))
        o = probe(res.output)
        self.assertEqual((o.vcodec, o.width, o.height), ("gif", 160, 90))
        self.assertEqual(red_corner(res.output), "TL")

    def test_gif_native(self):
        res = self.run_plan(self.build("silent60.mp4", fmt="gif", gif_width=0, gif_fps=0,
                                       gif_per_frame_palette=False))
        self.assertEqual(probe(res.output).width, 320)

    def test_gif_of_phone_clip_is_upright(self):
        res = self.run_plan(self.build("phone.mp4", fmt="gif", gif_width=0, gif_fps=0))
        self.assertEqual(red_corner(res.output), "TL")

    def test_hdr_stays_10bit_in_hevc(self):
        res = self.run_plan(self.build("hdr.mp4", fmt="mkv", vcodec="hevc", scale_height=90))
        o = probe(res.output)
        self.assertIn("10", o.pix_fmt)
        self.assertEqual(o.color_transfer, "arib-std-b67")

    def test_invalid_combos(self):
        with self.assertRaises(PlanError):
            self.build("upright.mp4", fmt="webm", vcodec="h264")
        with self.assertRaises(PlanError):
            self.build("guv.mkv", fmt="mp4", vcodec="copy")
        with self.assertRaises(PlanError):
            self.build("upright.mp4", vcodec="copy", scale_height=90)
        with self.assertRaises(PlanError):
            self.build("upright.mp4", acodec="copy", speed=2)

    @unittest.skipUnless(HAVE_NVENC, "no NVENC")
    def test_gpu_encoders(self):
        self.check("upright.mp4", "h264", "aac", gpu=True, vcodec="h264", scale_height=90)
        self.check("hdr.mp4", "hevc", None, gpu=True, vcodec="hevc", fmt="mkv")
        self.check("upright.mp4", "av1", "aac", gpu=True, vcodec="av1")

    def test_target_size_two_pass(self):
        plan = self.build("long.mp4", target_mb=1.0, vcodec="h264")
        self.assertEqual(len(plan.steps), 2)
        res = self.run_plan(plan, thorough=False)
        self.assertLess(os.path.getsize(res.output), 1.05 * 1024 * 1024)
        self.assertEqual(res.warnings, [])
        for f in plan.cleanup:
            self.assertFalse(os.path.exists(f), f)

    def test_target_too_small(self):
        with self.assertRaises(PlanError):
            self.build("long.mp4", target_mb=0.01)


# ------------------------------------------------------------------------------- audio & metadata

class AudioMetaTests(Base):
    def test_remove_audio(self):
        res = self.run_plan(self.build("upright.mp4", remove_audio=True))
        self.assertFalse(probe(res.output).has_audio)

    def test_remove_audio_copies_video(self):
        plan = self.build("upright.mp4", remove_audio=True)
        self.assertEqual(plan.expect.video_md5_from, src("upright.mp4"))

    def test_normalize_keeps_rate(self):
        res = self.run_plan(self.build("upright.mp4", normalize_audio=True))
        self.assertEqual(probe(res.output).audio[0].sample_rate,
                         probe(src("upright.mp4")).audio[0].sample_rate)

    def test_volume(self):
        self.run_plan(self.build("upright.mp4", volume_db=-6))

    def test_strip_metadata(self):
        def tags(p):
            return subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags",
                                   "-of", "json", p], capture_output=True, text=True).stdout
        self.assertIn("40.6892", tags(src("phone.mp4")))
        res = self.run_plan(self.build("phone.mp4", strip_metadata=True))
        self.assertNotIn("40.6892", tags(res.output))
        res = self.run_plan(self.build("phone.mp4"))
        self.assertIn("40.6892", tags(res.output))


# ------------------------------------------------------------------------------- merge & stills

class MergeStillTests(Base):
    def test_lossless_merge(self):
        infos = [probe(src("part a.mp4")), probe(src("part a.mp4"))]
        self.assertTrue(ops.can_concat_losslessly(infos))
        plan = ops.build_merge(infos, Recipe(), out_dir=self.out, encoders=ENC)
        res = self.run_plan(plan)
        self.assertAlmostEqual(probe(res.output).duration, 3.0, delta=0.2)
        self.assertFalse(os.path.exists(plan.files and list(plan.files)[0]))

    def test_mixed_merge(self):
        infos = [probe(src(n)) for n in ("part a.mp4", "part 'b'.mp4", "öther.mov")]
        self.assertFalse(ops.can_concat_losslessly(infos))
        res = self.run_plan(ops.build_merge(infos, Recipe(), out_dir=self.out, encoders=ENC))
        o = probe(res.output)
        self.assertEqual((o.width, o.height), (320, 180))
        self.assertAlmostEqual(o.duration, 3.5, delta=0.3)
        self.assertTrue(o.has_audio)

    def test_merge_needs_two(self):
        with self.assertRaises(PlanError):
            ops.build_merge([probe(src("part a.mp4"))], Recipe(), out_dir=self.out)

    def test_snapshot(self):
        res = self.run_plan(ops.build_snapshot(probe(src("phone.mp4")), 1.0, "png",
                                               out_dir=self.out))
        o = probe(res.output)
        self.assertEqual((o.width, o.height), (320, 180))
        self.assertEqual(red_corner(res.output), "TL")
        res = self.run_plan(ops.build_snapshot(probe(src("upright.mp4")), 0.2, "jpg",
                                               out_dir=self.out))
        self.assertEqual(probe(res.output).vcodec, "mjpeg")

    def test_snapshot_out_of_range(self):
        with self.assertRaises(PlanError):
            ops.build_snapshot(probe(src("upright.mp4")), 99)

    def test_contact_sheet(self):
        plan = ops.build_contact_sheet(probe(src("long.mp4")), 3, 2, 200, out_dir=self.out)
        res = self.run_plan(plan)
        o = probe(res.output)
        self.assertEqual((o.width, o.height), (plan.expect.width, plan.expect.height))


# ------------------------------------------------------------------------------- safety

class SafetyTests(Base):
    def test_never_overwrites(self):
        target = os.path.join(self.out, "upright_edited.mp4")
        with open(target, "w") as f:
            f.write("precious")
        res = self.run_plan(self.build("upright.mp4", scale_height=90))
        self.assertEqual(read(target), "precious")
        self.assertTrue(res.output.endswith("upright_edited (2).mp4"))

    def test_publish_race_never_overwrites(self):
        plan = self.build("upright.mp4", scale_height=90)
        # someone creates the target while we encode
        with open(plan.output, "w") as f:
            f.write("late arrival")
        res = self.run_plan(plan)
        self.assertEqual(read(plan.output), "late arrival")
        self.assertNotEqual(res.output, plan.output)

    def test_reserved_names_unique(self):
        info = probe(src("upright.mp4"))
        a = ops.build(info, Recipe(scale_height=90), out_dir=self.out)
        b = ops.build(info, Recipe(scale_height=90), out_dir=self.out, taken={a.output})
        self.assertNotEqual(a.output, b.output)

    def test_output_never_equals_input(self):
        info = probe(src("upright.mp4"))
        plan = ops.build(info, Recipe(scale_height=90))   # same dir as source, same ext
        self.assertNotEqual(plan.output, info.path)

    def test_failed_verification_quarantines(self):
        plan = self.build("upright.mp4", scale_height=90)
        plan.expect.width = 999
        res = self.run_plan(plan, expect_ok=False)
        self.assertFalse(res.ok)
        self.assertTrue(res.output.endswith(".BAD.mp4"))
        self.assertTrue(any("size" in f for f in res.failures))
        self.assertFalse(os.path.exists(plan.output))

    def test_ffmpeg_failure_cleans_up(self):
        plan = self.build("upright.mp4", scale_height=90)
        plan.steps[0].argv.insert(-1, "-bogus_option")
        res = self.run_plan(plan, expect_ok=False)
        self.assertFalse(res.ok)
        self.assertFalse(os.path.exists(plan.temp))
        self.assertIn("bogus", res.log)

    def test_cancel(self):
        plan = self.build("long.mp4", vcodec="h264", speed_preset="best")
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        t0 = time.monotonic()
        res = runner.execute(plan, cancel, lambda f, t: None)
        self.assertFalse(res.ok)
        self.assertEqual(res.failures, ["cancelled"])
        self.assertLess(time.monotonic() - t0, 8)
        self.assertFalse(os.path.exists(plan.temp))
        self.assertFalse(os.path.exists(plan.output))

    def test_corrupt_output_detected(self):
        plan = self.build("upright.mp4", scale_height=90)
        res = self.run_plan(plan)
        # chop the file in half and re-verify it
        data = read(res.output, "rb")
        bad = os.path.join(self.out, "chopped.mp4")
        with open(bad, "wb") as f:
            f.write(data[: len(data) // 2])
        fails, _ = runner.verify(plan, bad, True, threading.Event(), __import__("collections").deque(),
                                 lambda f: None)
        self.assertTrue(fails)


class ReviewRegressionTests(Base):
    """One test per bug found in the independent review."""

    def test_01_exact_trim_mkv_audio_is_cut_too(self):
        res = self.run_plan(self.build("longgop.mp4", fmt="mkv", trim_start=1.3, trim_end=2.0))
        self.assertAlmostEqual(probe(res.output).duration, 0.7, delta=0.15)

    def test_01b_explicit_audio_copy_with_exact_trim_refused(self):
        with self.assertRaises(PlanError):
            self.build("longgop.mp4", fmt="mkv", trim_start=1.3, acodec="copy")

    def test_02_fast_trim_long_gop_mkv(self):
        self.run_plan(self.build("longgop.mp4", fmt="mkv", trim_start=5, trim_end=7,
                                 trim_exact=False))

    def test_03_lossless_flips(self):
        for kw in (dict(rotate=0, hflip=True), dict(rotate=90, vflip=True)):
            res = self.run_plan(self.build("upright.mp4", rotate_lossless=True, **kw))
            want = "TR" if kw.get("hflip") else None
            if want:
                self.assertEqual(red_corner(res.output), want)

    def test_04_live_webm_without_duration(self):
        i = probe(src("live.webm"))
        self.assertAlmostEqual(i.duration, 3.0, delta=0.2)
        self.run_plan(ops.build(i, ops.autofix_recipe(i), out_dir=self.out, encoders=ENC))

    def test_04b_still_image_rejected_clearly(self):
        with self.assertRaisesRegex(PlanError, "still image"):
            self.build("still.png", fmt="mp4")

    def test_05_cancel_during_verification(self):
        plan = self.build("long.mp4", fmt="mkv")      # copy: encoding is instant
        cancel = threading.Event()

        def progress(f, text):
            if text == "Verifying":
                cancel.set()
        res = runner.execute(plan, cancel, progress)
        self.assertFalse(res.ok)
        self.assertEqual(res.failures, ["cancelled"])
        self.assertFalse(os.path.exists(plan.output))
        self.assertFalse(os.path.exists(plan.temp))

    def test_06_each_audio_track_keeps_its_channels(self):
        res = self.run_plan(self.build("stereo_mono.mkv", fmt="mkv", volume_db=3))
        self.assertEqual([a.channels for a in probe(res.output).audio], [2, 1])

    def test_07_anamorphic_merge_fills_frame(self):
        infos = [probe(src("anamorphic.mp4")), probe(src("part a.mp4"))]
        res = self.run_plan(ops.build_merge(infos, Recipe(), out_dir=self.out, encoders=ENC,
                                            force_reencode=True))
        from tests.fixtures import frame
        w, h, p = frame(res.output, 0.2)
        self.assertEqual((w, h), (854, 480))
        left = p[(240 * w + 10) * 3:(240 * w + 10) * 3 + 3]
        self.assertGreater(sum(left), 60, "left edge is black: picture was squeezed")

    def test_08_preroll_clips_not_joined_losslessly(self):
        infos = [probe(src("fasttrim.mp4")), probe(src("fasttrim.mp4"))]
        self.assertFalse(ops.can_concat_losslessly(infos))
        res = self.run_plan(ops.build_merge(infos, Recipe(), out_dir=self.out, encoders=ENC))
        self.assertAlmostEqual(probe(res.output).duration, 2 * infos[0].duration, delta=0.4)

    def test_09_mkv_remux_keeps_subtitles(self):
        res = self.run_plan(self.build("subs.mkv", fmt="mkv", bake_rotation=False))
        self.assertEqual(probe(res.output).subtitle_count, 1)

    def test_10_avi_bframes_copy(self):
        self.run_plan(self.build("xvid.avi", fmt="mkv"))
        i = probe(src("h264.avi"))
        res = self.run_plan(ops.build(i, ops.autofix_recipe(i), out_dir=self.out, encoders=ENC))
        self.assertEqual(probe(res.output).vcodec, "h264")

    def test_11_sideways_mov_stays_mov_with_pcm(self):
        i = probe(src("rot.mov"))
        r = ops.autofix_recipe(i)
        self.assertEqual(r.fmt, "mov")
        res = self.run_plan(ops.build(i, r, out_dir=self.out, encoders=ENC))
        o = probe(res.output)
        self.assertEqual((o.container, o.acodec, o.rotation), ("mov", "pcm_s24le", 0))

    def test_12_snapshot_at_the_very_end(self):
        i = probe(src("part 'b'.mp4"))
        for ext in ("png", "jpg"):
            self.run_plan(ops.build_snapshot(i, i.duration - 0.01, ext, out_dir=self.out))

    def test_13_dnxhr_too_small(self):
        with self.assertRaisesRegex(PlanError, "256"):
            self.build("upright.mp4", fmt="mov", vcodec="dnxhr", scale_height=90)

    def test_14_vfr_into_other_containers(self):
        for kw in (dict(fmt="mkv", vcodec="h264"), dict(fmt="webm"),
                   dict(fmt="mov", vcodec="prores")):
            self.run_plan(self.build("vfr.mp4", **kw), thorough=False)

    def test_15_ntsc_rates_exact(self):
        res = self.run_plan(self.build("upright.mp4", fps=29.97))
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=r_frame_rate", "-of", "csv=p=0", res.output],
                           capture_output=True, text=True).stdout.strip()
        self.assertEqual(r, "30000/1001")
        self.assertEqual(ops.rate_expr(23.976), "24000/1001")
        self.assertEqual(ops.snap_rate(29.79), 29.97)

    def test_16_opus_mkvs_join_by_reencoding(self):
        infos = [probe(src("o1.mkv")), probe(src("o2.mkv"))]
        self.assertFalse(ops.can_concat_losslessly(infos))
        self.run_plan(ops.build_merge(infos, Recipe(), out_dir=self.out, encoders=ENC))

    def test_vfr_frames_preserved_in_every_container(self):
        # found on real Pixel footage: MOV defaulted to constant rate and dropped frames,
        # then a coarse encoder time base made two timestamps collide
        # jitter.mp4 reproduces the drop (115 of 120 frames before the fix); its frames
        # are microseconds apart, which only MP4/MOV time bases can represent
        for kw in (dict(fmt="mov"), dict(fmt="mp4", vcodec="h264"), dict(fmt="mov", vcodec="hevc")):
            self.run_plan(self.build("jitter.mp4", **kw))   # thorough: frame count + decode
        for kw in (dict(fmt="mkv", vcodec="hevc"), dict(fmt="webm"), dict(fmt="mov")):
            self.run_plan(self.build("cadence.mp4", **kw))

    # ---- plausible concerns, now handled
    def test_keep_rotation_flag_when_not_baking(self):
        res = self.run_plan(self.build("phone.mp4", vcodec="h264", bake_rotation=False,
                                       scale_height=160))
        o = probe(res.output)
        self.assertEqual((o.rotation, o.display_size), (-90, (284, 160)))

    def test_no_bake_with_crop_refused(self):
        with self.assertRaises(PlanError):
            self.build("phone.mp4", bake_rotation=False, crop=(100, 100, 0, 0))

    def test_hdr_tonemapped_for_h264(self):
        res = self.run_plan(self.build("hdr.mp4", vcodec="h264"))
        self.assertEqual(probe(res.output).color_transfer, "bt709")

    def test_target_size_with_prores_refused(self):
        with self.assertRaises(PlanError):
            self.build("upright.mp4", fmt="mov", vcodec="prores", target_mb=5)

    def test_publish_without_hardlinks(self):
        tmp = os.path.join(self.out, "t.bin")
        with open(tmp, "w") as f:
            f.write("new")
        final = os.path.join(self.out, "f.bin")
        with open(final, "w") as f:
            f.write("old")
        real_link = os.link
        os.link = lambda a, b: (_ for _ in ()).throw(OSError(1, "no links"))
        try:
            got = runner.publish(tmp, final)
        finally:
            os.link = real_link
        self.assertEqual(read(final), "old")
        self.assertEqual(read(got), "new")

    def test_missing_source_fails_cleanly(self):
        work = os.path.join(self.out, "m")
        os.makedirs(work)
        shutil.copy(src("upright.mp4"), work)
        plan = ops.build(probe(os.path.join(work, "upright.mp4")), Recipe(scale_height=90))
        os.unlink(os.path.join(work, "upright.mp4"))
        res = runner.execute(plan, threading.Event(), lambda f, t: None)
        self.assertFalse(res.ok)
        self.assertIn("no longer exists", res.failures[0])

    def test_replace_waits_for_earlier_readers(self):
        work = os.path.join(self.out, "r")
        os.makedirs(work)
        shutil.copy(src("long.mp4"), work)
        info = probe(os.path.join(work, "long.mp4"))
        q = JobQueue(lambda j: None, workers=2)
        reader = q.submit(ops.build(info, Recipe(vcodec="h264", scale_height=360)))
        r = Recipe(vcodec="h264", scale_height=180, replace_original=True)
        orig = runner.trash
        runner.trash = lambda p: (os.rename(p, p + ".trashed"), True)[1]
        try:
            replacer = q.submit(ops.build(info, r, taken={reader.plan.output}))
            deadline = time.time() + 120
            while not (reader.finished and replacer.finished) and time.time() < deadline:
                if replacer.state == "running":
                    self.assertTrue(reader.finished, "replace job started before the reader")
                time.sleep(0.05)
        finally:
            runner.trash = orig
            q.shutdown()
        self.assertEqual((reader.state, replacer.state), (DONE, DONE),
                         (reader.result.failures, replacer.result.failures))

    def test_interlaced_not_vfr(self):
        i = probe(src("upright.mp4"))
        i.field_order, i.fps, i.r_fps = "tt", 29.97, 59.94
        self.assertFalse(i.is_vfr)

    def test_queue_shrinks(self):
        q = JobQueue(lambda j: None, workers=3)
        q.submit(self.build("upright.mp4", scale_height=90))
        time.sleep(0.3)
        q.set_workers(1)
        deadline = time.time() + 30
        while len([t for t in q._workers if t.is_alive()]) > 1 and time.time() < deadline:
            time.sleep(0.1)
        self.assertLessEqual(len([t for t in q._workers if t.is_alive()]), 1)
        q.shutdown()


class SecondReviewTests(Base):
    """One test per bug found in the second independent review."""

    def test_01_speed_on_coarse_time_bases(self):
        for name, speed in (("h264.avi", 2.0), ("xvid.avi", 1.5), ("ts30.mp4", 2.0),
                            ("h264.avi", 1.25)):
            res = self.run_plan(self.build(name, speed=speed))
            self.assertAlmostEqual(probe(res.output).duration,
                                   probe(src(name)).duration / speed, delta=0.25)

    def test_02_preroll_sources(self):
        i = probe(src("fasttrim.mp4"))
        self.assertTrue(i.preroll)
        for kw in (dict(fmt="mp4", vcodec="h264"), dict(fmt="webm"), dict(fmt="mkv")):
            res = self.run_plan(self.build("fasttrim.mp4", **kw))
            self.assertAlmostEqual(probe(res.output).duration, i.duration, delta=0.3)
        with self.assertRaises(PlanError):
            self.build("fasttrim.mp4", fmt="mkv", vcodec="copy")
        with self.assertRaises(PlanError):
            self.build("fasttrim.mp4", fmt="mkv", vcodec="h264", acodec="copy")

    def test_03_avi_explicit_copy_to_mp4_refused(self):
        with self.assertRaisesRegex(PlanError, "AVI"):
            self.build("h264.avi", fmt="mp4", vcodec="copy")
        self.run_plan(self.build("xvid.avi", fmt="mkv", vcodec="copy"))

    def test_04_hdr_with_only_transfer_tagged(self):
        for kw in (dict(fmt="gif"), dict(fmt="mp4", vcodec="h264")):
            res = self.run_plan(self.build("hdr_trc_only.mp4", **kw))
        self.assertEqual(probe(res.output).color_transfer, "bt709")

    def test_05_merge_hdr_with_sdr(self):
        infos = [probe(src("hdr.mp4")), probe(src("upright.mp4"))]
        res = self.run_plan(ops.build_merge(infos, Recipe(), out_dir=self.out, encoders=ENC))
        self.assertEqual(probe(res.output).color_transfer, "bt709")

    def test_06_lossless_rotate_with_target_refused(self):
        with self.assertRaises(PlanError):
            self.build("long.mp4", rotate_lossless=True, rotate=90, target_mb=1)

    def test_07_target_size_counts_every_audio_track(self):
        res = self.run_plan(self.build("two20.mkv", fmt="mp4", target_mb=3), thorough=False)
        self.assertEqual(res.warnings, [])
        self.assertLessEqual(os.path.getsize(res.output), 3 * 1024 * 1024 * 1.05)

    def test_08_extract_first_track_ignores_others(self):
        res = self.run_plan(self.build("aac_ac3.mkv", fmt="m4a", acodec="copy"))
        self.assertEqual(probe(res.output).acodec, "aac")

    def test_09_stale_plan_after_replace(self):
        work = os.path.join(self.out, "s")
        os.makedirs(work)
        shutil.copy(src("phone.mp4"), work)
        info = probe(os.path.join(work, "phone.mp4"))
        later = ops.build(info, Recipe(fmt="mkv", bake_rotation=False), out_dir=work)
        time.sleep(0.01)
        with open(info.path, "ab") as f:          # the file is replaced/modified meanwhile
            f.write(b"\0")
        res = runner.execute(later, threading.Event(), lambda f, t: None)
        self.assertFalse(res.ok)
        self.assertIn("changed", res.failures[0])
        self.assertFalse(any(".BAD." in n for n in os.listdir(work)))

    def test_10_constant_rate_stays_constant(self):
        res = self.run_plan(self.build("ntsc.mkv", fmt="mov", vcodec="prores"))
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=avg_frame_rate", "-of", "csv=p=0", res.output],
                           capture_output=True, text=True).stdout.strip()
        self.assertEqual(r, "30000/1001")

    def test_11_cover_art_does_not_become_video(self):
        i = probe(src("cover.mkv"))
        self.assertTrue(i.has_cover)
        res = self.run_plan(self.build("cover.mkv", fmt="mkv", bake_rotation=False))
        n = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                            "stream=index", "-of", "csv=p=0", res.output],
                           capture_output=True, text=True).stdout.split()
        self.assertEqual(len(n), 1)
        self.assertEqual(probe(res.output).subtitle_count, 1)

    def test_12_avi_slow_motion_to_mkv(self):
        res = self.run_plan(self.build("h264.avi", fmt="mkv", speed=0.5))
        self.assertAlmostEqual(probe(res.output).duration, 6.0, delta=0.3)

    def test_speed_with_explicit_fps(self):
        res = self.run_plan(self.build("upright.mp4", speed=2.0, fps=30))
        o = probe(res.output)
        self.assertAlmostEqual(o.fps, 30, delta=0.01)
        self.assertAlmostEqual(o.duration, 1.5, delta=0.15)

    def test_summary_mentions_baking(self):
        i = probe(src("phone.mp4"))
        plan = ops.build(i, ops.autofix_recipe(i), out_dir=self.out)
        self.assertTrue(any("upright" in s for s in plan.summary), plan.summary)


class LeakTests(Base):
    def test_no_threads_left_behind(self):
        before = threading.active_count()
        for _ in range(3):
            self.run_plan(self.build("upright.mp4", scale_height=90))
        time.sleep(0.6)
        self.assertLessEqual(threading.active_count(), before)


class QueueTests(Base):
    def test_queue_runs_and_cancels(self):
        events = []
        q = JobQueue(lambda j: events.append((j.id, j.state)), workers=2)
        info = probe(src("upright.mp4"))
        long = probe(src("long.mp4"))
        taken = set()
        jobs = []
        for h in (90, 120):
            p = ops.build(info, Recipe(scale_height=h), out_dir=self.out, taken=taken)
            taken.add(p.output)
            jobs.append(q.submit(p))
        slow = q.submit(ops.build(long, Recipe(vcodec="h264", speed_preset="best"),
                                  out_dir=self.out, taken=taken))
        queued = q.submit(ops.build(long, Recipe(vcodec="h264", scale_height=360),
                                    out_dir=self.out, taken=taken | {slow.plan.output}))
        q.cancel(queued)
        self.assertEqual(queued.state, CANCELLED)
        deadline = time.time() + 60
        while not all(j.finished for j in jobs) and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual([j.state for j in jobs], [DONE, DONE])
        while slow.state == "queued" and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        q.cancel(slow)
        while not slow.finished and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(slow.state, CANCELLED)
        self.assertEqual(q.active_count(), 0)
        q.shutdown()
        self.assertTrue(any(s == DONE for _, s in events))
        outs = sorted(os.listdir(self.out))
        self.assertEqual(len([o for o in outs if o.endswith(".mp4")]), 2, outs)

    def test_failed_job_state(self):
        q = JobQueue(lambda j: None)
        plan = self.build("upright.mp4", scale_height=90)
        plan.expect.vcodec = "nope"
        job = q.submit(plan)
        deadline = time.time() + 30
        while not job.finished and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(job.state, FAILED)
        q.shutdown()


class ThirdReviewTests(Base):
    # ---- planning
    def test_stream_lengths_differ(self):
        # keeping only the shorter stream must expect that stream's length, not the file's
        self.run_plan(self.build("alonger.mp4", remove_audio=True))
        self.run_plan(self.build("alonger.mp4", fmt="gif", gif_width=160))
        self.run_plan(self.build("vlonger.mp4", fmt="m4a"))
        self.run_plan(self.build("vlonger.mp4", fmt="mp3"))

    def test_opus_three_and_four_channels(self):
        for name, ch in (("ch3.mkv", 3), ("ch4.mkv", 4)):
            res = self.run_plan(self.build(name, fmt="webm"))
            self.assertEqual(probe(res.output).audio[0].channels, ch)

    def test_odd_sample_rate_is_snapped(self):
        for fmt, rate in (("m4a", 32000), ("mp3", 32000)):
            res = self.run_plan(self.build("odd_rate.wav", fmt=fmt))
            self.assertEqual(probe(res.output).audio[0].sample_rate, rate)

    def test_odd_crop_without_even_rounding(self):
        crop = (201, 101, 11, 11)
        res = self.run_plan(self.build("upright.mp4", fmt="gif", gif_width=0, crop=crop))
        self.assertEqual((probe(res.output).width, probe(res.output).height), (201, 101))
        res = self.run_plan(self.build("upright.mp4", fmt="mov", vcodec="prores", crop=crop))
        self.assertEqual((probe(res.output).width, probe(res.output).height), (201, 101))

    def test_preroll_is_never_copied(self):
        with self.assertRaises(PlanError):
            self.build("fasttrim.mp4", fmt="mp4", vcodec="copy", bake_rotation=False)
        with self.assertRaises(PlanError):
            self.build("fasttrim.mp4", fmt="mkv", rotate_lossless=True, rotate=90)
        res = self.run_plan(self.build("fasttrim.mp4", fmt="mov", strip_metadata=True))
        self.assertAlmostEqual(probe(res.output).duration, 2.0, delta=0.15)

    def test_lossless_rotation_refuses_avi_to_mp4(self):
        with self.assertRaises(PlanError):
            self.build("h264.avi", fmt="mp4", rotate_lossless=True, rotate=90)

    def test_svtav1_minimum_size(self):
        with self.assertRaises(PlanError):
            self.build("tiny.mp4", vcodec="av1", scale_height=32)

    @unittest.skipUnless(HAVE_NVENC, "no NVENC")
    def test_too_small_for_gpu_falls_back_to_cpu(self):
        plan = self.build("tiny.mp4", vcodec="hevc", gpu=True)
        self.assertNotIn("hevc_nvenc", plan.steps[0].argv)
        self.assertIn("libx265", plan.steps[0].argv)
        self.run_plan(plan)

    def test_target_size_counts_pcm_audio(self):
        # MOV keeps PCM: 20 s of 48 kHz stereo 24-bit is ~5.5 MB on its own
        with self.assertRaises(PlanError):
            self.build("st48.mp4", fmt="mov", vcodec="h264", target_mb=3)
        res = self.run_plan(self.build("st48.mp4", fmt="mov", vcodec="h264", acodec="pcm16",
                                       target_mb=6))
        self.assertLessEqual(os.path.getsize(res.output), 6 * 1048576)

    def test_gif_rate_capped_after_speedup(self):
        plan = self.build("silent60.mp4", fmt="gif", gif_fps=0, gif_width=160, speed=2.0)
        self.assertIn("fps=50", " ".join(plan.steps[0].argv))
        self.run_plan(plan)

    def test_contact_sheet_seeks_and_cleans_up(self):
        plan = ops.build_contact_sheet(probe(src("long.mp4")), 4, 3, 160, out_dir=self.out)
        self.assertEqual(len(plan.steps), 13)
        res = self.run_plan(plan)
        self.assertEqual(sorted(os.listdir(self.out)), [os.path.basename(res.output)])

    def test_long_names_fit(self):
        long = os.path.join(self.out, "ü" * 120 + ".mp4")       # 240 bytes
        shutil.copy(src("upright.mp4"), long)
        plan = ops.build(probe(long), Recipe(scale_height=90), encoders=ENC)
        self.assertLessEqual(len(os.path.basename(plan.output).encode()), 255)
        self.assertLessEqual(len(os.path.basename(plan.temp).encode()), 255)
        self.run_plan(plan)

    def test_cfr_fix_covers_nearly_constant_clips(self):
        # 29.97 nominal with a few dropped frames: under the VFR warning threshold, but
        # "constant frame rate" must still make it exactly constant
        path = os.path.join(self.out, "drops.mp4")
        fixtures.run("-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=10",
                     "-vf", "select='not(eq(n\\,150))'", "-fps_mode", "vfr",
                     "-c:v", "libx264", path)
        info = probe(path)
        self.assertFalse(info.is_vfr)
        self.assertFalse(info.is_exact_cfr)
        self.assertIsNone(ops.autofix_recipe(info))
        res = self.run_plan(ops.build(info, ops.autofix_recipe(info, cfr=True), out_dir=self.out,
                                      encoders=ENC))
        self.assertTrue(probe(res.output).is_exact_cfr)

    def test_contact_sheet_uses_video_length(self):
        plan = ops.build_contact_sheet(probe(src("alonger.mp4")), 4, 4, 80, out_dir=self.out)
        self.run_plan(plan)
        # a still that cannot be grabbed fails the job instead of leaving a blank tile
        plan = ops.build_contact_sheet(probe(src("alonger.mp4")), 2, 1, 80, out_dir=self.out)
        plan.steps[1].argv[plan.steps[1].argv.index("-ss") + 1] = "60"
        self.assertFalse(self.run_plan(plan, expect_ok=False).ok)

    def test_bit_depth_from_pixel_format(self):
        path = os.path.join(self.out, "nv12.mkv")
        fixtures.run("-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.2", "-pix_fmt", "nv12",
                     "-c:v", "rawvideo", path)
        self.assertEqual(probe(path).bit_depth, 8)
        self.assertNotIn("yuv420p10le", self.build_path(path, vcodec="hevc").steps[0].argv)

    def build_path(self, path, **kw):
        return ops.build(probe(path), Recipe(**kw), out_dir=self.out, encoders=ENC)

    def test_kept_result_survives_next_start(self):
        plan = self.build("upright.mp4", scale_height=90)
        orig = runner.publish

        def full_disk(tmp, final):
            if final == plan.output:
                raise OSError(28, "No space left on device")
            return orig(tmp, final)
        runner.publish = full_disk
        try:
            res = self.run_plan(plan, expect_ok=False)
        finally:
            runner.publish = orig
        self.assertTrue(os.path.isfile(res.output), res.failures)
        runner.clean_stale_partials()
        self.assertTrue(os.path.isfile(res.output), "kept result deleted at next start")
        os.unlink(res.output)

    # ---- running
    def test_size_target_retries_when_over(self):
        plan = self.build("long.mp4", vcodec="h264", target_mb=1.0)
        plan.expect.max_bytes = int(0.6 * 1048576)     # pretend the first encode lands over
        res = self.run_plan(plan)
        self.assertLessEqual(os.path.getsize(res.output), plan.expect.max_bytes)
        self.assertTrue(any("to fit the size" in w for w in res.warnings), res.warnings)

    def test_edit_list_output_frame_count(self):
        # a lossless copy of a keyframe-trimmed MP4 must not fail on hidden frames
        trimmed = self.run_plan(self.build("longgop.mp4", vcodec="copy", acodec="copy",
                                           trim_start=5, trim_end=7, trim_exact=False))
        info = probe(trimmed.output)
        self.assertTrue(info.preroll)
        self.run_plan(ops.build(info, Recipe(fmt="mkv"), out_dir=self.out, encoders=ENC))

    def test_dangling_symlink_at_output_name(self):
        plan = self.build("upright.mp4", scale_height=90)
        os.symlink("/nonexistent/target", plan.output)
        res = self.run_plan(plan)
        self.assertNotEqual(res.output, plan.output)
        self.assertTrue(os.path.islink(plan.output))

    def test_checksum_failure_is_a_failure(self):
        plan = self.build("upright.mp4", vcodec="copy", acodec="copy", fmt="mkv")
        plan.expect.video_md5_from = os.path.join(self.out, "missing.mp4")
        res = self.run_plan(plan, expect_ok=False)
        self.assertFalse(res.ok)

    def test_ffmpeg_error_text_in_failure(self):
        plan = self.build("upright.mp4", scale_height=90)
        plan.steps[0].argv.insert(-1, "-nonsense-option")
        res = self.run_plan(plan, expect_ok=False)
        self.assertIn("Option not found", res.failures[0])

    def test_trash_failure_reason_kept(self):
        work = os.path.join(self.out, "w")
        os.makedirs(work)
        shutil.copy(src("phone.mp4"), os.path.join(work, "clip.mp4"))
        orig = runner.trash
        runner.trash = lambda p: "Trashing on system internal mounts is not supported"
        try:
            info = probe(os.path.join(work, "clip.mp4"))
            r = ops.autofix_recipe(info)
            r.replace_original = True
            res = self.run_plan(ops.build(info, r, encoders=ENC))
        finally:
            runner.trash = orig
        self.assertTrue(any("not supported" in w for w in res.warnings), res.warnings)
        self.assertTrue(os.path.exists(os.path.join(work, "clip.mp4")))

    def test_replace_into_output_folder_stays_there(self):
        work, dest = os.path.join(self.out, "w"), os.path.join(self.out, "dest")
        os.makedirs(work)
        os.makedirs(dest)
        shutil.copy(src("phone.mp4"), os.path.join(work, "clip.mp4"))
        orig = runner.trash
        runner.trash = lambda p: (os.unlink(p), "")[1]
        try:
            info = probe(os.path.join(work, "clip.mp4"))
            r = ops.autofix_recipe(info)
            r.replace_original = True
            res = self.run_plan(ops.build(info, r, out_dir=dest, encoders=ENC))
        finally:
            runner.trash = orig
        self.assertEqual(os.path.dirname(res.output), dest)
        self.assertEqual(os.listdir(work), [])

    def test_stale_partials_cleaned(self):
        stale = os.path.join(self.out, ".x-0123456789.clipdeck-partial.mp4")
        keep = os.path.join(self.out, "precious.mp4")
        for p in (stale, keep):
            open(p, "w").close()
        runner._journal(add=[stale, keep])
        runner.clean_stale_partials()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(keep))      # never anything but our own temp names

    def test_settings_survive_odd_json(self):
        from clipdeck.settings import Settings, config_dir
        os.makedirs(config_dir(), exist_ok=True)
        path = os.path.join(config_dir(), "settings.json")
        for raw in ("null", "42", '"x"', '{"workers": true, "thorough": 0}'):
            with open(path, "w") as f:
                f.write(raw)
            s = Settings.load()
            self.assertEqual((s.workers, s.thorough), (1, True))
        os.unlink(path)

    # ---- queue ordering
    def wait(self, jobs, timeout=120):
        deadline = time.time() + timeout
        while not all(j.finished for j in jobs) and time.time() < deadline:
            time.sleep(0.05)

    def test_replace_original_blocks_later_readers(self):
        work = os.path.join(self.out, "w")
        os.makedirs(work)
        path = os.path.join(work, "clip.mp4")
        shutil.copy(src("phone.mp4"), path)
        orig = runner.trash
        runner.trash = lambda p: (os.unlink(p), "")[1]
        try:
            q = JobQueue(lambda j: None, workers=2)
            info = probe(path)
            r = ops.autofix_recipe(info)
            r.replace_original = True
            fix = q.submit(ops.build(info, r, encoders=ENC))
            later = q.submit(ops.build(info, Recipe(fmt="mkv"), out_dir=self.out,
                                       encoders=ENC))
            self.wait([fix, later])
            q.shutdown()
        finally:
            runner.trash = orig
        self.assertEqual(fix.state, DONE)
        # it read the file only after the replacement, and says so instead of mixing both
        self.assertEqual(later.state, FAILED)
        self.assertIn("source changed", later.result.failures[0])

    def test_failed_dependency_keeps_original(self):
        q = JobQueue(lambda j: None)
        first = self.build("upright.mp4", scale_height=90)
        first.expect.vcodec = "nope"                 # will fail verification
        a = q.submit(first)
        r = Recipe(scale_height=120, replace_original=True)
        b = q.submit(ops.build(probe(src("upright.mp4")), r, out_dir=self.out,
                               encoders=ENC, taken={first.output}), after=a)
        self.wait([a, b])
        q.shutdown()
        self.assertEqual((a.state, b.state), (FAILED, FAILED))
        self.assertIn("left untouched", b.result.failures[0])
        self.assertTrue(os.path.exists(src("upright.mp4")))

    def test_callback_error_does_not_kill_worker(self):
        calls = []

        def boom(j):
            calls.append(j.state)
            if len(calls) == 2:
                raise RuntimeError("UI exploded")
        q = JobQueue(boom)
        jobs = [q.submit(self.build("upright.mp4", scale_height=90))]
        jobs.append(q.submit(ops.build(probe(src("upright.mp4")), Recipe(scale_height=120),
                                       out_dir=self.out, encoders=ENC,
                                       taken={jobs[0].plan.output})))
        from unittest import mock
        with mock.patch("traceback.print_exc"):       # the expected error, not noise
            self.wait(jobs)
            q.shutdown()
        self.assertEqual([j.state for j in jobs], [DONE, DONE])


if __name__ == "__main__":
    unittest.main()
