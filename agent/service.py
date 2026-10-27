"""无界面 Agent 服务：WS 客户端 + 可选微信监听/外发 + 画面。"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from typing import Any

from agent.mapper import sessions_to_snapshot, wx_message_to_event
from agent.protocol import PROTOCOL, WS_PATH, encode_remote_frame, make_frame, new_id
from remote.input_lease import others_hold_input, release_modifiers

logger = logging.getLogger(__name__)


def _clamp_fps(raw: object) -> float:
    """缩略图可以低于 1 帧/秒，最快不超过 20。"""
    try:
        fps = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        fps = 6.0
    return max(0.2, min(fps, 20.0))


class AgentService:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.vm_id = str(config.get("vm_id") or "VM-01")
        self.token = str(config.get("token") or "CHANGE_ME")
        self.host = str(config.get("host") or "127.0.0.1")
        self.port = int(config.get("port") or 18765)
        self.enable_wechat = bool(config.get("enable_wechat", True))
        self.enable_remote = bool(config.get("enable_remote", True))
        self.include_self = bool(config.get("include_self", True))
        self.blacklist = list(config.get("listen_blacklist") or ["订阅号", "服务通知"])

        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._send_lock = asyncio.Lock()
        self._heartbeat_interval = 15
        self._agent_busy = False
        self._wechat_running = False
        self._wechat_key = ""
        self._wechat_key_at = 0.0
        self._unread_hint = 0
        self._pending_send = 0
        self._stop = threading.Event()

        self._listener = None
        self._controller = None
        self._rpa_lock = None

        # remote streaming
        self._remote_active = False
        self._remote_mode = "preview"
        self._remote_view = "jpeg"
        self._remote_fps = 6
        self._remote_quality = 50
        self._remote_max_width = 640
        self._remote_task: asyncio.Task | None = None
        self._clipboard_task: asyncio.Task | None = None
        self._clipboard_sig: str | None = None
        self._capture_busy = False
        self._ws_ping_interval = int(config.get("ws_ping_interval_sec") or 60)
        self._ws_ping_timeout = int(config.get("ws_ping_timeout_sec") or 120)
        self._input_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._input_worker_task: asyncio.Task | None = None
        self._input_gate_task: asyncio.Task | None = None
        # 接管不长期占锁。为真表示当前没有外发占用，控制台可以注入。
        self._input_open = False

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._start_input_worker()
        if self.enable_wechat:
            self._init_wechat()
        url = f"ws://{self.host}:{self.port}{WS_PATH}"
        logger.info("connecting to %s as %s", url, self.vm_id)
        import websockets

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    max_size=8 * 1024 * 1024,
                    ping_interval=self._ws_ping_interval,
                    ping_timeout=max(
                        self._ws_ping_interval + 10, self._ws_ping_timeout
                    ),
                ) as ws:
                    self._ws = ws
                    await self._handshake()
                    if self.enable_wechat and self._listener is not None:
                        self._start_listener()
                    hb = asyncio.create_task(self._heartbeat_loop())
                    try:
                        async for raw in ws:
                            if isinstance(raw, bytes):
                                continue
                            try:
                                frame = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            await self._dispatch_command(frame)
                    finally:
                        # Console 断开时只清理本会话，进程继续等待重连
                        await self._cancel_task(hb)
                        await self._stop_remote()
                        self._ws = None
                if self._stop.is_set():
                    break
                logger.info("console 已断开，%ss 后重连（Agent 保持运行）", 3)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
                logger.warning("连接任务被取消，3s 后重连")
                await asyncio.sleep(3)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                logger.warning("disconnected: %s; retry in 3s", exc)
                await asyncio.sleep(3)

        await self._stop_input_worker()
        logger.info("agent stopped")

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
        loop = self._loop
        ws = self._ws
        if loop and ws and loop.is_running():
            asyncio.run_coroutine_threadsafe(ws.close(), loop)

    def _init_wechat(self) -> None:
        try:
            from rpa.lock import rpa_lock
            from rpa.wechat_controller import WeChatController
            from rpa.wx_listener import ListenMode, WxListener

            self._rpa_lock = rpa_lock
            self._controller = WeChatController()
            self._listener = WxListener(
                mode=ListenMode.BLACKLIST,
                interval=float(self.config.get("listen_interval") or 1.2),
                skip_self=not self.include_self,
                default_callback=self._on_wx_message,
            )
            for name in self.blacklist:
                self._listener.add_listen_chat(name)
            self._wechat_running = self._probe_wechat()
            logger.info("wechat modules loaded; running=%s", self._wechat_running)
        except Exception as exc:  # noqa: BLE001
            logger.exception("wechat init failed (will run without RPA): %s", exc)
            self.enable_wechat = False

    def _current_wechat_key(self) -> str:
        """最多一分钟扫一次微信进程。窗口不在时不上报，避免把关窗当成换号。"""
        if not self.enable_wechat or not self._wechat_running:
            return ""
        now = time.monotonic()
        if self._wechat_key and now - self._wechat_key_at < 60:
            return self._wechat_key
        try:
            from rpa.wechat_controller import WechatController

            key = WechatController.get_current_wxid(self) or ""
        except Exception:  # noqa: BLE001
            return self._wechat_key
        self._wechat_key = key
        self._wechat_key_at = now
        return key

    def _probe_wechat(self) -> bool:
        try:
            from rpa.wechat_controller import _find_wechat_hwnd

            return _find_wechat_hwnd() is not None
        except Exception:  # noqa: BLE001
            return False

    def _start_listener(self) -> None:
        if self._listener is None:
            return
        try:
            if self._listener._thread is None or not self._listener._thread.is_alive():  # noqa: SLF001
                self._listener.start()
                logger.info("wx listener started")
        except Exception as exc:  # noqa: BLE001
            logger.warning("listener start failed: %s", exc)

    def _on_wx_message(self, msg: Any, chat: Any) -> None:
        # 在 listener 线程；需经事件循环发出
        if self._rpa_lock is not None and not self._rpa_lock.acquire(blocking=False):
            return
        try:
            # chat_name 可能已在 msg 上
            if not getattr(msg, "chat_name", None) and chat is not None:
                msg.chat_name = getattr(chat, "name", "") or ""
            frame = wx_message_to_event(self.vm_id, msg)
            self._emit_threadsafe(frame)
            self._unread_hint = max(0, self._unread_hint)
        finally:
            if self._rpa_lock is not None:
                try:
                    self._rpa_lock.release()
                except Exception:  # noqa: BLE001
                    pass

    async def _emit_input_open(self, open_: bool) -> None:
        """held=true 表示外发没占键鼠，控制台可以操作。"""
        await self._send_json(
            make_frame(
                "event.input_lease",
                vm_id=self.vm_id,
                payload={"held": bool(open_)},
            )
        )

    async def _input_gate_loop(self) -> None:
        """接管期间只观察外发是否占着键鼠，自己不握锁。"""
        free = not await asyncio.to_thread(others_hold_input)
        if free:
            await asyncio.to_thread(release_modifiers)
        self._input_open = free
        await self._emit_input_open(free)
        while self._remote_active and self._remote_mode == "control":
            free = not await asyncio.to_thread(others_hold_input)
            if not (self._remote_active and self._remote_mode == "control"):
                break
            if free != self._input_open:
                self._input_open = free
                if free:
                    await asyncio.to_thread(release_modifiers)
                await self._emit_input_open(free)
                logger.info("键鼠%s", "可操作" if free else "外发占用")
            await asyncio.sleep(0.04)

    def _emit_threadsafe(self, frame: dict) -> None:
        loop = self._loop
        if loop is None or self._ws is None:
            return
        asyncio.run_coroutine_threadsafe(self._send_json(frame), loop)

    async def _send_json(self, frame: dict) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _send_bytes(self, data: bytes) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(data)

    async def _handshake(self) -> None:
        hello = {
            "type": "hello",
            "protocol": PROTOCOL,
            "token": self.token,
            "agent": {
                "vm_id": self.vm_id,
                "vm_name": str(self.config.get("vm_name") or self.vm_id),
                "desktop_version": "vm_agent/0.1.0",
                "wechat_account_label": str(
                    self.config.get("wechat_account_label") or ""
                ),
                "hostname": socket.gethostname(),
                "capabilities": self._capabilities(),
            },
        }
        await self._send_json(hello)
        raw = await asyncio.wait_for(self._ws.recv(), timeout=15)
        if isinstance(raw, bytes):
            raise RuntimeError("expected hello_ok text")
        resp = json.loads(raw)
        if resp.get("type") != "hello_ok":
            raise RuntimeError(f"handshake failed: {resp}")
        self._heartbeat_interval = int(resp.get("heartbeat_interval_sec") or 15)
        logger.info("handshake ok session=%s", resp.get("session_id"))

    def _capabilities(self) -> list[str]:
        caps = [
            "msg.inbound",
            "cmd.send_text",
            "session.list",
            "remote.view",
            "remote.input",
        ]
        # 口令在宿主机库存里。这里只声明「这台机器的画面走 VNC，不要再要 JPEG」。
        if str(self.config.get("remote_view") or "jpeg") == "vnc":
            caps.append("remote.view=vnc")
        return caps

    async def _heartbeat_loop(self) -> None:
        while True:
            self._wechat_running = self._probe_wechat() if self.enable_wechat else False
            frame = make_frame(
                "event.heartbeat",
                vm_id=self.vm_id,
                payload={
                    "wechat_running": self._wechat_running,
                    "wechat_key": self._current_wechat_key(),
                    "agent_busy": self._agent_busy,
                    "pending_send": self._pending_send,
                    "unread_hint": self._unread_hint,
                },
            )
            await self._send_json(frame)
            await asyncio.sleep(self._heartbeat_interval)

    async def _start_input_worker(self) -> None:
        if self._input_worker_task is not None and not self._input_worker_task.done():
            return
        self._input_queue = asyncio.Queue(maxsize=64)
        self._input_worker_task = asyncio.create_task(self._input_worker_loop())

    async def _stop_input_worker(self) -> None:
        task = self._input_worker_task
        self._input_worker_task = None
        self._input_queue = None
        await self._cancel_task(task)

    def _enqueue_remote_input(self, payload: dict[str, Any]) -> None:
        # VNC 接管的键鼠由宿主机虚拟键鼠注入，不再走 SendInput。
        if self._remote_view == "vnc":
            return
        action = str(payload.get("action") or "")
        mouse = action.startswith("mouse_")
        if self._remote_mode != "control" or (not self._input_open and not mouse):
            if action and not mouse:
                logger.info("丢弃远控输入 %s：外发占用", action)
            return
        q = self._input_queue
        if q is None:
            return
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # 队列满时优先丢掉旧的 mouse_move，为点击/按键留位
            try:
                old = q.get_nowait()
            except asyncio.QueueEmpty:
                old = None
            if old is not None and old.get("action") != "mouse_move":
                try:
                    q.put_nowait(old)
                except asyncio.QueueFull:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning(
                    "remote input queue full, dropped %s", payload.get("action")
                )

    async def _input_worker_loop(self) -> None:
        """串行执行键鼠；合并 mouse_move，避免暂停恢复后指令风暴。"""
        from remote.input import handle_input

        q = self._input_queue
        assert q is not None
        pending_move: dict[str, Any] | None = None
        while True:
            try:
                if pending_move is not None:
                    payload = await asyncio.wait_for(q.get(), timeout=0.03)
                else:
                    payload = await q.get()
            except asyncio.TimeoutError:
                payload = None

            if payload is not None:
                mouse = str(payload.get("action") or "").startswith("mouse_")
                if not self._input_open and not mouse:
                    pending_move = None
                    continue
                if payload.get("action") == "mouse_move":
                    pending_move = payload
                else:
                    if pending_move is not None:
                        await asyncio.to_thread(handle_input, pending_move)
                        pending_move = None
                    if self._input_open or mouse:
                        await asyncio.to_thread(handle_input, payload)
                continue

            if pending_move is not None:
                await asyncio.to_thread(handle_input, pending_move)
                pending_move = None

    async def _dispatch_command(self, frame: dict) -> None:
        """高频键鼠进队列，其它指令仍同步处理。"""
        type_ = frame.get("type") or ""
        if type_ == "cmd.remote_input":
            if self.enable_remote:
                self._enqueue_remote_input(frame.get("payload") or {})
            return
        await self._on_command(frame)

    async def _on_command(self, frame: dict) -> None:
        type_ = frame.get("type") or ""
        cmd_id = frame.get("id")
        payload = frame.get("payload") or {}
        if type_ == "cmd.ping":
            await self._send_json(
                make_frame(
                    "event.cmd_result",
                    id_=cmd_id,
                    vm_id=self.vm_id,
                    payload={"ok": True},
                )
            )
        elif type_ == "cmd.sync_sessions":
            await self._sync_sessions(payload.get("mode") or "full")
        elif type_ == "cmd.send_text":
            await self._handle_send_text(cmd_id, payload)
        elif type_ == "cmd.remote_start":
            await self._start_remote(payload)
            await self._send_json(
                make_frame(
                    "event.cmd_result",
                    id_=cmd_id,
                    vm_id=self.vm_id,
                    payload={"ok": True},
                )
            )
        elif type_ == "cmd.remote_clipboard":
            await self._handle_remote_clipboard(cmd_id, payload)
        elif type_ == "cmd.remote_stop":
            await self._stop_remote()
            await self._send_json(
                make_frame(
                    "event.cmd_result",
                    id_=cmd_id,
                    vm_id=self.vm_id,
                    payload={"ok": True},
                )
            )

    async def _sync_sessions(self, mode: str) -> None:
        sessions: list[Any] = []
        if self._listener is not None:
            try:
                if self._rpa_lock is not None:
                    with self._rpa_lock:
                        sessions = list(self._listener.get_sessions() or [])
                else:
                    sessions = list(self._listener.get_sessions() or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_sessions failed: %s", exc)
        await self._send_json(sessions_to_snapshot(self.vm_id, sessions, mode=mode))

    async def _handle_send_text(self, cmd_id: str | None, payload: dict) -> None:
        text = str(payload.get("text") or "")
        candidates = list(payload.get("candidates") or [])
        chat_id = str(payload.get("chat_id") or "")
        self._pending_send += 1
        self._agent_busy = True
        ok = False
        error = None
        receiver_used = None
        receiver_source = None
        try:
            if not self.enable_wechat or self._controller is None:
                raise RuntimeError("wechat disabled or not loaded")

            def _do_send():
                listener = self._listener
                if listener is not None:
                    try:
                        listener.stop(join_timeout=2.0)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    lock = self._rpa_lock
                    if lock is not None:
                        with lock:
                            return self._controller.send_message_with_candidates(
                                candidates, text
                            )
                    return self._controller.send_message_with_candidates(candidates, text)
                finally:
                    if listener is not None:
                        try:
                            listener.start()
                        except Exception:  # noqa: BLE001
                            pass

            result = await asyncio.to_thread(_do_send)
            ok = bool(getattr(result, "ok", False))
            error = getattr(result, "error", None)
            receiver_used = getattr(result, "receiver_used", None)
            receiver_source = getattr(result, "receiver_source", None)
            if ok:
                # 回显 outbound
                title = ""
                if candidates:
                    title = str(candidates[0].get("keyword") or "")
                class _Msg:
                    content = text
                    type = "text"
                    sender = ""
                    chat_name = title
                    is_self = True
                    is_group = False

                await self._send_json(wx_message_to_event(self.vm_id, _Msg()))
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = str(exc)
            logger.exception("send_text failed")
        finally:
            self._pending_send = max(0, self._pending_send - 1)
            self._agent_busy = False

        await self._send_json(
            make_frame(
                "event.send_result",
                id_=cmd_id or new_id("cmd"),
                vm_id=self.vm_id,
                payload={
                    "ok": ok,
                    "error": error,
                    "receiver_used": receiver_used,
                    "receiver_source": receiver_source,
                    "chat_id": chat_id,
                },
            )
        )

    async def _start_remote(self, payload: dict) -> None:
        if not self.enable_remote:
            return
        mode = str(payload.get("mode") or "preview")
        view = str(payload.get("view") or "jpeg")
        self._remote_mode = mode
        self._remote_view = "vnc" if view == "vnc" else "jpeg"
        self._remote_fps = _clamp_fps(payload.get("fps"))
        self._remote_quality = max(30, min(int(payload.get("quality") or 50), 95))
        # 0 = 不缩放原分辨率
        self._remote_max_width = max(0, int(payload.get("max_width") or 640))
        self._remote_active = True
        logger.info(
            "remote %s view=%s fps=%s quality=%s max_width=%s",
            mode,
            self._remote_view,
            self._remote_fps,
            self._remote_quality,
            self._remote_max_width or "native",
        )
        if self._remote_view == "vnc":
            task = self._remote_task
            self._remote_task = None
            await self._cancel_task(task)
        elif self._remote_task is None or self._remote_task.done():
            self._remote_task = asyncio.create_task(self._remote_loop())
        if mode == "control":
            if self._input_gate_task is None or self._input_gate_task.done():
                self._input_gate_task = asyncio.create_task(self._input_gate_loop())
            self._start_clipboard_watch()
        else:
            await self._stop_input_gate()
            await self._stop_clipboard_watch()

    async def _stop_remote(self) -> None:
        self._remote_active = False
        self._remote_mode = "preview"
        self._remote_view = "jpeg"
        task = self._remote_task
        self._remote_task = None
        await self._cancel_task(task)
        await self._stop_clipboard_watch()
        await self._stop_input_gate()

    async def _handle_remote_clipboard(self, cmd_id: str | None, payload: dict) -> None:
        """控制台文本写入访客机剪贴板。外发占着租约时拒绝，避免冲掉粘贴内容。"""
        ok = False
        error: str | None = None
        text = payload.get("text")
        if payload.get("kind") not in (None, "text"):
            error = "仅支持文本"
        elif not self.enable_remote or self._remote_mode != "control":
            error = "未在接管"
        elif not isinstance(text, str) or not text:
            error = "空文本"
        elif len(text.encode("utf-8")) > 256 * 1024:
            error = "文本过长"
        elif await asyncio.to_thread(others_hold_input):
            error = "键鼠正被占用"
        else:
            from remote.clipboard import text_signature, write_text

            self._clipboard_sig = text_signature(text)
            ok = await asyncio.to_thread(write_text, text)
            if not ok:
                error = "写入剪贴板失败"
                self._clipboard_sig = None
        await self._send_json(
            make_frame(
                "event.cmd_result",
                id_=cmd_id or new_id("cmd"),
                vm_id=self.vm_id,
                payload={"ok": ok, "error": error},
            )
        )

    async def _stop_input_gate(self) -> None:
        self._input_open = False
        task = self._input_gate_task
        self._input_gate_task = None
        await self._cancel_task(task)

    def _start_clipboard_watch(self) -> None:
        if self._clipboard_task is not None and not self._clipboard_task.done():
            return
        from remote.clipboard import read_text, text_signature

        seed = read_text()
        self._clipboard_sig = text_signature(seed) if seed else None
        self._clipboard_task = asyncio.create_task(self._clipboard_watch_loop())

    async def _stop_clipboard_watch(self) -> None:
        task = self._clipboard_task
        self._clipboard_task = None
        self._clipboard_sig = None
        await self._cancel_task(task)

    async def _clipboard_watch_loop(self) -> None:
        """接管模式下轮询访客机剪贴板，变更时推 event.remote_clipboard。"""
        from remote.clipboard import read_text, text_signature

        while self._remote_active and self._remote_mode == "control":
            text = await asyncio.to_thread(read_text)
            if text is not None:
                sig = text_signature(text)
                if sig != self._clipboard_sig:
                    self._clipboard_sig = sig
                    await self._send_json(
                        make_frame(
                            "event.remote_clipboard",
                            vm_id=self.vm_id,
                            payload={"kind": "text", "text": text},
                        )
                    )
            await asyncio.sleep(0.35)

    async def _remote_loop(self) -> None:
        from remote.capture import grab_jpeg

        while self._remote_active:
            interval = 1.0 / max(float(self._remote_fps), 0.2)
            if self._capture_busy:
                await asyncio.sleep(min(0.1, interval))
                continue
            t0 = time.monotonic()
            self._capture_busy = True
            try:
                jpeg = await asyncio.to_thread(
                    grab_jpeg,
                    max_width=self._remote_max_width,
                    quality=self._remote_quality,
                )
            finally:
                self._capture_busy = False
            if jpeg:
                await self._send_bytes(encode_remote_frame(jpeg))
            elapsed = time.monotonic() - t0
            # 截屏已超时则跳帧，避免低配机越积越慢
            if elapsed > interval * 2:
                continue
            await asyncio.sleep(max(0.01, interval - elapsed))
