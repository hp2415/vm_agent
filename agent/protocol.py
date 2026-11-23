"""协议常量与帧辅助（与 console.agent.v1 对齐）。"""

from __future__ import annotations

import hashlib
import struct
import uuid
from datetime import datetime, timezone
from typing import Any

PROTOCOL = "console.agent.v1"
WS_PATH = "/v1/agent"
REMOTE_MAGIC = b"RB01"
# 接管时的扬声器回环。载荷是 PCM s16le、单声道、16000 Hz。
AUDIO_MAGIC = b"RA01"
AUDIO_RATE = 16000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def make_frame(
    type_: str,
    *,
    payload: dict[str, Any] | None = None,
    id_: str | None = None,
    vm_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": type_, "ts": utc_now_iso()}
    if id_ is not None:
        frame["id"] = id_
    if vm_id is not None:
        frame["vm_id"] = vm_id
    if payload is not None:
        frame["payload"] = payload
    frame.update(extra)
    return frame


def encode_remote_frame(jpeg: bytes) -> bytes:
    return REMOTE_MAGIC + struct.pack(">I", len(jpeg)) + jpeg


def encode_audio_frame(pcm: bytes) -> bytes:
    return AUDIO_MAGIC + struct.pack(">I", len(pcm)) + pcm


def title_chat_id(title: str) -> str:
    name = (title or "").strip()
    return f"title:{name}" if name else "title:unknown"


def stable_msg_id(
    vm_id: str,
    chat_id: str,
    msg_type: str,
    content: str,
    *,
    is_self: bool,
    bucket_sec: int,
) -> str:
    raw = f"{vm_id}|{chat_id}|{msg_type}|{content}|{int(is_self)}|{bucket_sec}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
