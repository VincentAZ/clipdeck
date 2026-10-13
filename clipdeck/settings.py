"""Persistent user preferences (~/.config/clipdeck/settings.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "clipdeck")


@dataclass
class Settings:
    output_mode: str = "beside"      # beside | folder
    output_folder: str = ""
    thorough: bool = True            # full decode + frame count after every job
    workers: int = 1
    gpu: bool = False
    autofix_cfr: bool = False
    autofix_mov: bool = False
    last_dir: str = ""
    civitai_model: str = "minimax-h3"   # Tools → Tag for Civitai
    civitai_air: str = ""
    civitai_replace: bool = True

    @property
    def out_dir(self) -> str | None:
        if self.output_mode == "folder" and self.output_folder and os.path.isdir(self.output_folder):
            return self.output_folder
        return None

    @classmethod
    def load(cls) -> "Settings":
        path = os.path.join(config_dir(), "settings.json")
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        s = cls()
        for f in fields(cls):
            v = raw.get(f.name)
            # bool is an int subclass: never let true/false stand in for a number
            if isinstance(v, type(getattr(s, f.name))) and \
                    isinstance(v, bool) == isinstance(getattr(s, f.name), bool):
                setattr(s, f.name, raw[f.name])
        s.workers = min(4, max(1, s.workers))
        if s.output_mode not in ("beside", "folder"):
            s.output_mode = "beside"
        return s

    def save(self):
        d = config_dir()
        try:
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, "settings.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp, os.path.join(d, "settings.json"))
        except OSError:
            pass
