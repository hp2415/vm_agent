"""在访客机里占住一份 CPU 和内存，用来看宿主机还能不能再加机器。

默认对着「微信开着、偶尔操作」：大约 400 MB 常驻，平时约占一个逻辑处理器的 8%，
每 90 秒抬到 35% 并持续 8 秒。这是观察用的量级，不是某台微信的实测曲线。
微信已经登录时不要再开，否则会和真进程叠在一起。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_MB = 400
DEFAULT_CPU_PERCENT = 8.0
BURST_CPU_PERCENT = 35.0
BURST_SEC = 8.0
QUIET_SEC = 90.0
LEAD_SEC = 15.0
_PAGE = 4096
_MAX_MEMORY_MB = 4096


class GuestLoad:
    """占住内存页，并按占空比烧掉一个逻辑处理器的一部分。"""

    def __init__(self, memory_mb: int = DEFAULT_MEMORY_MB, cpu_percent: float = DEFAULT_CPU_PERCENT) -> None:
        self.memory_mb = max(0, min(int(memory_mb), _MAX_MEMORY_MB))
        self.cpu_percent = max(0.0, min(float(cpu_percent), 100.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buf: bytearray | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="guest-load", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)
        self._buf = None

    def _run(self) -> None:
        self._buf = _allocate(self.memory_mb)
        held = 0 if self._buf is None else self.memory_mb
        logger.info(
            "正常使用模拟已开：内存 %s MB，平时约占 1 个逻辑处理器的 %s%%，每 %.0f 秒抬到 %.0f%% 持续 %.0f 秒",
            held,
            int(self.cpu_percent),
            QUIET_SEC,
            BURST_CPU_PERCENT,
            BURST_SEC,
        )
        if self._stop.wait(LEAD_SEC):
            return
        next_burst = time.monotonic()
        next_touch = time.monotonic() + 20.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_burst:
                self._burn(BURST_CPU_PERCENT, BURST_SEC)
                next_burst = time.monotonic() + QUIET_SEC
                continue
            if now >= next_touch:
                _touch(self._buf)
                next_touch = now + 20.0
            self._burn(self.cpu_percent, 0.5)

    def _burn(self, percent: float, seconds: float) -> None:
        """在 seconds 内按 percent 占用当前这一个线程。"""
        if seconds <= 0 or percent <= 0:
            if seconds > 0:
                self._stop.wait(seconds)
            return
        percent = max(0.0, min(percent, 100.0))
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            slice_s = min(0.1, deadline - time.monotonic())
            if slice_s <= 0:
                return
            busy = slice_s * percent / 100.0
            end = time.perf_counter() + busy
            acc = 0
            while time.perf_counter() < end:
                acc = (acc + 1) * 3
            rest = slice_s - busy
            if rest > 0 and self._stop.wait(rest):
                return


def _allocate(memory_mb: int) -> bytearray | None:
    if memory_mb <= 0:
        return None
    try:
        buf = bytearray(memory_mb * 1024 * 1024)
    except MemoryError:
        logger.warning("分不到 %s MB 内存，CPU 模拟继续", memory_mb)
        return None
    _touch(buf)
    return buf


def _touch(buf: bytearray | None) -> None:
    """每页写一个字节，让系统真的把页算进工作集。"""
    if not buf:
        return
    for index in range(0, len(buf), _PAGE):
        buf[index] = 1
