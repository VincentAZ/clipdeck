"""Visual crop interactions and their resulting export plan."""
import os
import sys
from tests.ui_harness import Wait, run, screenshot
from tests import fixtures


def scenario(app, win):
    from gi.repository import Gdk
    from clipdeck.media import probe
    from clipdeck.jobs import DONE
    path = os.path.join(fixtures.media_dir(), 'upright.mp4')
    win.add_paths([path], select=True)
    yield Wait(lambda: win.library.find(path).ready, 'media probe')
    win.stack.set_visible_child_name('edit')
    e = win.edit
    yield 1
    e.crop.set_enable_expansion(True)
    yield 0.3
    c = e.crop_overlay
    assert c.get_mapped()
    yield Wait(lambda: c.get_width() > 0, "crop overlay allocated", 5)
    sw, sh = c.source
    ox, oy, scale = c.frame()
    # Resize from the lower-right corner, then move the selection.
    c._begin(None, ox + sw * scale, oy + sh * scale)
    c._drag(None, -sw * scale / 2, -sh * scale / 2)
    w, h, x, y = e.recipe().crop
    assert abs(w - sw / 2) <= 1 and abs(h - sh / 2) <= 1
    c._begin(None, ox + w * scale / 2, oy + h * scale / 2)
    c._drag(None, 20 * scale, 10 * scale)
    assert e.recipe().crop == (w, h, 20, 10), (e.recipe().crop, (w, h), c.target, c.frame())
    c._key(None, Gdk.KEY_Right, 0, Gdk.ModifierType.SHIFT_MASK)
    assert e.recipe().crop == (w, h, 30, 10)
    # Drawing a new rectangle outside the selection and clamping at frame edges.
    c._begin(None, ox + (sw - 4) * scale, oy + (sh - 4) * scale)
    c._drag(None, -sw * scale / 4, -sh * scale / 4)
    w, h, x, y = e.recipe().crop
    assert w > 2 and h > 2 and x + w <= sw and y + h <= sh
    e._aspect(1)
    yield 0.3
    assert e.recipe().crop[0] == e.recipe().crop[1]
    e.c_x.set_value(0)
    assert c.crop[2] == 0
    yield 0.3
    assert e._valid
    screenshot(win, 'screenshots/crop-wide.png')
    # Export through the ordinary verified job queue, then independently probe.
    expected = e.recipe().crop[:2]
    plan = e._valid[0]
    job = e.ctx.submit(plan)
    yield Wait(lambda: win.queue.active_count() == 0, 'crop export', 60)
    assert job.state == DONE, job.status
    result = probe(plan.output)
    assert result.display_size == expected, (result.display_size, expected)
    e.reset_crop.emit('clicked')
    assert e.recipe().crop is None and not c.get_visible()
    e.crop.set_enable_expansion(True)
    assert e.recipe().crop == (sw - sw % 2, sh - sh % 2, 0, 0)
    phone = os.path.join(fixtures.media_dir(), 'stored.mp4')
    win.add_paths([phone], select=True)
    yield Wait(lambda: e._first_info() is not None and e._first_info().path == phone,
               'portrait selection')
    e.reset_crop.emit('clicked')
    e.crop.set_enable_expansion(True)
    yield 0.3
    pw, ph = e._first_info().display_size
    assert c.source == (pw, ph)
    ox, oy, scale = c.frame()
    if ox > 0:
        c._begin(None, ox / 2, c.get_height() / 2)
        assert c.target is None  # Letterboxing is not part of the video.
    e._aspect(1)
    yield 0.3
    assert e._valid and c.crop[0] == c.crop[1]
    e.fmt.set_key('mp3')
    assert not c.get_visible() and not e.crop_group.get_visible()
    e.fmt.set_key('mp4')
    win.set_default_size(720, 800)
    yield 0.5
    screenshot(win, 'screenshots/crop-narrow.png')
    assert e.queue_btn.get_mapped()


if __name__ == '__main__':
    fixtures.media_dir()  # Generate media before starting GTK's frame clock.
    sys.exit(run(scenario))
