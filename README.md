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
  published atomically, and partial files are removed on failure, cancel or quit.
  “Replace originals” moves originals to the Trash (recoverable), only after the new
  file has verified.

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
| **Edit & Convert** | Presets (share, smaller HEVC, fit in 10 MB, Resolve MOV, DNxHR, ProRes, WebM, MKV remux, GIF, MP3, WAV) and every knob: format/codec/quality/speed, NVENC GPU encoding, target file size (two-pass), trim (frame-accurate or lossless keyframe cut, start/end from the player), rotate/flip (re-encoded or lossless flag-only), crop (with black-bar detection and aspect shortcuts), resize, frame rate, speed, deinterlace, stabilize, denoise, remove/normalize/boost audio, strip metadata. A live summary shows the result before anything is queued. |
| **Tools** | Join clips (lossless when formats match, otherwise normalized re-encode), save a full-resolution frame, contact sheets. |
| **Queue** | Live progress, cancel, open, show in folder, add result to library, full ffmpeg log per job. |

Preferences: output next to originals or in one folder, thorough verification, jobs
in parallel.

## Tests

```sh
./run-tests.sh        # engine (real ffmpeg, ~100 tests) + headless UI scenario (Xvfb)
```

The engine tests generate synthetic media (phone-style rotated clips, MKV/AVI/WebM,
VFR, HDR, anamorphic, multi-track, subtitles, long-GOP…) and check results
independently, including decoded pixel orientation. The UI test drives the real app
under Xvfb through every page and fails on any exception or GTK warning.

## Layout

```
clipdeck/media.py    probe + diagnosis
clipdeck/ops.py      Recipe -> Plan (ffmpeg commands + what the output must look like)
clipdeck/runner.py   run with progress/cancel, verify, publish without clobbering
clipdeck/jobs.py     job queue (workers, dependencies, replace-original ordering)
clipdeck/ui/         GTK4 / libadwaita interface
```
