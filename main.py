"""访客机无界面 Agent 入口。

运行前提：
- Windows 交互桌面会话（勿锁屏）
- 能访问宿主机 agent_ws_port（默认 18765）
- 启用微信时：微信 4.1.9 已登录且无障碍树可见（mmui::MainWindow）

用法：
  python main.py
  python main.py --config config.yaml
  python main.py --no-wechat   # 仅握手/心跳/画面，便于本机联调
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml


def app_dir() -> Path:
    """源码运行：本文件目录；PyInstaller 打包：exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# onedir 时模块在 _internal
_internal = ROOT / "_internal"
if _internal.is_dir() and str(_internal) not in sys.path:
    sys.path.insert(0, str(_internal))

from agent.service import AgentService
from agent.win_console import disable_quick_edit


def load_config(path: Path) -> dict:
    if not path.exists():
        logging.getLogger(__name__).warning(
            "配置文件不存在: %s （将使用默认 host=127.0.0.1）", path
        )
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    logging.getLogger(__name__).info("已加载配置: %s", path)
    return data


def main() -> int:
    default_cfg = ROOT / "config.yaml"
    parser = argparse.ArgumentParser(description="Console VM Agent")
    parser.add_argument(
        "--config",
        default=str(default_cfg),
        help=f"配置文件路径（默认: {default_cfg}）",
    )
    parser.add_argument(
        "--no-wechat",
        action="store_true",
        help="禁用微信 UIA（空跑握手/心跳/远程画面）",
    )
    parser.add_argument(
        "--no-remote",
        action="store_true",
        help="禁用画面采集",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    disable_quick_edit()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (Path.cwd() / cfg_path).resolve()
    # 相对路径找不到时，回退到 exe 旁 config.yaml
    if not cfg_path.exists() and default_cfg.exists():
        logging.getLogger(__name__).info(
            "未找到 %s，改用 %s", args.config, default_cfg
        )
        cfg_path = default_cfg

    cfg = load_config(cfg_path)
    if args.no_wechat:
        cfg["enable_wechat"] = False
    if args.no_remote:
        cfg["enable_remote"] = False

    host = cfg.get("host") or "127.0.0.1"
    port = cfg.get("port") or 18765
    logging.getLogger(__name__).info(
        "目标 Console: ws://%s:%s/v1/agent  vm_id=%s",
        host,
        port,
        cfg.get("vm_id") or "VM-01",
    )
    if str(host).strip() in ("127.0.0.1", "localhost"):
        logging.getLogger(__name__).warning(
            "host 为本机回环地址：在虚拟机内将连到访客机自己，"
            "请改为物理机 Host-Only/NAT 网关 IP（如 192.168.159.1）"
        )

    service = AgentService(cfg)

    def _stop(*_a):
        service.stop()

    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except Exception:  # noqa: BLE001
        pass

    try:
        asyncio.run(service.run_forever())
    except KeyboardInterrupt:
        service.stop()
    except asyncio.CancelledError:
        # Console 断开不应结束进程；仅主动 stop 时退出
        if not service._stop.is_set():  # noqa: SLF001
            logging.getLogger(__name__).warning("unexpected cancel; exiting")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
