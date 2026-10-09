"""Thumbnail extraction with an on-disk cache keyed by path, size and mtime."""

from __future__ import annotations

import hashlib
import os
import subprocess

from .media import MediaInfo


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clipdeck", "thumbs")


def thumbnail(info: MediaInfo, width: int = 256) -> str | None:
    """Path to a JPEG thumbnail (upright), or None when the file has no picture."""
    if not info.has_video:
        return None
    try:
        st = os.stat(info.path)
    except OSError:
        return None
    key = hashlib.sha1(f"{info.path}|{st.st_size}|{st.st_mtime_ns}|{width}".encode()).hexdigest()
    d = cache_dir()
    out = os.path.join(d, key + ".jpg")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    os.makedirs(d, exist_ok=True)
    tmp = out + f".{os.getpid()}.tmp.jpg"
    at = min(1.0, info.duration * 0.1) if info.duration else 0
    for seek in (at, 0):
        try:
            res = subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{seek:.3f}", "-i", info.path,
                 "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
                 "-update", "1", tmp], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if res.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return out
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return None
