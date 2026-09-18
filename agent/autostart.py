"""Windows 登录自启：写入/清除 HKCU\\...\\Run（无需管理员，走交互桌面）。"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ConsoleVmAgent"


def _launch_prefix() -> list[str]:
    """返回启动命令前缀（可执行文件 + 若源码运行则含 main.py）。"""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return [str(Path(sys.executable).resolve()), str(main_py)]


def build_command(extra_args: list[str] | None = None) -> str:
    """拼出注册表里的命令行（带引号，可含 --no-wechat 等）。"""
    parts = _launch_prefix()
    for a in extra_args or []:
        a = str(a).strip()
        if a:
            parts.append(a)
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def install(extra_args: list[str] | None = None) -> str:
    """写入当前用户 Run 项，返回实际命令行。"""
    cmd = build_command(extra_args)
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    logger.info("已启用登录自启 (%s): %s", VALUE_NAME, cmd)
    return cmd


def uninstall() -> bool:
    """删除 Run 项；不存在则返回 False。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        logger.info("已关闭登录自启 (%s)", VALUE_NAME)
        return True
    except FileNotFoundError:
        logger.info("未注册自启 (%s)，无需卸载", VALUE_NAME)
        return False


def status() -> str | None:
    """当前注册的命令行；未安装返回 None。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ
        ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None


def extra_args_from_config(cfg: dict) -> list[str]:
    """从 config.autostart.extra_args 读取默认附加参数。"""
    block = cfg.get("autostart")
    if not isinstance(block, dict):
        return []
    raw = block.get("extra_args")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p for p in raw.split() if p]
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return []
