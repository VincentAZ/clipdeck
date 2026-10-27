"""Edit & Convert: one form that builds a Recipe (format, codecs, trim, rotate, crop,
resize, speed, audio, privacy) and queues it for every selected clip."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, GLib, Gtk

from .. import ops
from ..media import fmt_duration, parse_time
from ..ops import (ACODEC_LABELS, FORMATS, QUALITY_LABELS, QUALITY_LEVELS, SPEED_LABELS, SPEEDS,
                   VCODEC_LABELS, PlanError, Recipe)
from .library import MediaItem
from .crop import CropOverlay
from .frame import FrameActions
from .trim import TrimTimeline
from .widgets import KeyCombo, Player, fmt_time_entry

PRESETS: list[tuple[str, str, dict]] = [
    ("custom", "Custom", {}),
    ("share", "Share: MP4 H.264 (plays everywhere)",
     dict(fmt="mp4", vcodec="h264", acodec="aac", quality="high")),
    ("small", "Smaller: MP4 HEVC",
     dict(fmt="mp4", vcodec="hevc", acodec="aac", quality="balanced")),
    ("chat", "Chat & email: fit in 10 MB",
     dict(fmt="mp4", vcodec="h264", acodec="aac", target_mb=9.5, scale_height=720)),
    ("resolve", "DaVinci Resolve: MOV + PCM (video copied when possible)",
     dict(fmt="mov", vcodec="auto", acodec="pcm24", quality="lossless")),
    ("dnxhr", "DaVinci Resolve (free on Linux): DNxHR HQ MOV",
     dict(fmt="mov", vcodec="dnxhr", acodec="pcm24")),
    ("prores", "Editing master: ProRes 422 HQ MOV",
     dict(fmt="mov", vcodec="prores", acodec="pcm24")),
    ("web", "Web: WebM VP9 + Opus", dict(fmt="webm", vcodec="vp9", acodec="opus")),
    ("remux", "Remux to MKV (lossless; MKV sources keep subtitles)",
     dict(fmt="mkv", vcodec="auto", acodec="auto", bake_rotation=False)),
    ("gif", "Animated GIF", dict(fmt="gif")),
    ("mp3", "Audio: MP3", dict(fmt="mp3", acodec="mp3", audio_kbps=192)),
    ("wav", "Audio: WAV 24-bit", dict(fmt="wav", acodec="pcm24")),
]

RESIZE = [("0", "Original size"), ("2160", "2160p (4K)"), ("1440", "1440p"),
          ("1080", "1080p"), ("720", "720p"), ("480", "480p"), ("360", "360p"),
          ("custom", "Custom width…")]
FPS = [("0", "Original"), ("cfr", "Constant (nearest standard rate)"),
       ("23.976", "23.976"), ("24", "24"), ("25", "25"), ("29.97", "29.97"), ("30", "30"),
       ("50", "50"), ("59.94", "59.94"), ("60", "60")]
SPEED = [("0.25", "0.25× (slow motion)"), ("0.5", "0.5×"), ("0.75", "0.75×"), ("1", "Normal"),
         ("1.25", "1.25×"), ("1.5", "1.5×"), ("2", "2×"), ("4", "4× (timelapse)")]
GIF_FPS = [("0", "Source"), ("10", "10 fps"), ("12", "12 fps"), ("15", "15 fps"),
           ("20", "20 fps"), ("24", "24 fps"), ("30", "30 fps")]
GIF_W = [("0", "Source width"), ("320", "320 px"), ("480", "480 px"), ("640", "640 px"),
         ("800", "800 px"), ("1080", "1080 px")]
ABR = [("96", "96 kb/s"), ("128", "128 kb/s"), ("192", "192 kb/s"), ("256", "256 kb/s"),
       ("320", "320 kb/s")]


class InputError(ValueError):
    pass


def _copies_video(plan) -> bool:
    return any(s.startswith("Video:") and "copied" in s for s in plan.summary)


class EditPage(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.items: list[MediaItem] = []
        self._applying = False
        self._pending = 0
        self._valid: list = []

        self.stack = Gtk.Stack(vexpand=True)
        self.append(self.stack)
        self.stack.add_named(Adw.StatusPage(
            icon_name="document-edit-symbolic", title="Edit & Convert",
            description="Select one or more clips to trim, rotate, crop, resize, change "
                        "format, speed or audio."), "empty")

        self.paned = paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=480,
                          shrink_start_child=False, shrink_end_child=False,
                          resize_start_child=False)
        self.stack.add_named(paned, "main")

        # ---------------- left: preview + plan
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12,
                       margin_end=12, margin_top=12, margin_bottom=12)
        left.set_size_request(340, -1)
        self.player = Player(height=120)
        self.player.on_install = ctx.install_decoders
        self.player.on_fullscreen = ctx.fullscreen
        preview = Gtk.AspectFrame(ratio=16 / 9, obey_child=False)
        overlay = Gtk.Overlay(child=self.player)
        self.crop_overlay = CropOverlay(self._set_crop)
        self.crop_overlay.set_visible(False)
        overlay.add_overlay(self.crop_overlay)
        preview.set_child(overlay)
        left.append(preview)
        self.frame_actions = FrameActions(ctx, self.player)
        left.append(self.frame_actions)
        self.trim_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.append(self.trim_panel)
        heading = Gtk.Box(spacing=8)
        title = Gtk.Label(label="Trim clip", xalign=0, hexpand=True)
        title.add_css_class("heading")
        heading.append(title)
        self.reset_trim = Gtk.Button(label="Reset", tooltip_text="Keep the whole clip")
        self.reset_trim.add_css_class("flat")
        self.reset_trim.connect("clicked", self._reset_trim)
        heading.append(self.reset_trim)
        self.trim_panel.append(heading)
        hint = Gtk.Label(label="Drag the handles to keep the part you want.", xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        self.trim_panel.append(hint)
        self.timeline = TrimTimeline(self._range_changed, self._seek_trim)
        self.trim_panel.append(self.timeline)
        transport = Gtk.Box(spacing=6)
        self.preview_trim = Gtk.Button(label="Preview selection")
        self.preview_trim.connect("clicked", self._preview_trim)
        transport.append(self.preview_trim)
        self.trim_length = Gtk.Label(xalign=1, hexpand=True)
        self.trim_length.add_css_class("caption")
        transport.append(self.trim_length)
        self.trim_panel.append(transport)
        self._previewing = False
        self.add_tick_callback(self._trim_tick)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.add_css_class("plan-card")
        self.out_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.out_label.add_css_class("heading")
        self.summary = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.summary.add_css_class("caption")
        self.error = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
        self.error.add_css_class("error")
        card.append(self.out_label)
        card.append(self.summary)
        card.append(self.error)
        left.append(card)

        # the button stays in view below the scrolling preview, however short the pane
        self.queue_btn = Gtk.Button(label="Add to queue", halign=Gtk.Align.CENTER,
                                    margin_top=6, margin_bottom=10)
        self.queue_btn.add_css_class("pill")
        self.queue_btn.add_css_class("suggested-action")
        self.queue_btn.connect("clicked", lambda *_: self.run())
        lscroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=left,
                                     vexpand=True)
        lcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lcol.append(lscroll)
        lcol.append(self.queue_btn)
        paned.set_start_child(lcol)

        # ---------------- right: the form
        form = Adw.PreferencesPage()
        paned.set_end_child(form)
        self._build_form(form)

        self.connect("map", lambda *_: self._load_player())
        self.connect("unmap", self._stop_trim_preview)
        self.connect("unmap", lambda *_: self.player.pause())
        self._sync_visibility()

    # ================================================================== form
    def _build_form(self, page: Adw.PreferencesPage):
        ch = self._changed

        g = Adw.PreferencesGroup()
        page.add(g)
        self.preset = KeyCombo("Preset", [(k, lab) for k, lab, _ in PRESETS])
        self.preset.connect("notify::selected", lambda *_: self._apply_preset())
        g.add(self.preset)

        # output
        g = Adw.PreferencesGroup(title="Output")
        page.add(g)
        self.save_row = Adw.ActionRow(title="Save as")
        self.save_row.set_use_markup(False)
        self.save_mode = Adw.ToggleGroup(valign=Gtk.Align.CENTER, can_shrink=False)
        self.save_mode.add(Adw.Toggle(name="new", label="New file",
                                      tooltip="Keep the original and save the edited clip "
                                              "beside it"))
        self.save_mode.add(Adw.Toggle(name="replace", label="Replace original",
                                      tooltip="Trash the original once the edited clip has "
                                              "been verified"))
        self.save_mode.set_active_name("new")
        self.save_mode.connect("notify::active-name", lambda *_: self._save_mode_changed())
        self.save_row.add_suffix(self.save_mode)
        g.add(self.save_row)
        self._save_mode_changed()
        self.fmt = KeyCombo("Format", [(f.key, f.label) for f in FORMATS.values()])
        self.vcodec = KeyCombo("Video codec", [])
        self.acodec = KeyCombo("Audio codec", [])
        self.quality = KeyCombo("Quality", [(q, QUALITY_LABELS[q]) for q in QUALITY_LEVELS])
        self.quality.set_key("high")
        self.speedp = KeyCombo("Encoder speed", [(s, SPEED_LABELS[s]) for s in SPEEDS])
        self.speedp.set_key("balanced")
        self.gpu = Adw.SwitchRow(title="Use GPU (NVENC)",
                                 subtitle="Much faster; slightly larger files at equal quality")
        self.gpu.set_active(self.ctx.settings.gpu)
        self.gpu.connect("notify::active", lambda *_: self._remember_gpu())
        self.target = Adw.ExpanderRow(title="Target file size",
                                      subtitle="Pick the bitrate that lands on a size instead "
                                               "of a quality level")
        self.target.set_show_enable_switch(True)
        self.target.set_enable_expansion(False)
        self.target_mb = Adw.SpinRow.new_with_range(0.5, 100000, 0.5)
        self.target_mb.set_title("Size (MB)")
        self.target_mb.set_value(25)
        self.target_mb.set_digits(1)
        self.target.add_row(self.target_mb)
        self.abr = KeyCombo("Audio bitrate", ABR)
        self.abr.set_key("192")
        for w in (self.fmt, self.vcodec, self.acodec, self.quality, self.speedp, self.abr):
            w.connect("notify::selected", ch)
        self.fmt.connect("notify::selected", lambda *_: self._format_changed())
        self.vcodec.connect("notify::selected", lambda *_: self._sync_visibility())
        self.gpu.connect("notify::active", ch)
        self.target.connect("notify::enable-expansion", ch)
        self.target_mb.connect("notify::value", ch)
        for w in (self.fmt, self.vcodec, self.acodec, self.quality, self.speedp, self.gpu,
                  self.target, self.abr):
            g.add(w)

        # gif
        self.gif_group = Adw.PreferencesGroup(title="GIF")
        page.add(self.gif_group)
        self.gif_fps = KeyCombo("Frame rate", GIF_FPS)
        self.gif_fps.set_key("15")
        self.gif_w = KeyCombo("Width", GIF_W)
        self.gif_w.set_key("640")
        self.gif_pal = Adw.SwitchRow(title="Palette per frame",
                                     subtitle="Best colors; larger file")
        self.gif_pal.set_active(True)
        for w in (self.gif_fps, self.gif_w):
            w.connect("notify::selected", ch)
            self.gif_group.add(w)
        self.gif_pal.connect("notify::active", ch)
        self.gif_group.add(self.gif_pal)

        # trim
        g = Adw.PreferencesGroup()
        self.trim = Adw.ExpanderRow(title="Trim options", subtitle="Enable trim / choose cut accuracy")
        self.trim.set_show_enable_switch(True)
        self.trim.set_enable_expansion(False)
        self.t_start = Gtk.Entry(hexpand=True, width_chars=10, placeholder_text="0:00.000")
        self.t_end = Gtk.Entry(hexpand=True, width_chars=10, placeholder_text="Clip end")
        self.t_exact = Adw.SwitchRow(title="Frame-accurate",
                                     subtitle="Off: cut on keyframes without re-encoding (instant, lossless)")
        self.t_exact.set_active(True)
        times = Gtk.Box(spacing=12, homogeneous=True)
        for w, label in ((self.t_start, "Start"), (self.t_end, "End")):
            field = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            caption = Gtk.Label(label=label, xalign=0, mnemonic_widget=w)
            caption.add_css_class("caption")
            field.append(caption)
            field.append(w)
            w.set_tooltip_text(label + " time, e.g. 0:05.500")
            w.connect("changed", self._trim_entry_changed)
            times.append(field)
        self.trim_panel.append(times)
        self.trim_panel.append(g)
        self.t_exact.connect("notify::active", ch)
        self.trim.add_row(self.t_exact)
        self.trim.connect("notify::enable-expansion", ch)
        g.add(self.trim)

        # rotate
        self.geo_group = Adw.PreferencesGroup(title="Rotate and flip")
        page.add(self.geo_group)
        rot_row = Adw.ActionRow(title="Rotate", subtitle="Clockwise, applied to the upright picture")
        self.rot = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, label, tip in (("0", "0°", "No rotation"), ("90", "90°", "Quarter turn right"),
                                 ("180", "180°", "Upside down"),
                                 ("270", "270°", "Quarter turn left")):
            self.rot.add(Adw.Toggle(name=name, label=label, tooltip=tip))
        self.rot.set_active_name("0")
        self.rot.connect("notify::active-name", ch)
        rot_row.add_suffix(self.rot)
        self.hflip = Adw.SwitchRow(title="Flip horizontally (mirror)")
        self.vflip = Adw.SwitchRow(title="Flip vertically")
        self.lossless = Adw.SwitchRow(
            title="Lossless rotation",
            subtitle="Only rewrite the rotation flag: instant and no quality loss, but editors "
                     "like Resolve ignore it")
        self.bake = Adw.SwitchRow(
            title="Make upright",
            subtitle="Bake an existing rotation flag into the pixels (needs re-encoding)")
        self.bake.set_active(True)
        for w in (self.hflip, self.vflip, self.lossless, self.bake):
            w.connect("notify::active", ch)
        for w in (rot_row, self.hflip, self.vflip, self.lossless, self.bake):
            self.geo_group.add(w)

        # crop
        self.crop_group = Adw.PreferencesGroup(
            title="Crop", description="Enable crop, then drag the preview corners to resize "
                                      "or drag inside to move the selection.")
        page.add(self.crop_group)
        self.crop = Adw.ExpanderRow(title="Crop", subtitle="Pixels of the upright picture")
        self.crop.set_show_enable_switch(True)
        self.crop.set_enable_expansion(False)
        self.c_w = Adw.SpinRow.new_with_range(2, 16384, 2)
        self.c_h = Adw.SpinRow.new_with_range(2, 16384, 2)
        self.c_x = Adw.SpinRow.new_with_range(0, 16384, 1)
        self.c_y = Adw.SpinRow.new_with_range(0, 16384, 1)
        for w, t in ((self.c_w, "Width"), (self.c_h, "Height"), (self.c_x, "Left"),
                     (self.c_y, "Top")):
            w.set_title(t)
            w.connect("notify::value", ch)
            self.crop.add_row(w)
        tools = Adw.ActionRow(title="Shortcuts")
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.detect_btn = Gtk.Button(label="Detect black bars")
        self.detect_btn.connect("clicked", lambda *_: self._detect())
        box.append(self.detect_btn)
        for label, ratio in (("16:9", 16 / 9), ("9:16", 9 / 16), ("1:1", 1.0), ("4:5", 0.8)):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, r=ratio: self._aspect(r))
            box.append(b)
        tools.add_suffix(box)
        self.crop.add_row(tools)
        self.reset_crop = Gtk.Button(label="Reset", tooltip_text="Keep the whole picture")
        self.reset_crop.connect("clicked", self._reset_crop)
        self.crop_group.set_header_suffix(self.reset_crop)
        self.crop.connect("notify::enable-expansion", ch)
        self.crop_group.add(self.crop)

        # size & timing
        self.size_group = Adw.PreferencesGroup(title="Size and timing")
        page.add(self.size_group)
        self.resize = KeyCombo("Resize", RESIZE)
        self.resize_w = Adw.SpinRow.new_with_range(16, 16384, 2)
        self.resize_w.set_title("Width (px)")
        self.resize_w.set_value(1280)
        self.fps = KeyCombo("Frame rate", FPS)
        self.fps_lossless = Adw.SwitchRow(
            title="Change frame rate: lossless video",
            subtitle="Keeps every frame; changes speed and duration. "
                     "Audio is re-encoded to stay in sync. MP4/MOV only.")
        self.fps_lossless.connect("notify::active", ch)
        self.speed = KeyCombo("Speed", SPEED)
        self.speed.set_key("1")
        self.resize.connect("notify::selected", lambda *_: self._sync_visibility())
        for w in (self.resize, self.fps, self.speed):
            w.connect("notify::selected", ch)
        self.resize_w.connect("notify::value", ch)
        for w in (self.resize, self.resize_w, self.fps, self.fps_lossless, self.speed):
            self.size_group.add(w)

        # clean up
        self.clean_group = Adw.PreferencesGroup(title="Clean up")
        page.add(self.clean_group)
        self.deint = Adw.SwitchRow(title="Deinterlace", subtitle="Remove combing from camcorder/TV footage")
        self.stab = Adw.SwitchRow(title="Stabilize", subtitle="Reduce handheld shake (basic)")
        self.denoise = Adw.SwitchRow(title="Denoise", subtitle="Smooth low-light grain")
        for w in (self.deint, self.stab, self.denoise):
            w.connect("notify::active", ch)
            self.clean_group.add(w)

        # audio
        self.audio_group = Adw.PreferencesGroup(title="Audio")
        page.add(self.audio_group)
        self.mute = Adw.SwitchRow(title="Remove audio")
        self.norm = Adw.SwitchRow(title="Normalize loudness",
                                  subtitle="EBU R128 to −16 LUFS, good for phones and the web")
        self.vol = Adw.SpinRow.new_with_range(-30, 30, 1)
        self.vol.set_title("Volume (dB)")
        self.vol.set_value(0)
        for w in (self.mute, self.norm):
            w.connect("notify::active", ch)
        self.vol.connect("notify::value", ch)
        for w in (self.mute, self.norm, self.vol):
            self.audio_group.add(w)

        # privacy
        g = Adw.PreferencesGroup(title="Privacy")
        page.add(g)
        self.strip = Adw.SwitchRow(title="Strip metadata",
                                   subtitle="Remove GPS location, device and date tags")
        self.strip.connect("notify::active", ch)
        g.add(self.strip)

        self._format_changed()

    def narrow_setters(self, bp: Adw.Breakpoint, short: bool = False):
        """Below ~1200 px the preview stacks above the form, with a smaller player so the
        form keeps most of the height (and less of it again in a short window)."""
        bp.add_setter(self.paned, "orientation", Gtk.Orientation.VERTICAL)
        bp.add_setter(self.paned, "position", 360 if short else 560)
        bp.add_setter(self.player.video, "height-request", 100 if short else 150)

    @property
    def replacing(self) -> bool:
        return self.save_mode.get_active_name() == "replace"

    def _save_mode_changed(self):
        self.save_row.set_subtitle(
            "The original goes to the Trash once the edit verifies; the edit takes its "
            "place in the library" if self.replacing else
            "The original is kept; when the edit is saved you choose whether it joins the "
            "library or takes the original's place")
        if hasattr(self, "strip"):      # the form is complete: re-plan
            self._changed()

    def _remember_gpu(self):
        if self.ctx.settings.gpu != self.gpu.get_active():
            self.ctx.settings.gpu = self.gpu.get_active()
            self.ctx.settings.save()

    # ================================================================== reacting
    def _changed(self, *_):
        if self._applying:
            return
        if self.preset.get_key() != "custom" and not self._matches_preset():
            self._applying = True
            self.preset.set_key("custom")
            self._applying = False
        self._sync_visibility()
        self._schedule()

    def _schedule(self):
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(120, self._update_now)

    def _format_changed(self):
        f = FORMATS[self.fmt.get_key() or "mp4"]
        self._applying, was = True, self._applying
        self.vcodec.set_entries([("auto", VCODEC_LABELS["auto"]), ("copy", VCODEC_LABELS["copy"])]
                                + [(c, VCODEC_LABELS[c]) for c in f.vcodecs])
        self.acodec.set_entries([("auto", ACODEC_LABELS["auto"]), ("copy", ACODEC_LABELS["copy"])]
                                + [(c, ACODEC_LABELS[c]) for c in f.acodecs])
        self._applying = was
        self._sync_visibility()
        if not self._applying:
            self._changed()

    def _sync_visibility(self):
        f = FORMATS[self.fmt.get_key() or "mp4"]
        video = f.kind == "video"
        visual = f.kind in ("video", "gif")
        vc = self.vcodec.get_key()
        for w in (self.vcodec, self.quality, self.speedp, self.target):
            w.set_visible(video)
        gpu_ok = any(e.endswith("_nvenc") for e in self.ctx.encoders)
        self.gpu.set_visible(video and gpu_ok and vc in ("auto", "h264", "hevc", "av1"))
        enc_quality = vc not in ("prores", "dnxhr", "copy")
        self.quality.set_sensitive(enc_quality and not self.target.get_enable_expansion())
        self.speedp.set_sensitive(enc_quality)
        self.acodec.set_visible(f.kind in ("video", "audio"))
        self.abr.set_visible(f.kind in ("video", "audio"))
        self.gif_group.set_visible(f.kind == "gif")
        for g in (self.geo_group, self.crop_group, self.clean_group):
            g.set_visible(visual)
        self.size_group.set_visible(True)
        self.resize.set_visible(video)
        self.resize_w.set_visible(video and self.resize.get_key() == "custom")
        self.fps.set_visible(video)
        self.fps_lossless.set_visible(video)
        self.audio_group.set_visible(f.kind in ("video", "audio"))
        self.mute.set_visible(video)
        lossless = self.lossless.get_active()
        self.bake.set_sensitive(not lossless)
        has_player = self.player.path is not None
        self.player.set_visible(has_player)
        self._sync_trim()
        self._sync_crop()

    # ================================================================== presets
    def _apply_preset(self):
        if self._applying:
            return
        key = self.preset.get_key()
        values = next((v for k, _, v in PRESETS if k == key), None)
        if not values:
            return
        self._applying = True
        try:
            self.fmt.set_key(values.get("fmt", "mp4"))
            self._format_changed()
            self.vcodec.set_key(values.get("vcodec", "auto"))
            self.acodec.set_key(values.get("acodec", "auto"))
            self.quality.set_key(values.get("quality", "high"))
            self.abr.set_key(str(values.get("audio_kbps", 192)))
            tm = values.get("target_mb", 0)
            self.target.set_enable_expansion(bool(tm))
            if tm:
                self.target_mb.set_value(tm)
            sh = values.get("scale_height", 0)
            self.resize.set_key(str(sh))
            self.bake.set_active(values.get("bake_rotation", True))
            # flag-only rotation cannot combine with re-encoding presets
            self.lossless.set_active(False)
            if key == "remux":
                # a lossless remux: clear every edit that would force a re-encode
                self.rot.set_active_name("0")
                for sw in (self.hflip, self.vflip, self.deint, self.stab, self.denoise,
                           self.norm, self.t_exact):
                    sw.set_active(False)
                self.crop.set_enable_expansion(False)
                self.vol.set_value(0)
                self.fps.set_key("0")
                self.fps_lossless.set_active(False)
                self.speed.set_key("1")
        finally:
            self._applying = False
        self._sync_visibility()
        self._schedule()

    def _matches_preset(self) -> bool:
        key = self.preset.get_key()
        values = next((v for k, _, v in PRESETS if k == key), {})
        checks = {
            "fmt": self.fmt.get_key(), "vcodec": self.vcodec.get_key(),
            "acodec": self.acodec.get_key(), "quality": self.quality.get_key(),
            "audio_kbps": int(self.abr.get_key() or 0),
            "bake_rotation": self.bake.get_active(),
            "scale_height": int(self.resize.get_key()) if self.resize.get_key().isdigit() else -1,
        }
        defaults = {"bake_rotation": True, "scale_height": 0}
        for k, v in checks.items():
            want = values.get(k, defaults.get(k))
            if want is not None and want != v:
                return False
        tm = values.get("target_mb", 0)
        if bool(tm) != self.target.get_enable_expansion():
            return False
        if tm and abs(self.target_mb.get_value() - tm) > 1e-6:
            return False
        if key == "remux" and self._edits_pixels():
            return False      # it promises a lossless remux; edits force a re-encode
        return True

    def _edits_pixels(self) -> bool:
        return bool(self.rot.get_active_name() not in (None, "0") or self.hflip.get_active()
                    or self.vflip.get_active() or self.crop.get_enable_expansion()
                    or self.resize.get_key() != "0" or self.fps.get_key() != "0"
                    or self.speed.get_key() != "1" or self.deint.get_active()
                    or self.stab.get_active() or self.denoise.get_active()
                    or self.lossless.get_active() or self.norm.get_active()
                    or self.vol.get_value() != 0
                    or (self.trim.get_enable_expansion() and self.t_exact.get_active()))

    # ================================================================== recipe
    def recipe(self) -> Recipe:
        r = Recipe()
        r.fmt = self.fmt.get_key()
        f = FORMATS[r.fmt]
        r.vcodec = self.vcodec.get_key() if f.kind == "video" else "auto"
        r.acodec = self.acodec.get_key()
        r.gpu = self.gpu.get_visible() and self.gpu.get_active()
        r.quality = self.quality.get_key()
        r.speed_preset = self.speedp.get_key()
        r.audio_kbps = int(self.abr.get_key() or 192)
        if f.kind == "video" and self.target.get_enable_expansion():
            r.target_mb = float(self.target_mb.get_value())
        for row in (self.t_start, self.t_end):
            row.remove_css_class("error")
        if self.trim.get_enable_expansion():
            r.trim_start = self._time(self.t_start, "Start")
            r.trim_end = self._time(self.t_end, "End")
            r.trim_exact = self.t_exact.get_active()
        visual = f.kind in ("video", "gif")
        if visual:
            r.rotate = int(self.rot.get_active_name() or 0)
            r.hflip = self.hflip.get_active()
            r.vflip = self.vflip.get_active()
            r.rotate_lossless = self.lossless.get_active() and f.kind == "video"
            r.bake_rotation = self.bake.get_active() or f.kind == "gif"
            if self.crop.get_enable_expansion():
                r.crop = (int(self.c_w.get_value()), int(self.c_h.get_value()),
                          int(self.c_x.get_value()), int(self.c_y.get_value()))
            r.deinterlace = self.deint.get_active()
            r.stabilize = self.stab.get_active()
            r.denoise = self.denoise.get_active()
        if f.kind == "video":
            rk = self.resize.get_key()
            if rk == "custom":
                r.scale_width = int(self.resize_w.get_value())
            elif rk != "0":
                r.scale_height = int(rk)
            fk = self.fps.get_key()
            if fk == "cfr":
                r.cfr = True
            elif fk != "0":
                r.fps = float(fk)
            r.fps_lossless = self.fps_lossless.get_active()
            r.remove_audio = self.mute.get_active()
        r.speed = float(self.speed.get_key() or 1)
        if f.kind in ("video", "audio"):
            r.normalize_audio = self.norm.get_active()
            r.volume_db = float(self.vol.get_value())
        r.strip_metadata = self.strip.get_active()
        r.replace_original = self.replacing
        if f.kind == "gif":
            r.gif_fps = float(self.gif_fps.get_key())
            r.gif_width = int(self.gif_w.get_key())
            r.gif_per_frame_palette = self.gif_pal.get_active()
        r.suffix = self._suffix(r)
        return r

    @staticmethod
    def _time(row: Adw.EntryRow, what: str):
        text = row.get_text().strip()
        if not text:
            return None
        try:
            return parse_time(text)
        except ValueError:
            row.add_css_class("error")
            raise InputError(f"{what} time “{text}” is not a time (try 1:05 or 65.5)")

    @staticmethod
    def _suffix(r: Recipe) -> str:
        bits = []
        if r.trimmed():
            bits.append("trim")
        if r.rotate or r.hflip or r.vflip:
            bits.append("rotated")
        if r.crop:
            bits.append("crop")
        if r.scale_height:
            bits.append(f"{r.scale_height}p")
        elif r.scale_width:
            bits.append(f"{r.scale_width}w")
        if r.speed != 1:
            bits.append(f"{r.speed:g}x")
        if r.target_mb:
            bits.append(f"{r.target_mb:g}MB")
        return ("_" + "_".join(bits)) if bits else ""

    # ================================================================== planning
    def set_items(self, items: list[MediaItem]):
        self.items = items
        self.stack.set_visible_child_name("main" if items else "empty")
        first = next((i for i in items if i.ready and i.info.has_video), None)
        if first:
            w, h = first.info.display_size
            if not self.crop.get_enable_expansion():
                self.c_w.set_value(w - w % 2)
                self.c_h.set_value(h - h % 2)
                self.c_x.set_value(0)
                self.c_y.set_value(0)
        self._load_player()
        self._sync_visibility()
        self._schedule()

    def _load_player(self):
        if not self.get_mapped():
            if self.player.path and self.player.path not in {i.path for i in self.items}:
                self.player.load(None)       # removed from the selection: let the file go
            return
        first = next((i for i in self.items if i.ready and i.info.has_video), None)
        self.player.load(first.path if first else None, first.info if first else None)
        self._sync_visibility()

    def _plans(self):
        """(valid plans, errors as 'name: message') for the current form and selection."""
        r = self.recipe()
        plans, errors = [], []
        taken = self.ctx.taken()
        for it in self.items:
            if not it.ready:
                errors.append(f"{it.name}: {'still reading' if it.state == 'probing' else it.error}")
                continue
            try:
                p = ops.build(it.info, r, self.ctx.out_dir, self.ctx.encoders, taken)
            except PlanError as e:
                errors.append(f"{it.name}: {e}")
                continue
            taken.add(p.output)
            plans.append(p)
        return plans, errors

    def _update_now(self):
        self._pending = 0
        if not self.items:
            return False
        try:
            plans, errors = self._plans()
        except InputError as e:
            plans, errors = [], [str(e)]
        self._valid = plans
        if plans:
            p = plans[0]
            more = f"  (+{len(plans) - 1} more)" if len(plans) > 1 else ""
            self.out_label.set_label(f"→ {self._final_name(p)}{more}")
            lines = list(p.summary)
            if p.expect.duration:
                lines.append(f"Result length {fmt_duration(p.expect.duration)}")
            if len(plans) > 1 and any(q.summary != p.summary for q in plans[1:]):
                copied = sum(1 for q in plans if _copies_video(q))
                kinds = []
                if copied:
                    kinds.append(f"{copied} copied losslessly")
                if len(plans) - copied:
                    kinds.append(f"{len(plans) - copied} re-encoded")
                lines.insert(0, f"Shown for {os.path.basename(p.inputs[0].path)}; the clips "
                                f"differ ({', '.join(kinds)})")
            self.summary.set_label("\n".join("• " + s for s in lines))
            self.summary.set_visible(True)
        else:
            self.out_label.set_label("Nothing can be queued")
            self.summary.set_visible(False)
        self.error.set_visible(bool(errors))
        self.error.set_label("\n".join(errors[:6]) + (f"\n…and {len(errors) - 6} more"
                                                      if len(errors) > 6 else ""))
        n = len(plans)
        self.queue_btn.set_sensitive(n > 0)
        total = len(self.items)
        if n == total:
            label = "Add to queue" if n == 1 else f"Add {n} clips to queue"
        else:
            label = f"Add {n} of {total} to queue"
        if self.replacing and n:
            label += f" (original{'s' if n != 1 else ''} to Trash)"
        self.queue_btn.set_label(label)
        return False

    @staticmethod
    def _final_name(p) -> str:
        """What the output will be called once the job is done: a replacement saved
        beside its original in the same format takes over the original's name."""
        name = os.path.basename(p.output)
        if not p.replace_original:
            return name
        src = p.inputs[0].path
        same_dir = os.path.dirname(os.path.abspath(src)) == os.path.dirname(
            os.path.abspath(p.output))
        same_ext = os.path.splitext(src)[1].lower() == os.path.splitext(p.output)[1].lower()
        if same_dir and same_ext:
            name = os.path.basename(src)
        return f"{name} (replaces the original)"

    def run(self):
        try:
            plans, _ = self._plans()
        except InputError as e:
            self.ctx.toast(str(e))
            return
        for p in plans:
            # a new file is offered to the library when it is done; a replacement takes
            # the original's place by itself
            self.ctx.submit(p, offer=not p.replace_original)
        if plans:
            self.ctx.toast(f"Queued {len(plans)} job{'s' if len(plans) != 1 else ''}")
            self.ctx.show_queue()
            # a double click must not queue everything twice
            self.queue_btn.set_sensitive(False)
            GLib.timeout_add(700, lambda: self._schedule() and False)
        else:
            self._schedule()

    # ================================================================== helpers
    def _mark(self, start: bool):
        pos = self.player.position()
        self.trim.set_enable_expansion(True)
        self.trim.set_expanded(True)
        (self.t_start if start else self.t_end).set_text(fmt_time_entry(pos))

    def _trim_entry_changed(self, *_):
        if not self._applying:
            self.trim.set_enable_expansion(True)
            self._changed()

    def _sync_trim(self):
        if not hasattr(self, "t_end"):
            return
        info = self._first_info()
        duration = info.duration if info else 0
        self.timeline.duration = duration
        self.timeline.step = 1 / (info.fps or 30) if info else 1 / 30
        start, end = 0, duration
        valid = duration > 0
        if self.trim.get_enable_expansion():
            try:
                start = parse_time(self.t_start.get_text()) if self.t_start.get_text().strip() else 0
                end = parse_time(self.t_end.get_text()) if self.t_end.get_text().strip() else duration
                valid = valid and 0 <= start < end <= duration
            except ValueError:
                valid = False
        self.timeline.start = max(0, min(duration, start))
        self.timeline.end = max(self.timeline.start, min(duration, end))
        self.timeline.set_sensitive(duration > 0)
        self.timeline.queue_draw()
        self.preview_trim.set_sensitive(valid and self.player.path is not None)
        text = f"Keep {fmt_time_entry(end - start)}" if valid else "Check trim times"
        if len(self.items) > 1:
            text += f" · {len(self.items)} clips"
        self.trim_length.set_label(text)
        self._stop_trim_preview()

    def _range_changed(self, start, end):
        self._applying = True
        try:
            self.trim.set_enable_expansion(True)
            self.t_start.set_text(fmt_time_entry(start))
            self.t_end.set_text("" if end >= self.timeline.duration else fmt_time_entry(end))
            self.trim.set_expanded(False)
        finally:
            self._applying = False
        self._changed()

    def _reset_trim(self, *_):
        self._applying = True
        try:
            self.trim.set_enable_expansion(False)
            self.t_start.set_text("")
            self.t_end.set_text("")
        finally:
            self._applying = False
        self._seek_trim(0)
        self._changed()

    def _stop_trim_preview(self, *_):
        if self._previewing:
            self.player.pause()
        self._previewing = False
        self.preview_trim.set_label("Preview selection")

    def _seek_trim(self, seconds):
        self._stop_trim_preview()
        self.player.pause()
        self.player.seek(seconds)
        self.timeline.position = seconds
        self.timeline.queue_draw()

    def _preview_trim(self, *_):
        if self._previewing:
            self._stop_trim_preview()
            return
        stream = self.player.video.get_media_stream()
        if stream is None or not stream.is_seekable():
            self.ctx.toast("The video is still loading. Try again in a moment.")
            return
        self.player.seek(self.timeline.start)
        self._previewing = True
        self._preview_pending_seek = True
        self.preview_trim.set_label("Stop preview")
        stream.play()

    def _trim_tick(self, *_):
        self.timeline.position = self.player.position()
        if self._previewing:
            # Ignore the previous timestamp until the asynchronous seek completes.
            stream = self.player.video.get_media_stream()
            if self._preview_pending_seek:
                if stream is not None and not stream.is_seeking():
                    self._preview_pending_seek = False
            elif self.timeline.position >= self.timeline.end or stream is None or not stream.get_playing():
                self._stop_trim_preview()
        self.timeline.queue_draw()
        return True

    def _first_info(self):
        return next((i.info for i in self.items if i.ready and i.info.has_video), None)

    def _reset_crop(self, *_):
        info = self._first_info()
        if info:
            w, h = info.display_size
            self._set_crop(w - w % 2, h - h % 2, 0, 0)
        self.crop.set_enable_expansion(False)

    def _sync_crop(self):
        info = self._first_info()
        visual = FORMATS[self.fmt.get_key() or "mp4"].kind in ("video", "gif")
        enabled = self.crop.get_enable_expansion()
        self.crop_overlay.set_visible(visual and info is not None and enabled)
        self.reset_crop.set_sensitive(enabled)
        self.crop_overlay.source = info.display_size if info else (0, 0)
        values = tuple(int(row.get_value()) for row in (self.c_w, self.c_h, self.c_x, self.c_y))
        self.crop_overlay.crop = values
        self.crop_overlay.queue_draw()
        self.frame_actions.set_media(info, values if visual and enabled else None)

    def _aspect(self, ratio: float):
        info = self._first_info()
        if not info or not all(info.display_size):
            return
        w, h = info.display_size
        if w / h > ratio:
            cw, ch = int(h * ratio), h
        else:
            cw, ch = w, int(w / ratio)
        cw, ch = cw - cw % 2, ch - ch % 2
        self._set_crop(cw, ch, (w - cw) // 2, (h - ch) // 2)

    def _set_crop(self, w, h, x, y):
        self._applying = True
        try:
            self.crop.set_enable_expansion(True)
            self.crop.set_expanded(True)
            self.c_w.set_value(w)
            self.c_h.set_value(h)
            self.c_x.set_value(x)
            self.c_y.set_value(y)
        finally:
            self._applying = False
        self._changed()

    def _detect(self):
        info = self._first_info()
        if not info:
            return
        self.detect_btn.set_sensitive(False)
        self.detect_btn.set_label("Detecting…")

        def work():
            res = ops.detect_crop(info)
            GLib.idle_add(done, res)

        def done(res):
            self.detect_btn.set_sensitive(True)
            self.detect_btn.set_label("Detect black bars")
            cur = self._first_info()
            if cur is None or cur.path != info.path:
                return False     # selection changed meanwhile: not this clip's bars
            if res is None:
                self.ctx.toast("No black bars found")
            else:
                self._set_crop(*res)
                self.ctx.toast(f"Cropping to {res[0]}×{res[1]}")
            return False
        threading.Thread(target=work, daemon=True).start()
