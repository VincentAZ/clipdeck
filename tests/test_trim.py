"""Interaction coverage for the trim timeline and selection playback."""
import os
import sys
from tests.ui_harness import Wait, run, screenshot
from tests import fixtures


def scenario(app, win):
    from gi.repository import Gdk
    path = os.path.join(fixtures.media_dir(), 'upright.mp4')
    win.add_paths([path], select=True)
    yield Wait(lambda: win.library.find(path).ready, 'media probe')
    win.stack.set_visible_child_name('edit')
    e = win.edit
    yield 1
    t = e.timeline
    assert t.duration > 2
    # Drag each handle, then cross the other: the selection cannot invert.
    t._begin(None, t._x(0), 40)
    t._drag(None, (t.get_width() - 32) * 0.5 / t.duration, 0)
    assert abs(e.recipe().trim_start - 0.5) < 0.01
    t._begin(None, t._x(t.end), 40)
    t._drag(None, t._x(2) - t.origin, 0)
    assert abs(e.recipe().trim_end - 2) < 0.01
    t._move(0)
    assert t.end > t.start
    e._range_changed(0.5, 2)
    t.target = 'start'
    t._key(None, Gdk.KEY_Right, 0, Gdk.ModifierType(0))
    assert e.recipe().trim_start > 0.5
    # Typing synchronizes the timeline and produces a real trim plan.
    e.t_start.set_text('0:00.500')
    e.t_end.set_text('0:02.000')
    yield 0.3
    assert abs(t.start - 0.5) < 0.001 and t.end == 2
    assert abs(e._valid[0].expect.duration - 1.5) < 0.01
    stream = e.player.video.get_media_stream()
    yield Wait(lambda: stream.is_seekable(), 'seekable player')
    e.preview_trim.emit('clicked')
    yield Wait(lambda: not e._previewing, 'preview stops at selection end', 8)
    assert not stream.get_playing()
    assert 1.9 <= e.player.position() <= 2.2, e.player.position()
    e.t_start.set_text('bad')
    yield 0.3
    assert not e.preview_trim.get_sensitive() and not e.queue_btn.get_sensitive()
    e.reset_trim.emit('clicked')
    yield 0.3
    assert not e.recipe().trimmed()
    assert t.start == 0 and t.end == t.duration
    e._range_changed(0.5, 2)
    yield 0.3
    screenshot(win, 'screenshots/trim-wide.png')
    win.set_default_size(720, 800)
    yield 0.7
    screenshot(win, 'screenshots/trim-narrow.png')
    win.set_default_size(500, 480)
    yield 0.7
    assert e.queue_btn.get_mapped()
    assert e.timeline.get_width() > 0


if __name__ == '__main__':
    sys.exit(run(scenario))
