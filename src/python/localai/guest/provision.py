"""Provisionierung des Linux-Gasts (M8) – Gegenstück zu provision.ps1/LocalAiGuest.psm1 im Windows-Gast.

Läuft einmal als root, ohne Netzwerk, gestartet von ``provision.sh`` (cloud-init ``runcmd``), nachdem dieses
hyperv-daemons installiert und Python samt Worker nach /opt/localai entpackt hat::

    /opt/localai/python/bin/python3 -I -m localai.guest.provision --seed /mnt/localai-seed

1. Rechte setzen, Netzwerkprüfungen (alle müssen scheitern; Netzwerkkarten zählen)
2. Werkzeugbenutzer ``localai-tools`` anlegen
3. Toolchains installieren (Hashprüfung, Archiv/.deb, deploy, postInstall, Prüfbefehl, Hallo-Welt), danach nur lesbar
4. Worker als systemd-Dienst einrichten und starten, Copy-VMFile (hv_fcopy) prüfen
5. Seed-Laufwerk unbrauchbar machen (enthält den Sitzungsschlüssel), cloud-init abschalten, unnötige Dienste aus
6. Bericht über das Transfer-Laufwerk an den Host
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Any

from localai.pathrules import PathRuleError, normalize_relative

FCOPY_CLASS = "34d14be3-dee4-41c8-9ae7-6b174977c192"
TOOLS_USER = "localai-tools"
EMU_USER = "localai-emu"  # M9: QEMU der Debug-Sitzungen
WORKER_UNIT = "localai-worker.service"
FCOPY_UNIT = "localai-fcopy.service"
KVP_UNIT = "localai-kvp.service"
LINK_UNIT = "localai-link.service"  # M10: Verbindungskanal unter QEMU statt KVP/Copy-VMFile
# Dienste, die ohne Netz nichts tun, aber schreiben oder den Start verzögern.
QUIET_UNITS = ["ssh.service", "ssh.socket", "apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service",
               "man-db.timer", "e2scrub_all.timer", "dpkg-db-backup.timer", "systemd-networkd-wait-online.service"]


@dataclass
class Paths:
    """Feste Orte im Gast (Tests setzen eigene)."""

    root: pathlib.Path = pathlib.Path("/var/lib/localai")
    opt: pathlib.Path = pathlib.Path("/opt/localai")
    toolchains: pathlib.Path = pathlib.Path("/opt/toolchains")
    jobs: pathlib.Path = pathlib.Path("/var/lib/localai-jobs")
    systemd: pathlib.Path = pathlib.Path("/etc/systemd/system")
    sys_net: pathlib.Path = pathlib.Path("/sys/class/net")

    @property
    def python(self) -> pathlib.Path:
        return self.opt / "python" / "bin" / "python3"

    @property
    def published(self) -> pathlib.Path:
        """Manifeste und Offline-Paketquellen für Toolchain-Aktionen (für Werkzeugprozesse lesbar)."""
        return self.opt / "toolchains"


@dataclass
class Report:
    started: float = field(default_factory=time.monotonic)
    errors: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.data = {
            "schemaVersion": 1,
            "computerName": socket.gethostname(),
            "osVersion": _os_version(),
            "networkAdapters": -1,
            "probes": [],
            "worker": {"running": False, "python": ""},
            "toolchains": [],
            "errors": [],
            "durationSeconds": 0,
        }

    def finish(self) -> bytes:
        self.data["errors"] = self.errors
        self.data["durationSeconds"] = int(time.monotonic() - self.started)
        return json.dumps(self.data, ensure_ascii=False, indent=2).encode("utf-8")


def _os_version() -> str:
    try:
        for line in pathlib.Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"') + " / Kernel " + platform.release()
    except OSError:
        pass
    return "Linux " + platform.release()


def load_config(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def set_progress(token: str) -> None:
    """Fortschritt für den Host (KVP); darf die Provisionierung nie abbrechen."""
    try:
        from localai.worker import kvp
        kvp.write_value("LocalAiProgress", token)
    except Exception as err:  # noqa: BLE001
        log(f"Fortschritt {token} nicht gemeldet: {err}")


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def run(argv: list[str], timeout: int = 600, env: dict[str, str] | None = None, cwd: pathlib.Path | None = None,
        log_file: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    """Fester Programmaufruf ohne Shell; Ausgabe ins Protokoll."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, check=False,
                            stdin=subprocess.DEVNULL, errors="replace")
    if log_file is not None:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"> {' '.join(argv)}\n{result.stdout}{result.stderr}\n(Exitcode {result.returncode})\n")
    return result


