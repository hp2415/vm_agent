import os
import re
import time
import atexit
import ctypes
import threading
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

import psutil
import win32clipboard
import uiautomation as auto
from loguru import logger

from remote.input_lease import BUSY_ERROR, others_hold_input, try_input_owner


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    receiver_used: str | None = None
    receiver_source: str | None = None


StepCallback = Callable[[str, str], None]
ConfirmCallback = Callable[[str], bool]

_SOURCE_LABELS = {
    "remark": "备注",
    "name": "昵称",
    "wxid": "微信号",
    "phone": "手机",
}

# 发送结果校验：微信在网络较慢时消息可能延迟出现在列表中
SEND_VERIFY_TIMEOUT_S = 40
SEND_VERIFY_POLL_S = 0.6
SEND_VERIFY_STABLE_POLLS = 2
SEND_VERIFY_FINAL_CHECK_S = 0.6
SEND_POST_ENTER_GRACE_S = 0.8
SEND_VERIFY_READ_RETRIES = 3
SEND_VERIFY_READ_GAP_S = 0.12

# 窗口激活 / 搜索节奏（在保证稳定前提下尽量缩短准备阶段）
ACTIVATE_SETTLE_S = 0.12
ACTIVATE_COOLDOWN_S = 12.0
SEARCH_LIST_WAIT_S = 0.55
VERIFY_CHAT_RETRY_S = 0.2
VERIFY_CHAT_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Win32 直查微信窗口 —— 关键的性能 / 防卡死优化
#
# uiautomation 的 ``WindowControl(searchDepth=1, ...).Exists(0)`` 会枚举所有
# 顶层窗口，并通过 UIA / SendMessage 跨进程同步查询每个窗口的属性（ClassName
# 等）。当桌面端自己的 UI 线程在做较重的工作（例如新版 Markdown 气泡渲染）
# 时，UIA 查询会被 ``SendMessage`` 卡住——日志里曾经看到单次 ``Exists(0)``
# 卡了 61 秒。
#
# 这里改用 Win32 ``FindWindowW`` / ``EnumWindows``：它们直接读窗口管理器的
# 内核数据，不会向目标进程发送同步消息，毫秒级返回。
# ---------------------------------------------------------------------------
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.BlockInput.argtypes = [wintypes.BOOL]
_user32.BlockInput.restype = wintypes.BOOL
_user32.keybd_event.argtypes = [
    ctypes.c_byte,
    ctypes.c_byte,
    wintypes.DWORD,
    ctypes.c_size_t,
]

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL

_SW_RESTORE = 9
_SW_SHOW = 5
_ASFW_ANY = 0xFFFFFFFF
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002


def allow_rpa_foreground_steal() -> None:
    """由 UI 主线程调用：允许本进程内 RPA 线程将微信切到前台。"""
    try:
        _user32.AllowSetForegroundWindow(_ASFW_ANY)
    except Exception as e:
        logger.debug(f"AllowSetForegroundWindow 异常: {e}")


def _unblock_user_input() -> None:
    """确保键鼠屏蔽被释放（异常/退出路径兜底）。"""
    try:
        _user32.BlockInput(False)
    except Exception:
        pass


@contextmanager
def _guard_user_input(*, reason: str = ""):
    """短暂屏蔽物理键鼠，降低用户乱动鼠标对 RPA 的干扰。

    注意：屏蔽期间用户无法点击进度窗「中断」，因此只包住短促的关键点击/按键，
    步骤间隙保持放开。本进程发出的自动化输入通常仍可生效。
    """
    blocked = False
    try:
        blocked = bool(_user32.BlockInput(True))
        if blocked:
            logger.debug(f"BlockInput 开启{('：' + reason) if reason else ''}")
        else:
            logger.debug(f"BlockInput 未能开启{('：' + reason) if reason else ''}（可能权限不足）")
        yield blocked
    finally:
        _unblock_user_input()
        if blocked:
            logger.debug("BlockInput 已释放")


# 进程退出时兜底解除屏蔽，避免键鼠被锁死
atexit.register(_unblock_user_input)


def _force_activate_wechat_hwnd(
    hwnd: int,
    uia_control=None,
    *,
    attempts: int = 2,
    settle_s: float = ACTIVATE_SETTLE_S,
    heavy: bool = False,
) -> bool:
    """尽力将微信窗口置于前台（兼容 Win10/11 前台锁）。"""
    if not hwnd:
        return False
    if _user32.GetForegroundWindow() == hwnd:
        return True
    # 远控或另一路外发正持有键鼠时，不要再点 Alt 抢前台（Alt+双击会打开属性）
    if others_hold_input():
        return False

    for attempt in range(1, attempts + 1):
        try:
            if _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, _SW_RESTORE)
            else:
                _user32.ShowWindow(hwnd, _SW_SHOW)

            _user32.AllowSetForegroundWindow(_ASFW_ANY)

            use_heavy = heavy or attempt > 1
            if use_heavy:
                fg_hwnd = _user32.GetForegroundWindow()
                fg_tid = _user32.GetWindowThreadProcessId(fg_hwnd, None)
                cur_tid = _kernel32.GetCurrentThreadId()
                attached = False
                alt_down = False
                try:
                    if fg_tid and fg_tid != cur_tid:
                        attached = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
                    _user32.keybd_event(_VK_MENU, 0, 0, 0)
                    alt_down = True
                    _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
                    alt_down = False
                    _user32.BringWindowToTop(hwnd)
                    _user32.SetForegroundWindow(hwnd)
                finally:
                    if alt_down:
                        _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
                    if attached:
                        _user32.AttachThreadInput(cur_tid, fg_tid, False)
            else:
                _user32.BringWindowToTop(hwnd)
                _user32.SetForegroundWindow(hwnd)

            if uia_control is not None and attempt >= attempts:
                try:
                    uia_control.SetActive()
                except Exception:
                    pass

            time.sleep(settle_s)
            if _user32.GetForegroundWindow() == hwnd:
                logger.info(f"微信窗口已抢到前台 (HWND=0x{hwnd:x})")
                return True
            logger.debug(
                f"微信窗口激活第 {attempt}/{attempts} 次未抢到前台 "
                f"(当前前台 HWND=0x{_user32.GetForegroundWindow():x})"
            )
        except Exception as e:
            logger.warning(f"微信窗口激活第 {attempt}/{attempts} 次异常: {e}")

    return _user32.GetForegroundWindow() == hwnd


_WECHAT_TITLE_PREFIXES = ("微信", "Weixin", "WeChat")
_WECHAT_MAIN_CLASSES = {"mmui::MainWindow", "WeChatMainWndForPC"}
_WECHAT_QT_CLASS = re.compile(r"^Qt\d+QWindowIcon$")
_WECHAT_SKIP_CLASS = (
    "TrayIcon",
    "IME",
    "PowerMessage",
    "SystemMessage",
    "Chrome_",
    "SoPY_",
    "Sogou_",
)


def _hwnd_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n > 0 else ""


def _hwnd_title(hwnd: int) -> str:
    title_len = _user32.GetWindowTextLengthW(hwnd)
    if title_len <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(title_len + 1)
    _user32.GetWindowTextW(hwnd, buf, title_len + 1)
    return buf.value or ""


