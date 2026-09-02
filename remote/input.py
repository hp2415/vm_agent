"""键鼠注入（SendInput），坐标为桌面归一化 0–1。"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


def _screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _abs_coords(nx: float, ny: float) -> tuple[int, int]:
    w, h = _screen_size()
    nx = max(0.0, min(1.0, float(nx)))
    ny = max(0.0, min(1.0, float(ny)))
    # SendInput 绝对坐标 0..65535
    return int(nx * 65535), int(ny * 65535)


def _send_mouse(flags: int, nx: float, ny: float, data: int = 0) -> None:
    ax, ay = _abs_coords(nx, ny)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(ax, ay, data, flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _send_key(vk: int, up: bool = False) -> None:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    flags = KEYEVENTF_KEYUP if up else 0
    inp.union.ki = KEYBDINPUT(vk & 0xFFFF, 0, flags, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# Qt key → 常见 VK 粗映射（native 优先）
_QT_TO_VK = {
    16777216: 0x1B,  # Escape
    16777219: 0x08,  # Backspace
    16777220: 0x0D,  # Return
    16777221: 0x0D,  # Enter
    16777217: 0x09,  # Tab
    16777234: 0x25,  # Left
    16777235: 0x26,  # Up
    16777236: 0x27,  # Right
    16777237: 0x28,  # Down
    16777223: 0x2E,  # Delete
    16777248: 0x10,  # Shift
    16777249: 0x11,  # Ctrl
    16777251: 0x12,  # Alt
}


def handle_input(payload: dict[str, Any]) -> None:
    action = payload.get("action") or ""
    try:
        if action == "mouse_move":
            _send_mouse(0, float(payload.get("x", 0)), float(payload.get("y", 0)))
        elif action == "mouse_down":
            btn = payload.get("button") or "left"
            flags = {
                "left": MOUSEEVENTF_LEFTDOWN,
                "right": MOUSEEVENTF_RIGHTDOWN,
                "middle": MOUSEEVENTF_MIDDLEDOWN,
            }.get(btn, MOUSEEVENTF_LEFTDOWN)
            _send_mouse(flags, float(payload.get("x", 0)), float(payload.get("y", 0)))
        elif action == "mouse_up":
            btn = payload.get("button") or "left"
            flags = {
                "left": MOUSEEVENTF_LEFTUP,
                "right": MOUSEEVENTF_RIGHTUP,
                "middle": MOUSEEVENTF_MIDDLEUP,
            }.get(btn, MOUSEEVENTF_LEFTUP)
            _send_mouse(flags, float(payload.get("x", 0)), float(payload.get("y", 0)))
        elif action == "mouse_wheel":
            delta = int(payload.get("delta") or 0)
            # Qt angleDelta 通常为 120 的倍数
            _send_mouse(
                MOUSEEVENTF_WHEEL,
                float(payload.get("x", 0)),
                float(payload.get("y", 0)),
                data=delta,
            )
        elif action in ("key_down", "key_up"):
            native = int(payload.get("native") or 0)
            qt_key = int(payload.get("key") or 0)
            vk = native or _QT_TO_VK.get(qt_key, 0)
            text = payload.get("text") or ""
            if not vk and text:
                vk = ord(text[0].upper())
            if vk:
                _send_key(vk, up=(action == "key_up"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("input failed: %s", exc)
