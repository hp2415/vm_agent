"""监听与外发互斥锁。"""

from __future__ import annotations

import threading

# 全局 RPA 锁：listener 点会话 / controller 发送 互斥
rpa_lock = threading.RLock()
