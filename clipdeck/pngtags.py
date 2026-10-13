"""PNG text tags (where ComfyUI keeps its job, and Civitai looks for the model), read and
rewritten without touching the picture: every other chunk is copied byte for byte."""

from __future__ import annotations

import os
import zlib

SIG = b"\x89PNG\r\n\x1a\n"
TEXT = {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}


class PngError(ValueError):
    pass


def chunks(buf: bytes):
    """(type, whole chunk bytes) for every chunk, IEND included."""
    if not buf.startswith(SIG):
        raise PngError("not a PNG")
    i = len(SIG)
    while i + 12 <= len(buf):
        n = int.from_bytes(buf[i:i + 4], "big")
        kind = buf[i + 4:i + 8]
        end = i + 12 + n
        if end > len(buf):
            raise PngError("the PNG is cut short")
        yield kind, buf[i:end]
        if kind == b"IEND":
            return
        i = end
    raise PngError("the PNG has no end")


def read_text(path: str) -> dict[str, str]:
    """The tEXt and iTXt tags (zTXt unpacked too)."""
    with open(path, "rb") as f:
        buf = f.read()
    out: dict[str, str] = {}
    for kind, c in chunks(buf):
        data = c[8:-4]
        key, _, rest = data.partition(b"\0")
        if kind == b"tEXt":
            out[key.decode("latin-1")] = rest.decode("latin-1")
        elif kind == b"zTXt":
            out[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
        elif kind == b"iTXt":
            flag, method = rest[0], rest[1]
            _lang, _, rest = rest[2:].partition(b"\0")
            _tkey, _, text = rest.partition(b"\0")
            out[key.decode("latin-1")] = (zlib.decompress(text) if flag else text).decode("utf-8")
    return out


def picture(path: str) -> bytes:
    """Everything but the text: what must stay identical."""
    with open(path, "rb") as f:
        return b"".join(c for kind, c in chunks(f.read()) if kind not in TEXT)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (len(data).to_bytes(4, "big") + kind + data +
            zlib.crc32(kind + data).to_bytes(4, "big"))


def write_tagged(src: str, dst: str, tags: dict[str, str]):
    """dst = src without its text chunks, with a tEXt chunk per tag before the end."""
    with open(src, "rb") as f:
        buf = f.read()
    parts = [SIG]
    for kind, c in chunks(buf):
        if kind == b"IEND":
            for k, v in tags.items():
                # tEXt is Latin-1; anything else would need iTXt, which Civitai may not read
                parts.append(_chunk(b"tEXt", k.encode("latin-1") + b"\0" +
                                    v.encode("latin-1", "replace")))
        if kind not in TEXT:
            parts.append(c)
    with open(dst, "wb") as f:
        f.write(b"".join(parts))
        f.flush()
        os.fsync(f.fileno())
