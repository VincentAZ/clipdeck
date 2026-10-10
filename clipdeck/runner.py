"""Running a Plan: ffmpeg with live progress, cancellation, verification, and a
no-clobber publish of the result."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .media import ProbeError, probe
from .ops import Plan, unique_path
from .settings import config_dir

# ----------------------------------------------------------------------------- children

try:
    _prctl = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True).prctl
except (OSError, AttributeError):
    _prctl = None
_PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """Runs in the forked child: ffmpeg gets SIGTERM if Clipdeck is killed, instead of
    encoding on as an orphan. (The thread that started it waits for it, so its exit is
    always the end of the job.)"""
    if _prctl is not None:
        _prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


def popen(argv, **kw) -> subprocess.Popen:
    return subprocess.Popen(argv, preexec_fn=_die_with_parent, **kw)


# ----------------------------------------------------------------------------- journal
# Work files of running jobs are listed on disk, so a crash, power cut or kill -9 cannot
# leave hidden partial files behind forever: the next start removes them.

_journal_lock = threading.Lock()


def _journal_path() -> str:
    return os.path.join(config_dir(), "in-progress.txt")


def _journal_read() -> list[str]:
    try:
        with open(_journal_path(), encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []


def _journal_write(paths: list[str]):
    try:
        os.makedirs(config_dir(), exist_ok=True)
        tmp = _journal_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(p + "\n" for p in paths)
        os.replace(tmp, _journal_path())
    except OSError:
        pass


def _journal(add: list[str] = (), remove: list[str] = ()):
    with _journal_lock:
        cur = [p for p in _journal_read() if p not in remove]
        cur += [p for p in add if p not in cur]
        _journal_write(cur)


def clean_stale_partials() -> int:
    """Remove work files left by a previous run that did not shut down cleanly. Only
    Clipdeck's own temp names are ever touched. Call once, before any job starts."""
    removed = 0
    with _journal_lock:
        for p in _journal_read():
            base = os.path.basename(p)
            if ".clipdeck-partial" in base or base.startswith("clipdeck-pass-"):
                try:
                    os.unlink(p)
                    removed += 1
                except OSError:
                    pass
        _journal_write([])
    return removed


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
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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


def _capture(argv: list[str], cancel: threading.Event | None) -> tuple[int, str]:
    """Run a helper command to completion; stops promptly when cancel is set."""
    proc = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                 stdin=subprocess.DEVNULL, text=True, errors="replace")
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            if cancel is not None and cancel.wait(0.2):
                _terminate(proc)
                return
            if cancel is None:
                return
    threading.Thread(target=watch, daemon=True).start()
    try:
        out, _ = proc.communicate()
    finally:
        stop.set()
        _terminate(proc)
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    return proc.returncode, out


def _stream_md5(path: str, sel: str, cancel: threading.Event | None = None) -> str:
    rc, out = _capture(["ffmpeg", "-v", "error", "-nostdin", "-fflags", "+genpts", "-i", path,
                        "-map", sel, "-c", "copy", "-f", "md5", "-"], cancel)
    return out.strip().removeprefix("MD5=") if rc == 0 else ""


def count_frames(path: str, cancel: threading.Event | None = None) -> int:
    """Exact frame count: container value when trustworthy, else read and count."""
    try:
        info = probe(path)
    except ProbeError:
        return -1
    # the container count includes frames an edit list hides, so count those for real
    if info.nb_frames and info.container in ("mp4", "mov", "m4v") and not info.preroll:
        return info.nb_frames
    rc, out = _capture(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames",
                        "-of", "default=noprint_wrappers=1:nokey=1", path], cancel)
    digits = "".join(c for c in out.split("\n")[0] if c.isdigit())
    return int(digits) if digits else -1


