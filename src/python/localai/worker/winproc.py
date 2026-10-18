"""Prozessstart im Windows-Gast: eingeschränktes Token, niedrige Integrität, Job Object (SEC-21).

* ``CreateRestrictedToken(DISABLE_MAX_PRIVILEGE)`` entfernt alle Privilegien außer SeChangeNotify.
* Integritätsstufe *Low* (S-1-16-4096): Schreiben auf Objekte mittlerer/hoher Stufe ist verboten
  (Worker unter ``C:\\LocalAi``, Toolchains unter ``C:\\Toolchains``, Windows, Registry HKLM).
  Nur der Job-Ordner ist vorher als *Low* gekennzeichnet.
* Job Object mit ``KILL_ON_JOB_CLOSE``: nach dem Ende (oder bei Zeitüberschreitung) wird der ganze
  Prozessbaum beendet, auch Hintergrundprozesse wie MSBuild-Knoten.
* Keine Shell: ``CreateProcessAsUserW`` mit festem Programm und nach CommandLineToArgvW-Regeln gequoteter Liste.
"""

from __future__ import annotations

import ctypes
import msvcrt  # type: ignore[import-not-found]
import os
import subprocess
from ctypes import wintypes as wt

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
DISABLE_MAX_PRIVILEGE = 0x1
TOKEN_INTEGRITY_LEVEL = 25
SE_GROUP_INTEGRITY = 0x20
LOW_INTEGRITY_SID = "S-1-16-4096"

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount",
        "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _check(ok: object, what: str) -> None:
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"{what} fehlgeschlagen: {ctypes.FormatError(err).strip()}")  # type: ignore[attr-defined]


kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
kernel32.ResumeThread.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi32.CreateRestrictedToken.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
                                           wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.HANDLE)]
advapi32.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
advapi32.GetLengthSid.restype = wt.DWORD
advapi32.SetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
advapi32.CreateProcessAsUserW.argtypes = [
    wt.HANDLE, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD, ctypes.c_void_p,
    wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.CreateProcessW.argtypes = [
    wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD, ctypes.c_void_p,
    wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]


def _low_integrity_token() -> wt.HANDLE:
    process_token = wt.HANDLE()
    _check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT,
                                     ctypes.byref(process_token)), "OpenProcessToken")
    restricted = wt.HANDLE()
    try:
        _check(advapi32.CreateRestrictedToken(process_token, DISABLE_MAX_PRIVILEGE, 0, None, 0, None, 0, None,
                                              ctypes.byref(restricted)), "CreateRestrictedToken")
    finally:
        kernel32.CloseHandle(process_token)
    sid = ctypes.c_void_p()
    _check(advapi32.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)), "ConvertStringSidToSid")
    try:
        label = TOKEN_MANDATORY_LABEL(SID_AND_ATTRIBUTES(sid, SE_GROUP_INTEGRITY))
        size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
        _check(advapi32.SetTokenInformation(restricted, TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), size),
               "SetTokenInformation(IntegrityLevel)")
    except BaseException:
        kernel32.CloseHandle(restricted)
        raise
    finally:
        kernel32.LocalFree(sid)
    return restricted


def _job(memory_mb: int) -> wt.HANDLE:
    job = kernel32.CreateJobObjectW(None, None)
    _check(job, "CreateJobObject")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if memory_mb > 0:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = memory_mb * 1024 * 1024
    info.BasicLimitInformation.LimitFlags = flags
    _check(kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info),
                                            ctypes.sizeof(info)), "SetInformationJobObject")
    return job


def _environment_block(env: dict[str, str]) -> ctypes.Array:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    block = "".join(f"{k}={v}\0" for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(block, len(block))


def run(exe: str, args: list[str], cwd: str, env: dict[str, str], timeout: int, log_path: str,
        low_integrity: bool, memory_mb: int = 0) -> tuple[int, bool]:
    """Startet ``exe`` mit ``args``; stdout+stderr nach ``log_path``. Liefert (Exitcode, Zeitüberschreitung)."""
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline([exe, *args]))
    environment = _environment_block(env)
    token = _low_integrity_token() if low_integrity else None
    job = _job(memory_mb)
    pi = PROCESS_INFORMATION()
    with open(log_path, "wb") as log, open(os.devnull, "rb") as nul:
        out_handle = msvcrt.get_osfhandle(log.fileno())
        in_handle = msvcrt.get_osfhandle(nul.fileno())
        os.set_handle_inheritable(out_handle, True)
        os.set_handle_inheritable(in_handle, True)
        try:
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.dwFlags = STARTF_USESTDHANDLES
            si.hStdInput = in_handle
            si.hStdOutput = out_handle
            si.hStdError = out_handle
            flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
            if token is not None:
                ok = advapi32.CreateProcessAsUserW(token, exe, command, None, None, True, flags, environment, cwd,
                                                   ctypes.byref(si), ctypes.byref(pi))
            else:
                ok = kernel32.CreateProcessW(exe, command, None, None, True, flags, environment, cwd,
                                             ctypes.byref(si), ctypes.byref(pi))
            _check(ok, "CreateProcess")
        finally:
            os.set_handle_inheritable(out_handle, False)
            os.set_handle_inheritable(in_handle, False)
            if token is not None:
                kernel32.CloseHandle(token)
        try:
            _check(kernel32.AssignProcessToJobObject(job, pi.hProcess), "AssignProcessToJobObject")
            kernel32.ResumeThread(pi.hThread)
            waited = kernel32.WaitForSingleObject(pi.hProcess, min(timeout * 1000, INFINITE - 1))
            timed_out = waited == WAIT_TIMEOUT
            code = wt.DWORD()
            kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        finally:
            # Ganzen Baum beenden – auch Prozesse, die der Hauptprozess zurückgelassen hat.
            kernel32.TerminateJobObject(job, 1)
            kernel32.WaitForSingleObject(pi.hProcess, 10_000)
            for h in (pi.hThread, pi.hProcess, job):
                if h:
                    kernel32.CloseHandle(h)
    exit_code = -1 if timed_out else ctypes.c_int32(code.value).value
    return exit_code, timed_out
