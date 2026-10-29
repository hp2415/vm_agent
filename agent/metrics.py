"""访客机资源采样和本机健康日志。不上传每分钟那一行。"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_KEEP = timedelta(days=7)


class ResourceSampler:
    """用相邻两次系统时间算 CPU。第一次没有百分比。"""

    def __init__(self) -> None:
        self._prev: tuple[int, int, int] | None = None

    def take(self) -> dict[str, Any]:
        mem_used, mem_total = _memory()
        sample: dict[str, Any] = {
            "cpu_percent": None,
            "mem_used_mb": mem_used,
            "mem_total_mb": mem_total,
            "disks": _disks(),
        }
        cur = _cpu_times()
        prev = self._prev
        self._prev = cur
        if prev is not None and cur is not None:
            sample["cpu_percent"] = _cpu_percent(prev, cur)
        return sample


class HealthLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def line(self, text: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {text}\n")
            self._prune()
        except OSError as exc:
            logger.warning("写健康日志失败 %s: %s", self.path, exc)

    def _prune(self) -> None:
        try:
            if self.path.stat().st_size < 256 * 1024:
                return
            cutoff = datetime.now(timezone.utc) - _KEEP
            kept: list[str] = []
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    when = datetime.strptime(raw[:20], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                if when >= cutoff:
                    kept.append(raw)
            body = "\n".join(kept[-4000:])
            self.path.write_text(body + ("\n" if body else ""), encoding="utf-8")
        except OSError:
            return


def sample_line(vm_id: str, sample: dict[str, Any]) -> str:
    cpu = sample.get("cpu_percent")
    cpu_txt = "—" if cpu is None else f"{cpu}"
    disks = sample.get("disks") or []
    disk_txt = ",".join(
        f"{row.get('mount')} {row.get('free_mb')}/{row.get('total_mb')}"
        for row in disks
        if isinstance(row, dict)
    )
    return (
        f"vm={vm_id} cpu={cpu_txt} "
        f"mem={sample.get('mem_used_mb')}/{sample.get('mem_total_mb')} "
        f"disk={disk_txt or '-'}"
    )


def _cpu_times() -> tuple[int, int, int] | None:
    if sys.platform != "win32":
        return None
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    ok = ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    )
    if not ok:
        return None
    return (_filetime(idle), _filetime(kernel), _filetime(user))


def _cpu_percent(prev: tuple[int, int, int], cur: tuple[int, int, int]) -> float:
    total = (cur[1] - prev[1]) + (cur[2] - prev[2])
    idle = cur[0] - prev[0]
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (total - idle) * 100.0 / total)), 1)


def _memory() -> tuple[int, int]:
    if sys.platform != "win32":
        return 0, 0
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return 0, 0
    total = int(stat.ullTotalPhys // (1024 * 1024))
    avail = int(stat.ullAvailPhys // (1024 * 1024))
    return max(0, total - avail), total


def _disks() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []
    kernel = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(512)
    length = kernel.GetLogicalDriveStringsW(len(buf), buf)
    if length <= 0:
        return []
    rows: list[dict[str, Any]] = []
    for drive in buf[:length].split("\x00"):
        if not drive:
            continue
        free = ctypes.c_ulonglong()
        total = ctypes.c_ulonglong()
        ok = kernel.GetDiskFreeSpaceExW(drive, None, ctypes.byref(total), ctypes.byref(free))
        if not ok or total.value <= 0:
            continue
        rows.append(
            {
                "mount": drive.rstrip("\\"),
                "free_mb": int(free.value // (1024 * 1024)),
                "total_mb": int(total.value // (1024 * 1024)),
            }
        )
    return rows


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _filetime(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) + int(value.dwLowDateTime)
