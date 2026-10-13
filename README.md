# Clipdeck

A GNOME desktop command center for video files: fix sideways phone clips, convert
containers and codecs, trim, rotate, crop, resize, change speed or frame rate, fix
audio, strip GPS metadata, make GIFs, join clips, grab frames and build contact sheets.

It grew out of `~/scripts/fixrot` and keeps its two rules:

- **Every output is verified before it is saved.** Codec, size, rotation flag,
  duration, audio tracks (codec, sample rate, channels per track), bit-identical
  copies where streams are copied, and with *thorough verification* (default) a full
  decode of every frame plus a frame-count comparison with the source. An output that
  fails is kept as `name.BAD.ext` for inspection and is never presented as done.
- **Nothing is ever overwritten.** Outputs get a free name (`clip (2).mp4`), are
  published atomically, and partial files are removed on failure, cancel, quit, logout
  or `kill` (and, after a crash or power cut, on the next start).
  “Replace originals” moves originals to the Trash (recoverable), only after the new
  file has verified; no other job reads a file while it is being replaced.

## Install

```sh
sudo apt install ffmpeg python3-gi gir1.2-adw-1 gstreamer1.0-libav   # usually present
./install.sh          # adds Clipdeck to the app grid and `clipdeck` to ~/.local/bin
./install.sh --uninstall
```

The launcher runs from this checkout, so `git pull` updates the installed app.
`clipdeck file.mp4 …` opens files directly; files and folders can also be dropped on
the window, and Clipdeck appears in *Open With* for video files.

## Pages

| Page | What it does |
|---|---|
| **Inspect** | Player plus everything ffprobe knows; problems found are explained. |
| **Quick Fix** | fixrot in a window: bakes rotation, MKV/WebM/AVI → MP4, odd codecs → H.264, FLAC/Opus/MP2 audio → AAC, optional constant frame rate, optional Resolve MOV next to each clip, optional replace-originals. Shows exactly what will happen per file. |
| **Edit & Convert** | Presets (share, smaller HEVC, fit in 10 MB, Resolve MOV, DNxHR, ProRes, WebM, MKV remux, GIF, MP3, WAV) and every knob: format/codec/quality/speed, NVENC GPU encoding (remembered; frames too small for NVENC fall back to the CPU), target file size (two-pass; re-encoded at a lower bitrate if the result lands over), trim (draggable range handles, click-to-seek timeline, precise start/end fields, selection preview and reset; frame-accurate or lossless keyframe cut), rotate/flip (re-encoded or lossless flag-only), crop (draw, move and resize a box on the preview, precise pixel fields, reset, black-bar detection and aspect shortcuts), resize, frame rate, speed, deinterlace, stabilize, denoise, remove/normalize/boost audio, strip metadata. A live summary shows the result before anything is queued. |
| **Tools** | **Tag for Civitai** first: pick the model (videos: MiniMax H3, LTX 2.3; pictures: Krea 2 RAW, Krea 2 Turbo, Z-Image Turbo, Qwen Image Edit; or any other by the AIR on its Civitai page) and drop videos and PNG pictures (or folders of them) on the box, or click **Choose videos or pictures…** in it, or tag the clips selected in the library (the library's *Add Videos* takes videos only). Each MP4/MOV/PNG then carries only what Civitai reads to know the model (`workflow` with its AIR, `prompt` with the loader node; competitions require it): every other tag goes, a ComfyUI prompt included, and the video, sound or picture is copied bit for bit and verified. A video model won't tag pictures, nor a picture model videos; JPEG and WebP pictures are skipped (the tags go in PNG). *Replace originals* (on by default) gives the tagged file the original's name and moves the original to the Trash. Then join clips (lossless when formats match, otherwise normalized re-encode), save a full-resolution frame, contact sheets (seeks to each frame, so an hour of 4K takes seconds). |
| **Queue** | Live progress, cancel, open, show in folder, add result to library, full ffmpeg log per job. |

Preferences: output next to originals or in one folder, thorough verification, jobs
in parallel.

**Save frame (PNG)** below the Inspect and Edit & Convert previews saves an image
at the current scrub position, using the configured output folder. Pixel dimensions
and aspect ratio appear beside the control. In Edit & Convert, the image includes
the enabled crop, and its displayed dimensions and ratio update as you adjust it.

**Lossless frame-rate changes:** in Edit & Convert → Size and timing, choose a
specific frame rate and enable **Change frame rate: lossless video**. This keeps
every compressed video frame and changes playback speed and duration (for example,
60 → 30 fps takes twice as long). Audio is re-encoded to stay in sync. Use Automatic
or Copy video with MP4/MOV output and a constant-rate source; turn off other video
edits and trimming. The original frame-rate mode keeps the duration and re-encodes.

Keyboard: <kbd>Ctrl</kbd>+<kbd>O</kbd> add videos, <kbd>Alt</kbd>+<kbd>1</kbd>…<kbd>5</kbd>
switch pages, <kbd>Delete</kbd> (in the library list) removes clips from the library
(files are not touched), <kbd>Ctrl</kbd>+<kbd>,</kbd> preferences, <kbd>Ctrl</kbd>+<kbd>Q</kbd> quit.

## Tests

```sh
./run-tests.sh        # engine (real ffmpeg, ~140 tests) + headless UI scenario (Xvfb)
```

The engine tests generate synthetic media (phone-style rotated clips, MKV/AVI/WebM,
VFR, HDR, anamorphic, multi-track, subtitles, long-GOP, mismatched stream lengths,
3/4-channel audio, odd sample rates…) and check results
independently, including decoded pixel orientation. The UI test drives the real app
under Xvfb through every page, at several window sizes, and fails on any exception or
GTK warning.

## Layout

```
clipdeck/media.py    probe + diagnosis
clipdeck/ops.py      Recipe -> Plan (ffmpeg commands + what the output must look like)
clipdeck/runner.py   run with progress/cancel, verify, publish without clobbering
clipdeck/jobs.py     job queue (workers, dependencies, replace-original ordering)
clipdeck/ui/         GTK4 / libadwaita interface
```