# ------------------------------------------------------------------ 1 Netzwerk

def count_adapters(sys_net: pathlib.Path) -> int:
    try:
        return sum(1 for entry in sys_net.iterdir() if entry.name != "lo")
    except OSError:
        return -1


def _tcp(name: str, address: str, port: int) -> dict[str, Any]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect((address, port))
            reachable = True
    except OSError:
        reachable = False
    return {"name": name, "target": f"{address} port {port}", "reachable": reachable}


def probes() -> list[dict[str, Any]]:
    result = []
    try:
        socket.getaddrinfo("www.microsoft.com", 443)
        dns = True
    except OSError:
        dns = False
    result.append({"name": "dns", "target": "www.microsoft.com", "reachable": dns})
    try:
        urllib.request.urlopen("http://www.msftconnecttest.com/connecttest.txt", timeout=5).close()
        http = True
    except Exception:  # noqa: BLE001
        http = False
    result.append({"name": "http", "target": "www.msftconnecttest.com", "reachable": http})
    result.append(_tcp("tcp-ipv4-https", "1.1.1.1", 443))
    result.append(_tcp("tcp-ipv4-dns", "8.8.8.8", 53))
    result.append(_tcp("tcp-ipv6-https", "2606:4700:4700::1111", 443))
    result.append(_tcp("tcp-hyperv-default-switch", "172.17.0.1", 445))
    return result


# ------------------------------------------------------------------ 2 Werkzeugbenutzer

def ensure_tools_user(name: str = TOOLS_USER) -> None:
    check = run(["getent", "passwd", name])
    if check.returncode == 0:
        return
    created = run(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
                   "--user-group", name])
    if created.returncode != 0:
        raise RuntimeError(f"useradd {name} fehlgeschlagen: {created.stderr.strip()}")


# ------------------------------------------------------------------ 3 Toolchains

class ToolchainError(Exception):
    pass


def _safe_rel(path: str) -> str:
    try:
        rel = normalize_relative(path.replace("\\", "/"))
    except PathRuleError as err:
        raise ToolchainError(f"unzulässiger Pfad: {path}") from err
    if rel in ("", "."):
        raise ToolchainError(f"unzulässiger Pfad: {path}")
    return rel


def _expand(text: str, mapping: dict[str, str]) -> str:
    for key, value in mapping.items():
        text = text.replace("{" + key + "}", value)
    return text


def binary(manifest: dict[str, Any], name: str) -> pathlib.Path:
    from localai.worker.execution import LINUX_SYSTEM_BINARIES, LINUX_SYSTEM_PREFIX

    entry = next((b for b in manifest.get("binaries", []) if b.get("name") == name), None)
    if entry is None:
        raise ToolchainError(f"Binary {name} nicht deklariert")
    raw = str(entry.get("path", ""))
    if raw.startswith(LINUX_SYSTEM_PREFIX):
        target = LINUX_SYSTEM_BINARIES.get(raw[len(LINUX_SYSTEM_PREFIX):])
        if target is None:
            raise ToolchainError(f"Programm des Gastsystems nicht erlaubt: {raw}")
        return pathlib.Path(target)
    path = pathlib.Path(manifest["guestPath"]).joinpath(*_safe_rel(raw).split("/"))
    if not path.is_file():
        raise ToolchainError(f"Binary fehlt nach der Installation: {path}")
    if entry.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ToolchainError(f"Hash von {name} weicht ab")
    return path


