"""Drive the real GTK app headlessly (run under xvfb-run). A scenario is a generator
that yields conditions; the driver advances it from the GTK main loop, so every action
runs exactly like a user's click would. Any exception in any GTK callback fails the run."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

os.environ.setdefault("GSK_RENDERER", "cairo")
# These scenarios run under Xvfb, even when the developer's desktop uses Wayland.
os.environ["GDK_BACKEND"] = "x11"
os.environ["CLIPDECK_NON_UNIQUE"] = "1"
_sandbox = tempfile.mkdtemp(prefix="clipdeck-ui-home-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_sandbox, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_sandbox, "cache")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

from clipdeck.app import ClipdeckApp  # noqa: E402

ERRORS: list[str] = []
_orig_hook = sys.excepthook


def _hook(tp, val, tb):
    ERRORS.append("".join(traceback.format_exception(tp, val, tb)))
    _orig_hook(tp, val, tb)


sys.excepthook = _hook


class Wait:
    def __init__(self, cond, what: str, timeout: float = 120):
        self.cond, self.what, self.timeout = cond, what, timeout


def sleep(sec: float):
    end = time.monotonic() + sec
    return Wait(lambda: time.monotonic() >= end, f"sleep {sec}", sec + 5)


def screenshot(widget: Gtk.Widget, path: str):
    w, h = widget.get_width(), widget.get_height()
    if w <= 0 or h <= 0:
        print(f"screenshot {path}: widget not allocated", file=sys.stderr)
        return
    paintable = Gtk.WidgetPaintable.new(widget)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        print(f"screenshot {path}: nothing rendered", file=sys.stderr)
        return
    renderer = widget.get_native().get_renderer()
    tex = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
    tex.save_to_png(path)


def run(scenario, shots_dir: str | None = None) -> int:
    """Run scenario(app, win) -> generator. Returns a process exit code."""
    app = ClipdeckApp()
    state = {"gen": None, "wait": None, "since": 0.0, "rc": 0, "done": False}

    def fail(msg):
        print("UI TEST FAILURE:", msg, file=sys.stderr)
        state["rc"] = 1
        finish()

    def finish():
        if state["done"]:
            return
        state["done"] = True
        win = app.win
        if win is not None:
            win.queue.cancel_all()
            win._quit_confirmed = True
            win.close()
        app.release()
        app.quit()

    def tick():
        if state["done"]:
            return False
        if ERRORS:
            fail("exception in GTK callback:\n" + ERRORS[0])
            return False
        w = state["wait"]
        if w is not None:
            try:
                ok = w.cond()
            except Exception:  # noqa: BLE001
                fail(f"condition '{w.what}' raised:\n{traceback.format_exc()}")
                return False
            if not ok:
                if time.monotonic() - state["since"] > w.timeout:
                    fail(f"timed out waiting for: {w.what}")
                    return False
                return True
        try:
            nxt = next(state["gen"])
        except StopIteration:
            finish()
            return False
        except Exception:  # noqa: BLE001
            fail(traceback.format_exc())
            return False
        if isinstance(nxt, (int, float)):
            nxt = sleep(nxt)
        state["wait"] = nxt if isinstance(nxt, Wait) else None
        state["since"] = time.monotonic()
        return True

    def start():
        app.hold()     # keep the main loop alive if the scenario closes the window
        state["gen"] = scenario(app, app.win)
        GLib.timeout_add(40, tick)
        return False

    def on_activate(_app):
        # "activate" handlers run before the class handler that builds the window
        if state["gen"] is None:
            GLib.idle_add(start)
    app.connect("activate", on_activate)
    rc = app.run([sys.argv[0]])
    if ERRORS and not state["rc"]:
        print("UI TEST FAILURE: exception in GTK callback:\n" + ERRORS[0], file=sys.stderr)
        state["rc"] = 1
    return state["rc"] or rc
