"""跨进程键鼠租约。

vm_agent 接管期间持有 ``Local\\vm_input_owner``，desktop 外发与本进程外发在写入前抢同一把锁。
抢不到就放弃，不把点击排到对方结束再回放。释放前抬起修饰键，避免 Alt/Ctrl 粘住后把双击变成「属性」。

本文件与 ``work_place/desktop/rpa/wxrpa/input_lease.py`` 保持同一逻辑。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from contextlib import contextmanager
from ctypes import wintypes
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

MUTEX_NAME = "Local\\vm_input_owner"
BUSY_ERROR = "键鼠正被占用，已拒绝外发（远程接管或另一进程正在操作）"

WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x00000080
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

# 左/右修饰键，以及 keybd_event(VK_MENU/VK_CONTROL) 用的通用码
_MODIFIERS: tuple[tuple[int, bool], ...] = (
    (0xA2, False),  # VK_LCONTROL
    (0xA3, True),  # VK_RCONTROL
    (0xA4, False),  # VK_LMENU
    (0xA5, True),  # VK_RMENU
    (0xA0, False),  # VK_LSHIFT
    (0xA1, False),  # VK_RSHIFT
    (0x5B, True),  # VK_LWIN
    (0x5C, True),  # VK_RWIN
    (0x11, False),  # VK_CONTROL
    (0x12, False),  # VK_MENU
    (0x10, False),  # VK_SHIFT
)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
_kernel32.ReleaseMutex.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_depth = threading.local()


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT


def _create_mutex() -> int:
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        logger.warning("CreateMutexW 失败: %s", ctypes.get_last_error())
    return int(handle or 0)


def release_modifiers() -> None:
    """抬起可能粘住的 Ctrl / Alt / Shift / Win。键没按下时多一次抬起无效果。"""
    try:
        for vk, extended in _MODIFIERS:
            inp = _INPUT()
            inp.type = INPUT_KEYBOARD
            flags = KEYEVENTF_KEYUP
            if extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            inp.union.ki = _KEYBDINPUT(vk, 0, flags, 0, None)
            _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    except Exception as exc:  # noqa: BLE001
        logger.warning("抬起修饰键失败: %s", exc)


def others_hold_input() -> bool:
    """别的线程或进程正持有租约。本线程已持有时返回 False。"""
    if getattr(_depth, "n", 0) > 0:
        return False
    handle = _create_mutex()
    if not handle:
        return False
    result = _kernel32.WaitForSingleObject(handle, 0)
    if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
        if result == WAIT_ABANDONED:
            release_modifiers()
        _kernel32.ReleaseMutex(handle)
        _kernel32.CloseHandle(handle)
        return False
    _kernel32.CloseHandle(handle)
    return True


@contextmanager
def try_input_owner() -> Iterator[bool]:
    """非阻塞抢租约。抢到才写入键鼠；退出时先抬修饰键再解锁。同线程可重入。"""
    if getattr(_depth, "n", 0) > 0:
        yield True
        return
    handle = _create_mutex()
    if not handle:
        yield False
        return
    result = _kernel32.WaitForSingleObject(handle, 0)
    if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
        _kernel32.CloseHandle(handle)
        yield False
        return
    _depth.n = 1
    try:
        if result == WAIT_ABANDONED:
            release_modifiers()
        yield True
    finally:
        _depth.n = 0
        try:
            release_modifiers()
        finally:
            _kernel32.ReleaseMutex(handle)
            _kernel32.CloseHandle(handle)


class ControlLease:
    """接管会话在独立线程上握住租约，直到 stop。抢不到时持续重试，期间 held 为 False。"""

    def __init__(self, on_held: Callable[[bool], None] | None = None) -> None:
        self._thread: threading.Thread | None = None
        self._release = threading.Event()
        self._held = False
        self._got = False
        self._mu = threading.Lock()
        self._on_held = on_held

    @property
    def held(self) -> bool:
        return self._held

    def start(self) -> None:
        with self._mu:
            if self._thread is not None and self._thread.is_alive():
                return
            self._release.clear()
            self._held = False
            self._got = False
            self._thread = threading.Thread(
                target=self._run, name="vm-input-lease", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """结束持有。本会话抢到过租约时再抬一次修饰键。"""
        self._release.set()
        with self._mu:
            thread = self._thread
            self._thread = None
            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=1.5)
            got = self._got
            self._got = False
        self._publish(False)
        if got:
            release_modifiers()

    def _publish(self, held: bool) -> None:
        if self._held == held:
            return
        self._held = held
        cb = self._on_held
        if cb is None:
            return
        try:
            cb(held)
        except Exception:  # noqa: BLE001
            logger.exception("键鼠租约回调失败")

    def _run(self) -> None:
        handle = _create_mutex()
        if not handle:
            logger.warning("无法创建键鼠租约")
            return
        try:
            while not self._release.is_set():
                result = _kernel32.WaitForSingleObject(handle, 200)
                if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                    continue
                release_modifiers()
                self._got = True
                self._publish(True)
                logger.info("远控已取得键鼠租约")
                self._release.wait()
                release_modifiers()
                _kernel32.ReleaseMutex(handle)
                self._publish(False)
                logger.info("远控已放开键鼠租约")
                return
        finally:
            _kernel32.CloseHandle(handle)