def full_decode(path: str, cancel: threading.Event, log: deque, duration: float,
                on_progress: Callable[[float], None]) -> tuple[bool, int]:
    """Decode every frame; any decoder error fails. Returns (clean, video frames)."""
    errs: deque = deque(maxlen=50)
    frames = 0
    # passthrough timestamps: otherwise the null muxer complains about VFR sources whose
    # timestamps collide after rounding, which is not damage in the file
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-progress", "pipe:1",
           "-nostats", "-i", path, "-map", "0:v:0?", "-map", "0:a?",
           "-fps_mode", "passthrough", "-enc_time_base", "demux", "-f", "null", "-"]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        if ex.rotation is not None and info.rotation != ex.rotation:
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
                for n, (want, got) in enumerate(zip(ex.audio_tracks, info.audio), 1):
                    codec, rate, ch = want
                    if got.codec != codec:
                        fails.append(f"track {n}: audio codec {got.codec} ≠ {codec}")
                    if rate and got.sample_rate != rate:
                        fails.append(f"track {n}: sample rate {got.sample_rate} ≠ {rate}")
                    if ch and got.channels != ch:
                        fails.append(f"track {n}: {got.channels} channels ≠ {ch}")
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
        warns.append(f"file is {info.size / 1048576:.2f} MB, above the "
                     f"{ex.max_bytes / 1048576:.2f} MB target")

    if fails:
        return fails, warns

    if cancel.is_set():
        raise Cancelled()
    for src, sel, what in ((ex.video_md5_from, "0:v:0", "video"),
                           (ex.audio_md5_from, "0:a:0", "audio")):
        if not src:
            continue
        a, b = _stream_md5(src, sel, cancel), _stream_md5(path, sel, cancel)
        if not a or not b:
            fails.append(f"could not checksum the copied {what} to compare it with the source")
        elif a != b:
            fails.append(f"copied {what} is not bit-identical to the source")

    if thorough and ex.kind in ("video", "gif", "audio"):
        clean, decoded = full_decode(path, cancel, log, info.duration, on_progress)
        if not clean:
            fails.append("decode errors in output (see log)")
        want = ex.frames
        if want is None and ex.frames_from:
            want = count_frames(ex.frames_from, cancel)
            if want < 0:
                fails.append("could not count the source's frames to compare")
        if want is not None and want > 0 and ex.kind != "audio":
            # the MP4 frame count includes frames an edit list hides; count those for real
            if info.container in ("mp4", "mov", "m4v") and not info.preroll:
                got = info.nb_frames or decoded
            else:
                got = count_frames(path, cancel)
            if got != want:
                fails.append(f"{got} frames ≠ {want} in source")
    return fails, warns


# ----------------------------------------------------------------------------- publishing


def publish(tmp: str, final: str) -> str:
    """Move tmp to final without ever overwriting an existing file. Returns the path used."""
    target = final
    for _ in range(1000):
        if os.path.lexists(target):    # e.g. a dangling symlink: link() says EEXIST forever
            target = unique_path(final)
        try:
            os.link(tmp, target)       # atomic, fails if target exists
            os.unlink(tmp)
            return target
        except FileExistsError:
            target = unique_path(final)
        except OSError:
            # filesystem without hard links (exFAT, some network mounts): claim the name
            # atomically with O_EXCL, then move our file over our own empty claim
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                target = unique_path(final)
                continue
            os.close(fd)
            try:
                os.replace(tmp, target)
            except OSError:
                os.unlink(target)
                raise
            return target
    raise OSError(f"could not find a free name for {final}")


