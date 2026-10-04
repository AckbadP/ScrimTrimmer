"""
config.py - Persistent GUI settings for the EVE AT Practice Trimmer.

Settings are stored in ~/.config/eve-trimmer/config.json.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "eve-trimmer" / "config.json"


def _most_recent_file_mtime(directory: Path) -> float:
    """Return the mtime of the most recently created file in directory, or -1 if empty."""
    try:
        mtimes = [f.stat().st_mtime for f in directory.iterdir() if f.is_file()]
    except OSError:
        return -1.0
    return max(mtimes) if mtimes else -1.0


def default_log_dir() -> str:
    """Return the OS-specific default EVE chat log directory."""
    if sys.platform == "linux":
        steam_path = (
            Path.home()
            / ".local/share/Steam/steamapps/compatdata/8500/pfx"
            / "drive_c/users/steamuser/Documents/EVE/logs/Chatlogs"
        )
        username = os.environ.get("USER") or Path.home().name
        lutris_path = (
            Path.home()
            / "Games/eve-online/drive_c/users"
            / username
            / "Documents/EVE/logs/Chatlogs"
        )
        candidates = [p for p in (steam_path, lutris_path) if p.is_dir()]
        if not candidates:
            return str(steam_path)
        if len(candidates) == 1:
            return str(candidates[0])
        return str(max(candidates, key=_most_recent_file_mtime))
    # macOS and Windows
    return str(Path.home() / "Documents" / "EVE" / "logs" / "Chatlogs")


def default_youtube_title() -> str:
    """Return today's UTC date as MM-DD, the default YouTube video title."""
    return datetime.now(timezone.utc).strftime("%m-%d")


_DEFAULTS = {
    "video_dir": str(Path.home() / "Videos"),
    "log_dir": "",
    "output_dir": "",
    "chapters_dir": "",  # directory for the YouTube chapters .txt file; empty = same as output_dir
    "show_chapters_popup": True,  # show the timestamps popup window after a job completes
    "close_on_complete": False,  # close the main window when processing finishes successfully
    "show_debug_popup": False,  # show the OCR/verbose debug console popup
    "chat_region": [0.0, 0.35, 0.15, 1.0],  # [x1, y1, x2, y2] as fractions
    "threads": None,  # None means use os.cpu_count()
    "ram_cap_gb": 10,  # max GB of decoded frames held in memory at once
    "youtube_upload": False,  # auto-upload final video to YouTube as unlisted after processing
    "tournament_match": False,  # detect tournament match boundaries instead of CD/WF commands
    "detect_countdown": True,  # treat a bare descending countdown as an implicit CD
    "buffer_start": 0,  # seconds added to each clip's start (negative = earlier), normal mode
    "buffer_end": 0,  # seconds added to each clip's end (negative = earlier), normal mode
    "tournament_buffer_start": 0,  # same, tournament mode
    "tournament_buffer_end": 10,  # same, tournament mode (matches prior hardcoded +10s tail)
    "extract_audio": False,  # also save each clip's audio as MP3 in <output_dir>/<video name>/
    "window_geometry": "",  # last window geometry string, e.g. "900x700+100+50"
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
                return {**_DEFAULTS, **data}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