def _extract_zip(archive: pathlib.Path, target: pathlib.Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = _safe_rel(info.filename.rstrip("/")) if info.filename.strip("/") else ""
            if not rel:
                continue
            dest = target.joinpath(*rel.split("/"))
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(dest, mode & 0o755)


def extract_archive(archive: pathlib.Path, target: pathlib.Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    name = archive.name
    if name.endswith(".zip"):
        _extract_zip(archive, target)
    elif name.endswith((".tar.gz", ".tgz", ".tar.xz")):
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(target, filter="data")  # nichts außerhalb des Ziels, keine setuid-Bits
    else:
        raise ToolchainError(f"Archivformat nicht unterstützt: {name}")


def install_toolchain(name: str, source_root: pathlib.Path, paths: Paths, log_file: pathlib.Path) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "version": "", "installed": False, "check": "", "selfTest": "keiner", "seconds": 0, "error": None}
    started = time.monotonic()
    try:
        src = source_root / name
        manifest = json.loads((src / "toolchain.json").read_text(encoding="utf-8-sig"))
        pinned = json.loads((src / "pinned.json").read_text(encoding="utf-8-sig"))
        entry["version"] = str(manifest.get("version", ""))
        if manifest.get("name") != name:
            raise ToolchainError("Name in toolchain.json passt nicht")
        if manifest.get("guest") != "Linux":
            raise ToolchainError("Toolchain ist nicht für den Linux-Gast (guest: Linux)")
        guest = paths.toolchains / name
        if manifest.get("guestPath") != f"/opt/toolchains/{name}":
            raise ToolchainError(f"guestPath unzulässig: {manifest.get('guestPath')}")
        manifest["guestPath"] = str(guest)
        # Hashes im Gast erneut prüfen (zweite Prüfung nach dem Host, SEC-19).
        for rel, expected in pinned.get("files", {}).items():
            data = src.joinpath(*_safe_rel(rel).split("/")).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ToolchainError(f"Hash weicht ab: {rel}")
        work = paths.root / "work"
        home = work / "home"
        mapping = {"guest": str(guest), "source": str(src), "work": str(work), "results": str(work / "results"),
                   "selftest": str(work / f"selftest-{name}")}
        env = {"PATH": f"{guest}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home), "TMPDIR": str(home / "tmp"),
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "DOTNET_CLI_HOME": str(home)}
        (home / "tmp").mkdir(parents=True, exist_ok=True)
        for key, value in (manifest.get("environment") or {}).items():
            if key.upper() not in {"PATH", "LD_PRELOAD", "LD_AUDIT"}:
                env[key] = _expand(str(value), mapping)
        install = manifest["install"]
        timeout = (int(install.get("expectedMinutes", 5)) * 3 + 5) * 60
        kind = install.get("kind")
        if kind == "Archive":
            extract_archive(src.joinpath(*_safe_rel(install["file"]).split("/")), guest)
        elif kind == "Installer":
            deb = src.joinpath(*_safe_rel(install["file"]).split("/"))
            if deb.suffix != ".deb":
                raise ToolchainError("Linux-Installer muss ein .deb-Paket sein")
            result = run(["dpkg", "-i", str(deb)], timeout, env=env, log_file=log_file)
            if result.returncode != 0:
                raise ToolchainError(f"dpkg -i endete mit Exitcode {result.returncode}: {_tail(result)}")
            guest.mkdir(parents=True, exist_ok=True)
        elif kind == "None":
            guest.mkdir(parents=True, exist_ok=True)
        else:
            raise ToolchainError(f"Unbekannte Installationsart: {kind}")
        for item in manifest.get("deploy") or []:
            source = src.joinpath(*_safe_rel(item["source"]).split("/"))
            target = guest.joinpath(*_safe_rel(item["target"]).split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copyfile(source, target)
                shutil.copymode(source, target)
            else:
                raise ToolchainError(f"deploy: Quelle fehlt: {item['source']}")
        for command in manifest.get("postInstall") or []:
            result = _command(manifest, command, mapping, env, src, timeout, log_file)
            if result.returncode != 0:
                raise ToolchainError(f"Nachinstallation endete mit Exitcode {result.returncode}: {_tail(result)}")
        check = install["check"]
        result = _command(manifest, check, mapping, env, src, 600, log_file)
        output = result.stdout + result.stderr
        entry["check"] = (output.strip().splitlines() or [""])[0][:200]
        if result.returncode != 0:
            raise ToolchainError(f"Prüfbefehl endete mit Exitcode {result.returncode}: {_tail(result)}")
        if check.get("expectOutput") and check["expectOutput"] not in output:
            raise ToolchainError(f"Prüfbefehl liefert nicht '{check['expectOutput']}'")
        self_test = manifest.get("selfTest")
        if self_test:
            entry["selfTest"] = "fehlgeschlagen"
            directory = pathlib.Path(mapping["selftest"])
            if directory.exists():
                shutil.rmtree(directory)
            shutil.copytree(src.joinpath(*_safe_rel(self_test["directory"]).split("/")), directory)
            last = None
            for step in self_test["steps"]:
                last = _command(manifest, step, mapping, env, directory, 900, log_file)
                if last.returncode != 0:
                    raise ToolchainError(f"Selbsttest: Schritt {' '.join(step.get('arguments', []))} endete mit Exitcode "
                                         f"{last.returncode}: {_tail(last)}")
            if last is None or self_test["expectOutput"] not in last.stdout + last.stderr:
                raise ToolchainError(f"Selbsttest: Ausgabe '{self_test['expectOutput']}' fehlt")
            entry["selfTest"] = "ok"
        publish(name, src, manifest, paths)
        entry["installed"] = True
    except (ToolchainError, OSError, KeyError, ValueError, subprocess.TimeoutExpired, tarfile.TarError, zipfile.BadZipFile) as err:
        entry["error"] = str(err).replace("\n", " ")[:900]
    entry["seconds"] = int(time.monotonic() - started)
    return entry


def _tail(result: subprocess.CompletedProcess[str], limit: int = 600) -> str:
    lines = [ln.strip() for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]
    errors = [ln for ln in lines if any(w in ln.lower() for w in ("error", "fehler", "fail"))]
    text = " | ".join((errors or lines)[-4:])
    return text[-limit:]


def _command(manifest: dict[str, Any], command: dict[str, Any], mapping: dict[str, str], env: dict[str, str],
             cwd: pathlib.Path, timeout: int, log_file: pathlib.Path) -> subprocess.CompletedProcess[str]:
    exe = binary(manifest, command["binary"])
    args = [_expand(str(a), mapping) for a in command.get("arguments", [])]
    return run([str(exe), *args], timeout, env=env, cwd=cwd, log_file=log_file)


def publish(name: str, src: pathlib.Path, manifest: dict[str, Any], paths: Paths) -> None:
    """toolchain.json und Offline-Paketquelle nach /opt/localai/toolchains/<name> (Worker liest die Aktionen dort)."""
    dest = paths.published / name
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "packages").mkdir(parents=True)
    shutil.copyfile(src / "toolchain.json", dest / "toolchain.json")
    source = (manifest.get("packageSource") or {}).get("path")
    if source:
        packages = src.joinpath(*_safe_rel(source).split("/"))
        if packages.is_dir():
            shutil.copytree(packages, dest / "packages", dirs_exist_ok=True)


def make_read_only(*directories: pathlib.Path) -> None:
    """root gehört alles, niemand sonst darf schreiben (SEC-21)."""
    for directory in directories:
        if not directory.exists():
            continue
        for root, dirs, files in os.walk(directory):
            for name in [*dirs, *files]:
                path = os.path.join(root, name)
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode):
                    continue
                os.chown(path, 0, 0)
                os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o022 | (0o755 if stat.S_ISDIR(st.st_mode) else 0o444))
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)


