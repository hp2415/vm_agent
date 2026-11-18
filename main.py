"""访客机无界面 Agent 入口。

运行前提：
- Windows 交互桌面会话（勿锁屏）
- 能访问宿主机 agent_ws_port（默认 18765）
- 启用微信时：微信 4.1.9 已登录且无障碍树可见（mmui::MainWindow）

用法：
  python main.py                 # 静默启动：通知区域图标，不监听微信
  python main.py --config config.yaml
  python main.py --wechat        # 打开微信监听和外发
  python main.py --console       # 传统控制台。关掉窗口会退出进程
  python main.py --workload      # 约每 90 秒占用键鼠 8 秒，模拟外发。不往微信写字
  python main.py --load --workload   # 不打开微信，改用 CPU/内存模拟看宿主机负载
  python main.py --autostart install   # 登录自启（默认不监听微信）
  python main.py --autostart uninstall
  python main.py --autostart status
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import faulthandler
import logging
import signal
import sys
import threading
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

from agent.autostart import (
    extra_args_from_config,
    install as autostart_install,
    status as autostart_status,
    uninstall as autostart_uninstall,
)
from agent.service import AgentService
from agent.win_console import (
    attach_console,
    console_hwnd,
    disable_quick_edit,
    pause_console,
    prepare_windowed_stdio,
    release_console,
    spawn_silent_and_exit,
    stream_log_enabled,
)


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


def resolve_config_path(args_config: str, default_cfg: Path) -> Path:
    cfg_path = Path(args_config)
    if not cfg_path.is_absolute():
        cfg_path = (Path.cwd() / cfg_path).resolve()
    if not cfg_path.exists() and default_cfg.exists():
        logging.getLogger(__name__).info(
            "未找到 %s，改用 %s", args_config, default_cfg
        )
        return default_cfg
    return cfg_path


def handle_autostart(action: str, cfg: dict, args: argparse.Namespace) -> int:
    """处理 --autostart install|uninstall|status。"""
    if action == "status":
        cmd = autostart_status()
        if cmd:
            _tell(f"已启用自启: {cmd}")
        else:
            _tell("未启用自启")
        return 0
    if action == "uninstall":
        autostart_uninstall()
        return 0
    if action == "install":
        extras = list(extra_args_from_config(cfg))
        # 命令行开关覆盖/追加到注册命令（与当前这次启动意图一致）
        if args.wechat and "--wechat" not in extras:
            extras.append("--wechat")
            extras = [item for item in extras if item != "--no-wechat"]
        if args.no_wechat and "--no-wechat" not in extras:
            extras.append("--no-wechat")
            extras = [item for item in extras if item != "--wechat"]
        if args.workload and "--workload" not in extras:
            extras.append("--workload")
        if args.load and "--load" not in extras:
            extras.append("--load")
        if args.no_remote and "--no-remote" not in extras:
            extras.append("--no-remote")
        if args.console and "--console" not in extras:
            extras.append("--console")
        if args.config and Path(args.config).resolve() != (ROOT / "config.yaml").resolve():
            extras = ["--config", str(Path(args.config).resolve()), *extras]
        cmd = autostart_install(extras)
        _tell(f"已写入登录自启: {cmd}\n下次用户登录后生效；改参数请重新 install，关闭请 uninstall。")
        return 0
    _tell(f"未知 --autostart 动作: {action}")
    return 2


def _tell(msg: str) -> None:
    """有控制台就打印；窗口程序则弹出提示，避免静默 exe 把结果丢掉。"""
    if sys.stdout is not None:
        try:
            print(msg)
        except Exception:  # noqa: BLE001
            pass
    # 窗口程序的 stdout 是空设备，打印成功也看不见，改用对话框。
    if sys.platform != "win32" or console_hwnd() or stream_log_enabled():
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    user32.MessageBoxW.restype = ctypes.c_int
    # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND
    user32.MessageBoxW(None, msg, "VM Agent", 0x00010040)


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if stream_log_enabled():
        stream = logging.StreamHandler()
        stream.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(stream)
    _install_crash_log()


def _install_crash_log() -> None:
    """闪退时窗口会关掉。把未捕获异常写到 exe 旁的 agent.log。"""
    log_path = ROOT / "agent.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(file_handler)
    crash_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    faulthandler.enable(crash_file, all_threads=True)

    def _excepthook(exc_type, exc, tb) -> None:
        logging.getLogger("vm_agent").critical(
            "未捕获异常，进程退出", exc_info=(exc_type, exc, tb)
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook


def main() -> int:
    default_cfg = ROOT / "config.yaml"
    parser = argparse.ArgumentParser(description="Console VM Agent")
    parser.add_argument(
        "--config",
        default=str(default_cfg),
        help=f"配置文件路径（默认: {default_cfg}）",
    )
    parser.add_argument(
        "--wechat",
        action="store_true",
        help="启用微信监听和外发。默认关闭，只做心跳和远控",
    )
    parser.add_argument(
        "--no-wechat",
        action="store_true",
        help="明确关闭微信监听（默认已经关闭）",
    )
    parser.add_argument(
        "--no-remote",
        action="store_true",
        help="禁用画面采集",
    )
    parser.add_argument(
        "--workload",
        action="store_true",
        help="模拟外发占用键鼠（约每 90 秒握住 8 秒），不往微信会话写字",
    )
    parser.add_argument(
        "--load",
        action="store_true",
        help="模拟正常使用的 CPU 和内存（约 400 MB、一个处理器的 8%%）。微信已登录时不要开",
    )
    parser.add_argument(
        "--autostart",
        choices=("install", "uninstall", "status"),
        metavar="ACTION",
        help="登录自启：install / uninstall / status（写 HKCU Run，无需管理员）",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="显示传统控制台。关掉该窗口会退出进程。默认静默驻留在通知区域",
    )
    hold_help = prepare_windowed_stdio()
    try:
        args = parser.parse_args()
    except SystemExit:
        if hold_help:
            pause_console()
        raise

    if not args.console and not args.autostart:
        # 有可见控制台时改由无窗口子进程接手，当前进程退出。
        # 子进程若仍被挂上控制台，摘掉它，避免用户点关闭就把 Agent 杀掉。
        if spawn_silent_and_exit():
            return 0
        release_console()

    if args.console:
        attach_console()
    _setup_logging()

    cfg_path = resolve_config_path(args.config, default_cfg)
    cfg = load_config(cfg_path)

    if args.autostart:
        return handle_autostart(args.autostart, cfg, args)

    if console_hwnd():
        disable_quick_edit()

    tray = None
    quit_requested = threading.Event()
    service_box: list[AgentService] = []

    def _request_quit() -> None:
        quit_requested.set()
        if service_box:
            service_box[0].stop()

    if sys.platform == "win32" and not args.console:
        started = None
        try:
            from agent.tray import TrayApp

            started = TrayApp(on_quit=_request_quit)
            started.start()
            tray = started
        except Exception:
            logging.getLogger(__name__).exception(
                "托盘启动失败，进程继续运行但没有通知区域图标"
            )
            if started is not None:
                started.shutdown()
            tray = None

    try:
        # 配置里的 enable_wechat 不再单独打开监听，必须带 --wechat。
        cfg["enable_wechat"] = bool(args.wechat) and not args.no_wechat
        if args.no_remote:
            cfg["enable_remote"] = False
        # 外发占用、资源占用是压测。只认本次命令行，配置文件里写了也不启动。
        cfg["workload"] = bool(args.workload)
        cfg["load"] = bool(args.load)

        host = cfg.get("host") or "127.0.0.1"
        port = cfg.get("port") or 18765
        vm_id = str(cfg.get("vm_id") or "VM-01")
        log = logging.getLogger(__name__)
        log.info("目标 Console: ws://%s:%s/v1/agent  vm_id=%s", host, port, vm_id)
        log.info(
            "微信监听: %s",
            "开启" if cfg.get("enable_wechat") else "关闭（需要时加 --wechat）",
        )
        log.info(
            "占用测试: 外发占用 %s，资源占用 %s",
            "开" if args.workload else "关（默认）",
            "开" if args.load else "关（默认）",
        )
        if str(host).strip() in ("127.0.0.1", "localhost"):
            log.warning(
                "host 为本机回环地址：在虚拟机内将连到访客机自己，"
                "请改为物理机 Host-Only/NAT 网关 IP（如 192.168.159.1）"
            )
        if tray is not None:
            tray.set_title(f"VM Agent {vm_id}（关闭窗口不退出）")
            tray.set_tip(f"VM Agent {vm_id}（单击显示控制台）")
            log.info(
                "已静默启动，图标在任务栏通知区域。"
                "关闭日志窗口只会收回托盘；结束进程请右键图标选择「退出」。"
            )
        if quit_requested.is_set():
            return 0

        service = AgentService(cfg, health_dir=ROOT)
        service_box.append(service)
        if quit_requested.is_set():
            service.stop()
            return 0

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
        except Exception:
            logging.getLogger(__name__).exception("Agent 异常退出")
            return 1
        return 0
    finally:
        if tray is not None:
            tray.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