def trash(path: str) -> str:
    """Move to the desktop trash (recoverable). Never deletes outright.
    Returns "" on success, else the reason it failed."""
    try:
        res = subprocess.run(["gio", "trash", path], capture_output=True, text=True,
                             errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if res.returncode == 0 and not os.path.lexists(path):
        return ""
    msg = (res.stderr or "").strip().splitlines()
    reason = msg[-1] if msg else "gio trash failed"
    return reason.removeprefix("gio: ").removeprefix(path + ": ")


def _source_problem(plan: Plan, when: str) -> list[str]:
    """Failures when an input vanished or changed (e.g. another job replaced it)."""
    gone = [i.path for i in plan.inputs if not os.path.isfile(i.path)]
    if gone:
        return [f"source no longer exists: {os.path.basename(g)} (moved, renamed or sent to "
                f"the Trash {when})" for g in gone]
    changed = [i.path for i in plan.inputs if _changed(i)]
    return [f"source changed {when}: {os.path.basename(c)} (e.g. replaced by a Quick Fix); "
            "add the job again" for c in changed]


def _ffmpeg_error(log: deque) -> str:
    """The most useful line of ffmpeg's complaint, for the one-line failure text."""
    for line in reversed(log):
        if line.startswith("$ ") or not line.strip():
            continue
        low = line.lower()
        if "conversion failed" in low or low.startswith("exiting"):
            continue
        return line.strip()[:200]
    return ""


def _with_bitrate(steps, old: int, new: int):
    """The same steps with the video bitrate (and NVENC's peak/buffer) changed."""
    out = []
    for st in steps:
        argv = list(st.argv)
        for i in range(1, len(argv)):
            if argv[i - 1] in ("-b:v", "-maxrate") and argv[i] == str(old):
                argv[i] = str(new)
            elif argv[i - 1] == "-bufsize" and argv[i] == str(2 * old):
                argv[i] = str(2 * new)
        out.append(type(st)(argv, st.label, st.duration, st.weight))
    return out


def execute(plan: Plan, cancel: threading.Event, on_progress: ProgressFn,
            thorough: bool = True) -> Result:
    started = time.monotonic()
    log: deque = deque(maxlen=400)
    tmp = plan.temp
    res = Result(ok=False)
    verified = False
    work = [tmp, *plan.cleanup]

    def finish(ok: bool) -> Result:
        res.ok = ok
        res.log = "\n".join(log)
        res.elapsed = time.monotonic() - started
        for f in plan.cleanup:
            _remove(f)
        _journal(remove=work)
        return res

    def run_steps(steps, note: str = "") -> bool:
        total_w = sum(s.weight for s in steps) or 1.0
        done_w = 0.0
        for step in steps:
            problem = _source_problem(plan, "while this job was running")
            if problem:
                res.failures = problem
                return False
            base = done_w / total_w * (1 - verify_share)
            span = step.weight / total_w * (1 - verify_share)
            label = step.label + note
            on_progress(base, label)
            rc = run_ffmpeg(step.argv, step.duration,
                            lambda f, b=base, s=span, l=label: on_progress(b + f * s, l),
                            cancel, log)
            if rc != 0:
                why = _ffmpeg_error(log)
                res.failures = [f"ffmpeg exited with code {rc}" + (f": {why}" if why else "")]
                return False
            done_w += step.weight
        return True

    try:
        problem = _source_problem(plan, "after this job was queued")
        if problem:
            res.failures = problem
            return finish(False)
        os.makedirs(os.path.dirname(plan.output) or ".", exist_ok=True)
        _journal(add=work)
        for p, content in plan.files.items():
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
        verify_share = 0.15 if thorough else 0.03
        steps, vbit = plan.steps, plan.vbitrate
        for attempt in range(3):
            if not run_steps(steps, f" (smaller, try {attempt + 1})" if attempt else ""):
                _remove(tmp)
                return finish(False)
            size = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
            limit = plan.expect.max_bytes
            dur = max((s.duration for s in steps), default=0.0)
            if not (vbit and limit and size > limit and dur > 0 and attempt < 2):
                break
            # over the size target: take the excess out of the video bitrate and redo it
            # (single-pass GPU encoders and very short clips overshoot by a few percent)
            new = int(vbit - (size - limit * 0.985) * 8 / dur)
            if new < 50_000:
                break
            log.append(f"output {size} bytes is over the {limit} byte target; "
                       f"re-encoding at {new // 1000} kb/s")
            steps, vbit = _with_bitrate(steps, vbit, new), new
            _remove(tmp)
        problem = _source_problem(plan, "while this job was running")
        if problem:
            res.failures = problem
            _remove(tmp)
            return finish(False)
        on_progress(1 - verify_share, "Verifying")
        fails, warns = verify(plan, tmp, thorough, cancel, log,
                              lambda f: on_progress(1 - verify_share + f * verify_share * 0.95,
                                                    "Verifying"))
        res.warnings = warns
        if vbit != plan.vbitrate and not fails:
            res.warnings.append(f"re-encoded at {vbit // 1000} kb/s to fit the size target")
        if fails:
            res.failures = fails
            stem, ext = os.path.splitext(plan.output)
            try:
                res.output = publish(tmp, stem + ".BAD" + ext)
            except OSError:
                _remove(tmp)
            log.append("verification failed: " + "; ".join(fails))
            return finish(False)
        verified = True
        res.output = publish(tmp, plan.output)
        if plan.replace_original and len(plan.inputs) == 1:
            _replace_original(plan.inputs[0].path, res)
        on_progress(1.0, "Done")
        return finish(True)
    except Cancelled:
        _remove(tmp)
        res.failures = ["cancelled"]
        return finish(False)
    except Exception as e:  # never let a worker thread die silently
        res.failures = [f"{type(e).__name__}: {e}"]
        if verified and os.path.isfile(tmp):
            # the result is good; only saving it failed. Never throw verified work away
            # (finish() only drops it from the journal, so the next start leaves it alone)
            res.failures.append(f"the verified result was kept as {tmp}")
            res.output = tmp
        else:
            _remove(tmp)
        return finish(False)


def _replace_original(src: str, res: Result):
    """Trash the original after its replacement verified. When the fixed file sits next
    to it in the same format, the fixed file takes over the original's name; a file saved
    to a separate output folder stays there."""
    if os.path.abspath(src) == os.path.abspath(res.output):
        return
    same_dir = os.path.dirname(os.path.abspath(src)) == os.path.dirname(
        os.path.abspath(res.output))
    same_ext = os.path.splitext(src)[1].lower() == os.path.splitext(res.output)[1].lower()
    why = trash(src)
    if why:
        res.warnings.append(f"could not move the original to the Trash ({why}); it was kept")
        return
    res.replaced_original = src
    if same_dir and same_ext:
        try:
            res.output = publish(res.output, src)
        except OSError as e:
            res.warnings.append(f"could not rename to the original name: {e.strerror or e}")


def _changed(info) -> bool:
    if not info.mtime_ns:
        return False
    try:
        st = os.stat(info.path)
    except OSError:
        return True
    return st.st_mtime_ns != info.mtime_ns or st.st_size != info.size


def _remove(p: str):
    try:
        os.unlink(p)
    except OSError:
        pass
