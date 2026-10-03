"""User data lives outside the installation, so moving it preserves state."""
import os
from pathlib import Path


def user_data_dir():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "SimpleLimite"


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


DATA_DIR = user_data_dir()
