"""Windows 控制台辅助：禁用 QuickEdit，以及静默启动时脱离黑窗口。"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
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


_SILENT_ENV = "VM_AGENT_SILENT"


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    return kernel32


def console_hwnd() -> int:
    """当前控制台窗口句柄；没有控制台时为 0。"""
    if sys.platform != "win32":
        return 0
    hwnd = _kernel32().GetConsoleWindow()
    return int(hwnd or 0)


def prepare_windowed_stdio() -> bool:
    """窗口子系统进程没有 stdout。

    普通启动接到空设备，避免误 print 崩溃。
    `--help` 时分配一个控制台，并返回 True，调用方在帮助信息后等按键。
    """
    if sys.platform != "win32":
        return False
    if sys.stdout is not None and sys.stderr is not None:
        return False
    argv = sys.argv[1:]
    wants_help = any(a in ("-h", "--help") for a in argv)
    if wants_help and attach_console():
        return True
    devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = devnull
    if sys.stderr is None:
        sys.stderr = devnull
    return False


def pause_console() -> None:
    """新建的帮助控制台在进程退出时会立刻消失，先等一次回车。"""
    try:
        print("按回车关闭此窗口…")
        input()
    except Exception:  # noqa: BLE001
        pass


def attach_console() -> bool:
    """没有控制台时分配一个（窗口子系统 exe 的 --console / --help）。"""
    if sys.platform != "win32":
        return False
    kernel32 = _kernel32()
    kernel32.AllocConsole.restype = ctypes.c_int
    kernel32.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
    kernel32.SetConsoleTitleW.restype = ctypes.c_int
    allocated = False
    if not kernel32.GetConsoleWindow():
        if not kernel32.AllocConsole():
            return False
        allocated = True
    kernel32.SetConsoleTitleW("VM Agent")
    if not allocated:
        return True
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except OSError:
        return bool(kernel32.GetConsoleWindow())
    return True


def stream_log_enabled() -> bool:
    """是否还要把日志打到当前 stderr。

    真实控制台和管道（IDE）要；静默子进程的空设备不要。
    """
    err = sys.stderr
    if err is None:
        return False
    try:
        if err.closed:
            return False
    except Exception:  # noqa: BLE001
        return False
    if sys.platform != "win32":
        return True
    if console_hwnd():
        return True
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(err.fileno())
    except Exception:  # noqa: BLE001
        return False
    kernel32 = _kernel32()
    kernel32.GetFileType.argtypes = [ctypes.c_void_p]
    kernel32.GetFileType.restype = ctypes.c_uint32
    # FILE_TYPE_PIPE = 3。NUL 和普通文件都不是管道。
    return kernel32.GetFileType(ctypes.c_void_p(handle)) == 3


def spawn_silent_and_exit() -> bool:
    """带着可见控制台启动时，改拉一个无窗口子进程并让当前进程退出。

    这样关掉原来的黑窗口不会结束 Agent（子进程已经脱离该控制台）。
    返回 True 表示当前进程应当马上结束。
    """
    if sys.platform != "win32":
        return False
    if os.environ.get(_SILENT_ENV) == "1":
        return False
    hwnd = console_hwnd()
    if not hwnd:
        return False
    explorer = _parent_is_explorer()
    if explorer:
        _show_console(hwnd, show=False)
    else:
        try:
            print("VM Agent 已静默启动，图标在任务栏通知区域。右键图标选择「退出」可结束进程。")
        except Exception:  # noqa: BLE001
            pass
    env = os.environ.copy()
    env[_SILENT_ENV] = "1"
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, *sys.argv[1:]]
    else:
        cmd = [sys.executable, *sys.argv]
    # CREATE_NO_WINDOW：子进程没有可关闭的控制台。
    # 不要加 DETACHED_PROCESS，那个标志会让 python.exe 再分配一个黑窗口。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    log_path = _agent_log_path()
    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        log_file = subprocess.DEVNULL
    try:
        subprocess.Popen(
            cmd,
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            creationflags=flags,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001
        if explorer:
            _show_console(hwnd, show=True)
        return False
    finally:
        if log_file is not subprocess.DEVNULL:
            log_file.close()
    return True


def release_console() -> bool:
    """摘掉当前控制台。进程继续跑，关掉黑窗口不会再结束它。"""
    if sys.platform != "win32":
        return False
    hwnd = console_hwnd()
    if not hwnd:
        return False
    kernel32 = _kernel32()
    kernel32.FreeConsole.restype = ctypes.c_int
    _show_console(hwnd, show=False)
    if not kernel32.FreeConsole():
        return False
    devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout = devnull
    sys.stderr = devnull
    return True


def _agent_log_path() -> str:
    if getattr(sys, "frozen", False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "agent.log")


def _parent_is_explorer() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        parent = psutil.Process().parent()
    except Exception:  # noqa: BLE001
        return False
    if parent is None:
        return False
    try:
        return parent.name().lower() == "explorer.exe"
    except Exception:  # noqa: BLE001
        return False


def _show_console(hwnd: int, *, show: bool) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.ShowWindow(ctypes.c_void_p(hwnd), 5 if show else 0)