def _hwnd_size(hwnd: int) -> tuple[int, int]:
    rc = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return (0, 0)
    return (int(rc.right - rc.left), int(rc.bottom - rc.top))


def _is_wechat_main_title(title: str) -> bool:
    t = (title or "").strip()
    return any(t.startswith(p) for p in _WECHAT_TITLE_PREFIXES)


def _score_wechat_hwnd(hwnd: int) -> int:
    """给候选顶层窗口打分。隐藏的小辅助窗（标题常为 Weixin）必须排掉。"""
    if not hwnd or not _user32.IsWindow(hwnd):
        return -1
    cls = _hwnd_class_name(hwnd)
    if any(s in cls for s in _WECHAT_SKIP_CLASS):
        return -1
    if cls not in _WECHAT_MAIN_CLASSES and not _WECHAT_QT_CLASS.match(cls):
        return -1
    title = _hwnd_title(hwnd)
    if not _is_wechat_main_title(title):
        return -1
    w, h = _hwnd_size(hwnd)
    iconic = bool(_user32.IsIconic(hwnd))
    vis = bool(_user32.IsWindowVisible(hwnd))
    # 未最小化却很小：多半是 Qt 托盘/阴影辅助窗，不是主界面
    if not iconic and (w < 280 or h < 200):
        return -1
    score = 0
    if cls == "mmui::MainWindow":
        score += 100
    elif cls in _WECHAT_MAIN_CLASSES:
        score += 80
    if vis:
        score += 30
    if not iconic:
        score += 30
    if w >= 600 and h >= 400:
        score += 25
    elif iconic:
        score += 15
    return score


_last_logged_hwnd = 0

def _find_wechat_hwnd(
    cancel_event: threading.Event | None = None,
) -> int:
    """通过 Win32 API 直接定位微信主窗口 HWND。

    比 uiautomation 的顶层窗口枚举快几个数量级，且**不会**因桌面端自身 UI
    线程繁忙而 SendMessage 死锁。返回 0 表示没找到。

    注意：同进程常有多个 ``Qt51514QWindowIcon``，``FindWindowW`` 可能先命中
    隐藏的 ``Weixin`` 辅助窗。必须按标题/尺寸打分后再选。
    """

    def _stopped() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    best_hwnd = 0
    best_score = -1

    def _enum_proc(hwnd: int, _lparam: int) -> bool:
        nonlocal best_hwnd, best_score
        if _stopped():
            return False
        try:
            score = _score_wechat_hwnd(int(hwnd))
            if score > best_score:
                best_score = score
                best_hwnd = int(hwnd)
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_EnumWindowsProc(_enum_proc), 0)
    except Exception as e:
        logger.warning(f"EnumWindows 异常: {e}")

    if _stopped():
        return 0
    if best_hwnd:
        global _last_logged_hwnd
        if best_hwnd != _last_logged_hwnd:
            _last_logged_hwnd = best_hwnd
            logger.info(
                f"Win32 命中微信主窗口 HWND=0x{best_hwnd:x} "
                f"class={_hwnd_class_name(best_hwnd)!r} title={_hwnd_title(best_hwnd)!r} "
                f"iconic={bool(_user32.IsIconic(best_hwnd))}"
            )
        return best_hwnd
    return 0


