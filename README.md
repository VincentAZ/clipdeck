<p align="center">
  <img src="data/local.clipdeck.Clipdeck.svg" width="96" alt="Clipdeck icon">
</p>

<h1 align="center">Clipdeck</h1>

<p align="center"><strong>Fix your footage. Make the cut. Check the result.</strong></p>

<p align="center">
  A local video toolkit for the GNOME desktop.<br>
  Trim, crop, rotate, convert, and batch-fix clips—with verification before saving.
</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="#what-you-can-do">Features</a> ·
  <a href="#how-clipdeck-protects-your-work">Verification</a> ·
  <a href="https://github.com/VincentAZ/clipdeck/issues">Report a bug</a>
</p>

![Clipdeck’s Edit & Convert page showing a video preview, trim controls, export settings, and a plan for the output](docs/images/edit-and-convert.png)

<p align="center"><sub>The real app, using generated demo footage. Your files stay on your machine.</sub></p>

## From awkward clip to usable video

A phone video plays sideways. A recording arrives in the wrong container. An audio
codec refuses to cooperate with your editor. You need a shorter clip, a smaller
file, or one clean frame.

Clipdeck puts those everyday jobs in one GTK4/libadwaita interface, backed by
FFmpeg. Add your files, see what needs attention, choose an operation, and inspect
the plan before adding it to the queue. Work on one clip or a batch.

**No account, uploads, or cloud processing.** Clipdeck runs locally with your
installed media tools.

## What you can do

| Your task | In Clipdeck |
| --- | --- |
| **Fix a folder of problem clips** | Quick Fix diagnoses rotation, container, video codec, audio codec, and frame-rate issues, then shows the proposed changes. |
| **Make a clean cut** | Scrub the preview, drag trim handles, enter precise times, and choose frame-accurate or lossless keyframe cuts. |
| **Reframe the shot** | Draw and adjust a crop, use aspect-ratio shortcuts, detect black bars, rotate, flip, and resize. |
| **Prepare a file for sharing** | Choose H.264, HEVC, or WebM presets, adjust quality, or target a file size. |
| **Prepare footage for editing** | Export MOV with PCM audio, DNxHR HQ, or ProRes 422 HQ. |
| **Adjust timing and sound** | Change frame rate or speed; mute, normalize, boost, or extract audio. |
| **Finish small media jobs** | Join compatible clips losslessly, re-encode mixed clips, save full-resolution frames, build contact sheets, or make GIFs. |
| **Control the export** | Strip metadata, choose an output folder, use available NVIDIA NVENC encoders, and run jobs in parallel. |

<details>
<summary><strong>See Quick Fix and Tools</strong></summary>

### Diagnose first. Fix in a batch.

![Quick Fix showing detected issues and the proposed changes for three demo clips](docs/images/quick-fix.png)

### The useful extras, in one place

![Tools showing clip ordering, join settings, and frame-export controls](docs/images/tools.png)

</details>

## How Clipdeck protects your work

An FFmpeg process exiting successfully is only the beginning of a successful job.

- **Check the output against the plan.** Depending on the operation, verification
  checks codecs, dimensions, rotation, duration, audio tracks, frame counts, and
  stream hashes when streams are copied.
- **Decode it, too.** Thorough verification is on by default and adds a full decode
  and source frame-count comparisons where applicable. It takes extra time; you
  can turn it off in Preferences.
- **Keep existing files.** New outputs get an available name, such as
  `clip (2).mp4`. A file that fails verification is kept with a `.BAD` marker for
  inspection and the job is reported as failed.
- **Replace deliberately.** When you enable *Replace originals*, the original
  moves to the desktop Trash only after the new file passes verification.
- **Clean up interrupted work.** Cancellation and normal shutdown remove partial
  files. A journal lets the next launch clean up work left by a crash.

The Queue page shows progress, cancellation controls, results, warnings, and the
FFmpeg log for each job.

## Get started

Clipdeck currently runs from a source checkout on Linux. It uses **Python 3.10+**,
**GTK4**, **libadwaita 1.7+**, **PyGObject**, **FFmpeg**, and **ffprobe**.
Video previews also need the relevant GStreamer decoders.

On Debian/Ubuntu systems whose repositories provide those versions:

