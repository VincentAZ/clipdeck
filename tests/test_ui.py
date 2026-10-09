"""End-to-end UI test: drives the real app under Xvfb through every page.

Run directly:   xvfb-run -a python3 -m tests.test_ui
or via unittest (spawns xvfb-run itself):   python3 -m unittest tests.test_ui
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOTS = os.path.join(ROOT, "screenshots")


def scenario(app, win):
    from clipdeck import runner
    from clipdeck.jobs import CANCELLED, DONE
    from clipdeck.media import probe
    from tests import fixtures
    from tests.ui_harness import Wait, screenshot

    os.makedirs(SHOTS, exist_ok=True)
    src = fixtures.media_dir()
    work = tempfile.mkdtemp(prefix="clipdeck-ui-work-")
    trash_dir = tempfile.mkdtemp(prefix="clipdeck-ui-trash-")
    for n in ("phone.mp4", "guv.mkv", "flac.mp4", "upright.mp4", "letterbox.mp4", "part a.mp4",
              "part 'b'.mp4", "fake.mp4"):
        shutil.copy(os.path.join(src, n), work)
    shutil.copy(os.path.join(src, "upright.mp4"), os.path.join(work, "Tom & Jerry <1>.mp4"))
    shutil.copy(os.path.join(src, "part a.mp4"), os.path.join(work, "part a copy.mp4"))

    def fake_trash(p):
        shutil.move(p, os.path.join(trash_dir, os.path.basename(p)))
        return True
    runner.trash = fake_trash

    def check(cond, msg):
        if not cond:
            raise AssertionError(msg)

    def items_named(*names):
        found = [win.library.find(os.path.join(work, n)) for n in names]
        check(all(found), f"missing library items {names}")
        return found

    def select(*names):
        win.selection.unselect_all()
        win._select_items(items_named(*names))

    def idle():
        return Wait(lambda: win.queue.active_count() == 0, "queue to drain", 240)

    def page(name):
        win.stack.set_visible_child_name(name)

    yield 1.0
    screenshot(win, f"{SHOTS}/ui-00-empty.png")

    # ---------------------------------------------------------------- library
    win.add_paths([work])
    yield Wait(lambda: len(win.library) == 10 and
               all(i.state != "probing" for i in win.library.items()), "probing")
    bad = win.library.find(os.path.join(work, "fake.mp4"))
    check(bad.state == "error", "fake.mp4 should be unreadable")
    check(work not in bad.error, f"error message should not repeat the path: {bad.error}")
    check(win.library.find(os.path.join(work, "Tom & Jerry <1>.mp4")).ready, "markup name")
    # adding the same folder again must not duplicate anything
    win.add_paths([work])
    check(len(win.library) == 10, "duplicates added")
    yield 1.5     # thumbnails

    # ---------------------------------------------------------------- inspect
    select("phone.mp4")
    page("inspect")
    yield 1.0
    check(win.inspect.stack.get_visible_child_name() == "single", "inspect single view")
    stream = win.inspect.player.video.get_media_stream()
    if stream is not None:
        stream.play()
        yield 1.0
        stream.pause()
    yield 0.4
    screenshot(win, f"{SHOTS}/ui-01-inspect.png")
    select("Tom & Jerry <1>.mp4")
    yield 0.5
    screenshot(win, f"{SHOTS}/ui-01b-inspect-markup-name.png")

    # ---------------------------------------------------------------- quick fix
    select("phone.mp4", "guv.mkv", "flac.mp4", "upright.mp4")
    page("fix")
    yield 0.5
    check(win.fix.button.get_label() == "Fix 3 files", win.fix.button.get_label())
    win.fix.mov.set_active(True)
    yield 0.3
    check(win.fix.button.get_label() == "Fix 7 files", win.fix.button.get_label())
    win.fix.replace.set_active(True)
    yield 0.3
    screenshot(win, f"{SHOTS}/ui-02-fix.png")
    win.fix.button.emit("clicked")
    check(win.stack.get_visible_child_name() == "queue", "should jump to queue")
    yield 1.0
    screenshot(win, f"{SHOTS}/ui-03-queue-running.png")
    yield idle()
    states = [j.state for j in win.queue.jobs]
    check(states == [DONE] * 7, f"fix jobs: {states} "
          f"{[j.result.failures for j in win.queue.jobs if j.result and not j.result.ok]}")
    o = probe(os.path.join(work, "phone.mp4"))
    check((o.rotation, o.width, o.height) == (0, 320, 180), "phone.mp4 replaced upright")
    check(not os.path.exists(os.path.join(work, "guv.mkv")), "guv.mkv should be in trash")
    check(probe(os.path.join(work, "guv.mp4")).vcodec == "h264", "guv.mp4")
    check(probe(os.path.join(work, "flac.mp4")).acodec == "aac", "flac.mp4 replaced")
    for n in ("phone", "guv", "flac", "upright"):
        m = probe(os.path.join(work, n + ".mov"))
        check(m.acodec == "pcm_s24le" and m.rotation == 0, f"{n}.mov")
    check(sorted(os.listdir(trash_dir)) == ["flac.mp4", "guv.mkv", "phone.mp4"],
          f"trash: {os.listdir(trash_dir)}")
    yield 0.5
    check(win.library.find(os.path.join(work, "guv.mkv")) is None, "guv.mkv left library")
    leftovers = [f for f in os.listdir(work) if "clipdeck-partial" in f or ".BAD." in f]
    check(not leftovers, f"leftovers {leftovers}")
    screenshot(win, f"{SHOTS}/ui-04-queue-done.png")
    win.fix.mov.set_active(False)
    win.fix.replace.set_active(False)

    # ---------------------------------------------------------------- edit
    select("upright.mp4")
    page("edit")
    e = win.edit
    yield 1.0
    from clipdeck.ui.edit import PRESETS
    for key, _label, _v in PRESETS:
        e.preset.set_key(key)
        yield 0.35
        check(e.queue_btn.get_sensitive(), f"preset {key} cannot queue: {e.error.get_label()}")
        if key == "gif":
            check(e.gif_group.get_visible(), "gif group visible for gif")
        if key in ("mp3", "wav"):
            check(not e.geo_group.get_visible(), "no rotate for audio")
    screenshot(win, f"{SHOTS}/ui-05-edit-wav.png")
    # changing a preset's own setting flips the label to Custom
    e.preset.set_key("chat")
    yield 0.2
    e.resize.set_key("480")
    yield 0.2
    check(e.preset.get_key() == "custom", f"preset label stale: {e.preset.get_key()}")
    e.preset.set_key("remux")
    yield 0.2
    e.bake.set_active(True)
    yield 0.2
    check(e.preset.get_key() == "custom", "remux preset label stale after Make upright")
    e.preset.set_key("share")
    yield 0.2
    e.rot.set_active_name("90")
    yield 0.4
    check(e.preset.get_key() == "share", "rotation is not part of the preset")
    check("180×320" in e.summary.get_label(), e.summary.get_label())
    screenshot(win, f"{SHOTS}/ui-06-edit-rotate.png")
    e.queue_btn.emit("clicked")
    yield idle()
    job = win.queue.jobs[-1]
    check(job.state == DONE, f"rotate job {job.result.failures if job.result else ''}")
    check(fixtures.red_corner(job.result.output) == "TR", "rotated output orientation")
    page("edit")
    yield 0.3
    e.rot.set_active_name("0")

    # invalid input is caught before anything is queued
    e.trim.set_enable_expansion(True)
    e.t_start.set_text("abc")
    yield 0.4
    check(e.error.get_visible() and "not a time" in e.error.get_label(), e.error.get_label())
    check(not e.queue_btn.get_sensitive(), "queue must be disabled on bad input")
    e.t_start.set_text("0:00.5")
    e.t_end.set_text("2")
    yield 0.4
    check(e.queue_btn.get_sensitive(), e.error.get_label())
    e.queue_btn.emit("clicked")
    yield idle()
    job = win.queue.jobs[-1]
    check(job.state == DONE and abs(probe(job.result.output).duration - 1.5) < 0.1, "trim")
    page("edit")
    e.trim.set_enable_expansion(False)

    e.preset.set_key("custom")
    e.lossless.set_active(True)
    e.rot.set_active_name("90")
    e.resize.set_key("720")
    yield 0.4
    check("Lossless rotation" in e.error.get_label(), e.error.get_label())
    e.resize.set_key("0")
    yield 0.4
    check(e.queue_btn.get_sensitive(), "lossless-only rotate should be valid")
    e.queue_btn.emit("clicked")
    yield idle()
    job = win.queue.jobs[-1]
    check(job.state == DONE and probe(job.result.output).rotation == -90, "lossless rotate")
    page("edit")
    e.lossless.set_active(False)
    e.rot.set_active_name("0")

    # crop detection on the letterboxed clip
    select("letterbox.mp4")
    yield 0.5
    e._detect()
    yield Wait(lambda: e.crop.get_enable_expansion() and int(e.c_h.get_value()) == 140,
               "crop detect", 30)
    yield 0.4
    check("320×140" in e.summary.get_label(), e.summary.get_label())
    e._mark(True)
    check(e.t_start.get_text().startswith("0:0"), e.t_start.get_text())
    e.trim.set_enable_expansion(False)
    e.crop.set_enable_expansion(False)
    # aspect shortcut produces a centered 9:16 crop
    e._aspect(9 / 16)
    yield 0.4
    check((int(e.c_w.get_value()), int(e.c_h.get_value())) == (100, 180),
          (e.c_w.get_value(), e.c_h.get_value()))
    e.crop.set_enable_expansion(False)

    # ---------------------------------------------------------------- tools
    select("part a.mp4", "part 'b'.mp4")
    page("tools")
    t = win.tools
    yield 0.5
    check(len(t.merge_rows) == 2 and t.m_go.get_sensitive(), "merge ready")
    second = t._ready_order()[1]
    t._move(second, -1)
    check(t._ready_order()[0] is second, "reorder")
    yield 0.4
    screenshot(win, f"{SHOTS}/ui-07-tools.png")
    # the switch we forced on goes back off for a matching pair...
    check(t.m_reencode.get_active(), "differing clips force re-encode")
    select("part a.mp4", "part a copy.mp4")
    yield 0.4
    check(not t.m_reencode.get_active() and t.m_reencode.get_sensitive(), "forced switch reset")
    # ...but a choice the user made stays
    t.m_reencode.set_active(True)
    select("part a.mp4", "part 'b'.mp4")
    yield 0.3
    select("part a.mp4", "part a copy.mp4")
    yield 0.3
    check(t.m_reencode.get_active(), "user's re-encode choice was undone")
    t.m_reencode.set_active(False)
    select("part a.mp4", "part 'b'.mp4")
    yield 0.4
    t._move(t._ready_order()[1], -1)
    t.m_go.emit("activated")
    t.s_time.set_text("0:01")
    t.s_go.emit("activated")
    t.c_go.emit("activated")
    yield idle()
    last = win.queue.jobs[-4:]
    check(all(j.state == DONE for j in last),
          f"tools jobs {[(j.state, j.result.failures if j.result else '') for j in last]}")
    merged = [j for j in last if j.plan.output.endswith("_merged.mp4")][0]
    check(abs(probe(merged.result.output).duration - 2.5) < 0.3, "merged duration")

    # ---------------------------------------------------------------- queue page
    page("queue")
    yield 0.5
    q = win.queue_page
    check(len(q.rows) == len(win.queue.jobs), "one row per job")
    screenshot(win, f"{SHOTS}/ui-08-queue.png")
    row = next(iter(q.rows.values()))
    row._log()
    yield 0.5
    dlg = win.get_visible_dialog()
    check(dlg is not None, "log dialog")
    screenshot(win, f"{SHOTS}/ui-09-log.png")
    dlg.close()
    yield 0.3

    # cancel a long job from its row
    shutil.copy(os.path.join(src, "long.mp4"), work)
    win.add_paths([os.path.join(work, "long.mp4")], select=True)
    yield Wait(lambda: win.library.find(os.path.join(work, "long.mp4")).ready, "long probe")
    select("long.mp4")
    page("edit")
    e.preset.set_key("small")
    e.speedp.set_key("best")
    yield 0.4
    e.queue_btn.emit("clicked")
    long_job = win.queue.jobs[-1]
    yield Wait(lambda: long_job.state == "running" and long_job.progress > 0.02, "long start", 60)
    q.rows[long_job.id].b_cancel.emit("clicked")
    yield Wait(lambda: long_job.finished, "cancel", 30)
    check(long_job.state == CANCELLED, long_job.state)
    yield 0.3
    check(not any("clipdeck-partial" in f for f in os.listdir(work)), "partial left after cancel")
    q.clear_finished()
    check(not q.rows, "cleared")

    # ---------------------------------------------------------------- preferences
    win.show_preferences()
    yield 0.5
    prefs = win.get_visible_dialog()
    check(prefs is not None, "prefs open")
    screenshot(win, f"{SHOTS}/ui-10-prefs.png")
    prefs.workers.set_value(2)
    prefs.thorough.set_active(False)
    yield 0.2
    check(win.settings.workers == 2 and not win.settings.thorough, "settings applied")
    prefs.thorough.set_active(True)
    prefs.close()
    yield 0.3

    # ---------------------------------------------------------------- narrow layout
    win.set_default_size(720, 800)
    yield 1.0
    check(win.split.get_collapsed(), "sidebar collapses when narrow")
    screenshot(win, f"{SHOTS}/ui-11-narrow.png")
    win.set_default_size(1320, 860)
    yield 1.0

    # ---------------------------------------------------------------- quit while busy
    select("long.mp4")
    page("edit")
    e.queue_btn.emit("clicked")
    yield Wait(lambda: win.queue.active_count() > 0, "busy")
    win.close()
    yield 0.5
    alert = win.get_visible_dialog()
    check(alert is not None and alert.get_heading() == "Cancel running jobs?", "quit alert")
    screenshot(win, f"{SHOTS}/ui-12-quit.png")
    alert.emit("response", "quit")
    yield 1.5
    check(not any("clipdeck-partial" in f for f in os.listdir(work)),
          f"partial left on quit: {[f for f in os.listdir(work) if 'partial' in f]} "
          f"jobs={[(j.state, j.status) for j in win.queue.jobs[-2:]]}")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(trash_dir, ignore_errors=True)


def main() -> int:
    sys.path.insert(0, ROOT)
    from tests.ui_harness import run
    return run(scenario)


class UITest(unittest.TestCase):
    def test_full_ui_scenario(self):
        if not shutil.which("xvfb-run"):
            self.skipTest("xvfb-run not installed")
        res = subprocess.run(["xvfb-run", "-a", "-s", "-screen 0 1600x1000x24",
                              sys.executable, "-m", "tests.test_ui"], cwd=ROOT,
                             capture_output=True, text=True, timeout=900)
        self.assertEqual(res.returncode, 0, res.stderr[-6000:])
        # any GTK/libadwaita warning is a bug (markup errors, impossible layouts, ...)
        noisy = [line for line in res.stderr.splitlines()
                 if ("-WARNING" in line or "-CRITICAL" in line) and "DRI3" not in line]
        self.assertEqual(noisy, [], "\n".join(noisy[:20]))


if __name__ == "__main__":
    sys.exit(main())
