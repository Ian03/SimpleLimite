"""User data lives outside the installation, so moving it preserves state."""
import os
import sys
from pathlib import Path


def user_data_dir():
    return Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "SimpleLimite"


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


DATA_DIR = user_data_dir()


def cursor_config_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Cursor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Cursor"