# ------------------------------------------------------------------ 4 Worker und Dienste

def worker_command(config: dict[str, Any], paths: Paths = Paths()) -> list[str]:  # noqa: B008
    return [str(paths.python), "-I", "-m", "localai.worker",
            "--inbox", str(paths.root / "inbox"), "--work", str(paths.root / "work"),
            "--key-file", str(paths.root / "session.key"),
            "--transfer-disk", "auto", "--transfer-size", str(int(config["transferDiskBytes"])), "--require-ready",
            "--toolchains", str(paths.published), "--tools-user", TOOLS_USER, "--jobs-root", str(paths.jobs),
            "--osdebug", "--emu-user", EMU_USER]


def worker_unit(config: dict[str, Any], paths: Paths = Paths()) -> str:  # noqa: B008
    return "\n".join([
        "[Unit]",
        "Description=LocalAi Sandbox-Worker",
        "After=local-fs.target " + (LINK_UNIT if config.get("hypervisor") == "Qemu" else KVP_UNIT),
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(worker_command(config, paths)),
        # 75 = nach einem Gast-Update mit neuem Code neu starten; jeder andere Abbruch ebenfalls.
        "Restart=always",
        "RestartSec=2",
        f"StandardOutput=append:{paths.root / 'logs' / 'worker.log'}",
        "StandardError=inherit",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def write_worker_unit(config: dict[str, Any], paths: Paths = Paths(), reload: bool = True) -> pathlib.Path:  # noqa: B008
    if config.get("guest") != "Linux":
        raise ValueError("Worker-Einheit nur im Linux-Gast")
    target = paths.systemd / WORKER_UNIT
    target.write_text(worker_unit(config, paths), encoding="utf-8")
    if reload:
        run(["systemctl", "daemon-reload"])
    return target


def _find(*candidates: str) -> str | None:
    return next((c for c in candidates if pathlib.Path(c).is_file()), None)


def daemon_units(paths: Paths) -> list[str]:
    """Eigene Einheiten für KVP und Dateikopie – unabhängig davon, wie das Debian-Paket seine Dienste nennt."""
    problems = []
    kvp = _find("/usr/sbin/hv_kvp_daemon", "/usr/libexec/hypervkvpd")
    fcopy_uio = _find("/usr/sbin/hv_fcopy_uio_daemon")
    fcopy_old = _find("/usr/sbin/hv_fcopy_daemon", "/usr/libexec/hypervfcopyd")
    if kvp is None:
        problems.append("hv_kvp_daemon fehlt (Paket hyperv-daemons)")
    else:
        (paths.systemd / KVP_UNIT).write_text("\n".join([
            "[Unit]", "Description=LocalAi: Hyper-V-Datenaustausch (KVP)", "ConditionPathExists=/dev/vmbus/hv_kvp", "",
            "[Service]", f"ExecStart={kvp} -n", "Restart=always", "RestartSec=2", "",
            "[Install]", "WantedBy=multi-user.target", ""]), encoding="utf-8")
    if fcopy_uio is not None:
        (paths.systemd / FCOPY_UNIT).write_text("\n".join([
            "[Unit]", "Description=LocalAi: Hyper-V-Dateikopie (Copy-VMFile, uio)", "",
            "[Service]",
            "ExecStartPre=/sbin/modprobe uio_hv_generic",
            f"ExecStartPre=-/bin/sh -c 'echo {FCOPY_CLASS} > /sys/bus/vmbus/drivers/uio_hv_generic/new_id'",
            f"ExecStart={fcopy_uio} -n", "Restart=always", "RestartSec=2", "",
            "[Install]", "WantedBy=multi-user.target", ""]), encoding="utf-8")
    elif fcopy_old is not None:
        (paths.systemd / FCOPY_UNIT).write_text("\n".join([
            "[Unit]", "Description=LocalAi: Hyper-V-Dateikopie (Copy-VMFile)", "ConditionPathExists=/dev/vmbus/hv_fcopy", "",
            "[Service]", f"ExecStart={fcopy_old} -n", "Restart=always", "RestartSec=2", "",
            "[Install]", "WantedBy=multi-user.target", ""]), encoding="utf-8")
    else:
        problems.append("hv_fcopy_uio_daemon fehlt (Paket hyperv-daemons) – Copy-VMFile kann keine Jobs in die VM bringen")
    return problems


def link_unit(paths: Paths) -> str:
    """M10 (QEMU-Host): Verbindungskanal über virtio-serial statt hv_kvp_daemon/hv_fcopy."""
    return "\n".join([
        "[Unit]", "Description=LocalAi: Verbindungskanal zum Host (QEMU)",
        "ConditionPathExists=/dev/virtio-ports/org.localai.link", "",
        "[Service]", f"ExecStart={paths.python} -I -m localai.guest.qemulink", "Restart=always", "RestartSec=1", "",
        "[Install]", "WantedBy=multi-user.target", ""])


def start_link(paths: Paths) -> list[str]:
    (paths.systemd / LINK_UNIT).write_text(link_unit(paths), encoding="utf-8")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", LINK_UNIT])
    time.sleep(2)
    if run(["systemctl", "is-active", LINK_UNIT]).stdout.strip() != "active":
        status = run(["systemctl", "status", "--no-pager", "-n", "5", LINK_UNIT]).stdout.replace("\n", " ")[-400:]
        return [f"Verbindungskanal zum Host läuft nicht ({status})"]
    return []


def start_daemons(paths: Paths, config: dict[str, Any] | None = None) -> list[str]:
    if (config or {}).get("hypervisor") == "Qemu":
        return start_link(paths)
    # Gleichnamige Dienste des Debian-Pakets anhalten, damit nicht zwei Dienste dieselben Geräte öffnen.
    listed = run(["systemctl", "list-unit-files", "--no-legend", "--type=service"]).stdout
    for line in listed.splitlines():
        unit = line.split()[0] if line.split() else ""
        if ("kvp" in unit or "fcopy" in unit) and unit not in (KVP_UNIT, FCOPY_UNIT):
            run(["systemctl", "disable", "--now", unit])
    problems = daemon_units(paths)
    run(["systemctl", "daemon-reload"])
    for unit in (KVP_UNIT, FCOPY_UNIT):
        if (paths.systemd / unit).exists():
            run(["systemctl", "enable", "--now", unit])
    time.sleep(2)
    if (paths.systemd / FCOPY_UNIT).exists() and run(["systemctl", "is-active", FCOPY_UNIT]).stdout.strip() != "active":
        status = run(["systemctl", "status", "--no-pager", "-n", "5", FCOPY_UNIT]).stdout.replace("\n", " ")[-400:]
        problems.append(f"Dienst für Copy-VMFile läuft nicht ({status})")
    return problems


def quiet_services() -> None:
    for unit in QUIET_UNITS:
        run(["systemctl", "disable", "--now", unit])
    run(["systemctl", "mask", "systemd-networkd-wait-online.service"])


# ------------------------------------------------------------------ 5 Seed-Laufwerk

def retire_seed(seed: pathlib.Path) -> str | None:
    """Sitzungsschlüssel auf dem Seed-Laufwerk überschreiben, aushängen, Blöcke freigeben; cloud-init abschalten."""
    problem = None
    key = seed / "localai" / "session.key"
    source = next((line.split()[0] for line in pathlib.Path("/proc/mounts").read_text().splitlines()
                   if len(line.split()) > 1 and line.split()[1] == str(seed)), None)
    try:
        if source is not None:
            run(["mount", "-o", "remount,rw", str(seed)])
            if key.exists():
                size = key.stat().st_size
                with open(key, "r+b") as f:
                    f.write(b"\0" * size)
                    f.flush()
                    os.fsync(f.fileno())
                key.unlink()
            run(["umount", str(seed)])
            disk = source
            parent = pathlib.Path("/sys/class/block") / pathlib.Path(source).name / ".."
            if parent.exists() and (parent.resolve() / "dev").exists():
                disk = "/dev/" + parent.resolve().name
            run(["blkdiscard", "-f", disk])
    except OSError as err:
        problem = f"Seed-Laufwerk nicht bereinigt: {err}"
    pathlib.Path("/etc/cloud").mkdir(parents=True, exist_ok=True)
    pathlib.Path("/etc/cloud/cloud-init.disabled").write_text("LocalAi: Provisionierung abgeschlossen\n", encoding="utf-8")
    return problem


# ------------------------------------------------------------------ Ablauf

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LocalAi: Provisionierung des Linux-Gasts")
    parser.add_argument("--seed", type=pathlib.Path, default=pathlib.Path("/mnt/localai-seed"))
    args = parser.parse_args(argv)
    paths = Paths()
    report = Report()
    set_progress("start")
    for d in ("logs", "inbox", "work"):
        (paths.root / d).mkdir(parents=True, exist_ok=True)
    os.chmod(paths.root, 0o700)
    paths.jobs.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.jobs, 0o711)
    log_file = paths.root / "logs" / "toolchains.log"
    config: dict[str, Any] = {}
    try:
        config = load_config(paths.root / "guest-config.json")
        report.errors.extend(start_daemons(paths, config))
        quiet_services()

        set_progress("network")
        report.data["networkAdapters"] = count_adapters(paths.sys_net)
        report.data["probes"] = probes()

        ensure_tools_user()
        ensure_tools_user(EMU_USER)

        set_progress("toolchains")
        source_root = paths.root / "toolchain-src"
        paths.toolchains.mkdir(parents=True, exist_ok=True)
        paths.published.mkdir(parents=True, exist_ok=True)
        for name in config.get("toolchains") or []:
            log(f"Toolchain {name} wird installiert …")
            entry = install_toolchain(str(name), source_root, paths, log_file)
            log(f"Toolchain {name}: installiert={entry['installed']} Selbsttest={entry['selfTest']} {entry['error'] or ''}")
            report.data["toolchains"].append(entry)
            if entry["error"]:
                report.errors.append(f"Toolchain {name}: {entry['error']}")
        shutil.rmtree(source_root, ignore_errors=True)
        shutil.rmtree(paths.root / "work", ignore_errors=True)
        (paths.root / "work").mkdir()
        make_read_only(paths.toolchains, paths.opt)

        set_progress("worker")
        problem = retire_seed(args.seed)
        if problem:
            report.errors.append(problem)
        version = run([str(paths.python), "-I", "-c", "import sys, pydantic, localai.worker; print(sys.version.split()[0])"])
        if version.returncode != 0:
            raise RuntimeError(f"Python oder Worker im Gast nicht lauffähig: {_tail(version)}")
        report.data["worker"]["python"] = version.stdout.strip()
        write_worker_unit(config, paths)
        run(["systemctl", "enable", "--now", WORKER_UNIT])
        time.sleep(3)
        report.data["worker"]["running"] = run(["systemctl", "is-active", WORKER_UNIT]).stdout.strip() == "active"
    except Exception as err:  # noqa: BLE001 - jeder Fehler gehört in den Bericht
        report.errors.append(str(err).replace("\n", " ")[:900])

    set_progress("report")
    data = report.finish()
    (paths.root / "report.json").write_bytes(data)
    print(data.decode("utf-8"), flush=True)
    try:
        from localai.worker.transfer import Signal, TransferDisk, TransferKind
        size = int(config.get("transferDiskBytes") or 0)
        TransferDisk("auto", size).write(TransferKind.REPORT, 0, data)
        Signal(None).send("report:0")
        set_progress("report-sent")
        log("Bericht übergeben")
        return 0
    except Exception as err:  # noqa: BLE001
        log(f"Bericht konnte nicht übergeben werden: {err}")
        set_progress("report-failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
