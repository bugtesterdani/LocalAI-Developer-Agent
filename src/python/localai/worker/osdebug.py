"""Betriebssystem-Debugging mit QEMU in der Sandbox (M9).

Ein Agent bootet ein selbst geschriebenes Betriebssystem (ISO, Disketten-/Festplattenabbild oder Multiboot-Kernel)
in QEMU und bedient bzw. debuggt es: Haltepunkte, Einzelschritte, Register, Speicher, Disassemblierung,
Bildschirm (Screenshot und VGA-Textspeicher), Tastatur, Maus, serielle Ausgabe.

Sicherheit (zusätzlich zur VM ohne Netzwerkkarte):

* QEMU läuft mit reiner CPU-Emulation (TCG) oder – wenn ausdrücklich eingestellt und vorhanden – mit KVM in der VM,
  ohne Netzwerkgerät (``-nic none``), ohne Anzeige, mit seccomp (``-sandbox on``: keine neuen Prozesse, keine
  Rechteerweiterung), als eigener Benutzer ``localai-emu`` in einem leeren Netzwerk-Namespace mit ``no_new_privs``.
* Steuerung nur über Unix-Sockets im Sitzungsordner (QMP und GDB-Stub), nie über TCP.
* Es gibt keine freien Monitor-Befehle: jeder Befehl ist eine feste Vorlage mit geprüften Zahlen.
* Jede Sitzung endet spätestens nach ihrer freigegebenen Dauer (Wächter), mit ``OsDebug.Stop`` oder beim Neustart
  des Workers.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

MEDIA = {"Cdrom", "Disk", "Floppy", "Kernel"}
MACHINES = {"pc", "q35"}
FIRMWARE = {"Bios", "Uefi"}
ACCEL = {"Tcg", "Kvm"}
MAX_SERIAL = 64 * 1024
MAX_MEMORY_READ = 4096
MAX_SCREENSHOT = 4 * 1024 * 1024
MAX_STEPS = 1000
MAX_WAIT_MS = 600_000
MAX_KEYS = 200
OVMF_CANDIDATES = ("/usr/share/ovmf/OVMF.fd", "/usr/share/qemu/OVMF.fd", "/usr/share/OVMF/OVMF_CODE_4M.fd")
INFO_TOPICS = {"registers", "cpus", "mem", "tlb", "pic", "lapic", "irq", "mtree"}
_HEX = re.compile(r"^(0x)?[0-9a-fA-F]{1,16}$")
_QCODE = re.compile(r"^[a-z0-9_]{1,20}(-[a-z0-9_]{1,20}){0,3}$")
_CHAR_KEYS = {" ": "spc", "\n": "ret", "\t": "tab", "-": "minus", "=": "equal", ".": "dot", ",": "comma", "/": "slash",
              ";": "semicolon", "'": "apostrophe", "[": "bracket_left", "]": "bracket_right", "\\": "backslash", "`": "grave_accent"}
_SHIFTED = {"_": "minus", "+": "equal", ">": "dot", "<": "comma", "?": "slash", ":": "semicolon", '"': "apostrophe",
            "{": "bracket_left", "}": "bracket_right", "|": "backslash", "~": "grave_accent", "!": "1", "@": "2", "#": "3",
            "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}


class OsDebugError(Exception):
    """Befehl abgelehnt oder nicht ausführbar (Meldung geht an Agent und Nutzer)."""


# ------------------------------------------------------------------ Konfiguration

@dataclass(frozen=True)
class MachineConfig:
    image: str
    media: str = "Cdrom"
    machine: str = "pc"
    memory_mb: int = 128
    cpus: int = 1
    firmware: str = "Bios"
    accel: str = "Tcg"

    @classmethod
    def parse(cls, raw: Any) -> MachineConfig:
        if not isinstance(raw, dict):
            raise OsDebugError("config muss ein Objekt sein")
        allowed = {"image", "media", "machine", "memoryMB", "cpus", "firmware", "accel", "build"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise OsDebugError(f"config: unbekannte Felder {', '.join(unknown)}")
        image = raw.get("image")
        from localai.pathrules import PathRuleError, normalize_relative
        try:
            image = normalize_relative(str(image).replace("\\", "/"))
        except (PathRuleError, TypeError) as err:
            raise OsDebugError(f"config.image: unzulässiger Pfad ({err})") from err

        def pick(name: str, default: Any, allowed_values: set[str]) -> str:
            value = raw.get(name, default)
            if value not in allowed_values:
                raise OsDebugError(f"config.{name}: erlaubt sind {', '.join(sorted(allowed_values))}")
            return value

        def number(name: str, default: int, low: int, high: int) -> int:
            value = raw.get(name, default)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise OsDebugError(f"config.{name}: {low} bis {high}")
            return value

        return cls(image, pick("media", "Cdrom", MEDIA), pick("machine", "pc", MACHINES), number("memoryMB", 128, 16, 4096),
                   number("cpus", 1, 1, 4), pick("firmware", "Bios", FIRMWARE), pick("accel", "Tcg", ACCEL))


def parse_address(value: Any, name: str = "address") -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**64:
        return value
    if isinstance(value, str) and _HEX.match(value):
        return int(value, 16)
    raise OsDebugError(f"{name}: Hexadresse erwartet, z. B. 0x7c00")


def _int(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise OsDebugError(f"{name} muss zwischen {low} und {high} liegen")
    return value


def qemu_command(qemu: str, cfg: MachineConfig, image: pathlib.Path, session: pathlib.Path, kvm_available: bool,
                 sockets: pathlib.Path | None = None) -> list[str]:
    """Feste Befehlszeile – nichts davon kommt als freier Text vom Agenten."""
    sockets = sockets or session
    accel = "kvm" if cfg.accel == "Kvm" and kvm_available else "tcg,tb-size=64"
    args = [qemu, "-nodefaults", "-no-user-config", "-machine", cfg.machine, "-accel", accel,
            "-cpu", "host" if accel == "kvm" else "qemu64", "-m", str(cfg.memory_mb), "-smp", str(cfg.cpus),
            "-nic", "none", "-display", "none", "-vga", "std", "-S",
            "-chardev", f"file,id=ser0,path={session / 'serial.log'}", "-serial", "chardev:ser0",
            "-qmp", f"unix:{sockets / 'qmp.sock'},server=on,wait=off",
            "-gdb", f"unix:{sockets / 'gdb.sock'},server=on,wait=off",
            # Dreifachfehler: nicht neu starten, sondern anhalten – Register und Speicher bleiben prüfbar.
            "-action", "reboot=shutdown,shutdown=pause,panic=pause",
            "-d", "cpu_reset,guest_errors", "-D", str(session / "qemu-debug.log"),
            "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]
    if cfg.firmware == "Uefi":
        firmware = next((p for p in OVMF_CANDIDATES if pathlib.Path(p).is_file()), None)
        if firmware is None:
            raise OsDebugError("UEFI-Firmware (OVMF) ist in der Sandbox nicht installiert")
        args += ["-bios", firmware]
    if cfg.media == "Cdrom":
        args += ["-drive", f"file={image},media=cdrom,readonly=on,format=raw,if=ide,index=2", "-boot", "order=d"]
    elif cfg.media == "Disk":
        args += ["-drive", f"file={image},format=raw,if=ide,index=0", "-boot", "order=c"]
    elif cfg.media == "Floppy":
        args += ["-drive", f"file={image},format=raw,if=floppy,index=0", "-boot", "order=a"]
    else:
        args += ["-kernel", str(image)]
    return args


def cpu_mode(registers: str) -> int:
    """16/32/64 Bit aus „info registers“: Real Mode (CR0.PE = 0), sonst Kennung des Codesegments."""
    match = re.search(r"CR0=([0-9a-fA-F]+)", registers)
    if match and int(match.group(1), 16) & 1 == 0:
        return 16
    cs = re.search(r"^CS =.*$", registers, re.MULTILINE)
    if cs and "CS64" in cs.group(0):
        return 64
    if cs and "CS16" in cs.group(0):
        return 16
    return 32


def objdump(data: bytes, address: int, bits: int, count: int) -> str:
    tool = shutil.which("objdump")
    if tool is None:
        raise OsDebugError("Disassemblierung nicht möglich: weder QEMU mit Capstone noch objdump (binutils) vorhanden")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(data)
        f.flush()
        machine = {16: ["-m", "i8086"], 32: ["-m", "i386"], 64: ["-m", "i386:x86-64"]}[bits]
        run = subprocess.run([tool, "-D", "-b", "binary", *machine, f"--adjust-vma={address:#x}", f.name],
                             capture_output=True, text=True, timeout=30, check=False)
    lines = [ln for ln in run.stdout.splitlines() if re.match(r"^\s+[0-9a-f]+:\t", ln)]
    return "\n".join(ln.strip() for ln in lines[:count]) or run.stderr.strip()[:2000]


# ------------------------------------------------------------------ Steuerkanäle

class QmpClient:
    """QEMU Machine Protocol über einen Unix-Socket (JSON-Zeilen)."""

    def __init__(self, path: pathlib.Path, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(timeout)
                self.sock.connect(str(path))
                break
            except OSError:
                self.sock.close()
                if time.monotonic() > deadline:
                    raise OsDebugError("QEMU-Steuerkanal (QMP) nicht erreichbar") from None
                time.sleep(0.05)
        self.buffer = b""
        self.events: list[dict[str, Any]] = []
        self._read()  # Begrüßung
        self.execute("qmp_capabilities")

    def _read(self) -> dict[str, Any]:
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OsDebugError("QEMU hat den Steuerkanal geschlossen")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def execute(self, command: str, arguments: dict[str, Any] | None = None) -> Any:
        message: dict[str, Any] = {"execute": command}
        if arguments:
            message["arguments"] = arguments
        self.sock.sendall(json.dumps(message).encode() + b"\n")
        while True:
            reply = self._read()
            if "event" in reply:
                self.events.append(reply)
                continue
            if "error" in reply:
                raise OsDebugError(f"QEMU: {reply['error'].get('desc', 'Fehler')}")
            return reply.get("return")

    def hmp(self, command: str) -> str:
        return str(self.execute("human-monitor-command", {"command-line": command}))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class GdbClient:
    """Minimaler Client für das GDB-Remote-Protokoll (Haltepunkte, Einzelschritt, Fortsetzen, Anhalten)."""

    def __init__(self, path: pathlib.Path, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(str(path))
                break
            except OSError:
                self.sock.close()
                if time.monotonic() > deadline:
                    raise OsDebugError("GDB-Stub von QEMU nicht erreichbar") from None
                time.sleep(0.05)
        self.buffer = b""
        self.running = False

    def _recv(self, timeout: float | None) -> bytes | None:
        """Ein Paket lesen (mit Bestätigung). None = Zeitgrenze."""
        self.sock.settimeout(timeout)
        try:
            while True:
                start = self.buffer.find(b"$")
                end = self.buffer.find(b"#", start + 1) if start >= 0 else -1
                if start >= 0 and end >= 0 and len(self.buffer) >= end + 3:
                    payload = self.buffer[start + 1:end]
                    self.buffer = self.buffer[end + 3:]
                    self.sock.sendall(b"+")
                    return payload
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OsDebugError("GDB-Stub hat die Verbindung geschlossen")
                self.buffer += chunk
        except TimeoutError:
            return None

    def drain(self) -> str | None:
        """Wartende Stoppmeldung abholen (z. B. VM nach Dreifachfehler angehalten), damit sie nicht als Antwort gilt."""
        reply = self._recv(0.05)
        if reply is not None:
            self.running = False
            return reply.decode("ascii", errors="replace")
        return None

    def request(self, command: str, timeout: float = 10) -> str:
        if not self.running:
            self.drain()
        data = command.encode("ascii")
        packet = b"$" + data + b"#" + f"{sum(data) % 256:02x}".encode()
        self.sock.sendall(packet)
        reply = self._recv(timeout)
        if reply is None:
            raise OsDebugError(f"keine Antwort des GDB-Stubs auf {command[:10]}")
        return reply.decode("ascii", errors="replace")

    def wait_stop(self, timeout: float) -> str | None:
        reply = self._recv(timeout)
        if reply is None:
            return None
        self.running = False
        return reply.decode("ascii", errors="replace")

    def interrupt(self) -> str:
        if not self.running:
            return "S00"
        self.sock.sendall(b"\x03")
        reply = self.wait_stop(10)
        if reply is None:
            raise OsDebugError("Anhalten über den GDB-Stub fehlgeschlagen")
        return reply

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ Sitzung

@dataclass
class Session:
    session_id: str
    directory: pathlib.Path
    process: subprocess.Popen
    qmp: QmpClient
    gdb: GdbClient
    config: MachineConfig
    deadline: float
    allow_input: bool
    sockets: pathlib.Path | None = None  # eigener, kurzer Ordner für die Sockets (Pfadgrenze 108 Zeichen)
    serial_offset: int = 0
    commands: int = 0
    breakpoints: set[tuple[str, int]] = field(default_factory=set)
    watchdog: threading.Timer | None = None


def _stop_text(reply: str | None) -> str:
    if reply is None:
        return "läuft"
    if reply.startswith(("T05", "S05")):
        return "angehalten (Haltepunkt/Einzelschritt)"
    if reply.startswith(("T02", "S02")):
        return "angehalten"
    if reply.startswith(("T03", "S03", "W", "X")):
        return "beendet"
    return f"angehalten ({reply[:12]})"


class OsDebugManager:
    """Sitzungen eines Workers. Der Worker verarbeitet Jobs nacheinander – kein Lock nötig."""

    def __init__(self, root: pathlib.Path, emu_user: Any = None, qemu: str | None = None, executor: Any = None) -> None:
        self.root = root
        self.emu_user = emu_user
        self.qemu = qemu or shutil.which("qemu-system-x86_64") or "/usr/bin/qemu-system-x86_64"
        self.executor = executor
        self.sessions: dict[str, Session] = {}

    # -------------------------------------------------------------- Start/Stopp
    def start(self, session_id: str, args: dict[str, Any], index: Any, job_id: str) -> dict[str, Any]:
        self.reap()
        if self.sessions:
            raise OsDebugError("Es läuft bereits eine Debug-Sitzung – zuerst OsDebug.Stop")
        if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
            raise OsDebugError("Sitzungskennung ungültig")
        cfg = MachineConfig.parse(args.get("config"))
        minutes = _int(args, "maxMinutes", 30, 1, 480)
        halt = args.get("haltAtStart", True)
        allow_input = args.get("allowInput", False)
        if not isinstance(halt, bool) or not isinstance(allow_input, bool):
            raise OsDebugError("haltAtStart und allowInput müssen true oder false sein")
        if not pathlib.Path(self.qemu).is_file():
            raise OsDebugError("QEMU ist in dieser Sandbox nicht installiert (Linux-Bausteine mit -WithQemu laden, VM neu aufbauen)")
        directory = self.root / f"osdebug-{session_id}"
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        notes: list[str] = []
        build = (args.get("config") or {}).get("build")
        if build is not None:
            image_bytes, note = self._build(build, cfg.image, index, job_id)
            notes.append(note)
        else:
            if cfg.image not in index.files:
                raise OsDebugError(f"Abbild {cfg.image} gibt es im Snapshot nicht (oder config.build angeben)")
            image_bytes = index.resolve(cfg.image).read_bytes()
        image = directory / ("image.iso" if cfg.media == "Cdrom" else "image.bin")
        image.write_bytes(image_bytes)
        (directory / "serial.log").write_bytes(b"")
        # Unix-Sockets: Pfad höchstens 107 Zeichen – bei langen Arbeitsordnern ein kurzer Ordner unter /tmp.
        sockets = directory
        if len(str(directory / "qmp.sock")) > 100:
            import tempfile
            sockets = pathlib.Path(tempfile.mkdtemp(prefix="lai-dbg-", dir="/tmp"))
        if self.emu_user is not None:
            from localai.worker import linproc
            linproc.hand_over(directory, self.emu_user)
            if sockets != directory:
                linproc.hand_over(sockets, self.emu_user)
        kvm = os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)
        if cfg.accel == "Kvm" and not kvm:
            notes.append("KVM nicht verfügbar – reine CPU-Emulation (TCG)")
        command = qemu_command(self.qemu, cfg, image, directory, kvm, sockets)
        preexec = None
        if self.emu_user is not None:
            from localai.worker import linproc
            groups: tuple[int, ...] = ()
            if command[6] == "kvm":
                groups = (os.stat("/dev/kvm").st_gid,)  # nur die Gerätegruppe, sonst keine
            preexec = linproc.isolation(self.emu_user, 0, groups=groups)
        stderr = open(directory / "qemu-stderr.log", "wb")  # noqa: SIM115 - bleibt für die Laufzeit des Prozesses offen
        process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr,
                                   # preexec_fn: beim Start läuft keine andere Sitzung (also kein Wächter-Thread).
                                   start_new_session=True, preexec_fn=preexec,  # noqa: PLW1509
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        stderr.close()
        try:
            qmp = QmpClient(sockets / "qmp.sock")
            gdb = GdbClient(sockets / "gdb.sock")
        except OsDebugError:
            process.kill()
            process.wait()
            if sockets != directory:
                shutil.rmtree(sockets, ignore_errors=True)
            err = (directory / "qemu-stderr.log").read_text(errors="replace")[-1500:]
            raise OsDebugError(f"QEMU startet nicht: {err.strip() or 'ohne Meldung'}") from None
        session = Session(session_id, directory, process, qmp, gdb, cfg, time.monotonic() + minutes * 60, allow_input,
                          sockets=sockets if sockets != directory else None)
        session.watchdog = threading.Timer(minutes * 60, self._expire, args=(session_id,))
        session.watchdog.daemon = True
        session.watchdog.start()
        self.sessions[session_id] = session
        gdb.request("?")
        state = "angehalten vor dem ersten Befehl (BIOS-Einsprung)"
        if not halt:
            gdb.sock.sendall(b"$c#63")
            gdb.running = True
            state = "läuft"
        notes.append(f"QEMU {cfg.machine}, {cfg.memory_mb} MB, {cfg.cpus} CPU, {cfg.media}, {cfg.firmware}, "
                     f"{'KVM' if command[6] == 'kvm' else 'TCG'}; Sitzung höchstens {minutes} min")
        return {"session": session_id, "state": state, "notes": notes}

    def _build(self, build: Any, image: str, index: Any, job_id: str) -> tuple[bytes, str]:
        if self.executor is None:
            raise OsDebugError("config.build braucht Toolchains in der Sandbox")
        if not isinstance(build, dict) or set(build) - {"toolchain", "action", "parameters"}:
            raise OsDebugError("config.build: toolchain, action, parameters")
        tool = {"build": "Toolchain.Build", "run": "Toolchain.Run"}.get(build.get("action", "build"))
        if tool is None:
            raise OsDebugError("config.build.action: build oder run")
        captured: dict[str, bytes] = {}

        def keep(dirs: Any) -> None:
            path = dirs.repo.joinpath(*image.split("/"))
            if path.is_file() and not path.is_symlink():
                captured["image"] = path.read_bytes()

        result = self.executor.run(tool, f"{job_id}-build", {"toolchain": build.get("toolchain"),
                                                              "parameters": build.get("parameters") or {}}, index, keep=keep)
        if result.report.exit_code != 0 or result.report.timed_out:
            raise OsDebugError(f"Build fehlgeschlagen: {result.message}\n{result.log[-3000:]}")
        if "image" not in captured:
            raise OsDebugError(f"Build erfolgreich, aber {image} wurde nicht erzeugt")
        return captured["image"], f"Build: {result.message}"

    def stop(self, session_id: str) -> str:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return "Sitzung war bereits beendet"
        self._terminate(session)
        return f"Sitzung beendet nach {session.commands} Befehl(en)"

    def _expire(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            self._terminate(session)

    def _terminate(self, session: Session) -> None:
        if session.watchdog is not None:
            session.watchdog.cancel()
        for client in (session.gdb, session.qmp):
            client.close()
        try:
            os.killpg(session.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        session.process.wait(timeout=10)
        if self.emu_user is not None:
            from localai.worker import linproc
            linproc.kill_user_processes(self.emu_user.uid)
        shutil.rmtree(session.directory, ignore_errors=True)
        if session.sockets is not None:
            shutil.rmtree(session.sockets, ignore_errors=True)

    def reap(self) -> None:
        now = time.monotonic()
        for session_id, session in list(self.sessions.items()):
            if now > session.deadline or session.process.poll() is not None:
                self.sessions.pop(session_id, None)
                self._terminate(session)

    def stop_all(self) -> None:
        for session_id in list(self.sessions):
            self.stop(session_id)

    # -------------------------------------------------------------- Befehle
    def command(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None or session.process.poll() is not None or time.monotonic() > session.deadline:
            self.reap()
            raise OsDebugError("Keine laufende Debug-Sitzung mit dieser Kennung (abgelaufen oder beendet)")
        name = args.get("command")
        handler = getattr(self, f"_cmd_{str(name).replace('-', '_')}", None) if isinstance(name, str) and re.fullmatch(r"[a-z-]{1,20}", name) else None
        if handler is None:
            raise OsDebugError("command: continue, pause, step, break, registers, info, memory, disassemble, screenshot, "
                               "screen-text, keys, mouse, serial, reset, events")
        session.commands += 1
        result: dict[str, Any] = handler(session, args)
        result.setdefault("state", self._state(session))
        serial = self._serial(session, MAX_SERIAL if name == "serial" else 4096)
        if serial:
            result["serial"] = serial
        return result

    def _state(self, session: Session) -> str:
        if session.gdb.running:
            reply = session.gdb.drain()
            if reply is None:
                status = session.qmp.execute("query-status").get("status")
                if status == "running":
                    return "läuft"
                # Angehalten ohne Debugger (Dreifachfehler → shutdown=pause): Stoppmeldung kommt noch – abholen.
                reply = session.gdb.wait_stop(1)
            if reply is not None and not reply.startswith(("T05", "S05")):
                status = session.qmp.execute("query-status").get("status")
                if status == "shutdown":
                    return "angehalten nach Reset/Dreifachfehler"
            return _stop_text(reply)
        status = session.qmp.execute("query-status")
        text = {"debug": "angehalten (Debugger)", "paused": "angehalten", "shutdown": "angehalten nach Reset/Dreifachfehler",
                "prelaunch": "angehalten vor dem Start", "running": "läuft", "guest-panicked": "angehalten nach Panik"}
        return text.get(status.get("status"), str(status.get("status")))

    def _serial(self, session: Session, limit: int) -> str:
        path = session.directory / "serial.log"
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        if size <= session.serial_offset:
            return ""
        with open(path, "rb") as f:
            f.seek(session.serial_offset)
            data = f.read(limit)
        session.serial_offset += len(data)
        return data.decode("utf-8", errors="replace")

    def _ensure_stopped(self, session: Session) -> None:
        if session.gdb.running:
            session.gdb.interrupt()

    def _cmd_continue(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        wait = _int(args, "waitMs", 2000, 0, MAX_WAIT_MS)
        if not session.gdb.running:
            if session.breakpoints:
                # Steht die CPU auf einem Haltepunkt, träfe „continue“ ihn sofort wieder: erst einen Befehl weiter.
                reply = session.gdb.request("s", timeout=10)
                if not reply.startswith(("T05", "S05")):
                    return {"state": _stop_text(reply), "stopReply": reply}
            session.gdb.sock.sendall(b"$c#63")
            session.gdb.running = True
        reply = session.gdb.wait_stop(wait / 1000) if wait else None
        return {"state": _stop_text(reply) if reply else "läuft", "stopReply": reply or ""}

    def _cmd_pause(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        return {"state": _stop_text(session.gdb.interrupt())}

    def _cmd_step(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        count = _int(args, "count", 1, 1, MAX_STEPS)
        self._ensure_stopped(session)
        reply = ""
        for _ in range(count):
            reply = session.gdb.request("s", timeout=10)
            if not reply.startswith(("T05", "S05")):
                break
        return {"state": _stop_text(reply), "registers": self._registers(session)}

    def _cmd_break(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        address = parse_address(args.get("address"))
        kind = args.get("kind", "sw")
        remove = args.get("remove", False)
        if kind not in ("sw", "hw") or not isinstance(remove, bool):
            raise OsDebugError("kind: sw oder hw; remove: true/false")
        if not remove and len(session.breakpoints) >= 32:
            raise OsDebugError("höchstens 32 Haltepunkte")
        self._ensure_stopped(session)
        code = "0" if kind == "sw" else "1"
        reply = session.gdb.request(f"{'z' if remove else 'Z'}{code},{address:x},1")
        if reply != "OK":
            raise OsDebugError(f"Haltepunkt bei {address:#x} nicht gesetzt ({reply or 'nicht unterstützt'})")
        (session.breakpoints.discard if remove else session.breakpoints.add)((kind, address))
        listed = ", ".join(f"{a:#x} ({k})" for k, a in sorted(session.breakpoints, key=lambda b: b[1])) or "keine"
        return {"message": f"Haltepunkt {'entfernt' if remove else 'gesetzt'}: {address:#x}; aktiv: {listed}"}

    def _registers(self, session: Session) -> str:
        return session.qmp.hmp("info registers")[:20000]

    def _cmd_registers(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        return {"registers": self._registers(session)}

    def _cmd_info(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        topic = args.get("topic")
        if topic not in INFO_TOPICS:
            raise OsDebugError(f"topic: {', '.join(sorted(INFO_TOPICS))}")
        return {"info": session.qmp.hmp(f"info {topic}")[:30000]}

    def _read_memory(self, session: Session, address: int, length: int, physical: bool) -> bytes:
        target = session.directory / "mem.bin"
        target.unlink(missing_ok=True)
        if physical:
            session.qmp.execute("pmemsave", {"val": address, "size": length, "filename": str(target)})
        else:
            session.qmp.execute("memsave", {"val": address, "size": length, "filename": str(target), "cpu-index": 0})
        data = target.read_bytes()
        target.unlink(missing_ok=True)
        return data

    def _cmd_memory(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        address = parse_address(args.get("address"))
        length = _int(args, "length", 256, 1, MAX_MEMORY_READ)
        physical = args.get("physical", False)
        if not isinstance(physical, bool):
            raise OsDebugError("physical: true/false")
        data = self._read_memory(session, address, length, physical)
        lines = []
        for offset in range(0, len(data), 16):
            chunk = data[offset:offset + 16]
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{address + offset:016x}  {chunk.hex(' ')}  {text}")
        return {"memory": "\n".join(lines), "message": f"{len(data)} Bytes ab {address:#x} ({'physisch' if physical else 'virtuell'})"}

    def _cmd_disassemble(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        address = parse_address(args.get("address"))
        count = _int(args, "count", 16, 1, 64)
        mode = args.get("mode", "auto")
        if mode not in ("auto", 16, 32, 64):
            raise OsDebugError("mode: auto, 16, 32 oder 64")
        text = session.qmp.hmp(f"x /{count}i {address:#x}")
        if "not supported" not in text and text.strip():
            return {"disassembly": text[:20000]}
        # QEMU ohne Capstone: Bytes lesen und mit objdump (binutils) disassemblieren.
        bits = mode if mode != "auto" else cpu_mode(self._registers(session))
        data = self._read_memory(session, address, min(count * 15, MAX_MEMORY_READ), physical=False)
        return {"disassembly": objdump(data, address, bits, count), "mode": bits}

    def _cmd_screenshot(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        target = session.directory / "screen.png"
        target.unlink(missing_ok=True)
        session.qmp.execute("screendump", {"filename": str(target), "format": "png"})
        data = target.read_bytes()
        target.unlink(missing_ok=True)
        if len(data) > MAX_SCREENSHOT:
            raise OsDebugError("Screenshot zu groß")
        result = self._cmd_screen_text(session, args)
        result["screenshot"] = base64.b64encode(data).decode("ascii")
        return result

    def _cmd_screen_text(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        """VGA-Textmodus 80×25 direkt aus dem Bildspeicher (0xB8000) – für Modelle ohne Bildverständnis."""
        data = self._read_memory(session, 0xB8000, 80 * 25 * 2, physical=True)
        rows = []
        for row in range(25):
            line = data[row * 160:(row + 1) * 160:2].decode("cp437", errors="replace")
            rows.append(line.rstrip())
        while rows and not rows[-1]:
            rows.pop()
        return {"screenText": "\n".join(rows)}

    def _require_input(self, session: Session) -> None:
        if not session.allow_input:
            raise OsDebugError("Tastatur und Maus sind für diese Sitzung nicht freigegeben (allowInput)")

    def _cmd_keys(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        self._require_input(session)
        keys = args.get("keys")
        text = args.get("text")
        combos: list[list[str]] = []
        if isinstance(keys, list) and keys and len(keys) <= MAX_KEYS and all(isinstance(k, str) and _QCODE.match(k) for k in keys):
            combos = [k.split("-") for k in keys]
        elif isinstance(text, str) and 0 < len(text) <= MAX_KEYS:
            for ch in text:
                if ch.isascii() and ch.isalnum():
                    combos.append(["shift", ch.lower()] if ch.isupper() else [ch])
                elif ch in _CHAR_KEYS:
                    combos.append([_CHAR_KEYS[ch]])
                elif ch in _SHIFTED:
                    combos.append(["shift", _SHIFTED[ch]])
                else:
                    raise OsDebugError(f"Zeichen nicht auf der Tastatur abbildbar: {ch!r}")
        else:
            raise OsDebugError(f"keys (Liste von QEMU-Tastennamen wie \"ret\", \"ctrl-alt-delete\") oder text (höchstens {MAX_KEYS} Zeichen)")
        for combo in combos:
            session.qmp.execute("send-key", {"keys": [{"type": "qcode", "data": k} for k in combo], "hold-time": 50})
        wait = _int(args, "waitMs", 200, 0, 10_000)
        time.sleep(wait / 1000)
        return {"message": f"{len(combos)} Taste(n) gesendet"}

    def _cmd_mouse(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        self._require_input(session)
        dx = _int(args, "dx", 0, -2000, 2000)
        dy = _int(args, "dy", 0, -2000, 2000)
        events: list[dict[str, Any]] = []
        if dx or dy:
            events += [{"type": "rel", "data": {"axis": "x", "value": dx}}, {"type": "rel", "data": {"axis": "y", "value": dy}}]
        button = args.get("button")
        if button is not None:
            if button not in ("left", "right", "middle"):
                raise OsDebugError("button: left, right, middle")
            action = args.get("action", "click")
            if action not in ("click", "down", "up"):
                raise OsDebugError("action: click, down, up")
            if action in ("click", "down"):
                events.append({"type": "btn", "data": {"down": True, "button": button}})
            if action in ("click", "up"):
                events.append({"type": "btn", "data": {"down": False, "button": button}})
        if not events:
            raise OsDebugError("dx/dy oder button angeben")
        session.qmp.execute("input-send-event", {"events": events})
        time.sleep(_int(args, "waitMs", 200, 0, 10_000) / 1000)
        return {"message": f"Maus: dx={dx}, dy={dy}" + (f", {button}" if button else "")}

    def _cmd_serial(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _cmd_reset(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure_stopped(session)
        session.qmp.execute("system_reset")
        return {"message": "System zurückgesetzt (angehalten; continue startet)"}

    def _cmd_events(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        """CPU-Resets (z. B. Dreifachfehler) und Gastfehler aus dem QEMU-Protokoll, dazu QMP-Ereignisse."""
        log = (session.directory / "qemu-debug.log").read_bytes()[-20000:].decode("utf-8", errors="replace")
        events = [f"{e.get('event')} {json.dumps(e.get('data', {}), ensure_ascii=False)}" for e in session.qmp.events[-50:]]
        return {"events": "\n".join(events), "log": log}
