"""访客机剪贴板读取（远控接管时同步到 Console 本机）。"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

# 与协议草案一致：单帧文本上限
_MAX_TEXT_BYTES = 256 * 1024


def text_signature(text: str) -> str:
    """用于去重，避免重复推送相同剪贴板。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text() -> str | None:
    """读取 Windows Unicode 剪贴板文本；无文本或失败时返回 None。"""
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(
                win32clipboard.CF_UNICODETEXT
            ):
                return None
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            if data is None:
                return None
            text = str(data)
            if not text:
                return None
            encoded = text.encode("utf-8")
            if len(encoded) > _MAX_TEXT_BYTES:
                logger.debug(
                    "clipboard text too large (%s bytes), skip sync", len(encoded)
                )
                return None
            return text
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:  # noqa: BLE001
        logger.debug("read clipboard failed: %s", exc)
        return None
