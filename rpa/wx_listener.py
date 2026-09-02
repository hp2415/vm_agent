"""微信 4.1.9 消息监听（uiautomation，不依赖 wxautox4）。

对照 wxautox4 的 AddListenChat / GetNextNewMessage：
- 白名单：只处理 listen 列表里的会话（等同 AddListenChat）
- 黑名单：处理全部会话，排除 listen 列表（会话列表巡检 + 过滤）

原理：轮询主窗口左侧「会话」列表的未读角标，点进匹配会话，
对比右侧「消息」列表快照，把增量交给回调。

建议把白名单对象置顶，max_pages=0 时只扫可见区域，更稳。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

import uiautomation as auto
from loguru import logger
from PIL import Image

from rpa.wechat_controller import (
    _SW_RESTORE,
    _find_wechat_hwnd,
    _force_activate_wechat_hwnd,
    _hwnd_title,
    _user32,
)

# ---------------------------------------------------------------------------
# 微信 4.1.9 控件（简体中文）
# ---------------------------------------------------------------------------
WX_VERSION = "4.1.9"

CLASS_MAIN_WINDOW = "mmui::MainWindow"
CLASS_SESSION_CELL = "mmui::ChatSessionCell"
CLASS_CHAT_ITEM = "mmui::ChatItemView"
CLASS_MSG_LIST = "mmui::StickyHeaderRecyclerListView"
CLASS_TAB_ITEM = "mmui::XTabBarItem"
CLASS_CHAT_INPUT = "mmui::ChatInputField"
CLASS_CHAT_ITEMS = (
    "mmui::ChatItemView",
    "mmui::ChatTextItemView",
    "mmui::ChatBubbleItemView",
    "mmui::ChatBubbleReferItemView",
    "mmui::ChatPersonalCardItemView",
)

NAME_SESSION_LISTS = ("会话", "Chats", "对话")
NAME_MSG_LISTS = ("消息", "Messages", "訊息")
NAME_SIDEBAR_CHATS = ("微信", "Weixin", "Chats", "WeChat")
NAME_MUTE = "消息免打扰"
SESSION_META_LINES = frozenset(
    {
        "已置顶",
        "置顶",
        "消息免打扰",
        "折叠的聊天",
        "已折叠",
        "Pinned",
        "Mute notifications",
    }
)
MAX_CHATS_PER_POLL = 6
EMIT_DEDUPE_S = 75.0

# UIA FullDescription，侧栏「微信」按钮上会带总未读数
UIA_FULL_DESCRIPTION = 30159

RE_UNREAD = re.compile(r"\[(\d+)\s*(?:条|則)?\]")
RE_GROUP_COUNT = re.compile(r"\(\d+\)$")
# 会话列表预览：群聊常见「昵称: 正文」
RE_PREVIEW_SENDER = re.compile(r"^([^:：\n]{1,32})[:：][ \t]+(.+)$", re.S)
_BUBBLE_CAPTURE_PATH = os.path.join(tempfile.gettempdir(), "wxrpa_bubble_probe.png")
_AVATAR_STRIP_PX = 72
_AVATAR_DIST_THR = 36.0
_AVATAR_MIN_PIXELS = 20
_WX_BG_COLORS = ((0xFA, 0xFA, 0xFA), (0x1E, 0x1E, 0x1F), (0xF5, 0xF5, 0xF5))
RE_SESSION_TIME = re.compile(
    r"(?:\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}|\d{1,2}:\d{2}|昨天 \d{1,2}:\d{2}|星期\w)$"
)
RE_MSG_TIME = re.compile(
    r"^(?:\d{4}年\d{1,2}月\d{1,2}日\s星期\w\s\d{1,2}:\d{2}"
    r"|\d{1,2}月\d{1,2}日\s星期\w\s\d{1,2}:\d{2}"
    r"|\d{1,2}月\d{1,2}日\s\d{1,2}:\d{2}"
    r"|昨天\s\d{1,2}:\d{2}"
    r"|星期\w\s\d{1,2}:\d{2}"
    r"|\d{1,2}:\d{2})$"
)

DEFAULT_SKIP_CHATS = (
    "微信团队",
    "微信支付",
    "订阅号",
    "腾讯新闻",
    "服务通知",
    "微信游戏",
    "公众号",
    "服务号",
)

MSG_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("[图片]", "image"),
    ("[视频]", "video"),
    ("[动画表情]", "emoji"),
    ("[表情]", "emoji"),
    ("[文件]", "file"),
    ("[链接]", "link"),
    ("[视频号]", "channel"),
    ("[聊天记录]", "chat_history"),
    ("[小程序]", "miniprogram"),
    ("[音乐]", "music"),
    ("[位置]", "location"),
    ("微信红包", "redpacket"),
    ("微信转账", "transfer"),
    ("语音", "voice"),
)

SYSTEM_HINTS = (
    "撤回了一条消息",
    "拍了拍",
    "加入了群聊",
    "退出了群聊",
    "修改群名为",
    "邀请你加入了群聊",
    "以上是打招呼的内容",
    "消息已发出，但被对方拒收",
)


class ListenMode(str, Enum):
    """白名单：只听列表内；黑名单：听全部但排除列表。"""

    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"


MessageCallback = Callable[["WxMessage", "ListenChat"], None]


@dataclass
class WxMessage:
    content: str
    type: str = "text"
    sender: str = ""
    chat_name: str = ""
    is_self: bool = False
    is_group: bool = False
    attr: str = "friend"  # friend / self / time / system

    def __str__(self) -> str:
        who = "我" if self.is_self else (self.sender or self.chat_name)
        return f"[{self.type}] {who}: {self.content}"


@dataclass
class SessionInfo:
    name: str
    unread: int = 0
    muted: bool = False
    preview: str = ""
    control: object | None = field(default=None, repr=False)


@dataclass
class ListenChat:
    """回调里拿到的会话句柄（当前版本只带名称，回复请自行入队）。"""

    name: str
    is_group: bool = False

    def __str__(self) -> str:
        return self.name


def _as_names(who: str | Iterable[str]) -> list[str]:
    if isinstance(who, str):
        items = [who]
    else:
        items = list(who)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = (item or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _norm_chat_name(name: str) -> str:
    s = (name or "").strip()
    s = RE_GROUP_COUNT.sub("", s).strip()
    return s


def _name_matches(target: str, actual: str) -> bool:
    a = _norm_chat_name(target).lower()
    b = _norm_chat_name(actual).lower()
    if not a or not b:
        return False
    return a == b


def _detect_msg_type(content: str) -> str:
    if not content:
        return "unknown"
    if RE_MSG_TIME.match(content.strip()):
        return "time"
    if any(h in content for h in SYSTEM_HINTS):
        return "sys"
    for hint, typ in MSG_TYPE_HINTS:
        if hint in content:
            return typ
    if content.startswith("文件\n"):
        return "file"
    return "text"


def _msg_key(msg: WxMessage) -> str:
    return f"{int(msg.is_self)}|{msg.attr}|{msg.content[:160]}"


def _parse_session_preview(preview: str) -> tuple[str, str]:
    """会话列表预览「波松: 正文」→ (波松, 正文)；无发送人前缀则 sender 为空。"""
    text = (preview or "").strip()
    if not text:
        return "", ""
    m = RE_PREVIEW_SENDER.match(text)
    if not m:
        return "", text
    who, body = m.group(1).strip(), m.group(2).strip()
    if not who or who in ("我", "self") or who.startswith("["):
        return "", body or text
    if who.startswith("引用") or "的消息" in who:
        return "", body or text
    return who, body


def _color_dist2(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    return float((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2)


def _count_non_bg(img: Image.Image, box: tuple[int, int, int, int], bgs: list[tuple[int, int, int]]) -> int:
    region = img.crop(box).convert("RGB")
    data = region.tobytes()
    thr2 = _AVATAR_DIST_THR * _AVATAR_DIST_THR
    n = 0
    for i in range(0, len(data), 3):
        p = (data[i], data[i + 1], data[i + 2])
        if all(_color_dist2(p, bg) > thr2 for bg in bgs):
            n += 1
    return n


def _is_meta_preview(preview: str) -> bool:
    text = (preview or "").strip()
    return not text or text in SESSION_META_LINES


def _tail_unread(curr: list[WxMessage], unread_hint: int) -> list[WxMessage]:
    n = min(max(unread_hint, 1), len(curr))
    return [m for m in curr[-n:] if m.attr not in ("time",)]


def _diff_new_messages(
    prev_keys: list[str] | None,
    curr: list[WxMessage],
    *,
    unread_hint: int = 0,
    same_chat: bool = False,
) -> list[WxMessage]:
    """对比两次可见消息列表，返回增量。

    无重叠时绝不能把当前窗口最后几条当新消息：手动切会话后气泡会先变、
    标题后变，否则会把 B 的历史写进 A。
    """
    curr_keys = [_msg_key(m) for m in curr]
    if not prev_keys:
        if unread_hint > 0 and curr:
            return _tail_unread(curr, unread_hint)
        return []
    if curr_keys == prev_keys:
        return []

    for n in range(min(len(prev_keys), len(curr_keys)), 0, -1):
        suffix = prev_keys[-n:]
        for i in range(len(curr_keys) - n + 1):
            if curr_keys[i : i + n] == suffix:
                return [m for m in curr[i + n :] if m.attr not in ("time",)]

    prev_set = set(prev_keys)
    novel = [m for m in curr if _msg_key(m) not in prev_set and m.attr not in ("time",)]
    if unread_hint > 0:
        return _tail_unread(curr, unread_hint)
    # 同一会话内列表被刷掉（可见区全换了）才把新增键当增量；切会话禁止这条路径
    if same_chat and novel and len(novel) < len(curr):
        return novel
    return []


class WxListener:
    """基于 uiautomation 的简易消息监听，适配微信 4.1.9。"""

    def __init__(
        self,
        mode: ListenMode | str = ListenMode.WHITELIST,
        *,
        interval: float = 1.2,
        filter_mute: bool = False,
        skip_self: bool = True,
        max_pages: int = 0,
        default_callback: MessageCallback | None = None,
    ) -> None:
        self.mode = ListenMode(mode)
        self.interval = max(0.3, float(interval))
        self.filter_mute = filter_mute
        self.skip_self = skip_self
        self.max_pages = max(0, int(max_pages))
        self.default_callback = default_callback

        self._names: set[str] = set()
        self._callbacks: dict[str, MessageCallback] = {}
        self._skip_builtin = {n.lower() for n in DEFAULT_SKIP_CHATS}

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._uia_hwnd = 0
        self.wechat_window = None
        self._snapshots: dict[str, list[str]] = {}
        self._session_previews: dict[str, str] = {}
        self._avatar_cache: dict[tuple, bool] = {}
        self._recent_emitted: dict[str, float] = {}
        self._last_dedupe_purge = 0.0
        self._current_chat = ""
        self._active_chat = ""
        self.max_chats_per_poll = MAX_CHATS_PER_POLL
        self._restored_once = False
        self._last_diag = 0.0
        self._polls = 0
        self.verbose = False

    # ----- 名单 -----
    def add_listen_chat(
        self,
        who: str | Iterable[str],
        callback: MessageCallback | None = None,
    ) -> list[str]:
        """加入监听名单。白名单=要听的人；黑名单=要排除的人。"""
        names = _as_names(who)
        cb = callback or self.default_callback
        with self._lock:
            for name in names:
                self._names.add(name)
                if cb is not None:
                    self._callbacks[name] = cb
        logger.info(f"[{self.mode.value}] 已加入监听名单: {names}")
        return names

    def remove_listen_chat(self, who: str | Iterable[str]) -> None:
        names = _as_names(who)
        with self._lock:
            for name in names:
                self._names.discard(name)
                self._callbacks.pop(name, None)
                self._snapshots.pop(_norm_chat_name(name), None)
        logger.info(f"已移出监听名单: {names}")

    def set_callback(self, callback: MessageCallback | None) -> None:
        self.default_callback = callback

    def listen_names(self) -> list[str]:
        with self._lock:
            return sorted(self._names)

    def _allowed(self, chat_name: str) -> bool:
        name = _norm_chat_name(chat_name)
        if not name:
            return False
        if name.lower() in self._skip_builtin:
            return False
        with self._lock:
            listed = any(_name_matches(n, name) for n in self._names)
        if self.mode is ListenMode.WHITELIST:
            return listed
        return not listed

    def _callback_for(self, chat_name: str) -> MessageCallback | None:
        with self._lock:
            for n, cb in self._callbacks.items():
                if _name_matches(n, chat_name):
                    return cb
        return self.default_callback

    # ----- 窗口 -----
    def _ui_tree_visible(self) -> bool:
        """微信 4.x：UI 树可见时 UIA ClassName 会变成 mmui::MainWindow。"""
        win = self.wechat_window
        if win is None:
            return False
        try:
            cls = win.ClassName or ""
            if "mmui::" in cls:
                return True
        except Exception:
            pass
        return self._session_list() is not None

    def _bind_window(self, *, activate: bool = False) -> bool:
        hwnd = _find_wechat_hwnd()
        if not hwnd:
            self._diag("未找到微信主窗口，请确认 4.1.9 客户端已登录且未最小化到托盘")
            return False
        self._hwnd = hwnd

        if _user32.IsIconic(hwnd) or activate:
            if _user32.IsIconic(hwnd):
                logger.info("微信主窗口已最小化，正在还原")
            _force_activate_wechat_hwnd(
                hwnd, None, attempts=2, settle_s=0.25, heavy=True
            )
            time.sleep(0.35)
            self.wechat_window = None
            self._uia_hwnd = 0
            self._restored_once = True

        if (
            self._uia_hwnd == hwnd
            and self.wechat_window is not None
            and self._ui_tree_visible()
        ):
            return True

        try:
            win = auto.ControlFromHandle(hwnd)
        except Exception as e:
            logger.error(f"HWND 转 UIA 失败: {e}")
            self.wechat_window = None
            self._uia_hwnd = 0
            return False
        if not win:
            return False
        self.wechat_window = win
        self._uia_hwnd = hwnd
        return True

    def _diag(self, msg: str, *, every_s: float = 8.0) -> None:
        now = time.monotonic()
        if now - self._last_diag < every_s:
            return
        self._last_diag = now
        logger.warning(msg)

    def _switch_to_chat_tab(self) -> None:
        if not self.wechat_window:
            return
        if self._session_list() is not None:
            return
        try:
            for name in NAME_SIDEBAR_CHATS:
                btn = self.wechat_window.ButtonControl(
                    Name=name, ClassName=CLASS_TAB_ITEM, searchDepth=8
                )
                if btn.Exists(0.05):
                    btn.Click(simulateMove=False, waitTime=0.05)
                    return
        except Exception as e:
            logger.debug(f"切换到聊天页失败: {e}")

    def _sidebar_unread(self) -> int:
        if not self.wechat_window:
            return 0
        try:
            for name in NAME_SIDEBAR_CHATS:
                btn = self.wechat_window.ButtonControl(
                    Name=name, ClassName=CLASS_TAB_ITEM, searchDepth=8
                )
                if not btn.Exists(0):
                    continue
                desc = ""
                try:
                    desc = str(btn.GetPropertyValue(UIA_FULL_DESCRIPTION) or "")
                except Exception:
                    desc = btn.Name or ""
                m = re.search(r"\d+", desc)
                return int(m.group(0)) if m else 0
        except Exception:
            return 0
        return 0

    def _session_list(self):
        if not self.wechat_window:
            return None
        for name in NAME_SESSION_LISTS:
            try:
                lst = self.wechat_window.ListControl(Name=name, searchDepth=12)
                if lst.Exists(0.05):
                    return lst
            except Exception:
                continue
        try:
            cell = self.wechat_window.ListItemControl(
                ClassName=CLASS_SESSION_CELL, searchDepth=16
            )
            if cell.Exists(0.05):
                parent = cell.GetParentControl()
                while parent is not None:
                    if parent.ControlTypeName in ("ListControl", "List"):
                        return parent
                    parent = parent.GetParentControl()
        except Exception:
            pass
        return None

    def _message_list(self):
        """定位右侧聊天消息列表，避开左侧会话列表。"""
        if not self.wechat_window:
            return None
        for name in NAME_MSG_LISTS:
            try:
                lst = self.wechat_window.ListControl(Name=name, searchDepth=14)
                if lst.Exists(0.05) and not self._is_session_list(lst):
                    return lst
            except Exception:
                continue
        # 同 ClassName 的 Recycler 往往有两个：第 1 个是会话，第 2 个才是消息
        for idx in (2, 1):
            try:
                lst = self.wechat_window.ListControl(
                    ClassName=CLASS_MSG_LIST, searchDepth=14, foundIndex=idx
                )
                if lst.Exists(0.05) and not self._is_session_list(lst):
                    return lst
            except Exception:
                continue
        try:
            lst = self.wechat_window.ListControl(
                ClassName="mmui::ListView", searchDepth=14
            )
            if lst.Exists(0.05) and not self._is_session_list(lst):
                return lst
        except Exception:
            pass
        return None

    def _is_session_list(self, lst) -> bool:
        try:
            name = lst.Name or ""
            if name in NAME_SESSION_LISTS:
                return True
            for item in lst.GetChildren()[:6]:
                cls = item.ClassName or ""
                if CLASS_SESSION_CELL in cls or "ChatSession" in cls:
                    return True
        except Exception:
            pass
        return False

    def _focus_message_pane(self) -> None:
        """把焦点放到消息列表，禁止点对话框中部（会误触链接/卡片/按钮）。"""
        if not self.wechat_window:
            return
        lst = self._message_list()
        if lst is not None:
            try:
                lst.SetFocus()
                return
            except Exception:
                pass
            try:
                rect = lst.BoundingRectangle
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 40 and h > 40:
                    # 点列表内侧左上空白，避开气泡、链接卡片和底部输入区
                    x = int(rect.left + min(28, w * 0.08))
                    y = int(rect.top + min(16, h * 0.04))
                    auto.Click(x, y, waitTime=0.04)
                    return
            except Exception:
                pass
        logger.debug("聚焦消息区失败：未点击对话框中部")

    def get_current_chat_name(self) -> str:
        if not self.wechat_window:
            return ""
        selected = self._selected_session_name()
        if selected:
            return selected
        try:
            win_rect = self.wechat_window.BoundingRectangle
            found: list[str] = []

            def walk(c, depth=0):
                if depth > 8 or len(found) >= 3:
                    return
                try:
                    rect = c.BoundingRectangle
                    if c is not self.wechat_window and rect.top > win_rect.top + 110:
                        return
                except Exception:
                    pass
                if c.ControlTypeName == "TextControl":
                    try:
                        rect = c.BoundingRectangle
                        if (
                            win_rect.top + 28 < rect.top < win_rect.top + 90
                            and win_rect.left + 180 < rect.left < win_rect.left + 520
                            and c.Name
                        ):
                            found.append(c.Name.strip())
                    except Exception:
                        pass
                for child in c.GetChildren():
                    walk(child, depth + 1)

            walk(self.wechat_window)
            skip = set(NAME_SIDEBAR_CHATS) | set(NAME_SESSION_LISTS) | {"搜索", "Search"}
            for name in found:
                if name and name not in skip:
                    return name.strip()
        except Exception as e:
            logger.debug(f"读取聊天标题失败: {e}")
        return self._selected_session_name()

    # ----- 会话列表 -----
    def get_sessions(self) -> list[SessionInfo]:
        lst = self._session_list()
        if lst is None:
            return []
        sessions: list[SessionInfo] = []
        try:
            for item in lst.GetChildren():
                info = self._parse_session_item(item)
                if info is not None:
                    sessions.append(info)
        except Exception as e:
            logger.debug(f"遍历会话列表失败: {e}")
        return sessions

    def _selected_session_name(self) -> str:
        for sess in self.get_sessions():
            ctrl = sess.control
            if ctrl is None:
                continue
            try:
                pattern = ctrl.GetSelectionItemPattern()
                if pattern and pattern.IsSelected:
                    return sess.name
            except Exception:
                pass
            try:
                if ctrl.HasKeyboardFocus:
                    return sess.name
            except Exception:
                pass
        return ""

    def _parse_session_item(self, item) -> SessionInfo | None:
        try:
            ctype = item.ControlTypeName or ""
            if ctype not in ("ListItemControl", "ListItem"):
                return None
            auto_id = item.AutomationId or ""
            raw = item.Name or ""
            if auto_id.startswith("session_item_"):
                name = auto_id[len("session_item_") :]
            else:
                first = (raw.split("\n", 1)[0] if raw else "").strip()
                name = RE_SESSION_TIME.sub("", first).strip()
            name = _norm_chat_name(name)
            if not name:
                return None
            unread_m = RE_UNREAD.search(raw)
            unread = int(next(g for g in unread_m.groups() if g)) if unread_m else 0
            muted = NAME_MUTE in raw
            preview = RE_UNREAD.sub("", raw)
            preview = preview.replace(NAME_MUTE + "\n", "")
            lines = [
                ln.strip()
                for ln in preview.split("\n")
                if ln.strip() and ln.strip() not in SESSION_META_LINES
            ]
            if lines and _norm_chat_name(lines[0]) == name:
                preview_text = "\n".join(lines[1:]).strip()
            elif len(lines) > 1:
                preview_text = "\n".join(lines[1:]).strip()
            else:
                preview_text = lines[0] if lines else ""
            if _is_meta_preview(preview_text):
                preview_text = ""
            return SessionInfo(
                name=name,
                unread=unread,
                muted=muted,
                preview=preview_text,
                control=item,
            )
        except Exception:
            return None

    # ----- 消息列表 -----
    def collect_messages(self, chat_name: str = "") -> list[WxMessage]:
        if not self.wechat_window:
            return []
        chat_name = chat_name or self._current_chat
        items = self._iter_message_items()
        if not items:
            return []
        pane_rect = self._chat_pane_rect(items)
        is_group = self._looks_like_group(chat_name)
        out: list[WxMessage] = []
        try:
            for item in items:
                content = (item.Name or "").strip()
                if not content:
                    content = self._walk_item_text(item)
                if not content:
                    continue
                msg_type = _detect_msg_type(content)
                if msg_type == "time":
                    attr = "time"
                    is_self = False
                    sender = ""
                elif msg_type == "sys":
                    attr = "sys"
                    is_self = False
                    sender = ""
                else:
                    is_self, sender, content = self._resolve_sender(
                        item, content, chat_name, is_group, pane_rect
                    )
                    if is_self:
                        attr = "self"
                        sender = "self"
                    else:
                        attr = "friend"
                        if not sender:
                            sender = _norm_chat_name(chat_name)

                out.append(
                    WxMessage(
                        content=content,
                        type=msg_type if msg_type != "time" else "time",
                        sender=sender,
                        chat_name=chat_name,
                        is_self=is_self,
                        is_group=is_group,
                        attr=attr,
                    )
                )
        except Exception as e:
            logger.debug(f"提取消息列表失败: {e}")
        return out

    def _chat_pane_rect(self, items: list):
        """消息区矩形。必须用列表/父级，不能用整个微信窗口（消息区本身就在右半边）。"""
        lst = self._message_list()
        if lst is not None:
            try:
                rect = lst.BoundingRectangle
                if rect.right - rect.left > 80:
                    return rect
            except Exception:
                pass
        if items:
            try:
                parent = items[0].GetParentControl()
                if parent is not None:
                    rect = parent.BoundingRectangle
                    if rect.right - rect.left > 80:
                        return rect
            except Exception:
                pass
        return self.wechat_window.BoundingRectangle

    def _resolve_sender(
        self,
        item,
        content: str,
        chat_name: str,
        is_group: bool,
        pane_rect,
    ) -> tuple[bool, str, str]:
        raw = (item.Name or content or "").strip()
        first = raw.split("\n", 1)[0].strip() if raw else ""
        friend = _norm_chat_name(chat_name)

        # 1) 文本前缀：对方/群成员名字在首行
        if friend and first and first != friend:
            if raw.startswith(friend) or first.startswith(friend):
                rest = raw[len(friend) :].lstrip(" \n")
                return False, friend, rest or content
        if friend and first == friend and "\n" in raw:
            rest = raw.split("\n", 1)[1].strip()
            return False, friend, rest or content

        if is_group:
            sender, body = self._split_group_sender(raw, True)
            if sender:
                return False, sender, body

        # 2) 几何 / 头像：4.1.9 的 ChatTextItemView 常横贯整行且无子节点，
        #    不能再用「中心在右半边」判断。头像在左=对方，在右=自己。
        is_self = self._is_outgoing_bubble(item, pane_rect)
        if is_self:
            return True, "self", content
        sender = friend if not is_group else ""
        if is_group:
            sender, body = self._split_group_sender(content, True)
            if sender:
                return False, sender, body
        return False, sender, content

    @staticmethod
    def _bubble_rect(item):
        """ListItem 往往横贯整行，真正的气泡是其中较窄的子控件。"""
        try:
            item_rect = item.BoundingRectangle
        except Exception:
            return None
        item_w = max(1, item_rect.right - item_rect.left)
        candidates: list[tuple[int, object]] = []

        def walk(c, depth=0):
            if depth > 6:
                return
            try:
                r = c.BoundingRectangle
                w = r.right - r.left
                h = r.bottom - r.top
                if w >= 16 and h >= 10 and w <= item_w * 0.82:
                    ctype = c.ControlTypeName or ""
                    score = w * h
                    if ctype in ("TextControl", "DocumentControl", "EditControl"):
                        score += 10_000_000
                    candidates.append((score, r))
                for child in c.GetChildren():
                    walk(child, depth + 1)
            except Exception:
                return

        walk(item)
        if not candidates:
            return item_rect
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _is_outgoing_bubble(self, item, pane_rect) -> bool:
        bubble = self._bubble_rect(item)
        if bubble is not None and pane_rect is not None:
            pane_w = max(1, pane_rect.right - pane_rect.left)
            bubble_w = bubble.right - bubble.left
            # 子气泡明显偏窄时，才用左右位置
            if bubble_w < pane_w * 0.82:
                pane_mid = pane_rect.left + pane_w * 0.5
                margin = pane_w * 0.12
                center = (bubble.left + bubble.right) / 2
                if center >= pane_mid + margin:
                    return True
                if center <= pane_mid - margin:
                    return False
        side = self._is_outgoing_by_avatar(item)
        if side is not None:
            return side
        # 监听场景：无法判断时默认对方，避免把来信当成自己而跳过
        return False

    def _capture_item_image(self, item) -> Image.Image | None:
        try:
            if item.CaptureToImage(_BUBBLE_CAPTURE_PATH):
                img = Image.open(_BUBBLE_CAPTURE_PATH).copy()
                return img.convert("RGB")
        except Exception:
            pass
        try:
            r = item.BoundingRectangle
            from PIL import ImageGrab

            img = ImageGrab.grab((r.left, r.top, r.right, r.bottom))
            return img.convert("RGB")
        except Exception:
            return None

    def _is_outgoing_by_avatar(self, item) -> bool | None:
        """比较行左右两侧非背景像素：右侧有头像=自己，左侧有头像=对方。"""
        try:
            r = item.BoundingRectangle
            cache_key = (
                (item.Name or "")[:80],
                int(r.left),
                int(r.top),
                int(r.right),
                int(r.bottom),
            )
        except Exception:
            cache_key = None
        if cache_key and cache_key in self._avatar_cache:
            return self._avatar_cache[cache_key]

        img = self._capture_item_image(item)
        if img is None:
            return None
        w, h = img.size
        if w < 80 or h < 18:
            return None
        bgs = list(_WX_BG_COLORS)
        try:
            bgs.append(img.getpixel((2, 2))[:3])
            bgs.append(img.getpixel((w - 3, 2))[:3])
        except Exception:
            pass
        strip = min(_AVATAR_STRIP_PX, max(40, w // 5))
        # 头像在行顶部，不能按整行高度统计，否则长文本会污染左右两侧
        avatar_h = min(52, max(22, h))
        left_n = _count_non_bg(img, (0, 0, strip, avatar_h), bgs)
        right_n = _count_non_bg(img, (w - strip, 0, w, avatar_h), bgs)
        result: bool | None
        if right_n >= _AVATAR_MIN_PIXELS and right_n > left_n * 1.5:
            result = True
        elif left_n >= _AVATAR_MIN_PIXELS and left_n > right_n * 1.5:
            result = False
        elif right_n >= _AVATAR_MIN_PIXELS and left_n < _AVATAR_MIN_PIXELS:
            result = True
        elif left_n >= _AVATAR_MIN_PIXELS and right_n < _AVATAR_MIN_PIXELS:
            result = False
        else:
            result = None
        if cache_key is not None and result is not None:
            if len(self._avatar_cache) > 120:
                self._avatar_cache.clear()
            self._avatar_cache[cache_key] = result
        if self.verbose:
            logger.debug(f"头像判定 left={left_n} right={right_n} self={result}")
        return result

    def _iter_message_items(self) -> list:
        msg_list = self._message_list()
        items: list = []
        if msg_list is not None:
            try:
                for item in msg_list.GetChildren():
                    ctype = item.ControlTypeName or ""
                    cls = item.ClassName or ""
                    if ctype in ("ListItemControl", "ListItem") or self._is_chat_item_class(cls):
                        items.append(item)
            except Exception as e:
                logger.debug(f"读取消息 List 子项失败: {e}")
        if items:
            return items
        return self._find_chat_item_controls()

    @staticmethod
    def _is_chat_item_class(cls: str) -> bool:
        if not cls:
            return False
        if cls in CLASS_CHAT_ITEMS:
            return True
        return cls.startswith("mmui::Chat") and "Session" not in cls and "Item" in cls

    def _find_chat_item_controls(self) -> list:
        if not self.wechat_window:
            return []
        found: list = []
        for cls in CLASS_CHAT_ITEMS:
            try:
                item = self.wechat_window.Control(ClassName=cls, searchDepth=18)
                if not item.Exists(0.05):
                    continue
                parent = item.GetParentControl()
                if parent is not None:
                    for child in parent.GetChildren():
                        if self._is_chat_item_class(child.ClassName or ""):
                            found.append(child)
                if found:
                    return found
                found.append(item)
                return found
            except Exception:
                continue
        return found

    @staticmethod
    def _looks_like_group(chat_name: str) -> bool:
        raw = (chat_name or "").strip()
        if RE_GROUP_COUNT.search(raw):
            return True
        return bool(re.search(r"(群|Group|Team|大会|大本营)$", raw, re.I))

    @staticmethod
    def _walk_item_text(item) -> str:
        texts: list[str] = []

        def walk(c, depth=0):
            if depth > 6:
                return
            try:
                if c.ControlTypeName in (
                    "TextControl",
                    "DocumentControl",
                    "EditControl",
                ):
                    name = (c.Name or "").strip()
                    if name and name not in ("重新发送", "重发"):
                        texts.append(name)
                for child in c.GetChildren():
                    walk(child, depth + 1)
            except Exception:
                return

        walk(item)
        return " ".join(texts).strip()

    @staticmethod
    def _split_group_sender(content: str, is_group: bool) -> tuple[str, str]:
        if not is_group or not content:
            return "", content
        first_line = content.split("\n", 1)[0].strip()
        if first_line.startswith("引用") or "的消息" in first_line:
            return "", content
        # 4.1.9 气泡 Name 通常只有正文、没有「昵称\n正文」。
        # 不能把多行消息的首行当成发送人（会把「不是。举个例」之类判成昵称）。
        who, body = _parse_session_preview(content)
        if who:
            return who, body
        return "", content

    # ----- 监听循环 -----
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning("监听已在运行")
                return
            if self.mode is ListenMode.WHITELIST and not self._names:
                logger.warning("白名单为空，不会收到任何消息；请先 add_listen_chat")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="wx-listener", daemon=True
            )
            self._thread.start()
        logger.info(
            f"监听已启动 mode={self.mode.value} interval={self.interval}s "
            f"微信={WX_VERSION} 名单={self.listen_names()}"
        )

    def stop(self, join_timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=join_timeout)
        logger.info("监听已停止")

    def keep_running(self) -> None:
        """阻塞主线程，直到 Ctrl+C。"""
        logger.info("KeepRunning 中，Ctrl+C 结束")
        try:
            while not self._stop.is_set():
                time.sleep(0.4)
        except KeyboardInterrupt:
            self.stop()

    def _loop(self) -> None:
        # uiautomation 必须在使用它的线程里 CoInitialize，否则 ControlFromHandle 会报
        # WinError -2147221008（尚未调用 CoInitialize）。
        with auto.UIAutomationInitializerInThread():
            primed = False
            while not self._stop.is_set():
                try:
                    if not self._bind_window(activate=False):
                        self._stop.wait(self.interval)
                        continue
                    self._switch_to_chat_tab()
                    if not primed:
                        self._prime_snapshots()
                        n_sess = len(self.get_sessions())
                        if n_sess:
                            primed = True
                            logger.info(
                                f"监听就绪 当前聊天={self._current_chat or '(未识别)'} "
                                f"会话数={n_sess} "
                                f"可见消息={len(self.collect_messages(self._current_chat))}"
                            )
                    self._poll_once()
                except Exception:
                    logger.exception("监听轮询异常")
                self._stop.wait(self.interval)

    def _prime_snapshots(self) -> None:
        """启动时先记下当前可见消息，避免把历史当新消息。"""
        self._current_chat = self.get_current_chat_name()
        self._active_chat = self._current_chat
        if self._current_chat and self._allowed(self._current_chat):
            msgs = self.collect_messages(self._current_chat)
            self._snapshots[_norm_chat_name(self._current_chat)] = [_msg_key(m) for m in msgs]
            logger.debug(
                f"已对当前会话建立快照: {self._current_chat} ({len(msgs)} 条可见)"
            )
        for sess in self.get_sessions():
            key = _norm_chat_name(sess.name)
            self._session_previews[key] = sess.preview
            # 未打开过的会话不要写空快照：[] 会被当成「已对比过」，切过去时会误回放

    def _poll_once(self) -> None:
        self._polls += 1
        sessions = self.get_sessions()
        if not sessions:
            uia_cls = ""
            try:
                uia_cls = self.wechat_window.ClassName if self.wechat_window else ""
            except Exception:
                pass
            self._diag(
                "当前微信窗口的 UI 树不可见，无法读取会话列表。"
                f" Win32标题={_hwnd_title(self._hwnd)!r} UIA ClassName={uia_cls!r}。"
                " 微信 4.1.9 只有对无障碍开放过的账号才会把控件暴露给 uiautomation"
                "（可见时应为 mmui::MainWindow，而不是 Qt51514QWindowIcon）。"
            )
            return

        current = self.get_current_chat_name()
        switched = bool(
            current
            and self._active_chat
            and not _name_matches(current, self._active_chat)
        )
        if current:
            self._current_chat = current
            sess_now = self._find_session_in(sessions, current)
            if switched:
                unread_n = sess_now.unread if sess_now else 0
                preview = sess_now.preview if sess_now else ""
                preview_changed = self._preview_changed(current, preview)
                logger.info(
                    f"检测到会话切换 {self._active_chat} → {current}"
                    f"{'（有未读，只取增量）' if unread_n or preview_changed else '，重建快照不回放历史'}"
                )
                if self._allowed(current) and (unread_n > 0 or preview_changed):
                    self._emit_from_open_chat(
                        current,
                        unread_hint=max(unread_n, 1),
                        preview=preview,
                        expected=current,
                        switched=True,
                        previous_chat=self._active_chat,
                    )
                    self._remember_preview(current, preview)
                elif self._allowed(current):
                    self._resync_snapshot(current)
                    self._remember_preview(current, preview)
                self._active_chat = current
            elif self._allowed(current):
                self._emit_from_open_chat(
                    current,
                    unread_hint=0,
                    expected=current,
                    switched=False,
                )
                self._active_chat = current
            else:
                self._active_chat = current
        elif self.verbose:
            logger.debug("未能识别当前聊天标题")

        unread_sessions = []
        for sess in self._iter_unread_sessions(sessions):
            if self.filter_mute and sess.muted:
                continue
            if not self._allowed(sess.name):
                continue
            if current and _name_matches(sess.name, current):
                continue
            unread_sessions.append(sess)

        processed = 0
        for sess in unread_sessions:
            if self._stop.is_set():
                return
            if processed >= self.max_chats_per_poll:
                logger.debug(
                    f"本轮已处理 {processed} 个未读会话，其余留到下一轮"
                )
                return
            fresh = self._find_session_by_name(sess.name) or sess
            logger.info(
                f"发现未读会话 {fresh.name} unread={fresh.unread} "
                f"preview={(fresh.preview or '')[:30]}"
            )
            if not self._open_session(fresh):
                continue
            opened = self._wait_until_chat(fresh.name, timeout=1.8)
            if not opened or not _name_matches(opened, fresh.name):
                logger.warning(
                    f"点击 [{fresh.name}] 后当前是 [{opened or '未识别'}]，跳过以免串会话"
                )
                continue
            self._focus_message_pane()
            try:
                self.wechat_window.SendKeys("{END}", waitTime=0.08)
            except Exception:
                pass
            time.sleep(0.18)
            opened = self.get_current_chat_name() or opened
            if not _name_matches(opened, fresh.name):
                logger.warning(
                    f"等待后会话仍不是 [{fresh.name}]（当前 [{opened}]），跳过"
                )
                continue
            previous_chat = self._active_chat
            self._current_chat = opened
            self._emit_from_open_chat(
                opened,
                unread_hint=max(fresh.unread, 1),
                preview=fresh.preview,
                expected=opened,
                switched=True,
                previous_chat=previous_chat,
            )
            self._active_chat = opened
            self._remember_preview(opened, fresh.preview)
            processed += 1

    def _iter_unread_sessions(self, sessions: list[SessionInfo] | None = None) -> list[SessionInfo]:
        found: dict[str, SessionInfo] = {}
        lst = self._session_list()
        if lst is None:
            return []

        def collect(items: list[SessionInfo]):
            for sess in items:
                key = _norm_chat_name(sess.name)
                preview_changed = self._preview_changed(sess.name, sess.preview)
                if sess.unread > 0 or preview_changed:
                    if preview_changed and sess.unread <= 0:
                        sess.unread = 1
                    found[key] = sess

        collect(sessions if sessions is not None else self.get_sessions())
        if self.max_pages <= 0:
            return list(found.values())

        try:
            lst.SetFocus()
        except Exception:
            pass
        for _ in range(self.max_pages):
            if self._stop.is_set():
                break
            try:
                lst.SendKeys("{PGDN}", waitTime=0.15)
            except Exception:
                break
            collect(self.get_sessions())
        try:
            lst.SendKeys("{HOME}", waitTime=0.1)
        except Exception:
            pass
        return list(found.values())

    def _find_session_in(
        self, sessions: list[SessionInfo], name: str
    ) -> SessionInfo | None:
        for sess in sessions:
            if _name_matches(sess.name, name):
                return sess
        return None

    def _find_session_by_name(self, name: str) -> SessionInfo | None:
        return self._find_session_in(self.get_sessions(), name)

    def _preview_changed(self, chat_name: str, preview: str) -> bool:
        if _is_meta_preview(preview):
            return False
        key = _norm_chat_name(chat_name)
        stored = self._session_previews.get(key)
        return bool(stored is not None and preview and preview != stored)

    def _remember_preview(self, chat_name: str, preview: str) -> None:
        if _is_meta_preview(preview):
            return
        self._session_previews[_norm_chat_name(chat_name)] = preview

    def _wait_until_chat(self, expected: str, timeout: float = 1.6) -> str:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            last = self.get_current_chat_name()
            if last and _name_matches(last, expected):
                time.sleep(0.12)
                return self.get_current_chat_name() or last
            time.sleep(0.08)
        return last

    def _resync_snapshot(self, chat_name: str) -> None:
        live = self.get_current_chat_name()
        if live and not _name_matches(live, chat_name):
            return
        msgs = self.collect_messages(chat_name)
        live = self.get_current_chat_name()
        if live and not _name_matches(live, chat_name):
            logger.info(f"重建快照时会话已变为 [{live}]，放弃写入 [{chat_name}]")
            return
        self._snapshots[_norm_chat_name(chat_name)] = [_msg_key(m) for m in msgs]
        logger.debug(f"已重建快照 {chat_name} ({len(msgs)} 条可见)")

    def _preview_matches_messages(self, preview: str, msgs: list[WxMessage]) -> bool:
        if _is_meta_preview(preview):
            return True
        if not msgs:
            return False
        _, body = _parse_session_preview(preview)
        needle = (body or preview).replace("\n", " ").strip()
        if len(needle) < 2:
            return True
        needle = needle[:24]
        for m in reversed(msgs[-6:]):
            hay = (m.content or "").replace("\n", " ")
            if needle in hay or hay[:24] in needle:
                return True
        return False

    def _pane_belongs_to_snapshot(self, chat_name: str, msgs: list[WxMessage]) -> bool:
        prev = self._snapshots.get(_norm_chat_name(chat_name))
        if not prev or not msgs:
            return False
        curr_keys = [_msg_key(m) for m in msgs]
        return bool(set(prev) & set(curr_keys))

    def _live_chat_is(self, expected: str) -> bool:
        live = self.get_current_chat_name()
        if not live:
            return True
        return _name_matches(live, expected)

    def _dedupe_allow(self, chat_name: str, msg: WxMessage) -> bool:
        now = time.monotonic()
        if now - self._last_dedupe_purge > 30:
            self._recent_emitted = {
                k: t for k, t in self._recent_emitted.items() if now - t < EMIT_DEDUPE_S
            }
            self._last_dedupe_purge = now
        key = f"{_norm_chat_name(chat_name)}|{int(msg.is_self)}|{msg.content[:120]}"
        prev = self._recent_emitted.get(key)
        if prev is not None and now - prev < EMIT_DEDUPE_S:
            return False
        self._recent_emitted[key] = now
        return True

    def _open_session(self, sess: SessionInfo) -> bool:
        ctrl = sess.control
        if ctrl is None:
            return False
        try:
            ctrl.Click(simulateMove=False, waitTime=0.08)
            return True
        except Exception as e:
            logger.debug(f"点击会话 {sess.name} 失败: {e}")
            try:
                rect = ctrl.BoundingRectangle
                auto.Click(
                    int((rect.left + rect.right) / 2),
                    int((rect.top + rect.bottom) / 2),
                )
                return True
            except Exception:
                return False

    def _emit_from_open_chat(
        self,
        chat_name: str,
        unread_hint: int,
        preview: str = "",
        *,
        expected: str = "",
        switched: bool = False,
        previous_chat: str = "",
    ) -> None:
        expected = expected or chat_name
        if not self._live_chat_is(expected):
            logger.info(
                f"期望 [{expected}] 但当前是 [{self.get_current_chat_name()}]，放弃读取"
            )
            return

        msgs: list[WxMessage] = []
        for attempt in range(5):
            if not self._live_chat_is(expected):
                logger.info(
                    f"读取 [{expected}] 过程中会话已切换为 "
                    f"[{self.get_current_chat_name()}]，丢弃本批"
                )
                return
            msgs = self.collect_messages(expected)
            if (
                switched
                and previous_chat
                and not _name_matches(previous_chat, expected)
                and self._pane_belongs_to_snapshot(previous_chat, msgs)
            ):
                time.sleep(0.18)
                continue
            if preview and not _is_meta_preview(preview):
                if msgs and self._preview_matches_messages(preview, msgs):
                    break
                time.sleep(0.2)
                continue
            if msgs:
                break
            time.sleep(0.2)
            if attempt == 1:
                self._focus_message_pane()

        if not self._live_chat_is(expected):
            logger.info(
                f"读完气泡后当前已不是 [{expected}]（"
                f"{self.get_current_chat_name()}），不写入快照"
            )
            return

        if (
            switched
            and previous_chat
            and not _name_matches(previous_chat, expected)
            and self._pane_belongs_to_snapshot(previous_chat, msgs)
        ):
            logger.info(
                f"[{expected}] 气泡仍像 [{previous_chat}]，跳过以免串会话"
            )
            return

        if (
            preview
            and not _is_meta_preview(preview)
            and msgs
            and not self._preview_matches_messages(preview, msgs)
        ):
            logger.info(
                f"[{expected}] 气泡与会话预览不一致，疑似还在上一个窗口，跳过"
            )
            return

        chat_name = expected
        key = _norm_chat_name(chat_name)
        msgs = self._annotate_from_preview(msgs, preview, chat_name)
        prev = self._snapshots.get(key)
        same_chat = (
            not switched
            and bool(self._active_chat)
            and _name_matches(self._active_chat, chat_name)
        )
        new_msgs = _diff_new_messages(
            prev, msgs, unread_hint=0, same_chat=same_chat
        )
        if not new_msgs and unread_hint > 0 and msgs:
            curr_keys = [_msg_key(m) for m in msgs]
            overlap = bool(prev and set(prev) & set(curr_keys))
            preview_ok = _is_meta_preview(preview) or self._preview_matches_messages(
                preview, msgs
            )
            if not prev:
                new_msgs = _tail_unread(msgs, unread_hint)
            elif overlap:
                novel = [
                    m
                    for m in msgs
                    if _msg_key(m) not in set(prev) and m.attr not in ("time",)
                ]
                new_msgs = novel[-unread_hint:] if len(novel) > unread_hint else novel
            elif preview_ok and not _is_meta_preview(preview):
                new_msgs = _tail_unread(msgs, unread_hint)
            else:
                logger.info(
                    f"[{chat_name}] 气泡与快照无交集，疑似串会话，保留原快照"
                )
                return

        self._snapshots[key] = [_msg_key(m) for m in msgs]
        if not new_msgs and not msgs and unread_hint > 0 and preview:
            who, body = _parse_session_preview(preview)
            new_msgs = [
                WxMessage(
                    content=body or preview,
                    type=_detect_msg_type(body or preview),
                    sender=who,
                    chat_name=chat_name,
                    is_self=False,
                    is_group=self._looks_like_group(chat_name),
                    attr="friend",
                )
            ]
            logger.info(f"[{chat_name}] 消息列表为空，改用会话预览: {preview[:60]}")
        if not new_msgs:
            if unread_hint > 0:
                logger.info(
                    f"[{chat_name}] 已打开会话但未读到新气泡 "
                    f"(list={'有' if self._message_list() is not None else '无'}, "
                    f"items={len(msgs)}, unread={unread_hint})"
                )
            return
        cb = self._callback_for(chat_name)
        if cb is None:
            logger.debug(f"{chat_name} 有新消息但未设置回调")
            return
        chat = ListenChat(name=chat_name, is_group=any(m.is_group for m in new_msgs))
        emitted = 0
        for msg in new_msgs:
            if msg.attr in ("time",):
                continue
            if self.skip_self and msg.is_self:
                logger.info(f"[{chat_name}] 跳过自身消息: {msg.content[:60]}")
                continue
            if not self._dedupe_allow(chat_name, msg):
                logger.debug(f"[{chat_name}] 去重跳过: {msg.content[:40]}")
                continue
            msg.chat_name = chat_name
            try:
                cb(msg, chat)
                emitted += 1
            except Exception:
                logger.exception(f"回调处理 {chat_name} 消息失败: {msg.content[:40]}")
        if emitted == 0 and new_msgs:
            logger.info(
                f"[{chat_name}] 检测到 {len(new_msgs)} 条变化，但均被过滤"
                "（自身消息可用 --include-self 查看）"
            )

    def _annotate_from_preview(
        self,
        msgs: list[WxMessage],
        preview: str,
        chat_name: str,
    ) -> list[WxMessage]:
        """群会话预览带「昵称: 正文」时，纠正最后一条被误判成自己的来信。"""
        who, body = _parse_session_preview(preview)
        if not who or not body or not msgs:
            return msgs
        is_group = self._looks_like_group(chat_name)
        for msg in reversed(msgs):
            if msg.attr in ("time", "sys"):
                continue
            c = (msg.content or "").replace("\n", " ").strip()
            b = body.replace("\n", " ").strip()
            if not c:
                continue
            if not (c in b or b in c or c[:48] == b[:48]):
                continue
            msg.is_self = False
            msg.sender = who
            msg.attr = "friend"
            if is_group:
                msg.is_group = True
            break
        return msgs

    # ----- 调试 -----
    def inspect(self) -> None:
        """打印当前会话列表和可见消息，便于对照 4.1.9 控件。"""
        if not self._bind_window(activate=True):
            return
        self._switch_to_chat_tab()
        time.sleep(0.2)
        print(f"微信版本目标: {WX_VERSION}")
        print(f"HWND=0x{self._hwnd:x} 标题={getattr(self.wechat_window, 'Name', '')}")
        print(f"侧栏未读约计: {self._sidebar_unread()}")
        print(f"当前聊天: {self.get_current_chat_name() or '(未识别)'}")
        print("\n--- 会话列表 ---")
        sessions = self.get_sessions()
        if not sessions:
            print("未找到会话列表。请还原微信主窗口；若仍为空，当前账号可能 UI 树不可见。")
        for s in sessions:
            flag = []
            if s.unread:
                flag.append(f"未读{s.unread}")
            if s.muted:
                flag.append("免打扰")
            extra = f" [{' '.join(flag)}]" if flag else ""
            print(f"  {s.name}{extra}  preview={s.preview[:40]}")
        print("\n--- 当前可见消息（末 8 条）---")
        msgs = self.collect_messages(self.get_current_chat_name())
        if not msgs:
            print("未找到消息列表。")
        for m in msgs[-8:]:
            who = "我" if m.is_self else (m.sender or m.chat_name)
            print(f"  is_self={m.is_self} attr={m.attr} {who}: {m.content[:80]}")


def _demo_callback(msg: WxMessage, chat: ListenChat) -> None:
    logger.info(f"[{chat.name}] {msg}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="微信 4.1.9 消息监听（uiautomation）")
    parser.add_argument(
        "--mode",
        choices=["whitelist", "blacklist"],
        default="whitelist",
        help="whitelist=只听名单内；blacklist=听全部并排除名单",
    )
    parser.add_argument(
        "--chat",
        action="append",
        default=[],
        help="名单中的会话名，可重复传入",
    )
    parser.add_argument("--interval", type=float, default=1.2)
    parser.add_argument("--max-pages", type=int, default=0, help="会话列表向下翻页次数")
    parser.add_argument("--filter-mute", action="store_true", help="跳过消息免打扰")
    parser.add_argument(
        "--include-self",
        action="store_true",
        help="连自己发出的消息也回调（文件传输助手自测常用）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出更多诊断日志")
    parser.add_argument("--inspect", action="store_true", help="只打印控件信息后退出")
    args = parser.parse_args(argv)

    listener = WxListener(
        mode=args.mode,
        interval=args.interval,
        filter_mute=args.filter_mute,
        skip_self=not args.include_self,
        max_pages=args.max_pages,
        default_callback=_demo_callback,
    )
    listener.verbose = args.verbose
    if args.inspect:
        listener.inspect()
        return 0
    if args.chat:
        listener.add_listen_chat(args.chat, callback=_demo_callback)
    elif args.mode == "whitelist":
        logger.error("白名单模式请用 --chat 指定至少一个会话，或先 --inspect 查看会话名")
        return 2
    listener.start()
    listener.keep_running()
    return 0


if __name__ == "__main__":
    sys.exit(main())
