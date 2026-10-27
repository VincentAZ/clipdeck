"""Which codecs the preview player can decode.

Previews play through Gtk.Video, which decodes with GStreamer. Processing uses ffmpeg.
The two are separate media stacks, so a machine can convert H.264 all day and still show
a black preview because the GStreamer decoder plugin is not installed. This module asks
the GStreamer registry directly, so the app can say exactly what is missing and what to
install instead of showing a bare error icon."""

from __future__ import annotations

import functools

# ffprobe codec_name -> GStreamer caps a decoder would have to accept on its sink pad
CODEC_CAPS = {
    "h264": "video/x-h264",
    "hevc": "video/x-h265",
    "h265": "video/x-h265",
    "mpeg4": "video/mpeg,mpegversion=4",
    "mpeg2video": "video/mpeg,mpegversion=2",
    "mpeg1video": "video/mpeg,mpegversion=1",
    "vp8": "video/x-vp8",
    "vp9": "video/x-vp9",
    "av1": "video/x-av1",
    "prores": "video/x-prores",
    "mjpeg": "image/jpeg",
    "theora": "video/x-theora",
    "wmv3": "video/x-wmv,wmvversion=3",
    "aac": "audio/mpeg,mpegversion=4",
    "mp3": "audio/mpeg,mpegversion=1,layer=3",
    "mp2": "audio/mpeg,mpegversion=1,layer=2",
    "ac3": "audio/x-ac3",
    "eac3": "audio/x-eac3",
    "dts": "audio/x-dts",
    "opus": "audio/x-opus",
    "vorbis": "audio/x-vorbis",
    "flac": "audio/x-flac",
    "alac": "audio/x-alac",
    "wmav2": "audio/x-wma,wmaversion=2",
}

# Debian/Ubuntu packages that provide the decoders people actually run into.
# gstreamer1.0-libav carries the ffmpeg-backed decoders (H.264, HEVC, AAC, AC-3, ...);
# gstreamer1.0-plugins-bad carries the parsers decodebin puts in front of them.
DECODER_PACKAGES = "gstreamer1.0-libav gstreamer1.0-plugins-bad"
INSTALL_COMMAND = f"sudo apt install {DECODER_PACKAGES}"


@functools.lru_cache(maxsize=1)
def _gst():
    """The GStreamer module, initialised, or None when the bindings are unavailable."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        if not Gst.is_initialized():
            Gst.init(None)
        return Gst
    except (ImportError, ValueError):  # no gir1.2-gstreamer-1.0: cannot tell, assume fine
        return None


def available() -> bool:
    """True when the GStreamer registry can be queried at all."""
    return _gst() is not None


@functools.lru_cache(maxsize=None)
def can_decode(codec: str) -> bool:
    """Does GStreamer have a decoder for this ffprobe codec name?

    Codecs we have no caps for (raw PCM, exotic formats) count as decodable: we cannot
    prove otherwise, and a wrong "missing" message is worse than a generic error."""
    caps_str = CODEC_CAPS.get(codec)
    Gst = _gst()
    if not caps_str or Gst is None:
        return True
    factories = Gst.ElementFactory.list_get_elements(Gst.ELEMENT_FACTORY_TYPE_DECODER,
                                                     Gst.Rank.MARGINAL)
    matches = Gst.ElementFactory.list_filter(factories, Gst.Caps.from_string(caps_str),
                                             Gst.PadDirection.SINK, False)
    return bool(matches)


def missing_for(info) -> list[str]:
    """Codecs in this clip that the preview player cannot decode (video first)."""
    codecs = []
    if info.vcodec:
        codecs.append(info.vcodec)
    codecs += [a.codec for a in info.audio]
    missing = []
    for c in codecs:
        if c and c not in missing and not can_decode(c):
            missing.append(c)
    return missing


def common_missing() -> list[str]:
    """Missing decoders for the codecs nearly every phone and camera clip uses.
    Checked once at startup so people hear about it before opening a file."""
    return [c for c in ("h264", "aac") if not can_decode(c)]


def describe(missing: list[str]) -> str:
    names = {"h264": "H.264", "hevc": "HEVC", "h265": "HEVC", "aac": "AAC", "mpeg4": "MPEG-4",
             "mpeg2video": "MPEG-2", "ac3": "AC-3", "eac3": "E-AC-3", "av1": "AV1",
             "vp9": "VP9", "vp8": "VP8", "prores": "ProRes", "mp3": "MP3"}
    return ", ".join(names.get(c, c) for c in missing)
