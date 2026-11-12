"""Record Video: preview, record, stop, and the take lands in the library selected.
Runs with a synthetic camera and microphone, so it needs no hardware."""
import os
import shutil
import sys
import tempfile

from tests.ui_harness import Wait, run, screenshot

FOLDER = tempfile.mkdtemp(prefix="clipdeck-record-ui-")


def scenario(app, win):
    from clipdeck import recorder
    from clipdeck.media import probe
    from clipdeck.recorder import Camera, Microphone

    cam = Camera("Test pattern", "fake-cam", ((1280, 720, ""), (640, 360, "")),
                 fake="testsrc2=size={w}x{h}:rate={rate}")
    tone = Microphone("Test tone", "sine=frequency=440:sample_rate=48000", "lavfi",
                      default=True)
    muted = Microphone("Muted mic", "anullsrc=r=48000:cl=mono", "lavfi")
    devices = {"cams": [cam], "mics": [tone, muted]}
    recorder.cameras = lambda: list(devices["cams"])
    recorder.microphones = lambda: list(devices["mics"])
    win.settings.record_folder = FOLDER

    def check(cond, msg):
        if not cond:
            raise AssertionError(msg)

    def open_dialog():
        win.record_video()
        dlg = win.recorder
        check(dlg is not None, "recorder dialog opened")
        return dlg

    def live(dlg):
        return Wait(lambda: dlg.state == "preview" and dlg.has_frame,
                    "camera preview", 20)

    def saved_count(n):
        return Wait(lambda: win.recorder is None and len(win.library) == n
                    and all(i.ready for i in win.library.items()), f"{n} recordings saved", 30)

    yield 0.5
    check(win.side_stack.get_visible_child_name() == "empty", "empty library")

    # ---------------------------------------------------------- preview, then a take
    dlg = open_dialog()
    yield live(dlg)
    check(dlg.cam_row.get_key() == "fake-cam", dlg.cam_row.get_key())
    check(dlg.size_row.get_key() == "1280x720", "the camera's best size by default")
    check(dlg.mic_row.get_key() == tone.device, "the default microphone first")
    check(dlg.mic_row.keys[-1] == "none", "recording without sound is an option")
    check(dlg.get_can_close(), "previewing can be closed")
    yield 0.8
    check(dlg.meter.get_value() > 0.5, f"meter shows the tone: {dlg.meter.get_value()}")
    check(not dlg.hint.get_visible(), "no silence warning for a live microphone")
    screenshot(win, "screenshots/record-preview.png")

    dlg.rec_btn.emit("clicked")
    yield Wait(lambda: dlg.state == "recording", "recording", 20)
    check(not dlg.get_can_close(), "a take cannot be dropped by closing")
    check(dlg.pill.get_visible(), "recording indicator")
    check(not dlg.cam_row.get_sensitive(), "devices are fixed while recording")
    yield 2.2
    check(dlg.clock.get_label() in ("0:02", "0:03"), dlg.clock.get_label())
    screenshot(win, "screenshots/record-recording.png")
    # closing mid-take stops, saves, and then closes
    dlg.close()
    yield saved_count(1)
    first = win.library.items()[0]
    check(os.path.dirname(first.path) == FOLDER, first.path)
    check(first.name.startswith("Recording ") and first.name.endswith(".mp4"), first.name)
    check([i.path for i in win.selected_items()] == [first.path], "the take is selected")
    check(win.stack.get_visible_child_name() == "inspect", "Inspect shows it")
    info = probe(first.path)
    check((info.vcodec, info.acodec) == ("h264", "aac"), (info.vcodec, info.acodec))
    check((info.width, info.height) == (1280, 720), (info.width, info.height))
    check(info.duration > 2.0, info.duration)
    check(not [n for n in os.listdir(FOLDER) if "clipdeck-partial" in n], "no work files")
    yield Wait(lambda: win.inspect.player.path == first.path, "Inspect loaded the take", 10)
    yield 0.6
    screenshot(win, "screenshots/record-saved.png")

    # ---------------------------------------------------------- choices are remembered
    dlg = open_dialog()
    yield live(dlg)
    check(dlg.size_row.get_key() == "1280x720", "size remembered")
    # a muted microphone is called out
    dlg.mic_row.set_key(muted.device)
    yield live(dlg)
    yield Wait(lambda: dlg.hint.get_visible(), "silent microphone hint", 8)
    # video only, smaller, with the Stop button
    dlg.size_row.set_key("640x360")
    dlg.mic_row.set_key("none")
    yield live(dlg)
    check(not dlg.hint.get_visible(), "no microphone, no silence warning")
    check(not dlg.meter.get_sensitive(), "the meter is off without a microphone")
    dlg.rec_btn.emit("clicked")
    yield Wait(lambda: dlg.state == "recording", "recording", 20)
    yield 1.2
    dlg.rec_btn.emit("clicked")
    check(dlg.state == "saving", dlg.state)
    yield saved_count(2)
    second = [i for i in win.library.items() if i is not first][0]
    check(second.info.has_video and not second.info.has_audio, "video only")
    check(second.info.display_size == (640, 360), second.info.display_size)
    check(win.settings.record_mic == "none", "no-microphone choice saved")

    # ---------------------------------------------------------- what can go wrong
    devices["cams"] = [Camera("Locked camera", "/dev/video9", problem="Not allowed.",
                              fix=recorder.GROUP_FIX)]
    dlg = open_dialog()
    yield Wait(lambda: dlg.state == "notice", "permission notice", 10)
    check(dlg.copy_btn.get_visible(), "the fix is one click away")
    check(recorder.GROUP_FIX in dlg.notice_body.get_label(), dlg.notice_body.get_label())
    check(not dlg.rec_btn.get_sensitive(), "nothing to record from")
    yield 0.6
    screenshot(win, "screenshots/record-permission.png")
    dlg.copy_btn.emit("clicked")
    check(dlg.copy_btn.get_label() == "Copied", "copied")
    devices["cams"] = []
    dlg.retry_btn.emit("clicked")
    yield Wait(lambda: dlg.state == "notice" and "No camera" in dlg.notice_title.get_label(),
               "no camera notice", 10)
    dlg.close()
    yield Wait(lambda: win.recorder is None, "closed", 5)

    # ---------------------------------------------------------- quitting mid-take keeps it
    devices["cams"] = [cam]
    dlg = open_dialog()
    yield live(dlg)
    dlg.rec_btn.emit("clicked")
    yield Wait(lambda: dlg.state == "recording", "recording", 20)
    yield 1.5
    # the harness now quits the app; the take must be finished and kept (checked below)


if __name__ == "__main__":
    os.makedirs("screenshots", exist_ok=True)
    try:
        rc = run(scenario)
        if rc == 0:
            names = sorted(os.listdir(FOLDER))
            if len(names) != 3 or any("partial" in n for n in names):
                print(f"UI TEST FAILURE: quitting mid-take: {names}", file=sys.stderr)
                rc = 1
        sys.exit(rc)
    finally:
        shutil.rmtree(FOLDER, ignore_errors=True)
