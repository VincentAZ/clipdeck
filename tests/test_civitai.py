"""Tag for Civitai: drop several videos on the tool, pick the model, check the tags written
(exactly what Civitai reads, nothing else) and that the originals went to the Trash."""
import json
import os
import shutil
import sys
import tempfile

from tests import fixtures
from tests.ui_harness import Wait, run, screenshot

MINIMAX_AIR = "urn:air:minimaxh3:checkpoint:civitai:2821932@3216500"


def scenario(app, win):
    from gi.repository import Gdk, Gio
    from clipdeck import runner
    from clipdeck.jobs import DONE
    from clipdeck.media import probe

    def fake_trash(p):
        shutil.move(p, os.path.join(trash_dir, os.path.basename(p)))
        return ""
    runner.trash = fake_trash

    win.stack.set_visible_child_name("tools")
    t = win.tools
    assert t.t_model.get_key() == "minimax-h3", "MiniMax H3 is picked first"
    assert not t.t_air.get_visible(), "the AIR field shows only for another model"
    assert not t.t_go.get_sensitive()
    yield 0.5
    screenshot(win, "screenshots/civitai-tool.png")
    files = Gdk.FileList.new_from_list([Gio.File.new_for_path(p) for p in clips])
    assert t._on_tag_drop(None, files, 0, 0)
    yield Wait(lambda: len(win.queue.jobs) == 2 and win.queue.active_count() == 0,
               "both dropped clips tagged", 60)
    for job, path in zip(win.queue.jobs, clips):
        assert job.state == DONE, (job.status, job.result and job.result.failures)
        assert job.result.output == path, f"takes the original's name: {job.result.output}"
        tags = probe(path).tags
        assert json.loads(tags["workflow"]) == {"extra": {"airs": [MINIMAX_AIR]}}, tags
        assert json.loads(tags["prompt"])["1"]["class_type"] == "UNETLoader", tags
        assert "comment" not in tags and "MOCK PROMPT" not in json.dumps(tags), tags
    assert sorted(os.listdir(trash_dir)) == ["a.mp4", "b clip.mp4"], os.listdir(trash_dir)
    assert win.library.find(clips[0]) is not None

    # another model, by its AIR; replace off: a copy beside it
    t.t_model.set_key("other")
    yield 0.1
    assert t.t_air.get_visible()
    t.t_replace.set_active(False)
    t.t_air.set_text("not an air")
    win.add_paths([clips[0]], select=True)
    yield 0.3
    t._tag([i.info for i in t._tag_clips()])
    assert "error" in t.t_air.get_css_classes() and len(win.queue.jobs) == 2, "a bad AIR queues nothing"
    t.t_air.set_text("urn:air:ltxv23:checkpoint:civitai:2448150@2752717")
    t._tag([i.info for i in t._tag_clips()])
    yield Wait(lambda: len(win.queue.jobs) == 3 and win.queue.active_count() == 0, "copy", 60)
    job = win.queue.jobs[2]
    assert job.state == DONE, job.result.failures
    assert job.result.output.endswith("a_civitai.mp4"), job.result.output
    tags = probe(job.result.output).tags
    assert set(tags) - {"major_brand", "minor_version", "compatible_brands", "encoder"} == {"workflow"}, tags
    # pictures: a video model is refused, a picture model tags them (not via the library)
    from clipdeck import pngtags
    t.t_model.set_key("minimax-h3")
    t.t_replace.set_active(True)
    files = Gdk.FileList.new_from_list([Gio.File.new_for_path(picture)])
    assert t._on_tag_drop(None, files, 0, 0)
    yield 1.0
    assert len(win.queue.jobs) == 3, "MiniMax H3 doesn't tag a picture"
    t.t_model.set_key("krea-2-raw")
    before = pngtags.picture(picture)
    assert t._on_tag_drop(None, files, 0, 0)
    yield Wait(lambda: len(win.queue.jobs) == 4 and win.queue.active_count() == 0, "picture", 30)
    job = win.queue.jobs[3]
    assert job.state == DONE, job.result.failures
    assert job.result.output == picture, job.result.output
    assert pngtags.picture(picture) == before, "the picture is byte-identical"
    tags = pngtags.read_text(picture)
    assert set(tags) == {"workflow", "prompt"}, tags
    assert "urn:air:krea2:checkpoint:civitai:2726029@3147780" in tags["workflow"], tags
    assert win.library.find(picture) is None, "pictures don't go in the video library"
    yield 0.3
    screenshot(win, "screenshots/civitai-queue.png")
    # remembered for next time
    s = win.settings
    assert (s.civitai_model, s.civitai_replace) == ("krea-2-raw", True), (s.civitai_model, s.civitai_replace)


if __name__ == "__main__":
    os.makedirs("screenshots", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clipdeck-civitai-ui-") as directory:
        trash_dir = os.path.join(directory, "trash")
        os.mkdir(trash_dir)
        clips = [os.path.join(directory, "a.mp4"), os.path.join(directory, "b clip.mp4")]
        for c in clips:
            fixtures.run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=1", "-f", "lavfi",
                         "-i", "sine=d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a",
                         "aac", "-shortest", "-metadata", 'prompt={"6":{"inputs":{"text":"MOCK PROMPT"}}}',
                         "-metadata", "comment=made in ComfyUI",
                         "-movflags", "+use_metadata_tags", c)
        picture = os.path.join(directory, "still.png")
        raw = os.path.join(directory, "raw.png")
        fixtures.run("-f", "lavfi", "-i", "testsrc2=s=64x48", "-frames:v", "1", raw)
        sys.path.insert(0, os.getcwd())
        from clipdeck import pngtags
        pngtags.write_tagged(raw, picture, {"prompt": '{"6":{"inputs":{"text":"MOCK PROMPT"}}}'})
        os.unlink(raw)
        sys.exit(run(scenario))
