"""Small storage context for receipts in the commands-only profile."""
import os
from pathlib import Path

import compat


def state_path(host="claude"):
    host = os.environ.get("ROUTER_COMMANDS_HOST", host)
    override = os.environ.get("ROUTER_COMMANDS_STATE")
    if override:
        return Path(override).expanduser().absolute()
    base = Path(os.environ.get("XDG_STATE_HOME") or Path(os.environ.get("HOME", str(Path.home()))) / ".local/state")
    if not base.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    return base / "router-commands" / host
