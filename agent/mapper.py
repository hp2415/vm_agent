"""WxMessage / SessionInfo → console.agent.v1 事件载荷。"""

from __future__ import annotations

import time
from typing import Any

from agent.protocol import make_frame, stable_msg_id, title_chat_id, utc_now_iso


def map_msg_type(raw: str) -> str:
    t = (raw or "text").lower()
    if t == "text":
        return "text"
    if t in ("image", "pic", "picture"):
        return "image"
    if t in ("file",):
        return "file"
    return "other"


def wx_message_to_event(vm_id: str, msg: Any) -> dict[str, Any]:
    chat_title = getattr(msg, "chat_name", "") or ""
    chat_id = title_chat_id(chat_title)
    content = getattr(msg, "content", "") or ""
    msg_type = map_msg_type(getattr(msg, "type", "text"))
    is_self = bool(getattr(msg, "is_self", False))
    is_group = bool(getattr(msg, "is_group", False))
    sender = getattr(msg, "sender", "") or ""
    bucket = int(time.time())
    msg_id = stable_msg_id(
        vm_id, chat_id, msg_type, content, is_self=is_self, bucket_sec=bucket
    )
    direction = "outbound" if is_self else "inbound"
    text = content
    if msg_type != "text" and not text:
        text = f"[{msg_type}]"
    return make_frame(
        "event.message",
        id_=f"evt_{msg_id}",
        vm_id=vm_id,
        payload={
            "msg_id": msg_id,
            "chat_id": chat_id,
            "chat_title": chat_title,
            "direction": direction,
            "msg_type": msg_type,
            "text": text,
            "sender_id": "" if is_group else chat_id,
            "sender_name": sender or chat_title,
            "is_group": is_group,
            "media": None,
            "raw_ref": None,
        },
    )


def sessions_to_snapshot(vm_id: str, sessions: list[Any], mode: str = "full") -> dict:
    items = []
    for s in sessions:
        name = getattr(s, "name", "") or ""
        items.append(
            {
                "chat_id": title_chat_id(name),
                "title": name,
                "chat_type": "unknown",
                "last_preview": getattr(s, "preview", "") or "",
                "last_ts": utc_now_iso(),
                "unread": int(getattr(s, "unread", 0) or 0),
                "tags": [],
            }
        )
    return make_frame(
        "event.session_snapshot",
        id_=f"evt_sess_{int(time.time())}",
        vm_id=vm_id,
        payload={"mode": mode, "sessions": items},
    )