```bash
sudo apt install git ffmpeg python3-gi gir1.2-adw-1 gstreamer1.0-libav

git clone https://github.com/VincentAZ/clipdeck.git
cd clipdeck
./install.sh
```

Launch **Clipdeck** from your application menu, or open files from a terminal:

```bash
clipdeck
clipdeck /path/to/video.mp4 /path/to/another.mov
```

You can also drag files or folders onto the window, use **Add Videos**, or choose
Clipdeck from your file manager’s **Open With** menu.

> The installer checks for libadwaita 1.7 or newer. An older distribution may need
> newer system packages. Available codecs and GPU acceleration depend on your
> FFmpeg build and hardware; development checks currently use FFmpeg 8.0.1.

### Your first export

1. **Add a clip.** Inspect shows its media details and any detected problems.
2. **Choose your job.** Use Quick Fix for compatibility issues, Edit & Convert for
   individual adjustments or presets, and Tools for joins, frames, and contact sheets.
3. **Read the plan and queue it.** Clipdeck processes and verifies the result.
   Open the output or reveal it in your file manager from Queue.

By default, outputs go beside their originals. Preferences lets you choose a
single output folder, verification depth, and the number of parallel jobs.

### Update or uninstall

The launcher uses this checkout directly. Keep the folder in place and update it
with:

```bash
git pull --ff-only
```

To remove the launcher, application-menu entry, and icon:

```bash
./install.sh --uninstall
```

Uninstalling leaves preferences in `~/.config/clipdeck` and thumbnails in
`~/.cache/clipdeck`.

## A few useful details

- **Lossless trimming follows keyframes.** Choose frame-accurate trimming when
  exact cut boundaries matter; that requires re-encoding.
- **Lossless frame-rate changes alter playback speed.** Keeping every compressed
  frame while changing 60 fps to 30 fps doubles the duration. Audio is re-encoded
  to stay in sync. This mode requires a constant-rate source, MP4/MOV output, and
  no conflicting video edits.
- **File-size targets are checked.** Clipdeck can retry an oversized encode at a
  lower bitrate. Very small targets still depend on the source and encoder; check
  the job’s warnings.
- **Metadata stripping is optional.** It removes mapped metadata and chapters;
  it does not remove identifying content visible or audible in the media itself.
- **Frame exports respect the crop.** Save a PNG from Inspect or Edit & Convert
  at the current preview position. Edit exports include the enabled crop.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| <kbd>Ctrl</kbd> + <kbd>O</kbd> | Add videos |
| <kbd>Alt</kbd> + <kbd>1</kbd> … <kbd>5</kbd> | Switch pages |
| <kbd>Delete</kbd> | Remove selected clips from the focused library list; keep the files |
| <kbd>Ctrl</kbd> + <kbd>,</kbd> | Preferences |
| <kbd>Ctrl</kbd> + <kbd>Q</kbd> | Quit |

## Development

Run directly from the repository:

```bash
python3 -m clipdeck
```

The engine tests create synthetic media and exercise real FFmpeg operations.
The UI tests drive the actual GTK app under Xvfb, including crop, trim, frame
saving, queue operations, and window layouts.

```bash
# Xvfb is needed for the headless UI checks.
sudo apt install xvfb xauth

./run-tests.sh

# Or run just the engine checks:
python3 -m unittest tests.test_engine
```

Current validation: the engine suite runs 150 tests, with two NVENC-dependent
skips on the development machine. The crop, trim, and frame-export UI tests pass.
The full UI scenario completes its interaction checks, but its strict warning check
still reports GStreamer stream-start/segment warnings in the development environment.

| Code | Responsibility |
| --- | --- |
| `clipdeck/media.py` | Probe files and diagnose issues |
| `clipdeck/ops.py` | Turn an operation into an FFmpeg plan and verification expectations |
| `clipdeck/runner.py` | Execute, verify, and save results |
| `clipdeck/jobs.py` | Schedule work, cancellation, and dependencies |
| `clipdeck/ui/` | GTK4/libadwaita interface |

Found a problem? [Open an issue](https://github.com/VincentAZ/clipdeck/issues) with
reproduction steps, your distribution, FFmpeg version, and the relevant job log.
Remove private paths and metadata from logs before posting them. Small synthetic
or non-sensitive sample files are especially helpful.

---

Built by [VincentAZ](https://github.com/VincentAZ).
