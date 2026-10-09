"""Running a Plan: ffmpeg with live progress, cancellation, verification, and a
no-clobber publish of the result."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .media import ProbeError, probe
from .ops import Plan, unique_path


class Cancelled(Exception):
    pass


@dataclass
class Result:
    ok: bool
    output: str = ""                 # final path (or quarantined .BAD path on failure)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log: str = ""
    elapsed: float = 0.0
    replaced_original: str = ""


ProgressFn = Callable[[float, str], None]   # fraction 0..1, status text


def _terminate(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_ffmpeg(argv: list[str], duration: float, on_progress: Callable[[float], None],
               cancel: threading.Event, log: deque) -> int:
    """Run one ffmpeg command, reporting the fraction of `duration` processed."""
    cmd = argv[:1] + ["-progress", "pipe:1", "-nostats"] + argv[1:]
    log.append("$ " + " ".join(_quote(a) for a in argv))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1)

    def drain_err():
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                log.append(line)
    t = threading.Thread(target=drain_err, daemon=True)
    t.start()

    stop = threading.Event()

    def watch_cancel():
        while not stop.is_set():
            if cancel.wait(0.2):
                _terminate(proc)
                return
    threading.Thread(target=watch_cancel, daemon=True).start()

    try:
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and duration > 0:
                try:
                    us = int(val)
                except ValueError:
                    continue
                on_progress(max(0.0, min(1.0, us / 1e6 / duration)))
            elif key == "progress" and val == "end":
                on_progress(1.0)
        rc = proc.wait()
    finally:
        stop.set()
        _terminate(proc)
        t.join(2)
        _close(proc)
    if cancel.is_set():
        raise Cancelled()
    return rc


def _close(proc: subprocess.Popen):
    for f in (proc.stdout, proc.stderr):
        try:
            if f:
                f.close()
        except OSError:
            pass


def _quote(a: str) -> str:
    if a and all(c.isalnum() or c in "-_./:=+,@%" for c in a):
        return a
    return "'" + a.replace("'", "'\\''") + "'"


# ----------------------------------------------------------------------------- verification


def _stream_md5(path: str, sel: str, timeout: float = 600) -> str:
    res = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", sel,
                          "-c", "copy", "-f", "md5", "-"],
                         capture_output=True, text=True, timeout=timeout)
    return res.stdout.strip().removeprefix("MD5=") if res.returncode == 0 else ""


def count_frames(path: str, cancel: threading.Event | None = None, timeout: float = 3600) -> int:
    """Exact frame count: container value when trustworthy, else decode and count."""
    try:
        info = probe(path)
    except ProbeError:
        return -1
    if info.nb_frames and info.container in ("mp4", "mov", "m4v"):
        return info.nb_frames
    res = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_frames",
                          "-of", "default=noprint_wrappers=1:nokey=1", path],
                         capture_output=True, text=True, timeout=timeout)
    digits = "".join(c for c in res.stdout.split("\n")[0] if c.isdigit())
    return int(digits) if digits else -1


def full_decode(path: str, cancel: threading.Event, log: deque, duration: float,
                on_progress: Callable[[float], None]) -> tuple[bool, int]:
    """Decode every frame; any decoder error fails. Returns (clean, video frames)."""
    errs: deque = deque(maxlen=50)
    frames = 0
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-progress", "pipe:1",
           "-nostats", "-i", path, "-map", "0:v:0?", "-map", "0:a?", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1)
    t = threading.Thread(target=lambda: [errs.append(l.rstrip()) for l in proc.stderr], daemon=True)
    t.start()
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            if cancel.wait(0.2):
                _terminate(proc)
                return
    threading.Thread(target=watch, daemon=True).start()
    try:
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "frame":
                try:
                    frames = int(v)
                except ValueError:
                    pass
            elif k == "out_time_us" and duration > 0:
                try:
                    on_progress(min(1.0, int(v) / 1e6 / duration))
                except ValueError:
                    pass
        rc = proc.wait()
    finally:
        stop.set()
        _terminate(proc)
        t.join(2)
        _close(proc)
    if cancel.is_set():
        raise Cancelled()
    real = [e for e in errs if e and not _benign(e, path)]
    for e in errs:
        if e:
            log.append("decode: " + e + ("" if e in real else "  (known harmless)"))
    return rc == 0 and not real, frames


def _benign(line: str, path: str) -> bool:
    # ffmpeg 8 prints this for every Opus stream in Matroska/WebM, including ones it
    # encoded itself; all samples still decode (checked sample-for-sample).
    return ("Error parsing Opus packet header" in line
            and path.lower().endswith((".webm", ".mkv", ".mka")))


def verify(plan: Plan, path: str, thorough: bool, cancel: threading.Event, log: deque,
           on_progress: Callable[[float], None]) -> tuple[list[str], list[str]]:
    ex = plan.expect
    fails: list[str] = []
    warns: list[str] = []
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return ["output file is missing or empty"], warns
    try:
        info = probe(path)
    except ProbeError as e:
        return [f"output is unreadable ({e})"], warns

    if ex.kind in ("video", "gif", "image"):
        if info.vcodec != ex.vcodec:
            fails.append(f"video codec {info.vcodec or 'none'} ≠ {ex.vcodec}")
        if (info.width, info.height) != (ex.width, ex.height):
            fails.append(f"size {info.width}×{info.height} ≠ {ex.width}×{ex.height}")
        if info.rotation != ex.rotation:
            fails.append(f"rotation flag {info.rotation}° ≠ {ex.rotation}°")
    elif info.has_video:
        fails.append("audio file unexpectedly contains video")

    if ex.kind in ("video", "audio"):
        if ex.acodec:
            if not info.has_audio:
                fails.append("audio track missing")
            else:
                if info.acodec != ex.acodec:
                    fails.append(f"audio codec {info.acodec} ≠ {ex.acodec}")
                if ex.audio_count and len(info.audio) != ex.audio_count:
                    fails.append(f"{len(info.audio)} audio tracks ≠ {ex.audio_count}")
                a = info.audio[0]
                if ex.sample_rate and a.sample_rate != ex.sample_rate:
                    fails.append(f"sample rate {a.sample_rate} ≠ {ex.sample_rate}")
                if ex.channels and a.channels != ex.channels:
                    fails.append(f"{a.channels} channels ≠ {ex.channels}")
        elif info.has_audio:
            fails.append("unexpected audio track")

    if ex.kind in ("video", "audio", "gif") and ex.duration > 0:
        d = info.duration
        if ex.min_duration is not None:
            if d < ex.min_duration or d > ex.duration + ex.dur_tol:
                fails.append(f"duration {d:.3f}s outside {ex.min_duration:.3f}–"
                             f"{ex.duration + ex.dur_tol:.3f}s")
        elif abs(d - ex.duration) > ex.dur_tol:
            fails.append(f"duration {d:.3f}s ≠ {ex.duration:.3f}s (±{ex.dur_tol:.2f})")

    if ex.max_bytes and info.size > ex.max_bytes:
        warns.append(f"file is {info.size / 1048576:.1f} MB, above the "
                     f"{ex.max_bytes / 1048576 / 1.05:.1f} MB target")

    if fails:
        return fails, warns

    if cancel.is_set():
        raise Cancelled()
    if ex.video_md5_from:
        if _stream_md5(ex.video_md5_from, "0:v:0") != _stream_md5(path, "0:v:0"):
            fails.append("copied video is not bit-identical to the source")
    if ex.audio_md5_from:
        if _stream_md5(ex.audio_md5_from, "0:a:0") != _stream_md5(path, "0:a:0"):
            fails.append("copied audio is not bit-identical to the source")

    if thorough and ex.kind in ("video", "gif", "audio"):
        clean, decoded = full_decode(path, cancel, log, info.duration, on_progress)
        if not clean:
            fails.append("decode errors in output (see log)")
        want = ex.frames
        if want is None and ex.frames_from:
            want = count_frames(ex.frames_from)
        if want is not None and want > 0 and ex.kind != "audio":
            got = count_frames(path) if info.container not in ("mp4", "mov", "m4v") else \
                (info.nb_frames or decoded)
            if got != want:
                fails.append(f"{got} frames ≠ {want} in source")
    return fails, warns


# ----------------------------------------------------------------------------- publishing


def publish(tmp: str, final: str) -> str:
    """Move tmp to final without ever overwriting an existing file. Returns the path used."""
    target = final
    for _ in range(1000):
        try:
            os.link(tmp, target)       # atomic, fails if target exists
            os.unlink(tmp)
            return target
        except FileExistsError:
            target = unique_path(final)
        except OSError:
            # filesystem without hard links (exFAT, some network mounts)
            if os.path.exists(target):
                target = unique_path(final)
                continue
            shutil.move(tmp, target)
            return target
    raise OSError(f"could not find a free name for {final}")


def trash(path: str) -> bool:
    """Move to the desktop trash (recoverable). Never deletes outright."""
    try:
        res = subprocess.run(["gio", "trash", path], capture_output=True, timeout=30)
        return res.returncode == 0 and not os.path.exists(path)
    except (OSError, subprocess.TimeoutExpired):
        return False


def execute(plan: Plan, cancel: threading.Event, on_progress: ProgressFn,
            thorough: bool = True) -> Result:
    started = time.monotonic()
    log: deque = deque(maxlen=400)
    tmp = plan.temp
    res = Result(ok=False)

    def finish(ok: bool) -> Result:
        res.ok = ok
        res.log = "\n".join(log)
        res.elapsed = time.monotonic() - started
        for f in plan.cleanup:
            try:
                os.unlink(f)
            except OSError:
                pass
        return res

    try:
        os.makedirs(os.path.dirname(plan.output) or ".", exist_ok=True)
        for p, content in plan.files.items():
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
        verify_share = 0.15 if thorough else 0.03
        total_w = sum(s.weight for s in plan.steps) or 1.0
        done_w = 0.0
        for step in plan.steps:
            base = done_w / total_w * (1 - verify_share)
            span = step.weight / total_w * (1 - verify_share)
            on_progress(base, step.label)
            rc = run_ffmpeg(step.argv, step.duration,
                            lambda f, b=base, s=span, l=step.label: on_progress(b + f * s, l),
                            cancel, log)
            if rc != 0:
                res.failures.append(f"ffmpeg exited with code {rc}")
                _remove(tmp)
                return finish(False)
            done_w += step.weight
        on_progress(1 - verify_share, "Verifying")
        fails, warns = verify(plan, tmp, thorough, cancel, log,
                              lambda f: on_progress(1 - verify_share + f * verify_share * 0.95,
                                                    "Verifying"))
        res.warnings = warns
        if fails:
            res.failures = fails
            stem, ext = os.path.splitext(plan.output)
            bad = unique_path(stem + ".BAD" + ext)
            try:
                os.replace(tmp, bad)
                res.output = bad
            except OSError:
                _remove(tmp)
            log.append("verification failed: " + "; ".join(fails))
            return finish(False)
        res.output = publish(tmp, plan.output)
        if plan.replace_original and len(plan.inputs) == 1:
            src = plan.inputs[0].path
            if os.path.abspath(src) != os.path.abspath(res.output):
                if trash(src):
                    res.replaced_original = src
                    if os.path.splitext(src)[1].lower() == os.path.splitext(res.output)[1].lower():
                        # same container: the fixed file takes over the original name
                        try:
                            final = publish(res.output, src)
                            res.output = final
                        except OSError as e:
                            res.warnings.append(f"could not rename to original name: {e}")
                else:
                    res.warnings.append("could not move the original to Trash; it was kept")
        on_progress(1.0, "Done")
        return finish(True)
    except Cancelled:
        _remove(tmp)
        res.failures = ["cancelled"]
        return finish(False)
    except Exception as e:  # never let a worker thread die silently
        _remove(tmp)
        res.failures = [f"{type(e).__name__}: {e}"]
        return finish(False)


def _remove(p: str):
    try:
        os.unlink(p)
    except OSError:
        pass
