"""Windows 控制台辅助：禁用 QuickEdit，避免误点窗口冻结整个 Agent。"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

STD_INPUT_HANDLE = -10
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_INSERT_MODE = 0x0020
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def disable_quick_edit() -> bool:
    """关闭 QuickEdit / Insert 模式。

    Windows 控制台默认开启 QuickEdit：在窗口内单击会进入文本选择，
    整个进程（含 asyncio 事件循环）会暂停，直到右键或回车取消。
    远控指令会在恢复后集中执行，表现为「卡住后右键又好了」。
    """
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
        new_mode &= ~ENABLE_INSERT_MODE
        if not kernel32.SetConsoleMode(handle, new_mode):
            return False
        logger.info("已禁用控制台 QuickEdit（避免误点暂停 Agent）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("disable quick edit skipped: %s", exc)
        return False
