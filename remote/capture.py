"""屏幕抓取为 JPEG。"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


def grab_jpeg(*, max_width: int = 640, quality: int = 50) -> bytes | None:
    """抓取主屏并缩放编码为 JPEG。

    max_width<=0 表示不缩放，保留原始分辨率（接管模式推荐）。
    """
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        logger.error("缺少 pillow")
        return None
    try:
        img = ImageGrab.grab()
    except Exception as exc:  # noqa: BLE001
        logger.warning("截屏失败: %s", exc)
        return None

    if max_width > 0 and img.width > max_width:
        ratio = max_width / float(img.width)
        h = max(1, int(img.height * ratio))
        resample = getattr(Image, "Resampling", Image).BILINEAR
        img = img.resize((max_width, h), resample)

    buf = io.BytesIO()
    img = img.convert("RGB")
    q = max(30, min(int(quality), 95))
    # subsampling=0 画质最好但编码慢；低配 VM 接管仍用 2 以减轻 CPU
    subsampling = 2 if q < 85 else 0
    img.save(buf, format="JPEG", quality=q, subsampling=subsampling)
    return buf.getvalue()
