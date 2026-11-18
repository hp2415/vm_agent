"""通知区域图标和可关闭收回的日志窗口。

Windows 点掉系统控制台会结束进程，拦不住。这里用自己的窗口代替控制台：
关掉只是隐藏，进程继续留在通知区域。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes

logger = logging.getLogger(__name__)

CLASS_NAME = "VmAgentSilentWindow"
WM_TRAY = 0x0400 + 20  # WM_USER + 20
WM_SHOW = 0x8000 + 1
WM_HIDE = 0x8000 + 2
WM_EXIT = 0x8000 + 3
WM_APPEND = 0x8000 + 4
WM_SHUTDOWN = 0x8000 + 5
ID_SHOW = 1
ID_HIDE = 2
ID_EXIT = 3
_ICON_ID = 1


class _QueueLogHandler(logging.Handler):
    def __init__(self, app: TrayApp) -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._app.enqueue(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class TrayApp:
    """托盘图标 + 日志窗口。窗口线程自己跑消息循环。"""

    def __init__(self, on_quit: Callable[[], None]) -> None:
        self._on_quit = on_quit
        self._queue: queue.Queue[str] = queue.Queue(maxsize=2000)
        self._handler = _QueueLogHandler(self)
        self._handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._closed = False
        self._icon_added = False
        self._icon_owned = False
        self._tray_hwnd = 0
        self._console = 0
        self._edit = 0
        self._hicon = 0
        self._font = 0
        self._brush = 0
        self._taskbar_created = 0
        self._title = "VM Agent（关闭窗口不退出）"
        self._tip = "VM Agent（单击显示控制台）"

    def start(self) -> None:
        if sys.platform != "win32":
            return
        _enable_dpi()
        self._thread = threading.Thread(
            target=self._thread_main, name="vm-agent-tray", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=8):
            raise TimeoutError("托盘窗口未能在 8 秒内创建")
        if self._error is not None:
            raise self._error
        root = logging.getLogger()
        if self._handler not in root.handlers:
            root.addHandler(self._handler)

    def shutdown(self) -> None:
        """去掉图标并结束消息循环。可重复调用，不会触发退出回调。"""
        self._remove_handler()
        thread = self._thread
        if thread is None:
            return
        if threading.current_thread() is thread:
            self._teardown_ui()
            self._post_quit()
            return
        hwnd = self._tray_hwnd
        if hwnd and not self._closed:
            try:
                import win32gui

                win32gui.PostMessage(hwnd, WM_SHUTDOWN, 0, 0)
            except Exception:  # noqa: BLE001
                self._remove_icon()
        thread.join(timeout=3)
        if thread.is_alive():
            self._remove_icon()

    def set_on_quit(self, on_quit: Callable[[], None]) -> None:
        self._on_quit = on_quit

    def set_title(self, title: str) -> None:
        self._title = title or self._title
        hwnd = self._console
        if not hwnd:
            return
        try:
            import win32gui

            win32gui.SetWindowText(hwnd, self._title)
        except Exception:  # noqa: BLE001
            pass

    def set_tip(self, tip: str) -> None:
        self._tip = (tip or self._tip)[:120]
        self._refresh_icon(modify=True)

    def show_console(self) -> None:
        self._send(WM_SHOW)

    def hide_console(self) -> None:
        self._send(WM_HIDE)

    def flush(self) -> None:
        """把已入队的日志写进窗口。测试和刚启动时用来避免只靠 PostMessage。"""
        self._send(WM_APPEND)

    def enqueue(self, line: str) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(line)
            except queue.Full:
                return
        hwnd = self._tray_hwnd
        if hwnd and not self._closed:
            try:
                import win32gui

                win32gui.PostMessage(hwnd, WM_APPEND, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def _send(self, msg: int) -> None:
        hwnd = self._tray_hwnd
        if not hwnd or self._closed:
            return
        try:
            import win32gui

            win32gui.SendMessage(hwnd, msg, 0, 0)
        except Exception:  # noqa: BLE001
            pass

    def _thread_main(self) -> None:
        try:
            self._create_ui()
        except Exception as exc:  # noqa: BLE001
            self._error = exc
            self._teardown_ui()
            self._ready.set()
            return
        self._ready.set()
        try:
            import win32gui

            win32gui.PumpMessages()
        finally:
            self._teardown_ui()

    def _create_ui(self) -> None:
        import win32api
        import win32con
        import win32gui

        hinst = win32api.GetModuleHandle(None)
        self._hicon, self._icon_owned = _load_icon()
        self._brush = win32gui.CreateSolidBrush(win32api.RGB(24, 24, 24))
        self._font = _console_font()
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = CLASS_NAME
        wc.lpfnWndProc = self._wndproc
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW + 1
        try:
            win32gui.RegisterClass(wc)
        except Exception:  # noqa: BLE001
            # 同一进程里重复启动时类已经注册过
            pass
        self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        self._tray_hwnd = int(
            win32gui.CreateWindow(
                CLASS_NAME,
                "VmAgentTray",
                win32con.WS_POPUP,
                0,
                0,
                0,
                0,
                0,
                0,
                hinst,
                None,
            )
        )
        self._console = int(
            win32gui.CreateWindow(
            CLASS_NAME,
            self._title,
            win32con.WS_OVERLAPPEDWINDOW | win32con.WS_CLIPCHILDREN,
            win32con.CW_USEDEFAULT,
            win32con.CW_USEDEFAULT,
            860,
            520,
            0,
            0,
            hinst,
            None,
        )
        )
        edit_style = (
            win32con.WS_CHILD
            | win32con.WS_VISIBLE
            | win32con.WS_VSCROLL
            | win32con.ES_MULTILINE
            | win32con.ES_AUTOVSCROLL
            | win32con.ES_READONLY
        )
        self._edit = int(
            win32gui.CreateWindow(
                "EDIT",
                "",
                edit_style,
                0,
                0,
                860,
                520,
                self._console,
                0,
                hinst,
                None,
            )
        )
        win32gui.SendMessage(self._edit, win32con.EM_SETLIMITTEXT, 400_000, 0)
        if self._font:
            win32gui.SendMessage(self._edit, win32con.WM_SETFONT, self._font, True)
        if self._hicon:
            win32gui.SendMessage(
                self._console, win32con.WM_SETICON, win32con.ICON_BIG, self._hicon
            )
            win32gui.SendMessage(
                self._console, win32con.WM_SETICON, win32con.ICON_SMALL, self._hicon
            )
        self._refresh_icon(modify=False)
        if not self._icon_added:
            raise RuntimeError("未能把 VM Agent 加到通知区域")

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            return self._dispatch(hwnd, msg, wparam, lparam)
        except Exception:  # noqa: BLE001
            logger.exception("托盘窗口消息处理失败")
            import win32gui

            return int(win32gui.DefWindowProc(hwnd, msg, wparam, lparam) or 0)

    def _dispatch(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32api
        import win32con
        import win32gui

        if msg == WM_SHUTDOWN:
            self._teardown_ui()
            self._post_quit()
            return 0
        if msg == WM_SHOW:
            self._show_console()
            return 0
        if msg == WM_HIDE:
            self._hide_console()
            return 0
        if msg == WM_EXIT:
            self._exit_app()
            return 0
        if msg == WM_APPEND:
            self._drain()
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            self._icon_added = False
            self._refresh_icon(modify=False)
            return 0
        if msg == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                self._show_console()
            elif event == win32con.WM_RBUTTONUP:
                self._popup_menu()
            return 0
        if hwnd == self._console and msg == win32con.WM_CLOSE:
            self._hide_console()
            return 0
        if hwnd == self._console and msg == win32con.WM_SIZE and self._edit:
            packed = int(lparam) & 0xFFFFFFFF
            width = packed & 0xFFFF
            height = (packed >> 16) & 0xFFFF
            win32gui.MoveWindow(self._edit, 0, 0, width, height, True)
            return 0
        if hwnd == self._console and msg == win32con.WM_CTLCOLOREDIT and self._brush:
            try:
                win32gui.SetTextColor(wparam, win32api.RGB(230, 230, 230))
                win32gui.SetBkColor(wparam, win32api.RGB(24, 24, 24))
            except Exception:  # noqa: BLE001
                pass
            return self._brush
        if msg == win32con.WM_COMMAND:
            cmd = int(wparam) & 0xFFFF
            if cmd == ID_SHOW:
                self._show_console()
            elif cmd == ID_HIDE:
                self._hide_console()
            elif cmd == ID_EXIT:
                self._exit_app()
            return 0
        return int(win32gui.DefWindowProc(hwnd, msg, wparam, lparam) or 0)

    def _show_console(self) -> None:
        import win32con
        import win32gui

        hwnd = self._console
        if not hwnd:
            return
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        _force_foreground(hwnd)

    def _hide_console(self) -> None:
        import win32con
        import win32gui

        hwnd = self._console
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

    def _popup_menu(self) -> None:
        import win32con
        import win32gui

        menu = win32gui.CreatePopupMenu()
        cmd = 0
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_SHOW, "显示控制台")
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_HIDE, "隐藏控制台")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_EXIT, "退出")
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self._tray_hwnd)
            cmd = win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN
                | win32con.TPM_BOTTOMALIGN
                | win32con.TPM_RIGHTBUTTON
                | win32con.TPM_RETURNCMD
                | win32con.TPM_NONOTIFY,
                x,
                y,
                0,
                self._tray_hwnd,
                None,
            )
            win32gui.PostMessage(self._tray_hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)
        if cmd == ID_SHOW:
            self._show_console()
        elif cmd == ID_HIDE:
            self._hide_console()
        elif cmd == ID_EXIT:
            self._exit_app()

    def _exit_app(self) -> None:
        cb = self._on_quit
        self._on_quit = None
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.exception("退出回调失败")
        self.shutdown()

    def _drain(self) -> None:
        if not self._edit or self._closed:
            return
        import win32con
        import win32gui

        lines: list[str] = []
        while True:
            try:
                lines.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        length = int(win32gui.GetWindowTextLength(self._edit) or 0)
        if length > 300_000:
            win32gui.SendMessage(self._edit, win32con.EM_SETSEL, 0, 100_000)
            win32gui.SendMessage(self._edit, win32con.EM_REPLACESEL, 0, "")
            length = int(win32gui.GetWindowTextLength(self._edit) or 0)
        text = "\r\n".join(lines) + "\r\n"
        win32gui.SendMessage(self._edit, win32con.EM_SETSEL, length, length)
        win32gui.SendMessage(self._edit, win32con.EM_REPLACESEL, 0, text)
        win32gui.SendMessage(self._edit, win32con.WM_VSCROLL, win32con.SB_BOTTOM, 0)

    def _refresh_icon(self, *, modify: bool) -> None:
        if self._closed or not self._tray_hwnd or not self._hicon:
            return
        import win32gui

        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self._tray_hwnd, _ICON_ID, flags, WM_TRAY, self._hicon, self._tip)
        try:
            if modify and self._icon_added:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
            elif not self._icon_added:
                ok = win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
                # pywin32 成功时可能返回 True，也可能不返回值。
                self._icon_added = ok is None or bool(ok)
        except Exception:  # noqa: BLE001
            logger.exception("更新通知区域图标失败")

    def _remove_icon(self) -> None:
        hwnd = self._tray_hwnd
        if not self._icon_added or not hwnd:
            self._icon_added = False
            return
        self._icon_added = False
        try:
            import win32gui

            win32gui.Shell_NotifyIcon(
                win32gui.NIM_DELETE,
                (hwnd, _ICON_ID, 0, WM_TRAY, self._hicon, self._tip),
            )
        except Exception:  # noqa: BLE001
            pass

    def _remove_handler(self) -> None:
        root = logging.getLogger()
        if self._handler in root.handlers:
            root.removeHandler(self._handler)

    def _teardown_ui(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove_icon()
        import win32gui

        console = self._console
        tray = self._tray_hwnd
        self._console = 0
        self._edit = 0
        self._tray_hwnd = 0
        for hwnd in (console, tray):
            if not hwnd:
                continue
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        if self._font:
            try:
                win32gui.DeleteObject(self._font)
            except Exception:  # noqa: BLE001
                pass
            self._font = 0
        if self._brush:
            try:
                win32gui.DeleteObject(self._brush)
            except Exception:  # noqa: BLE001
                pass
            self._brush = 0
        if self._hicon and self._icon_owned:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.DestroyIcon.argtypes = [wintypes.HICON]
                user32.DestroyIcon.restype = wintypes.BOOL
                user32.DestroyIcon(self._hicon)
            except Exception:  # noqa: BLE001
                pass
        self._hicon = 0

    @staticmethod
    def _post_quit() -> None:
        try:
            import win32gui

            win32gui.PostQuitMessage(0)
        except Exception:  # noqa: BLE001
            pass


def _enable_dpi() -> None:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _console_font() -> int:
    import win32con
    import win32gui

    lf = win32gui.LOGFONT()
    lf.lfHeight = -16
    lf.lfWeight = 400
    lf.lfFaceName = "Consolas"
    lf.lfQuality = getattr(win32con, "CLEARTYPE_QUALITY", 5)
    try:
        return int(win32gui.CreateFontIndirect(lf) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _force_foreground(hwnd: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_int
    user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
    user32.AttachThreadInput.restype = ctypes.c_int
    kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    our = kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != our:
        attached = bool(user32.AttachThreadInput(fg_tid, our, True))
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    if attached:
        user32.AttachThreadInput(fg_tid, our, False)


def _load_icon() -> tuple[int, bool]:
    """返回 (HICON, 是否需要 DestroyIcon)。失败时退回系统图标。"""
    try:
        handle = _make_icon(32)
        if handle:
            return handle, True
    except Exception:  # noqa: BLE001
        logger.debug("自定义托盘图标失败", exc_info=True)
    import win32con
    import win32gui

    shared = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
    return int(shared or 0), False


def _make_icon(size: int) -> int:
    pixels = _icon_pixels(size)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = size
    header.biHeight = size
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0
    header.biSizeImage = size * size * 4

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateBitmap.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    gdi32.CreateBitmap.restype = wintypes.HBITMAP
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    user32.CreateIconIndirect.restype = wintypes.HICON

    hdc = user32.GetDC(0)
    bits = ctypes.c_void_p()
    color = gdi32.CreateDIBSection(
        hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0
    )
    user32.ReleaseDC(0, hdc)
    if not color or not bits.value:
        if color:
            gdi32.DeleteObject(color)
        return 0
    ctypes.memmove(bits, pixels, len(pixels))
    stride = ((size + 31) // 32) * 4
    mask_buf = (ctypes.c_ubyte * (stride * size))()
    mask = gdi32.CreateBitmap(
        size, size, 1, 1, ctypes.cast(mask_buf, ctypes.c_void_p)
    )
    info = ICONINFO()
    info.fIcon = True
    info.xHotspot = 0
    info.yHotspot = 0
    info.hbmMask = mask
    info.hbmColor = color
    icon = user32.CreateIconIndirect(ctypes.byref(info))
    if color:
        gdi32.DeleteObject(color)
    if mask:
        gdi32.DeleteObject(mask)
    return int(icon or 0)


def _icon_pixels(size: int) -> bytes:
    """自下而上的 32 位 BGRA。蓝底白 V，缩到通知区域仍能认出。"""
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            row += _icon_pixel(x, y, size)
        rows.append(bytes(row))
    return b"".join(reversed(rows))


def _icon_pixel(x: int, y: int, size: int) -> bytes:
    edge = x == 0 or y == 0 or x == size - 1 or y == size - 1
    if edge:
        blue = (160, 70, 0, 255)
    else:
        blue = (215, 120, 0, 255)  # BGRA of RGB(0, 120, 215)
    px = x + 0.5
    py = y + 0.5
    left = _dist_seg(px, py, size * 0.28, size * 0.30, size * 0.50, size * 0.72)
    right = _dist_seg(px, py, size * 0.50, size * 0.72, size * 0.72, size * 0.30)
    if min(left, right) <= size * 0.07:
        return bytes((255, 255, 255, 255))
    return bytes(blue)


def _dist_seg(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x2 - x1
    dy = y2 - y1
    denom = dx * dx + dy * dy
    if denom <= 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / denom))
    qx = x1 + t * dx
    qy = y1 + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5
