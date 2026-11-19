"""Owner-only files for secrets (private keys, tokens).

POSIX: the file is created with mode 0600, so it is never readable by others, even briefly.
Windows: mode bits are ignored. Inherited ACL entries are removed, the current account (by SID) gets
full control, and the broad groups (Everyone, Authenticated Users, Users) are removed, using the built-in
``icacls``; nothing extra needs installing. SYSTEM and Administrators may keep access where Windows sets
it explicitly: they can read any file anyway, and Windows' OpenSSH accepts the same set for private keys.
What this blocks is another ordinary user on the same machine.
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
        # Everyone, Authenticated Users, Users: by SID, so it works on any Windows language.
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{_current_sid()}:F",
                        "/remove:g", "*S-1-1-0", "*S-1-5-11", "*S-1-5-32-545"],
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
    mine = f"\\{user}:("
    # Allowed: the owner, plus SYSTEM and Administrators (see the module docstring). Nothing inherited.
    allowed = (mine, "nt authority\\system:(", "builtin\\administrators:(")
    return (bool(entries) and any(mine in e for e in entries)
            and all("(i)" not in e and any(a in e for a in allowed) for e in entries))
