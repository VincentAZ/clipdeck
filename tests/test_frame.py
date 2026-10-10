"""Save a scrubbed frame through both previews, checking dimensions and actual pixels."""
import os
import subprocess
import sys
import tempfile

from tests import fixtures
from tests.ui_harness import Wait, run, screenshot


def scenario(app, win):
    from clipdeck.jobs import DONE
    from clipdeck.media import probe

    win.add_paths([source], select=True)
    yield Wait(lambda: win.library.find(source).ready, "probe")
    win.stack.set_visible_child_name("edit")
    edit = win.edit
    yield Wait(lambda: edit.player.video.get_media_stream() is not None and
               edit.player.video.get_media_stream().is_seekable(), "seekable edit preview")
    actions = edit.frame_actions
    assert actions.dimensions.get_label() == "Image: 320 × 180 px · 16:9"
    edit._set_crop(160, 160, 40, 10)
    assert actions.dimensions.get_label() == "Cropped image: 160 × 160 px · 1:1"
    edit._seek_trim(1.5)
    # Click immediately after seeking: the export must wait for the new timestamp.
    actions.save.emit("clicked")
    yield Wait(lambda: len(win.queue.jobs) == 1 and win.queue.active_count() == 0,
               "cropped frame saved", 30)
    first = win.queue.jobs[0]
    assert first.state == DONE, first.status
    result = probe(first.plan.output)
    assert (result.width, result.height) == (160, 160)
    pixel = subprocess.check_output(["ffmpeg", "-v", "error", "-i", first.plan.output,
                                     "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    assert pixel[2] > 200 and pixel[0] < 30, pixel  # Blue after one second.
    yield 0.3
    screenshot(win, "screenshots/frame-edit.png")
    edit._reset_crop()
    assert actions.dimensions.get_label() == "Image: 320 × 180 px · 16:9"

    win.stack.set_visible_child_name("inspect")
    inspect = win.inspect
    yield Wait(lambda: inspect.player.video.get_media_stream() is not None and
               inspect.player.video.get_media_stream().is_seekable(), "seekable inspect preview")
    inspect.player.seek(0.5)
    inspect.frame_actions.save.emit("clicked")
    yield Wait(lambda: len(win.queue.jobs) == 2 and win.queue.active_count() == 0,
               "full frame saved", 30)
    second = win.queue.jobs[1]
    assert second.state == DONE, second.status
    result = probe(second.plan.output)
    assert (result.width, result.height) == (320, 180)
    pixel = subprocess.check_output(["ffmpeg", "-v", "error", "-i", second.plan.output,
                                     "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    assert pixel[0] > 200 and pixel[2] < 30, pixel  # Red before one second.
    win.set_default_size(720, 800)
    yield 0.5
    screenshot(win, "screenshots/frame-inspect-narrow.png")
    assert inspect.frame_actions.save.get_mapped()
    win.selection.unselect_all()
    yield 0.2
    assert not edit.frame_actions.save.get_sensitive()
    assert not inspect.frame_actions.save.get_sensitive()


if __name__ == "__main__":
    os.makedirs("screenshots", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clipdeck-frame-ui-") as directory:
        source = os.path.join(directory, "changing.mp4")
        fixtures.run("-f", "lavfi", "-i",
                     "color=blue:s=320x180:d=3:r=10,drawbox=color=red:t=fill:enable='lt(t,1)'",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", source)
        sys.exit(run(scenario))