class WeChatController:
    def __init__(self):
        self.wechat_window = None
        self._last_receiver = None
        self._bound_wxid = None      # 记录当前已校验过的 WxID
        self._bound_nickname = None  # 记录当前已校验过的昵称
        self._cached_hwnd: int = 0
        self._uia_hwnd: int = 0
        self._fg_ok_until: float = 0.0
        # 禁止并行外发：剪贴板 / SendKeys 不可重入
        self._send_lock = threading.Lock()

    def _mark_foreground_ok(self, hwnd: int) -> None:
        self._cached_hwnd = hwnd
        self._fg_ok_until = time.monotonic() + ACTIVATE_COOLDOWN_S

    def _fg_recently_ok(self, hwnd: int) -> bool:
        if not hwnd or hwnd != self._cached_hwnd:
            return False
        if time.monotonic() > self._fg_ok_until:
            return False
        return _user32.GetForegroundWindow() == hwnd

    def prepare_for_rpa(self) -> bool:
        """UI 主线程在外发 RPA 启动前调用：解除前台锁、缓存 HWND 并预激活。"""
        with try_input_owner() as owned:
            if not owned:
                logger.info("prepare_for_rpa: %s", BUSY_ERROR)
                return False
            allow_rpa_foreground_steal()
            hwnd = _find_wechat_hwnd()
            if not hwnd:
                logger.warning("prepare_for_rpa: 未找到微信窗口")
                return False
            self._cached_hwnd = hwnd
            self._uia_hwnd = 0
            ok = _force_activate_wechat_hwnd(hwnd, None, attempts=2, settle_s=ACTIVATE_SETTLE_S)
            if not ok:
                ok = _force_activate_wechat_hwnd(
                    hwnd, None, attempts=1, settle_s=ACTIVATE_SETTLE_S, heavy=True
                )
            if ok:
                self._mark_foreground_ok(hwnd)
            logger.info(f"prepare_for_rpa: HWND=0x{hwnd:x}, foreground={ok}")
            return ok

    def _resolve_wechat_window(
        self,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """定位 HWND 并绑定 UIA 控件，不重复激活窗口。"""

        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if _stopped():
            return False

        hwnd = self._cached_hwnd
        if not hwnd or not _user32.IsWindow(hwnd):
            hwnd = _find_wechat_hwnd(cancel_event=cancel_event)
            if not hwnd:
                return False
            self._cached_hwnd = hwnd
            if hwnd != self._uia_hwnd:
                self._uia_hwnd = 0

        if self._uia_hwnd != hwnd or self.wechat_window is None:
            try:
                self.wechat_window = auto.ControlFromHandle(hwnd)
                if not self.wechat_window:
                    return False
                self._uia_hwnd = hwnd
            except Exception as e:
                logger.exception(f"HWND 0x{hwnd:x} 转 UIA Control 失败: {e}")
                return False
        return True

    def _get_wechat_window(
        self,
        cancel_event: threading.Event | None = None,
        *,
        activate: bool = True,
    ) -> bool:
        """查找并（按需）激活微信窗口。"""

        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        t0 = time.monotonic()
        if _stopped():
            return False
        if not self._resolve_wechat_window(cancel_event=cancel_event):
            logger.warning("未找到微信窗口，请确认微信已登录。")
            return False

        hwnd = self._cached_hwnd
        if not activate:
            return True

        if self._fg_recently_ok(hwnd):
            logger.debug(f"微信窗口已在后台缓存为前台 (HWND=0x{hwnd:x})")
            return True
        if _user32.GetForegroundWindow() == hwnd:
            self._mark_foreground_ok(hwnd)
            return True

        activated = _force_activate_wechat_hwnd(
            hwnd, self.wechat_window, attempts=2, settle_s=ACTIVATE_SETTLE_S
        )
        if not activated:
            activated = _force_activate_wechat_hwnd(
                hwnd,
                self.wechat_window,
                attempts=1,
                settle_s=ACTIVATE_SETTLE_S,
                heavy=True,
            )
        if activated:
            self._mark_foreground_ok(hwnd)
        logger.info(
            f"微信窗口激活 (HWND=0x{hwnd:x}, ok={activated}), "
            f"耗时 {int((time.monotonic() - t0) * 1000)}ms"
        )
        return True

    def _clipboard_text_equals(self, expected: str) -> bool:
        """回读剪贴板 Unicode 文本，确认与期望一致。"""
        expected = expected if isinstance(expected, str) else str(expected or "")
        try:
            win32clipboard.OpenClipboard()
            try:
                if not win32clipboard.IsClipboardFormatAvailable(
                    win32clipboard.CF_UNICODETEXT
                ):
                    return False
                got = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return (got or "") == expected
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return False

    def _safe_set_clipboard_text(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
        *,
        attempts: int = 8,
        delay_s: float = 0.12,
    ) -> bool:
        """带重试 + 回读校验的 Unicode 剪贴板写入。

        必须回读确认：否则 OpenClipboard/SetClipboardText 嵌套或微信抢剪贴板时，
        可能“看似成功”但 Ctrl+V 仍粘出用户外发前手动复制的旧内容。
        """
        payload = text if isinstance(text, str) else str(text or "")
        for i in range(1, attempts + 1):
            if cancel_event is not None and cancel_event.is_set():
                logger.info("剪贴板写入被用户中断。")
                return False
            try:
                # 只用 SetClipboardData，避免 SetClipboardText 内部再次 Open 造成嵌套失败
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(
                        win32clipboard.CF_UNICODETEXT, payload
                    )
                finally:
                    win32clipboard.CloseClipboard()
                time.sleep(0.03)
                if self._clipboard_text_equals(payload):
                    return True
                logger.warning(
                    f"剪贴板回读不一致第 {i}/{attempts} 次"
                    f"（期望前 20 字={payload[:20]!r}）"
                )
            except Exception as e:
                logger.warning(f"剪贴板写入第 {i}/{attempts} 次失败: {e}")
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
            time.sleep(delay_s)
        logger.error("剪贴板写入多次失败或回读不一致，拒绝粘贴以免带入旧剪贴板内容。")
        return False

    def _paste_clipboard_text(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
        *,
        on_step: StepCallback | None = None,
        reason: str = "paste",
    ) -> bool:
        """聚焦后：清输入 → 立刻写入剪贴板并回读 → 马上 Ctrl+V。

        把写入紧贴在粘贴前，缩短与用户旧剪贴板/微信抢占的时间窗。
        """
        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if _stopped():
            return False
        self._clear_focused_edit(cancel_event)
        if _stopped():
            return False
        if not self._safe_set_clipboard_text(text, cancel_event):
            self._emit_step(on_step, "clipboard_fail", "写入剪贴板失败或回读不一致")
            return False
        if _stopped():
            return False
        # 粘贴前再确认一次，防止抢前台间隙被改写
        if not self._clipboard_text_equals(
            text if isinstance(text, str) else str(text or "")
        ):
            if not self._safe_set_clipboard_text(text, cancel_event, attempts=4):
                self._emit_step(on_step, "clipboard_fail", "粘贴前剪贴板被改写且无法恢复")
                return False
        self.wechat_window.SendKeys("{CTRL}v", waitTime=0.2)
        return True

    def get_current_wxid(self) -> str:
        """从微信进程内存映射中提取当前登录账号的 WxID。"""
        try:
            for proc in psutil.process_iter(['name', 'cmdline']):
                if proc.info['name'] == 'Weixin.exe':
                    # 排除副进程
                    if proc.info['cmdline'] and any('--type' in arg for arg in proc.info['cmdline']):
                        continue
                    # 遍历内存映射找到包含 wxid_ 的路径
                    for mem_map in proc.memory_maps():
                        if 'wxid_' in mem_map.path:
                            import re
                            match = re.search(r'wxid_[a-zA-Z0-9]+', mem_map.path)
                            if match:
                                return match.group(0)
            return ""
        except Exception as e:
            logger.debug(f"提取 WxID 失败: {e}")
            return ""

    def _emit_step(
        self,
        on_step: StepCallback | None,
        step_id: str,
        message: str,
    ) -> None:
        if on_step is not None:
            try:
                on_step(step_id, message)
            except Exception as e:
                logger.debug(f"步骤回调异常: {e}")

    def get_current_chat_name(self, cancel_event: threading.Event | None = None) -> str:
        """获取当前聊天窗口标题区域的联系人/群名称。"""

        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if _stopped() or not self._resolve_wechat_window(cancel_event=cancel_event):
            return ""

        try:
            win_rect = self.wechat_window.BoundingRectangle
            win_top = win_rect.top
            win_left = win_rect.left
            found_names: list[str] = []

            def walk(c):
                if _stopped():
                    return
                try:
                    rect = c.BoundingRectangle
                    if c != self.wechat_window and rect.top > win_top + 100:
                        return
                except Exception:
                    pass

                if c.ControlTypeName == "TextControl":
                    try:
                        rect = c.BoundingRectangle
                        if (
                            win_top + 30 < rect.top < win_top + 85
                            and win_left + 200 < rect.left < win_left + 450
                            and c.Name
                        ):
                            found_names.append(c.Name.strip())
                    except Exception:
                        pass
                for child in c.GetChildren():
                    walk(child)

            walk(self.wechat_window)
            for name in found_names:
                if name:
                    return name
        except Exception as e:
            logger.debug(f"读取当前聊天标题失败: {e}")
        return ""

    @staticmethod
    def _chat_name_matches(who: str, current_chat: str) -> bool:
        who = (who or "").strip()
        current_chat = (current_chat or "").strip()
        if not who or not current_chat:
            return False
        clean_current = re.sub(r"\(\d+\)$", "", current_chat).strip()
        who_l = who.lower()
        cur_l = clean_current.lower()
        return who_l == cur_l or who_l in cur_l

    @staticmethod
    def _normalize_msg_text(text: str) -> str:
        s = (text or "").strip()
        s = re.sub(r"[\u200b\uFEFF]", "", s)
        s = re.sub(r"[\.…]{2,}$", "", s)
        return re.sub(r"\s+", " ", s)

    @classmethod
    def _content_matches(cls, sent: str, displayed: str) -> bool:
        """比对发送正文与 UI 展示文本（兼容换行折叠、长文截断）。"""
        sent_n = cls._normalize_msg_text(sent)
        disp_n = cls._normalize_msg_text(displayed)
        if not sent_n or not disp_n:
            return False
        if sent_n == disp_n:
            return True
        if sent_n in disp_n or disp_n in sent_n:
            return True
        # 微信列表可能对长消息截断，用前缀匹配
        for prefix_len in (min(32, len(sent_n)), min(16, len(sent_n)), min(8, len(sent_n))):
            if prefix_len >= 4 and sent_n[:prefix_len] in disp_n:
                return True
        return False

    @staticmethod
    def _messages_tail_signature(msgs: list[dict], n: int = 3) -> str:
        parts: list[str] = []
        for m in msgs[-n:]:
            content = (m.get("content") or "")[:48]
            parts.append(f"{int(bool(m.get('is_self')))}|{content}")
        return "||".join(parts)


    @staticmethod
    def _build_verify_targets(candidates: list[dict]) -> list[str]:
        """
        窗口标题校验目标：有备注用备注，否则用昵称。
        微信窗口标题通常显示备注而非昵称，因此校验与搜索词分离。
        """
        targets: list[str] = []
        for source in ("remark", "name"):
            for cand in candidates:
                if cand.get("source") != source:
                    continue
                kw = (cand.get("keyword") or "").strip()
                if kw:
                    targets.append(kw)
                break
        if not targets:
            for cand in candidates:
                if cand.get("source") in ("wxid", "phone"):
                    continue
                kw = (cand.get("keyword") or "").strip()
                if kw and kw not in targets:
                    targets.append(kw)
        return targets

    def _verify_chat_names(
        self,
        verify_names: list[str],
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        current_chat = ""
        names = [(n or "").strip() for n in verify_names if (n or "").strip()]
        if not names:
            return False, current_chat
        for _ in range(VERIFY_CHAT_ATTEMPTS):
            if cancel_event is not None and cancel_event.is_set():
                return False, current_chat
            current_chat = self.get_current_chat_name(cancel_event=cancel_event)
            for who in names:
                if self._chat_name_matches(who, current_chat):
                    return True, current_chat
            time.sleep(VERIFY_CHAT_RETRY_S)
        return False, current_chat

    def _ensure_wechat_foreground(
        self,
        cancel_event: threading.Event | None = None,
        *,
        force: bool = False,
    ) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return False
        if not self._resolve_wechat_window(cancel_event=cancel_event):
            return False
        hwnd = self._cached_hwnd
        if not force and self._fg_recently_ok(hwnd):
            return True
        if _user32.GetForegroundWindow() == hwnd:
            self._mark_foreground_ok(hwnd)
            return True
        ok = _force_activate_wechat_hwnd(
            hwnd, self.wechat_window, attempts=1, settle_s=ACTIVATE_SETTLE_S
        )
        if not ok:
            ok = _force_activate_wechat_hwnd(
                hwnd,
                self.wechat_window,
                attempts=1,
                settle_s=ACTIVATE_SETTLE_S,
                heavy=True,
            )
        if ok:
            self._mark_foreground_ok(hwnd)
        return ok

    @contextmanager
    def _critical_ui(
        self,
        cancel_event: threading.Event | None = None,
        *,
        reason: str = "",
        on_step: StepCallback | None = None,
    ):
        """关键 UI 动作：强制抢前台 + 短暂屏蔽物理键鼠。"""
        if cancel_event is not None and cancel_event.is_set():
            yield False
            return
        if not self._ensure_wechat_foreground(cancel_event, force=True):
            self._emit_step(on_step, "fg_warn", "微信未在前台，仍尝试继续…")
        with _guard_user_input(reason=reason) as blocked:
            yield blocked

    def verify_login(self, expected_nickname: str) -> bool:
        """
        人工确认 + 底层 WxID 绑定校验。
        """
        current_wxid = self.get_current_wxid()
        if not current_wxid:
            logger.warning("未能检测到微信登录状态，请确保微信已登录。")
            return False

        # 如果已经成功绑定了该 WxID
        if self._bound_wxid == current_wxid:
            return True

        # 如果 WxID 发生了变化（或者是第一次运行）
        if self._bound_wxid is not None and self._bound_wxid != current_wxid:
            logger.critical(f"检测到微信账号切换！当前 ID: {current_wxid}，原绑定 ID: {self._bound_wxid}")
            # 这种情况属于运行中切换账号，为了安全直接拦截
            return False

        # 首次确认：无界面 Agent 自动绑定（不做 MessageBox）
        self._bound_wxid = current_wxid
        logger.success(
            f"自动确认通过，已锁定微信账号: {current_wxid} "
            f"(expected_nickname={expected_nickname!r})"
        )
        return True

    def _chat_input_control(self):
        """定位底部聊天输入框；排除顶部搜索框，避免跳转后误点搜框。"""
        if not self.wechat_window:
            return None
        try:
            win_rect = self.wechat_window.BoundingRectangle
            height = max(1, win_rect.bottom - win_rect.top)
            mid_y = win_rect.top + height * 0.55

            chat_input = self.wechat_window.EditControl(
                searchDepth=15, autoId="chat_input_field"
            )
            if not chat_input.Exists(0):
                chat_input = self.wechat_window.Control(
                    searchDepth=15, ClassName="mmui::ChatInputField"
                )
            if not chat_input.Exists(0):
                return None
            try:
                # 必须在窗口下半区，否则可能是搜索框
                if chat_input.BoundingRectangle.top < mid_y:
                    return None
            except Exception:
                return None
            return chat_input
        except Exception:
            pass
        return None

    def _dismiss_search_ui(
        self,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """离开搜索态：只点底部聊天输入框（已跳转后不要再碰搜索框）。"""
        if cancel_event is not None and cancel_event.is_set():
            return
        if not self.wechat_window and not self._get_wechat_window(cancel_event=cancel_event):
            return
        try:
            chat_input = self._chat_input_control()
            if chat_input is not None:
                try:
                    chat_input.SetFocus()
                    time.sleep(0.05)
                    return
                except Exception:
                    try:
                        chat_input.Click()
                        time.sleep(0.05)
                        return
                    except Exception:
                        pass
            # 找不到可靠输入框时点右下角，绝不点左上搜索区
            with self._critical_ui(cancel_event, reason="dismiss_search"):
                rect = self.wechat_window.BoundingRectangle
                width = max(1, rect.right - rect.left)
                height = max(1, rect.bottom - rect.top)
                auto.Click(
                    int(rect.left + width * 0.72),
                    int(rect.top + height * 0.92),
                )
                time.sleep(0.05)
        except Exception as e:
            logger.debug(f"离开搜索态失败: {e}")

    def _find_top_search_edit(self):
        """查找会话列表上方搜索框，排除底部聊天输入框。"""
        if not self.wechat_window:
            return None
        try:
            win_rect = self.wechat_window.BoundingRectangle
            height = max(1, win_rect.bottom - win_rect.top)
            top_limit = win_rect.top + height * 0.42
            bottom_guard = win_rect.top + height * 0.55

            chat_input = self._chat_input_control()
            chat_runtime_id = None
            if chat_input is not None:
                try:
                    chat_runtime_id = chat_input.GetRuntimeId()
                except Exception:
                    chat_runtime_id = None

            candidates = []
            for name in ("搜索", "Search"):
                try:
                    edit = self.wechat_window.EditControl(Name=name, searchDepth=14)
                    if edit.Exists(0):
                        candidates.append(edit)
                except Exception:
                    pass

            found: list = []

            def walk(c, depth=0):
                if depth > 10 or len(found) >= 6:
                    return
                try:
                    if c.ControlTypeName in ("EditControl", "Edit"):
                        rect = c.BoundingRectangle
                        if rect.top < top_limit and rect.bottom < bottom_guard:
                            if chat_runtime_id is not None:
                                try:
                                    if c.GetRuntimeId() == chat_runtime_id:
                                        return
                                except Exception:
                                    pass
                            cls = ""
                            try:
                                cls = c.ClassName or ""
                            except Exception:
                                pass
                            if "ChatInput" in cls:
                                return
                            found.append(c)
                    for child in c.GetChildren():
                        walk(child, depth + 1)
                except Exception:
                    return

            try:
                walk(self.wechat_window, 0)
            except Exception:
                pass

            for edit in candidates + found:
                try:
                    if edit.Exists(0):
                        return edit
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"查找搜索框失败: {e}")
        return None

    def _focus_search_box(
        self,
        cancel_event: threading.Event | None = None,
        *,
        reopen: bool = True,
    ) -> bool:
        """聚焦搜索框。reopen=False 时复用已打开的搜索框，避免反复 Ctrl+F。"""
        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if _stopped():
            return False
        try:
            # 已打开则直接聚焦，禁止再次 Ctrl+F（否则搜索框会反复弹出）
            if not reopen:
                edit = self._find_top_search_edit()
                if edit is not None:
                    try:
                        edit.SetFocus()
                        time.sleep(0.05)
                        return True
                    except Exception:
                        try:
                            edit.Click()
                            time.sleep(0.05)
                            return True
                        except Exception:
                            pass
                # 搜框已关才允许重新打开
                reopen = True

            with self._critical_ui(cancel_event, reason="focus_search"):
                if _stopped():
                    return False
                if reopen:
                    # Ctrl+F 已聚焦搜索框，不要再 Click 搜框（跳转后会看到鼠标滑回去）
                    self.wechat_window.SendKeys("{CTRL}f", waitTime=0.25)
                    time.sleep(0.12)
                if _stopped():
                    return False

                edit = self._find_top_search_edit()
                if edit is not None:
                    # 仅在复用已打开搜框、或 Ctrl+F 未吃到焦点时 SetFocus（不 Click，少移鼠标）
                    try:
                        edit.SetFocus()
                        time.sleep(0.04)
                        return True
                    except Exception:
                        if not reopen:
                            try:
                                edit.Click()
                                time.sleep(0.05)
                                return True
                            except Exception as e:
                                logger.debug(f"点击搜索框失败: {e}")
                        else:
                            # 新建搜索：Ctrl+F 已足够，无需再点
                            return True

                if not reopen:
                    return False
                # Ctrl+F 后找不到控件也视为已打开，避免再点左上角搜框
                return True
        except Exception as e:
            logger.warning(f"聚焦搜索框失败: {e}")
            return False

    def _clear_focused_edit(
        self,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            return
        if not self.wechat_window:
            return
        try:
            self.wechat_window.SendKeys("{CTRL}a", waitTime=0.05)
            self.wechat_window.SendKeys("{DELETE}", waitTime=0.05)
        except Exception:
            pass

    def _search_contact(
        self,
        receiver: str,
        cancel_event: threading.Event | None = None,
        *,
        reuse_open_search: bool = False,
    ) -> bool:
        """通过搜索切换联系人。reuse_open_search=True 时不重新 Ctrl+F。"""

        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        try:
            if _stopped():
                return False
            if not self._get_wechat_window(cancel_event=cancel_event):
                return False
            if _stopped():
                return False
            if not self._focus_search_box(
                cancel_event=cancel_event,
                reopen=not reuse_open_search,
            ):
                return False
            if _stopped():
                return False
            with self._critical_ui(cancel_event, reason="search_paste_enter"):
                if _stopped():
                    return False
                # 紧贴粘贴前写入搜索词并回读，避免粘出用户旧剪贴板
                if not self._paste_clipboard_text(
                    receiver, cancel_event, reason="search_paste"
                ):
                    return False
                time.sleep(SEARCH_LIST_WAIT_S)
                if _stopped():
                    return False
                self.wechat_window.SendKeys("{ENTER}", waitTime=0.4)
            time.sleep(0.12)
            self._last_receiver = receiver
            return True
        except Exception as e:
            logger.exception(f"搜索联系人 {receiver} 失败: {e}")
            return False

    def _try_match_current_chat(
        self,
        verify_names: list[str],
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
    ) -> tuple[bool, str]:
        """检测当前对话是否已是目标联系人，是则跳过搜索。"""
        label = verify_names[0] if verify_names else ""
        self._emit_step(
            on_step,
            "check_current_chat",
            f"检查当前对话是否匹配：{label or '目标客户'}",
        )
        ok, current_chat = self._verify_chat_names(verify_names, cancel_event=cancel_event)
        if ok:
            if verify_names:
                self._last_receiver = verify_names[0]
            logger.info(f"[RPA] 当前已在目标对话（{current_chat}），跳过搜索")
            self._emit_step(
                on_step,
                "verify_chat_ok",
                f"当前已在目标对话：{current_chat}",
            )
        return ok, current_chat

    def chat_with(
        self,
        search_keyword: str,
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
        verify_names: list[str] | None = None,
        *,
        reuse_open_search: bool = False,
    ) -> tuple[bool, str, bool]:
        """搜索切换聊天窗口。

        返回 (ok, current_chat, did_search)。
        did_search=True 表示本次确实执行了搜索（按了 Ctrl+F 或复用搜索框粘贴）。
        """
        if cancel_event is not None and cancel_event.is_set():
            return False, "", False
        if not self._get_wechat_window(cancel_event=cancel_event):
            return False, "", False

        targets = verify_names or [search_keyword]
        verify_label = targets[0] if targets else search_keyword

        ok, current_chat = self._try_match_current_chat(
            targets, cancel_event=cancel_event, on_step=on_step
        )
        if ok:
            return True, current_chat, False

        self._emit_step(on_step, "switch_chat", f"正在搜索联系人：{search_keyword}")
        if not self._search_contact(
            search_keyword,
            cancel_event=cancel_event,
            reuse_open_search=reuse_open_search,
        ):
            return False, current_chat, True

        self._emit_step(
            on_step,
            "verify_chat",
            f"正在验证对话窗口（期望：{verify_label}）",
        )
        ok, current_chat = self._verify_chat_names(targets, cancel_event=cancel_event)
        if ok:
            logger.info(
                f"[RPA] 搜索词 '{search_keyword}' 已切换，"
                f"窗口校验通过（当前对话: {current_chat}）"
            )
            self._emit_step(on_step, "verify_chat_ok", f"对话窗口匹配：{current_chat}")
            self._last_receiver = search_keyword
            # 搜索回车后微信通常已关掉搜框并进入会话，此处不再点搜框/输入框
            return True, current_chat, True

        logger.warning(
            f"[RPA] 窗口校验失败：搜索词 '{search_keyword}'，"
            f"期望 '{verify_label}'，当前对话 '{current_chat}'"
        )
        self._emit_step(
            on_step,
            "verify_chat_fail",
            f"窗口不匹配（期望：{verify_label}，当前：{current_chat or '未知'}），"
            f"将尝试下一搜索词",
        )
        return False, current_chat, True

    def _pick_matched_candidate(
        self,
        candidates: list[dict],
        search_keyword: str,
    ) -> dict:
        for cand in candidates:
            if cand.get("keyword") == search_keyword:
                return cand
        return candidates[0]

    def _match_candidate_on_current_chat(
        self,
        candidates: list[dict],
        verify_names: list[str],
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
    ) -> dict | None:
        """若当前对话标题已匹配备注/昵称校验目标，直接返回对应候选。"""
        if not verify_names:
            return None
        current_chat = self.get_current_chat_name(cancel_event=cancel_event)
        if not current_chat:
            return None
        for vn in verify_names:
            if not self._chat_name_matches(vn, current_chat):
                continue
            matched = self._pick_matched_candidate(candidates, vn)
            for cand in candidates:
                if cand.get("source") == "remark" and cand.get("keyword") == vn:
                    matched = cand
                    break
                if cand.get("source") == "name" and cand.get("keyword") == vn:
                    matched = cand
            self._emit_step(
                on_step,
                "verify_chat_ok",
                f"当前已在目标对话：{current_chat}，跳过搜索",
            )
            self._last_receiver = matched.get("keyword") or vn
            logger.info(f"[RPA] 当前对话 '{current_chat}' 已通过窗口校验")
            return matched
        return None

    def _focus_input(
        self,
        cancel_event: threading.Event | None = None,
        *,
        dismiss_search: bool = False,
    ) -> bool:
        """聚焦聊天输入框。发送阶段禁止 Ctrl+F；必要时点一次输入框收起搜框。"""
        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        try:
            if _stopped():
                return False
            if not self.wechat_window and not self._get_wechat_window(cancel_event=cancel_event):
                return False
            if not self._ensure_wechat_foreground(cancel_event, force=True):
                return False

            # 若刚搜过：只点聊天输入框离开搜框，绝不再按 Ctrl+F
            if dismiss_search:
                self._dismiss_search_ui(cancel_event)
                if _stopped():
                    return False
                return True

            chat_input = self._chat_input_control()
            if chat_input is not None and chat_input.Exists(0.2):
                rect = self.wechat_window.BoundingRectangle
                height = rect.bottom - rect.top
                if chat_input.BoundingRectangle.top > rect.top + height * 0.5:
                    try:
                        chat_input.SetFocus()
                        time.sleep(0.05)
                        return True
                    except Exception:
                        try:
                            chat_input.Click()
                            time.sleep(0.05)
                            return True
                        except Exception:
                            pass

            with self._critical_ui(cancel_event, reason="focus_input_fallback"):
                if _stopped():
                    return False
                rect = self.wechat_window.BoundingRectangle
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                auto.Click(
                    int(rect.left + width * 0.72),
                    int(rect.top + height * 0.92),
                )
                time.sleep(0.05)
                return True
        except Exception as e:
            logger.error(f"聚焦聊天输入框异常: {e}")
            return False

    def _switch_to_chat(
        self, receiver: str, cancel_event: threading.Event | None = None
    ) -> bool:
        ok, _, did_search = self.chat_with(receiver, cancel_event=cancel_event)
        if not ok:
            return False
        return self._focus_input(cancel_event=cancel_event, dismiss_search=did_search)

    def _scroll_chat_to_bottom(
        self,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """尽量滚到最新消息；优先键盘，减少鼠标挪动。"""
        if cancel_event is not None and cancel_event.is_set():
            return
        if not self.wechat_window:
            return
        try:
            msg_list = self.wechat_window.ListControl(Name="消息")
            if not msg_list.Exists(0.1):
                msg_list = self.wechat_window.ListControl(
                    ClassName="mmui::StickyHeaderRecyclerListView"
                )
            if msg_list.Exists(0.1):
                try:
                    msg_list.SetFocus()
                except Exception:
                    # 仅在无法 SetFocus 时点一下列表
                    rect = msg_list.BoundingRectangle
                    mid_x = int((rect.left + rect.right) / 2)
                    click_y = int(rect.bottom - max(12, (rect.bottom - rect.top) * 0.08))
                    auto.Click(mid_x, click_y)
            self.wechat_window.SendKeys("{END}", waitTime=0.1)
        except Exception as e:
            logger.debug(f"滚动聊天到底部失败: {e}")

    def _collect_visible_messages(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        if cancel_event is not None and cancel_event.is_set():
            return []
        if not self.wechat_window and not self._resolve_wechat_window(cancel_event):
            return []

        win_rect = self.wechat_window.BoundingRectangle
        win_width = max(1, win_rect.right - win_rect.left)
        msg_list = self.wechat_window.ListControl(Name="消息")
        if not msg_list.Exists(0.15):
            msg_list = self.wechat_window.ListControl(
                ClassName="mmui::StickyHeaderRecyclerListView"
            )
        if not msg_list.Exists(0.15):
            msg_list = self.wechat_window.ListControl(ClassName="mmui::ListView")
        if not msg_list.Exists(0):
            return []

        messages: list[dict] = []
        try:
            for item in msg_list.GetChildren():
                if item.ControlTypeName not in ["ListItemControl", "ListItem"]:
                    continue

                content = item.Name.strip() if item.Name else ""
                if not content:
                    def walk_get_text(c):
                        t_list: list[str] = []
                        for child in c.GetChildren():
                            if child.ControlTypeName in [
                                "TextControl",
                                "DocumentControl",
                                "EditControl",
                            ]:
                                if child.Name and child.Name not in ["重新发送", "重发"]:
                                    t_list.append(child.Name)
                            t_list.extend(walk_get_text(child))
                        return t_list

                    content = " ".join(walk_get_text(item)).strip()

                if not content:
                    continue

                try:
                    rect = item.BoundingRectangle
                    is_self = (win_rect.right - rect.right) < 120
                    if not is_self:
                        center_x = (rect.left + rect.right) / 2
                        is_self = center_x > win_rect.left + win_width * 0.55
                except Exception:
                    is_self = False

                has_error = False
                if "重新发送" in (item.Name or "") or "重发" in (item.Name or ""):
                    has_error = True
                if not has_error:
                    resend_btn = item.ButtonControl(searchDepth=3, Name="重新发送")
                    if resend_btn.Exists(0):
                        has_error = True
                if not has_error:
                    resend_btn2 = item.ButtonControl(searchDepth=3, Name="重发")
                    if resend_btn2.Exists(0):
                        has_error = True

                messages.append(
                    {"content": content, "is_self": is_self, "has_error": has_error}
                )
        except Exception as e:
            logger.error(f"提取聊天文本列表异常: {e}")
        return messages

    def _read_messages_for_verify(
        self,
        cancel_event: threading.Event | None = None,
        *,
        scroll: bool = True,
    ) -> list[dict]:
        """多次采样消息列表，取信息最完整的一次。"""
        if scroll:
            self._scroll_chat_to_bottom(cancel_event)
        best: list[dict] = []
        for i in range(SEND_VERIFY_READ_RETRIES):
            if cancel_event is not None and cancel_event.is_set():
                break
            msgs = self._collect_visible_messages(cancel_event)
            if len(msgs) > len(best):
                best = msgs
            if i + 1 < SEND_VERIFY_READ_RETRIES:
                time.sleep(SEND_VERIFY_READ_GAP_S)
        return best

    def get_all_messages(self, cancel_event: threading.Event | None = None) -> list[dict]:
        return self._collect_visible_messages(cancel_event)

    def _count_self_matches(self, content: str, msgs: list[dict]) -> int:
        return sum(
            1
            for m in msgs
            if m.get("is_self")
            and self._content_matches(content, m.get("content") or "")
        )

    def _find_sent_message(
        self,
        content: str,
        msgs: list[dict],
        baseline_count: int = 0,
        baseline_match_count: int = 0,
        baseline_tail_sig: str = "",
    ) -> dict | None:
        """查找发送后新增的自己消息（兼容连续发送相同内容）。"""
        matching = [
            m
            for m in msgs
            if m.get("is_self")
            and self._content_matches(content, m.get("content") or "")
        ]
        # 连续发送相同文案时，可见列表长度可能不变，但匹配条数会增加
        if len(matching) > baseline_match_count:
            return matching[-1]

        if baseline_count < len(msgs):
            for msg in reversed(msgs[baseline_count:]):
                if msg.get("is_self") and self._content_matches(
                    content, msg.get("content") or ""
                ):
                    return msg

        # 虚拟列表条数不变时：尾部签名变化 + 末尾出现匹配内容
        tail_sig = self._messages_tail_signature(msgs)
        if tail_sig and tail_sig != baseline_tail_sig:
            for msg in reversed(msgs[-4:]):
                if not self._content_matches(content, msg.get("content") or ""):
                    continue
                if msg.get("is_self"):
                    return msg
        return None

    def check_send_status(
        self,
        content: str,
        timeout: int = SEND_VERIFY_TIMEOUT_S,
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
        baseline_messages: list[dict] | None = None,
    ) -> bool:
        self._emit_step(
            on_step,
            "verify_send",
            f"正在确认消息是否送达（最多等待 {timeout} 秒）…",
        )
        baseline_count = len(baseline_messages) if baseline_messages is not None else 0
        baseline_match_count = (
            self._count_self_matches(content, baseline_messages)
            if baseline_messages is not None
            else 0
        )
        baseline_tail_sig = (
            self._messages_tail_signature(baseline_messages)
            if baseline_messages is not None
            else ""
        )

        start_verify = time.monotonic()
        last_progress_at = start_verify
        stable_ok = 0
        poll_idx = 0

        # 给微信一点时间把消息写入列表（网络慢时尤其需要）
        time.sleep(SEND_POST_ENTER_GRACE_S)

        while time.monotonic() - start_verify < timeout:
            if cancel_event is not None and cancel_event.is_set():
                return False

            elapsed = int(time.monotonic() - start_verify)
            if elapsed > 0 and elapsed % 5 == 0 and time.monotonic() - last_progress_at >= 4.5:
                self._emit_step(
                    on_step,
                    "verify_send_wait",
                    f"仍在等待微信确认送达…（已等待 {elapsed} 秒）",
                )
                last_progress_at = time.monotonic()

            poll_idx += 1
            scroll = poll_idx == 1 or poll_idx % 4 == 0
            msgs = self._read_messages_for_verify(
                cancel_event=cancel_event,
                scroll=scroll,
            )
            if msgs:
                hit = self._find_sent_message(
                    content,
                    msgs,
                    baseline_count,
                    baseline_match_count,
                    baseline_tail_sig,
                )
                if hit is not None:
                    if hit.get("has_error"):
                        stable_ok = 0
                        self._emit_step(
                            on_step,
                            "verify_send_fail",
                            "检测到红色叹号，消息发送失败",
                        )
                        return False
                    stable_ok += 1
                    if stable_ok >= SEND_VERIFY_STABLE_POLLS:
                        time.sleep(SEND_VERIFY_FINAL_CHECK_S)
                        if cancel_event is not None and cancel_event.is_set():
                            return False
                        final_msgs = self._read_messages_for_verify(
                            cancel_event=cancel_event,
                            scroll=False,
                        )
                        hit_final = self._find_sent_message(
                            content,
                            final_msgs,
                            baseline_count,
                            baseline_match_count,
                            baseline_tail_sig,
                        )
                        if hit_final is not None and hit_final.get("has_error"):
                            self._emit_step(
                                on_step,
                                "verify_send_fail",
                                "最终校验发现发送失败",
                            )
                            return False
                        if hit_final is not None:
                            self._emit_step(on_step, "verify_send_ok", "消息已成功送达")
                            return True
                        # 最终采样偶发读不到时，若已连续命中则仍视为成功
                        self._emit_step(
                            on_step,
                            "verify_send_ok",
                            "消息已成功送达（连续确认）",
                        )
                        return True
                else:
                    stable_ok = 0
            else:
                stable_ok = 0

            time.sleep(SEND_VERIFY_POLL_S)

        self._emit_step(
            on_step,
            "verify_send_fail",
            f"发送结果校验超时（{timeout} 秒），消息可能仍在发送中",
        )
        return False

    def _set_clipboard_files(self, file_paths: list[str]):
        """将一组文件路径放入 Windows 剪贴板 (CF_HDROP 格式)。"""

        class DROPFILES(ctypes.Structure):
            _fields_ = [
                ("pFiles", ctypes.c_uint),
                ("x", ctypes.c_long),
                ("y", ctypes.c_long),
                ("fNC", ctypes.c_int),
                ("fWide", ctypes.c_bool),
            ]

        pDropFiles = DROPFILES()
        pDropFiles.pFiles = ctypes.sizeof(DROPFILES)
        pDropFiles.fWide = True

        # 将路径转换为 Windows 格式并以 \0 分隔，结尾双 \0
        files = ("\0".join([os.path.abspath(p) for p in file_paths])).replace("/", "\\")
        data = files.encode("U16")[2:] + b"\0\0"

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, bytes(pDropFiles) + data)
        finally:
            win32clipboard.CloseClipboard()

    def _send_text_to_current(
        self,
        message: str,
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
        *,
        dismiss_search: bool = False,
    ) -> bool:
        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        self._emit_step(on_step, "send_text", "正在写入并发送消息…")
        if _stopped():
            return False
        if not self._ensure_wechat_foreground(cancel_event, force=True):
            self._emit_step(on_step, "send_text_fail", "无法聚焦微信窗口")
            return False
        if not self._focus_input(cancel_event=cancel_event, dismiss_search=dismiss_search):
            self._emit_step(on_step, "send_text_fail", "无法聚焦聊天输入框")
            return False
        with self._critical_ui(cancel_event, reason="paste_and_enter", on_step=on_step):
            if _stopped():
                return False
            # 先聚焦再写剪贴板，避免抢前台间隙里 Ctrl+V 粘出用户旧复制内容
            if not self._paste_clipboard_text(
                message, cancel_event, on_step=on_step, reason="send_paste"
            ):
                self._emit_step(on_step, "send_text_fail", "写入剪贴板失败或回读不一致")
                return False
            time.sleep(0.35)
            if _stopped():
                return False
            self.wechat_window.SendKeys("{ENTER}", waitTime=0.25)
        return True

    def send_message_with_candidates(
        self,
        candidates: list[dict],
        message: str,
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
        on_confirm: ConfirmCallback | None = None,
    ) -> SendResult:
        """按候选关键词依次切换对话、校验窗口、发送并确认送达。"""
        if not self._send_lock.acquire(blocking=False):
            logger.warning("拒绝并行微信外发：已有 RPA 发送任务在执行")
            return SendResult(False, error="已有微信发送任务在执行，请稍后再试")
        try:
            with try_input_owner() as owned:
                if not owned:
                    logger.info(BUSY_ERROR)
                    return SendResult(False, error=BUSY_ERROR)
                return self._send_message_with_candidates_locked(
                    candidates,
                    message,
                    cancel_event=cancel_event,
                    on_step=on_step,
                    on_confirm=on_confirm,
                )
        finally:
            self._send_lock.release()

    def _send_message_with_candidates_locked(
        self,
        candidates: list[dict],
        message: str,
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
        on_confirm: ConfirmCallback | None = None,
    ) -> SendResult:
        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        msg = (message or "").strip()
        if not msg:
            return SendResult(False, error="消息内容为空")

        normalized: list[dict] = []
        seen: set[str] = set()
        for item in candidates or []:
            kw = (item.get("keyword") or "").strip()
            if not kw or kw in seen:
                continue
            seen.add(kw)
            normalized.append(
                {
                    "keyword": kw,
                    "source": (item.get("source") or "").strip() or "unknown",
                }
            )
        if not normalized:
            return SendResult(False, error="缺少可用的联系人搜索关键词")

        verify_targets = self._build_verify_targets(normalized)
        verify_label = verify_targets[0] if verify_targets else "目标客户"

        t0 = time.monotonic()
        try:
            if _stopped():
                return SendResult(False, error="用户中断")

            self._emit_step(on_step, "prepare", "正在预连接微信窗口…")
            if self.prepare_for_rpa():
                self._emit_step(on_step, "prepare_ok", "微信已就绪，开始外发…")
            else:
                self._emit_step(on_step, "prepare_warn", "未能预连接微信，将继续尝试…")

            self._emit_step(on_step, "find_wechat", "正在连接微信窗口…")
            if not self._get_wechat_window(cancel_event=cancel_event):
                return SendResult(False, error="未找到微信窗口，请确认微信已登录")

            matched = self._match_candidate_on_current_chat(
                normalized,
                verify_targets,
                cancel_event=cancel_event,
                on_step=on_step,
            )
            last_chat = self.get_current_chat_name(cancel_event=cancel_event)
            last_attempted: dict | None = None
            search_opened = False

            if matched is None:
                for cand in normalized:
                    if _stopped():
                        return SendResult(False, error="用户中断")
                    # 上一轮可能已跳到正确会话：先复检，避免再次 Ctrl+F
                    pre = self._match_candidate_on_current_chat(
                        normalized,
                        verify_targets,
                        cancel_event=cancel_event,
                        on_step=on_step,
                    )
                    if pre is not None:
                        matched = pre
                        break

                    keyword = cand["keyword"]
                    source = cand["source"]
                    src_label = _SOURCE_LABELS.get(source, source)
                    last_attempted = cand
                    self._emit_step(
                        on_step,
                        "try_keyword",
                        f"尝试 {src_label}：{keyword}",
                    )
                    ok, current_chat, searched = self.chat_with(
                        keyword,
                        cancel_event=cancel_event,
                        on_step=on_step,
                        verify_names=verify_targets,
                        reuse_open_search=search_opened,
                    )
                    if searched:
                        search_opened = True
                    last_chat = current_chat or last_chat
                    if ok:
                        matched = cand
                        break

            if not matched:
                last_chat = self.get_current_chat_name(cancel_event=cancel_event) or last_chat
                confirm_msg = (
                    f"无法自动确认对话窗口。\n\n"
                    f"期望窗口：{verify_label}\n"
                    f"当前窗口：{last_chat or '未知'}\n\n"
                    f"请确认微信已切换到正确的客户对话，是否继续发送？"
                )
                self._emit_step(
                    on_step,
                    "user_confirm",
                    "窗口校验未通过，等待您确认是否已跳转…",
                )
                user_ok = False
                if on_confirm is not None:
                    try:
                        user_ok = bool(on_confirm(confirm_msg))
                    except Exception as e:
                        logger.warning(f"用户确认回调异常: {e}")
                if user_ok:
                    matched = last_attempted or normalized[0]
                    self._emit_step(
                        on_step,
                        "user_confirm_ok",
                        f"用户确认已跳转，继续发送（当前：{last_chat or '未知'}）",
                    )
                    logger.info("[RPA] 用户确认窗口已跳转，跳过自动校验继续发送")
                else:
                    detail = f"（最后窗口：{last_chat or '未知'}）" if last_chat else ""
                    return SendResult(
                        False,
                        error=f"无法切换到目标客户对话窗口，已尝试全部搜索词{detail}",
                    )

            if _stopped():
                return SendResult(False, error="用户中断")

            baseline_messages = self._read_messages_for_verify(
                cancel_event=cancel_event,
                scroll=True,
            )

            # 已跳转后只走发送：聚焦底部输入框并粘贴，不再触碰搜索框
            if not self._send_text_to_current(
                msg,
                cancel_event=cancel_event,
                on_step=on_step,
                dismiss_search=False,
            ):
                return SendResult(False, error="消息发送操作失败（无法聚焦输入框或粘贴失败）")

            if _stopped():
                return SendResult(False, error="用户中断")

            if not self.check_send_status(
                msg,
                cancel_event=cancel_event,
                on_step=on_step,
                baseline_messages=baseline_messages,
            ):
                return SendResult(
                    False,
                    error=(
                        f"消息发送未确认成功（红色叹号或校验超时 {SEND_VERIFY_TIMEOUT_S} 秒）"
                    ),
                )

            logger.info(
                f"消息已确认送达（{matched['keyword']}），"
                f"总耗时 {int((time.monotonic() - t0) * 1000)}ms"
            )
            return SendResult(
                True,
                receiver_used=matched["keyword"],
                receiver_source=matched["source"],
            )
        except Exception as e:
            logger.exception(f"发送消息失败: {e}")
            return SendResult(False, error=f"RPA 异常: {e}")
        finally:
            _unblock_user_input()

    def send_message(
        self,
        receiver: str,
        message: str,
        cancel_event: threading.Event | None = None,
        on_step: StepCallback | None = None,
    ) -> bool:
        """向指定联系人发送文本消息（单关键词，兼容旧调用）。"""
        result = self.send_message_with_candidates(
            [{"keyword": receiver, "source": "unknown"}],
            message,
            cancel_event=cancel_event,
            on_step=on_step,
        )
        return result.ok

    def send_images(self, receiver: str, image_paths: list[str]) -> bool:
        """向指定联系人发送一组图片。"""
        with try_input_owner() as owned:
            if not owned:
                logger.info(BUSY_ERROR)
                return False
            try:
                # 校验文件是否存在
                valid_paths = [p for p in image_paths if os.path.exists(p)]
                if not valid_paths:
                    logger.error(f"没有找到有效的图片文件: {image_paths}")
                    return False

                if not self._switch_to_chat(receiver):
                    return False

                # 2. 复制图片文件到剪贴板
                self._set_clipboard_files(valid_paths)

                # 3. 粘贴并发送
                auto.SendKeys('{CTRL}v', waitTime=1.5)
                auto.SendKeys('{ENTER}', waitTime=0.5)

                logger.info(f"成功发送 {len(valid_paths)} 张图片给 {receiver}")
                return True
            except Exception as e:
                logger.error(f"发送图片给 {receiver} 失败: {e}")
                return False

    def get_self_nickname(self) -> str:
        """获取当前登录账号的昵称（占位符，建议从配置获取）。"""
        return "未知"


# Singleton instance
wechat = WeChatController()


def prepare_wechat_for_rpa() -> bool:
    """UI 主线程在外发 RPA 启动前调用。"""
    return wechat.prepare_for_rpa()

