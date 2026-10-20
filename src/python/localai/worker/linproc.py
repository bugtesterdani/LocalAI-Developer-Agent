"""Werkzeugprozesse im Linux-Gast isolieren (M8, Gegenstück zu winproc.py).

Der Worker läuft als root (systemd). Ein Werkzeugprozess (Build, Test, Programm des Projekts) dagegen:

* läuft als eigener Systembenutzer ``localai-tools`` ohne Zusatzgruppen – er kann weder den Sitzungsschlüssel
  (/var/lib/localai, nur root) noch Worker, Python oder Toolchains (/opt, root, nur lesbar) verändern,
* bekommt ``no_new_privs`` (keine Rechteerweiterung über setuid-Programme),
* startet in einem eigenen, leeren Netzwerk-Namespace – zusätzlich zur fehlenden Netzwerkkarte gibt es dort nicht
  einmal Loopback (kein Zugriff auf Dienste im Gast),
* hat Grenzen für Kerndateien, offene Dateien, Prozesse und optional Speicher.

Nach jedem Schritt beendet der Worker alle Prozesse des Benutzers (auch solche, die sich mit setsid aus der
Prozessgruppe gelöst haben) und löscht, was der Benutzer in /tmp, /var/tmp und /dev/shm angelegt hat.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import pathlib
import shutil
import signal
import time
from dataclasses import dataclass

TOOLS_USER = "localai-tools"
PR_SET_NO_NEW_PRIVS = 38
MAX_PROCESSES = 1024
MAX_OPEN_FILES = 8192
TEMP_DIRS = (pathlib.Path("/tmp"), pathlib.Path("/var/tmp"), pathlib.Path("/dev/shm"))

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True) if os.name != "nt" else None


@dataclass(frozen=True)
class ToolUser:
    name: str
    uid: int
    gid: int

    @classmethod
    def lookup(cls, name: str = TOOLS_USER) -> ToolUser:
        import pwd

        entry = pwd.getpwnam(name)
        if entry.pw_uid == 0 or entry.pw_gid == 0:
            raise ValueError(f"Werkzeugbenutzer {name} darf nicht root sein")
        return cls(name, entry.pw_uid, entry.pw_gid)


def isolation(user: ToolUser, memory_mb: int = 0, new_netns: bool = True, groups: tuple[int, ...] = ()):
    """preexec_fn für subprocess: läuft im Kindprozess vor exec (noch als root)."""
    import resource

    def apply() -> None:
        if new_netns:
            os.unshare(os.CLONE_NEWNET)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))
        if memory_mb > 0:
            limit = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        os.setgroups(list(groups))  # leer – nur QEMU mit KVM bekommt die Gruppe von /dev/kvm (M9)
        os.setgid(user.gid)
        os.setuid(user.uid)
        if _libc is None or _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            os._exit(126)
        os.umask(0o077)

    return apply


def hand_over(directory: pathlib.Path, user: ToolUser) -> None:
    """Arbeitsordner eines Jobs dem Werkzeugbenutzer geben (nur er und root kommen hinein)."""
    for root, dirs, files in os.walk(directory):
        for name in [*dirs, *files]:
            os.lchown(os.path.join(root, name), user.uid, user.gid)
    os.chown(directory, user.uid, user.gid)
    os.chmod(directory, 0o700)


def _user_pids(uid: int, proc: pathlib.Path) -> list[int]:
    pids = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid:
                pids.append(int(entry.name))
        except OSError:
            continue
    return pids


def kill_user_processes(uid: int, proc: pathlib.Path = pathlib.Path("/proc"), rounds: int = 50) -> int:
    """SIGKILL an alle Prozesse des Benutzers, bis keiner mehr da ist. Rückgabe: Zahl der beendeten Prozesse."""
    if uid == 0:
        raise ValueError("niemals Prozesse von root beenden")
    killed = 0
    for _ in range(rounds):
        pids = _user_pids(uid, proc)
        if not pids:
            return killed
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.05)
    return killed


def cleanup_temp(uid: int, directories: tuple[pathlib.Path, ...] = TEMP_DIRS) -> int:
    """Löscht Einträge des Benutzers in gemeinsamen Temp-Ordnern (nichts reicht in den nächsten Job)."""
    if uid == 0:
        raise ValueError("niemals Dateien von root löschen")
    removed = 0
    for directory in directories:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.lstat().st_uid != uid:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                removed += 1
            except OSError:
                continue
    return removed
