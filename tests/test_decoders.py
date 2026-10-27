"""The preview-decoder check must name real codecs and never cry wolf."""

import unittest

from clipdeck import decoders, media


def audio(codec):
    return media.AudioStream(index=1, codec=codec, channels=2, sample_rate=48000, bitrate=0)


class DecoderTests(unittest.TestCase):
    def test_known_caps_parse(self):
        if not decoders.available():
            self.skipTest("GStreamer bindings unavailable")
        from gi.repository import Gst
        for codec, caps in decoders.CODEC_CAPS.items():
            self.assertFalse(Gst.Caps.from_string(caps).is_empty(), codec)

    def test_unknown_codec_counts_as_decodable(self):
        self.assertTrue(decoders.can_decode("pcm_s16le"))
        self.assertTrue(decoders.can_decode(""))

    def test_missing_for_lists_video_first_without_duplicates(self):
        info = media.MediaInfo(path="x.mp4", container="mp4", format_name="mp4",
                               duration=1.0, size=1, bitrate=1)
        info.vcodec = "h264"
        info.audio = [audio("aac"), audio("aac")]
        missing = decoders.missing_for(info)
        self.assertEqual(missing, [c for c in ("h264", "aac") if not decoders.can_decode(c)])
        self.assertEqual(missing, decoders.common_missing())

    def test_describe_is_human(self):
        self.assertEqual(decoders.describe(["h264", "aac", "weird"]), "H.264, AAC, weird")
        self.assertIn("apt install", decoders.INSTALL_COMMAND)


if __name__ == "__main__":
    unittest.main()
