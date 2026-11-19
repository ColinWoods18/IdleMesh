"""Owner-only files for secrets (private keys, tokens).

POSIX: the file is created with mode 0600, so it is never readable by others, even briefly.
Windows: mode bits are ignored, so inherited ACL entries are removed and only the current account
(by SID) gets access, using the built-in ``icacls`` tool. Nothing extra needs installing.
"""
import os
import subprocess
import sys
from pathlib import Path

_WINDOWS = sys.platform == "win32"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flash when run from a service or GUI


def _current_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         check=True, creationflags=_NO_WINDOW).stdout.strip()
    # One CSV row: "DOMAIN\user","S-1-5-..."
    return out.rsplit(",", 1)[-1].strip().strip('"')


def restrict_to_owner(path: Path) -> None:
    """Make an existing file readable and writable by its owner only."""
    if _WINDOWS:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{_current_sid()}:F"],
                       capture_output=True, check=True, creationflags=_NO_WINDOW)
    else:
        os.chmod(path, 0o600)


def write_private(path: Path, data: bytes) -> None:
    """Create or replace ``path`` with ``data``, restricted to the owner before any secret is written."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        if _WINDOWS:
            restrict_to_owner(path)  # still empty here, so the secret never sits under inherited ACLs
        f.write(data)


def is_owner_only(path: Path) -> bool:
    """True when nobody but the current account can access ``path``."""
    if not _WINDOWS:
        return (path.stat().st_mode & 0o077) == 0
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True,
                         creationflags=_NO_WINDOW).stdout
    # icacls lists one access entry per line, as "<path or padding><account>:(rights)".
    entries = [line.lower() for line in out.splitlines() if ":(" in line]
    who = subprocess.run(["whoami"], capture_output=True, text=True, check=True,
                         creationflags=_NO_WINDOW).stdout.strip().lower()
    # Compare the account name only: icacls may show a truncated (NetBIOS) machine or domain name where
    # whoami shows the full one, as on GitHub's Windows runners.
    user = who.rsplit("\\", 1)[-1]
    return len(entries) == 1 and "(i)" not in entries[0] and f"\\{user}:(" in entries[0]
