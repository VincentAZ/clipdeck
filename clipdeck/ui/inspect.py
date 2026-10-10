"""Inspect: preview the selected clip and show everything ffprobe knows about it."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..media import fmt_bitrate, fmt_duration, fmt_size
from .library import ERROR, PROBING, MediaItem
from .frame import FrameActions
from .widgets import Player, clear_group, plain_row


class InspectPage(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.items: list[MediaItem] = []
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.append(self.stack)

        self.empty = Adw.StatusPage(icon_name="video-x-generic-symbolic",
                                    title="Nothing selected",
                                    description="Pick a clip in the library to inspect it.")
        self.stack.add_named(self.empty, "empty")

        # single clip: player on top, details below
        single = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.player = Player(height=300)
        self.player.set_margin_start(12)
        self.player.set_margin_end(12)
        self.player.set_margin_top(12)
        single.append(self.player)
        self.frame_actions = FrameActions(ctx, self.player)
        self.frame_actions.set_margin_start(12)
        self.frame_actions.set_margin_end(12)
        self.frame_actions.set_margin_top(8)
        single.append(self.frame_actions)
        self.details = Adw.PreferencesPage()
        self.details.set_vexpand(True)
        single.append(self.details)
        self.groups: list[Adw.PreferencesGroup] = []
        self.stack.add_named(single, "single")

        multi = Adw.PreferencesPage()
        self.multi_group = Adw.PreferencesGroup()
        multi.add(self.multi_group)
        self.multi_rows: list = []
        self.stack.add_named(multi, "multi")

        self.connect("map", lambda *_: self._load_player())
        self.connect("unmap", lambda *_: self.player.pause())

    # ------------------------------------------------------------------ updates
    def set_items(self, items: list[MediaItem]):
        self.items = items
        self.refresh()

    def refresh(self):
        items = self.items
        first = items[0] if len(items) == 1 and items[0].ready else None
        self.frame_actions.set_media(first.info if first else None)
        if not items:
            self.stack.set_visible_child_name("empty")
            self.player.load(None)
            return
        if len(items) > 1:
            self._show_multi(items)
            self.stack.set_visible_child_name("multi")
            self.player.load(None)
            return
        self._show_single(items[0])
        self.stack.set_visible_child_name("single")
        self._load_player()

    def _load_player(self):
        if not self.get_mapped():
            return
        it = self.items[0] if len(self.items) == 1 else None
        self.player.load(it.path if it and it.ready and it.info.has_video else None)
        self.player.set_visible(bool(it and it.ready and it.info.has_video))

    def _show_multi(self, items):
        clear_group(self.multi_group, self.multi_rows)
        total = sum(i.info.duration for i in items if i.ready)
        size = sum(i.info.size for i in items if i.ready)
        self.multi_group.set_title(f"{len(items)} clips selected")
        self.multi_group.set_description(f"{fmt_duration(total)} total · {fmt_size(size)}")
        for it in items:
            row = plain_row(it.name)
            row.set_title_lines(1)
            if it.ready:
                issues = it.info.issues()
                row.set_subtitle(", ".join(i.title for i in issues) if issues else "No problems found")
                icon = "dialog-warning-symbolic" if issues else "object-select-symbolic"
            elif it.state == PROBING:
                row.set_subtitle("Reading…")
                icon = "content-loading-symbolic"
            else:
                row.set_subtitle(it.error)
                icon = "dialog-error-symbolic"
            row.add_suffix(Gtk.Image(icon_name=icon))
            self.multi_group.add(row)
            self.multi_rows.append(row)

    def _group(self, title: str, description: str = "") -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=GLib.markup_escape_text(title))
        if description:
            g.set_description(GLib.markup_escape_text(description))
        self.details.add(g)
        self.groups.append(g)
        return g

    @staticmethod
    def _row(group, title: str, value: str, copyable: bool = False):
        row = plain_row(title, value or "—")
        row.add_css_class("property")
        row.set_subtitle_selectable(copyable)
        group.add(row)
        return row

    def _show_single(self, it: MediaItem):
        for g in self.groups:
            self.details.remove(g)
        self.groups.clear()
        if it.state == PROBING:
            self._group("Reading file…")
            return
        if it.state == ERROR or not it.info:
            g = self._group("Cannot read this file", it.error)
            self._row(g, "Path", it.path, True)
            return
        info = it.info

        issues = info.issues()
        if issues:
            g = self._group("Problems found")
            for iss in issues:
                row = plain_row(iss.title, iss.detail)
                row.set_subtitle_lines(0)
                icon = Gtk.Image(icon_name="dialog-warning-symbolic")
                icon.add_css_class("warning")
                row.add_prefix(icon)
                if iss.fixable:
                    tag = Gtk.Label(label="Quick Fix")
                    tag.add_css_class("badge")
                    tag.add_css_class("accent")
                    tag.set_valign(Gtk.Align.CENTER)
                    row.add_suffix(tag)
                g.add(row)

        g = self._group("File")
        self._row(g, "Path", info.path, True)
        self._row(g, "Container", f".{info.container} ({info.format_name})")
        self._row(g, "Duration", f"{fmt_duration(info.duration)} ({info.duration:.3f} s)")
        self._row(g, "Size", f"{fmt_size(info.size)} · {fmt_bitrate(info.bitrate)}")

        if info.has_video:
            g = self._group("Video")
            prof = f" ({info.vprofile})" if info.vprofile else ""
            self._row(g, "Codec", f"{info.vcodec}{prof}")
            dw, dh = info.display_size
            self._row(g, "Displayed size", f"{dw}×{dh}")
            stored = f"{info.width}×{info.height}"
            if info.sar != 1:
                stored += f" (pixel aspect {info.sar.numerator}:{info.sar.denominator})"
            self._row(g, "Stored pixels", stored)
            if info.rotation == 180:
                rot = "Players turn it 180°"
            elif info.rotation:
                rot = (f"Players turn it 90° "
                       f"{'clockwise' if info.rotation < 0 else 'counter-clockwise'}")
            else:
                rot = "None"
            self._row(g, "Rotation flag", rot)
            rate = f"{info.fps:.3f} fps" if info.fps else "—"
            if info.is_vfr:
                rate += f" average, variable (nominal {info.r_fps:.3f})"
            self._row(g, "Frame rate", rate)
            if info.nb_frames:
                self._row(g, "Frames", str(info.nb_frames))
            self._row(g, "Pixel format", f"{info.pix_fmt} · {info.bit_depth}-bit")
            color = " / ".join(x for x in (info.color_primaries, info.color_transfer,
                                           info.color_space, info.color_range) if x)
            if color:
                self._row(g, "Color", color + (" (HDR)" if info.is_hdr else ""))
            if info.field_order:
                self._row(g, "Scan", "Interlaced" if info.is_interlaced else "Progressive")

        if info.audio:
            for n, a in enumerate(info.audio, 1):
                g = self._group("Audio" if len(info.audio) == 1 else f"Audio track {n}")
                self._row(g, "Codec", a.codec)
                ch = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(a.channels, f"{a.channels} ch")
                self._row(g, "Channels", ch)
                self._row(g, "Sample rate", f"{a.sample_rate} Hz" if a.sample_rate else "—")
                self._row(g, "Bitrate", fmt_bitrate(a.bitrate))
                if a.language and a.language != "und":
                    self._row(g, "Language", a.language)
        else:
            g = self._group("Audio")
            self._row(g, "Tracks", "None")
        if info.subtitle_count:
            g = self._group("Other")
            self._row(g, "Subtitle tracks", f"{info.subtitle_count} (dropped by conversions)")
