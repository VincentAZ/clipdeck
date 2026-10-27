"""Full-screen playback: hands over the preview position, steps through the playable
library with the arrow keys (skipping audio), and selects the clip it was left on."""
import os
import shutil
import sys
import tempfile

from tests import fixtures
from tests.ui_harness import Wait, run, screenshot


def scenario(app, win):
    from gi.repository import Gdk

    upright, long_, wav = (os.path.join(work, n) for n in ("upright.mp4", "long.mp4",
                                                            "odd_rate.wav"))
    win.add_paths([upright, long_, wav])
    yield Wait(lambda: all(win.library.find(p).ready for p in (upright, long_, wav)), "probe")
    assert [i.path for i in win.playable_clips()] == [upright, long_]
    win.selection.unselect_all()
    win._select_items([win.library.find(long_)])
    win.stack.set_visible_child_name("inspect")
    inspect = win.inspect
    yield Wait(lambda: inspect.player.video.get_media_stream() is not None and
               inspect.player.video.get_media_stream().is_seekable(), "seekable preview")
    assert inspect.player.fullscreen_btn.get_visible(), "full-screen button on the preview"
    inspect.player.seek(8.0)
    yield Wait(lambda: inspect.player.position() >= 7.9, "preview seek", 5)

    # the viewer opens on the previewed clip, at the previewed position
    inspect.player.fullscreen_btn.emit("clicked")
    v = win.viewer
    assert v is not None and v.current.path == long_, v and v.current
    assert v.get_visible()
    yield Wait(lambda: v.player.position() >= 7.5, "handed-over position", 6)
    assert "2 of 2" in v.title_label.get_label(), v.title_label.get_label()
    yield 0.5
    screenshot(v, "screenshots/fullscreen.png")

    # arrows step through the playable clips and wrap; the audio file is skipped
    seen = []
    for key in (Gdk.KEY_Right, Gdk.KEY_Right, Gdk.KEY_Left, Gdk.KEY_Left):
        assert v._key(None, key, 0, 0)
        seen.append(v.current.path)
        yield 0.2
    assert seen == [upright, long_, upright, long_], seen
    assert "2 of 2" in v.title_label.get_label()
    assert v._key(None, Gdk.KEY_Home, 0, 0) and v.current.path == upright
    assert "1 of 2" in v.title_label.get_label()
    assert not v._key(None, Gdk.KEY_a, 0, 0), "unrelated keys pass through"

    # Escape leaves, and the clip it was left on becomes the selection
    assert v._key(None, Gdk.KEY_Escape, 0, 0)
    yield 0.5
    assert win.viewer is None
    assert v.player.path is None, "the viewer let go of the file"
    assert [i.path for i in win.selected_items()] == [upright]
    assert inspect.player.path == upright

    # F11 anywhere in the main window plays the selection
    win.stack.set_visible_child_name("queue")
    yield 0.3
    win._fullscreen_shortcut()
    v = win.viewer
    assert v is not None and v.current.path == upright
    yield 0.3
    v.close()
    yield 0.3
    assert win.viewer is None
    assert [i.path for i in win.selected_items()] == [upright], "selection unchanged"

    # nothing playable: a toast instead of a viewer
    win.clear_library()
    win.show_fullscreen()
    assert win.viewer is None


if __name__ == "__main__":
    os.makedirs("screenshots", exist_ok=True)
    src = fixtures.media_dir()
    work = tempfile.mkdtemp(prefix="clipdeck-fullscreen-ui-")
    for n in ("upright.mp4", "long.mp4", "odd_rate.wav"):
        shutil.copy(os.path.join(src, n), work)
    try:
        sys.exit(run(scenario))
    finally:
        shutil.rmtree(work, ignore_errors=True)
