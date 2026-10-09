"""Keep a Windows background command's descendants in one owned Job Object.

Lazy-imported only on Windows. Closing the last handle kills descendants,
including those still running after the command's original parent exited.
This manages lifetime; it is not a filesystem/network sandbox.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w


class _Limits(c.Structure):
    _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                ("flags", w.DWORD), ("min_working", c.c_size_t), ("max_working", c.c_size_t),
                ("active_limit", w.DWORD), ("affinity", c.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class _IOCounters(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops",
                                                 "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(c.Structure):
    _fields_ = [("basic", _Limits), ("io", _IOCounters), ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t), ("peak_process_memory", c.c_size_t),
                ("peak_job_memory", c.c_size_t)]


class _Accounting(c.Structure):
    _fields_ = [(name, c.c_longlong) for name in ("user_time", "kernel_time", "period_user", "period_kernel")]
    _fields_ += [(name, w.DWORD) for name in ("page_faults", "total", "active", "terminated")]


class _ThreadEntry(c.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("thread_id", w.DWORD),
                ("owner_pid", w.DWORD), ("priority", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]


class WindowsJob:
    def __init__(self, popen):
        self.api = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, c.POINTER(_ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, c.POINTER(_ThreadEntry)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            if not self.api.AssignProcessToJobObject(self.handle, int(popen._handle)):
                raise c.WinError(c.get_last_error())
        except BaseException:
            self.close()
            raise

    def resume(self, pid: int) -> None:
        # Popen closes CreateProcess's thread handle. Find the sole suspended
        # initial thread using the documented Toolhelp API, then resume it.
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.size = c.sizeof(entry)
            more = self.api.Thread32First(snapshot, c.byref(entry))
            while more:
                if entry.owner_pid == pid:
                    thread = self.api.OpenThread(0x2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise c.WinError(c.get_last_error())
                    try:
                        if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise c.WinError(c.get_last_error())
                        return
                    finally:
                        self.api.CloseHandle(thread)
                more = self.api.Thread32Next(snapshot, c.byref(entry))
            raise OSError("Cannot find the background command's suspended thread")
        finally:
            self.api.CloseHandle(snapshot)

    def running(self) -> bool:
        if not self.handle:
            return False
        info = _Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, c.byref(info), c.sizeof(info), None):
            raise c.WinError(c.get_last_error())
        return bool(info.active)

    def terminate(self) -> None:
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise c.WinError(c.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
