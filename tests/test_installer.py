"""The in-app installer must ask for the right things and always have a plain-text fallback."""

import unittest

from clipdeck import installer


class InstallerTests(unittest.TestCase):
    def test_decoder_need_uses_capabilities(self):
        need = installer.decoder_need(["h264", "aac"])
        self.assertEqual(need.provides, ["gstreamer1(decoder-video/x-h264)",
                                         "gstreamer1(decoder-audio/mpeg)(mpegversion=4)"])
        self.assertIn("gstreamer1.0-libav", need.packages_for("debian"))
        self.assertTrue(need.packages_for("unknown-distro"))   # always something to name

    def test_decoder_need_defaults_to_common_codecs(self):
        self.assertEqual(len(installer.decoder_need([]).provides), 2)

    def test_manual_command_names_packages(self):
        cmd = installer.manual_command(installer.ffmpeg_need())
        self.assertIn("ffmpeg", cmd)
        self.assertNotIn("-y", cmd.split())
        cmd = installer.manual_command(installer.decoder_need(["h264"]))
        self.assertIn("gstreamer", cmd)

    def test_distro_family_is_known_or_empty(self):
        self.assertIn(installer.distro_family(), ("debian", "fedora", "arch", "suse", ""))


if __name__ == "__main__":
    unittest.main()
