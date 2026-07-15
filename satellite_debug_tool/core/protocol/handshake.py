"""
协议 v2 握手状态机。

连接建立后主动请求 META_INFO + 三张 DEFINE 表，周期重发直到全部到齐；
收到 META 后独立请求 PROFILE_SEMANTICS，失败不会阻塞基础握手就绪；
收到 `HEARTBEAT` 帧刷新链路心跳，超时发射 `link_lost`。

时间推进由外部调用 ``tick(dt_ms)`` 驱动。调用方可把 receiver 吐出的 record
逐个喂入 ``feed(record)``；其它非握手相关记录（DataReport/StateReport/EventReport）
会被忽略，便于直接串接到 FrameReceiverV2 输出。

通信方向：
- 发送侧由 `sender` 回调完成（Serial/Udp worker 的 send）
- 元数据侧由 `ProfileStore.apply_*` 更新
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from satellite_debug_tool.core.link_trace import trace_message
from satellite_debug_tool.core.profile import ProfileStore
from .codec_v2 import (
    build_request_channel_define,
    build_request_event_define,
    build_request_meta_info,
    build_request_profile_semantics,
    build_request_state_define,
)
from .frame_v2 import (
    ChannelDefineTable,
    EventDefineTable,
    FrameV2Record,
    Heartbeat,
    MetaInfo,
    ProfileSemanticsReport,
    StateDefineTable,
)


Sender = Callable[[bytes], None]


class Handshake(QObject):
    """握手状态机。线程亲和性：建议与 ProfileStore 同线程实例化。"""

    # 全部 DEFINE 就绪（meta + 3 张表）时发射 hw_type
    ready = Signal(str)
    # 连续 ``heartbeat_timeout_ms`` 未收到 HEARTBEAT 时发射
    link_lost = Signal()
    # 心跳恢复（从 lost → alive）
    link_restored = Signal()
    # 某类 DEFINE 超时重发时发射帧名（"meta"|"channel"|"state"|"event"）
    define_timeout = Signal(str)

    def __init__(
        self,
        store: ProfileStore,
        sender: Sender,
        *,
        define_timeout_ms: int = 500,
        semantics_retry_ms: int = 1000,
        semantics_max_attempts: int = 3,
        heartbeat_timeout_ms: int = 3000,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._send = sender
        self._define_timeout_ms = define_timeout_ms
        self._semantics_retry_ms = semantics_retry_ms
        self._semantics_max_attempts = semantics_max_attempts
        self._hb_timeout_ms = heartbeat_timeout_ms

        self._enabled = False
        self._ready = False
        self._received_meta = False
        self._received_channel = False
        self._received_state = False
        self._received_event = False
        self._received_semantics = False
        self._retry_paused = False

        self._elapsed_since_req_ms = 0
        self._elapsed_since_semantics_req_ms = 0
        self._semantics_attempts = 0
        self._elapsed_since_hb_ms = 0
        self._heartbeat_alive = False  # 从未收到过 HB 时为 False

    # ---- 生命周期 ----

    def start(self) -> None:
        """连接建立后调用，立即发出四条基础定义 REQUEST。"""
        self._enabled = True
        self._ready = False
        self._received_meta = False
        self._received_channel = False
        self._received_state = False
        self._received_event = False
        self._received_semantics = False
        self._retry_paused = False
        self._elapsed_since_req_ms = 0
        self._elapsed_since_semantics_req_ms = 0
        self._semantics_attempts = 0
        self._elapsed_since_hb_ms = 0
        self._heartbeat_alive = False
        self._trace("START missing=meta,channel,state,event")
        self._send_base_requests()

    def stop(self) -> None:
        """断开连接时调用。握手状态置位 disabled，但 ProfileStore 保留以便下次连接复用。"""
        self._enabled = False
        self._trace("STOP")

    def set_retry_paused(self, paused: bool) -> None:
        """暂停定义/semantics 重试；心跳计时仍继续。"""
        was_paused = self._retry_paused
        self._retry_paused = bool(paused)
        if was_paused != self._retry_paused:
            self._trace(f"RETRY_PAUSED value={int(self._retry_paused)}")
        if was_paused and not self._retry_paused:
            self._request_semantics_if_due(immediate_if_never_sent=True)

    # ---- 读取 ----

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def heartbeat_alive(self) -> bool:
        return self._heartbeat_alive

    # ---- 接收：消费 receiver 输出 ----

    def feed(self, record: FrameV2Record) -> None:
        """
        消费一条 v2 record。非握手相关类型直接忽略。
        必须在 receiver.feed 返回的序列上按顺序调用。
        """
        if not self._enabled:
            return

        if isinstance(record, MetaInfo):
            self._store.apply_meta(record)
            first_meta = not self._received_meta
            self._received_meta = True
            self._trace(
                f"APPLY META hw={record.hw_type!r} fw={record.fw_ver!r} "
                f"first={int(first_meta)} missing={self._missing_text()}"
            )
            if first_meta:
                self._request_semantics_if_due(immediate_if_never_sent=True)
        elif isinstance(record, ChannelDefineTable):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_channel_define(hw, record.table_ver, record.channels)
                self._received_channel = True
                self._trace(
                    f"APPLY CHANNEL_DEFINE ver={record.table_ver} "
                    f"count={len(record.channels)} missing={self._missing_text()}"
                )
            else:
                self._trace("IGNORE CHANNEL_DEFINE reason=meta_missing")
        elif isinstance(record, StateDefineTable):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_state_define(hw, record.table_ver, record.states)
                self._received_state = True
                self._trace(
                    f"APPLY STATE_DEFINE ver={record.table_ver} "
                    f"count={len(record.states)} missing={self._missing_text()}"
                )
            else:
                self._trace("IGNORE STATE_DEFINE reason=meta_missing")
        elif isinstance(record, EventDefineTable):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_event_define(hw, record.table_ver, record.events)
                self._received_event = True
                self._trace(
                    f"APPLY EVENT_DEFINE ver={record.table_ver} "
                    f"count={len(record.events)} missing={self._missing_text()}"
                )
            else:
                self._trace("IGNORE EVENT_DEFINE reason=meta_missing")
        elif isinstance(record, ProfileSemanticsReport):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_profile_semantics(hw, record)
                self._received_semantics = True
                self._trace(
                    f"APPLY PROFILE_SEMANTICS ver={record.table_ver} "
                    f"missing={self._missing_text()}"
                )
            else:
                self._trace("IGNORE PROFILE_SEMANTICS reason=meta_missing")
        elif isinstance(record, Heartbeat):
            self._on_heartbeat()

        self._check_ready()

    def _on_heartbeat(self) -> None:
        self._elapsed_since_hb_ms = 0
        if not self._heartbeat_alive:
            self._heartbeat_alive = True
            self._trace("HEARTBEAT restored=1")
            # 首次收到 HB：若此前发过 link_lost，提示恢复
            # （首次 start() 时本来就是 False，不重复发射）
            self.link_restored.emit()

    def _check_ready(self) -> None:
        if (
            self._received_meta
            and self._received_channel
            and self._received_state
            and self._received_event
            and not self._ready
        ):
            self._ready = True
            hw = self._store.current_hw_type() or "unknown"
            self._trace(f"READY hw={hw!r}")
            self.ready.emit(hw)

    # ---- 定时推进 ----

    def tick(self, dt_ms: int) -> None:
        """由外部计时器按 dt_ms 调用。"""
        if not self._enabled:
            return

        # 1) DEFINE 超时重发
        if not self._ready and not self._retry_paused:
            self._elapsed_since_req_ms += dt_ms
            if self._elapsed_since_req_ms >= self._define_timeout_ms:
                self._resend_missing_requests()
                self._elapsed_since_req_ms = 0

        # 2) PROFILE_SEMANTICS 在 META 后独立重试，不阻塞 ready。
        if self._received_meta and not self._received_semantics and not self._retry_paused:
            self._elapsed_since_semantics_req_ms += dt_ms
            self._request_semantics_if_due()

        # 3) HEARTBEAT 链路检查
        self._elapsed_since_hb_ms += dt_ms
        if self._heartbeat_alive and self._elapsed_since_hb_ms >= self._hb_timeout_ms:
            self._heartbeat_alive = False
            self._trace(f"HEARTBEAT_LOST timeout_ms={self._hb_timeout_ms}")
            self.link_lost.emit()

    # ---- 内部 ----

    def _send_base_requests(self) -> None:
        self._send_request("REQUEST_META_INFO", build_request_meta_info(), "initial")
        self._send_request("REQUEST_CHANNEL_DEFINE", build_request_channel_define(), "initial")
        self._send_request("REQUEST_STATE_DEFINE", build_request_state_define(), "initial")
        self._send_request("REQUEST_EVENT_DEFINE", build_request_event_define(), "initial")

    def _request_semantics_if_due(self, *, immediate_if_never_sent: bool = False) -> None:
        if (
            not self._enabled
            or self._retry_paused
            or not self._received_meta
            or self._received_semantics
            or self._semantics_attempts >= self._semantics_max_attempts
        ):
            return
        due = self._elapsed_since_semantics_req_ms >= self._semantics_retry_ms
        if self._semantics_attempts == 0 and immediate_if_never_sent:
            due = True
        if not due:
            return
        self._send_request(
            "REQUEST_PROFILE_SEMANTICS",
            build_request_profile_semantics(),
            f"semantics_attempt_{self._semantics_attempts + 1}",
        )
        self._semantics_attempts += 1
        self._elapsed_since_semantics_req_ms = 0

    def _resend_missing_requests(self) -> None:
        if not self._received_meta:
            self._send_request("REQUEST_META_INFO", build_request_meta_info(), "retry_missing")
            self.define_timeout.emit("meta")
        if not self._received_channel:
            self._send_request(
                "REQUEST_CHANNEL_DEFINE", build_request_channel_define(), "retry_missing",
            )
            self.define_timeout.emit("channel")
        if not self._received_state:
            self._send_request("REQUEST_STATE_DEFINE", build_request_state_define(), "retry_missing")
            self.define_timeout.emit("state")
        if not self._received_event:
            self._send_request("REQUEST_EVENT_DEFINE", build_request_event_define(), "retry_missing")
            self.define_timeout.emit("event")

    def _send_request(self, name: str, frame: bytes, reason: str) -> None:
        result = self._send(frame)
        status = "failed" if result is False else "accepted"
        self._trace(
            f"TX {name} reason={reason} result={status} missing={self._missing_text()}"
        )

    def _missing_text(self) -> str:
        missing = []
        if not self._received_meta:
            missing.append("meta")
        if not self._received_channel:
            missing.append("channel")
        if not self._received_state:
            missing.append("state")
        if not self._received_event:
            missing.append("event")
        return ",".join(missing) or "none"

    @staticmethod
    def _trace(message: str) -> None:
        trace_message("DBG_HANDSHAKE", message)
