"""Small pieces every way of running Argus shares (the Mac app, the always-on server, Android): the log
file and the lock that keeps a second copy from starting. Nothing here needs the Mac's audio libraries."""
from __future__ import annotations

import fcntl
import logging
import logging.handlers
import os
import sys

from . import config as config_mod


def setup_logging(settings: config_mod.Settings, verbose: bool = False) -> None:
    path = settings.audit_path.parent / "argus.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if verbose or sys.stderr.isatty():
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)
    os.chmod(path, 0o600)


def single_instance(settings: config_mod.Settings):
    """An exclusive lock so a second copy (e.g. opened from Finder) exits instead of double-answering."""
    path = settings.run_dir / "argus.lock"
    fh = open(path, "w")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def kind() -> str:
    """Which Argus this is: "android" (in Termux on a phone), "server" (the always-on machine; its systemd
    service and admin command set ARGUS_MODE=server) or "mac"."""
    if os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""):
        return "android"
    if os.environ.get("ARGUS_MODE") == "server":
        return "server"
    return "mac"


START_HINT = {"mac": "open -a Argus   (or: argusctl run)", "server": "sudo systemctl start argus",
              "android": "argus-android serve &"}
