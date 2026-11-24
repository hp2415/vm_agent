"""访客机剪贴板同步（远控接管时与 Console 互通）。

要点：
- 同进程多线程调用 OpenClipboard(NULL) 会并发进入导致 EmptyClipboard/GetClipboardData
  堆损坏（0xC0000374），所有剪贴板访问必须经进程级 CLIPBOARD_LOCK 串行。
- 访客机内还运行其他监听/外发进程，平时只读无锁的 GetClipboardSequenceNumber()，
  序号未变绝不调用 OpenClipboard()；需要读写时只极短时间持有剪贴板并立刻关闭。
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 与协议草案一致：单帧文本上限
_MAX_TEXT_BYTES = 256 * 1024

# 进程内所有剪贴板操作（含 rpa/wechat_controller.py）共用此锁，杜绝同进程并发 OpenClipboard。
CLIPBOARD_LOCK = threading.Lock()


def sequence_number() -> int:
    """读取系统剪贴板变更序号（无锁、不占用剪贴板）。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32
        return int(user32.GetClipboardSequenceNumber())
    except Exception:  # noqa: BLE001
        return 0


def text_signature(text: str) -> str:
    """用于去重，避免重复推送相同剪贴板。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_raw_locked() -> tuple[bool, object]:
    """调用方已持有 CLIPBOARD_LOCK。极短时间打开并立刻关闭剪贴板。"""
    import win32clipboard

    try:
        win32clipboard.OpenClipboard()
    except Exception as exc:  # noqa: BLE001
        logger.debug("open clipboard skipped (busy): %s", exc)
        return False, None
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return True, None
        return True, win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    except Exception as exc:  # noqa: BLE001
        logger.debug("read clipboard failed: %s", exc)
        return False, None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass


def _normalize_text(data: object) -> str | None:
    if data is None:
        return None
    text = str(data)
    if not text:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_TEXT_BYTES:
        logger.debug("clipboard text too large (%s bytes), skip sync", len(encoded))
        return None
    return text


def read_text() -> str | None:
    """读取 Windows Unicode 剪贴板文本；被占用、无文本或失败时返回 None。"""
    with CLIPBOARD_LOCK:
        ok, data = _read_raw_locked()
    if not ok:
        return None
    return _normalize_text(data)


def read_text_if_changed(last_seq: int | None) -> tuple[bool, int, str | None]:
    """仅当系统剪贴板序号变化时才短暂打开读取。

    返回 (handled, current_seq, text)：
    - 序号未变：返回 (True, seq, None)，完全不调用 OpenClipboard()。
    - 剪贴板正被其他进程占用：返回 (False, last_seq 或 0, None)，不争抢、留待下次。
    - 读取成功（含非文本内容）：返回 (True, seq, text_or_none)。
    """
    seq = sequence_number()
    if last_seq is not None and seq == last_seq:
        return True, seq, None
    with CLIPBOARD_LOCK:
        ok, data = _read_raw_locked()
        after_seq = sequence_number()
    if not ok:
        return False, last_seq if last_seq is not None else 0, None
    return True, after_seq, _normalize_text(data)


def write_text(text: str, *, attempts: int = 3, retry_delay_s: float = 0.02) -> bool:
    """把 Unicode 文本写入访客机剪贴板。短占即放，遇其他进程暂占时轻度让避重试。"""
    import win32clipboard

    for idx in range(attempts):
        with CLIPBOARD_LOCK:
            try:
                win32clipboard.OpenClipboard()
            except Exception as exc:  # noqa: BLE001
                if idx + 1 >= attempts:
                    logger.warning("write clipboard failed (busy): %s", exc)
                    return False
            else:
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("write clipboard failed: %s", exc)
                    return False
                finally:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:  # noqa: BLE001
                        pass
        time.sleep(retry_delay_s)
    return False
