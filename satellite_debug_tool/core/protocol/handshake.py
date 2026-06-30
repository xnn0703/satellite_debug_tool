"""
协议 v2 握手状态机。

连接建立后主动请求 META_INFO + 三张 DEFINE 表，周期重发直到全部到齐；
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

from satellite_debug_tool.core.profile import ProfileStore
from .codec_v2 import (
    build_request_channel_define,
    build_request_event_define,
    build_request_meta_info,
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
        heartbeat_timeout_ms: int = 3000,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._send = sender
        self._define_timeout_ms = define_timeout_ms
        self._hb_timeout_ms = heartbeat_timeout_ms

        self._enabled = False
        self._ready = False
        self._received_meta = False
        self._received_channel = False
        self._received_state = False
        self._received_event = False

        self._elapsed_since_req_ms = 0
        self._elapsed_since_hb_ms = 0
        self._heartbeat_alive = False  # 从未收到过 HB 时为 False

    # ---- 生命周期 ----

    def start(self) -> None:
        """连接建立后调用，立即发出四条 REQUEST。"""
        self._enabled = True
        self._ready = False
        self._received_meta = False
        self._received_channel = False
        self._received_state = False
        self._received_event = False
        self._elapsed_since_req_ms = 0
        self._elapsed_since_hb_ms = 0
        self._heartbeat_alive = False
        self._send_all_requests()

    def stop(self) -> None:
        """断开连接时调用。握手状态置位 disabled，但 ProfileStore 保留以便下次连接复用。"""
        self._enabled = False

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
            self._received_meta = True
        elif isinstance(record, ChannelDefineTable):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_channel_define(hw, record.table_ver, record.channels)
                self._received_channel = True
        elif isinstance(record, StateDefineTable):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_state_define(hw, record.table_ver, record.states)
                self._received_state = True
        elif isinstance(record, EventDefineTable):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_event_define(hw, record.table_ver, record.events)
                self._received_event = True
        elif isinstance(record, ProfileSemanticsReport):
            hw = self._store.current_hw_type()
            if hw is not None:
                self._store.apply_profile_semantics(hw, record)
        elif isinstance(record, Heartbeat):
            self._on_heartbeat()

        self._check_ready()

    def _on_heartbeat(self) -> None:
        self._elapsed_since_hb_ms = 0
        if not self._heartbeat_alive:
            self._heartbeat_alive = True
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
            self.ready.emit(hw)

    # ---- 定时推进 ----

    def tick(self, dt_ms: int) -> None:
        """由外部计时器按 dt_ms 调用。"""
        if not self._enabled:
            return

        # 1) DEFINE 超时重发
        if not self._ready:
            self._elapsed_since_req_ms += dt_ms
            if self._elapsed_since_req_ms >= self._define_timeout_ms:
                self._resend_missing_requests()
                self._elapsed_since_req_ms = 0

        # 2) HEARTBEAT 链路检查
        self._elapsed_since_hb_ms += dt_ms
        if self._heartbeat_alive and self._elapsed_since_hb_ms >= self._hb_timeout_ms:
            self._heartbeat_alive = False
            self.link_lost.emit()

    # ---- 内部 ----

    def _send_all_requests(self) -> None:
        self._send(build_request_meta_info())
        self._send(build_request_channel_define())
        self._send(build_request_state_define())
        self._send(build_request_event_define())

    def _resend_missing_requests(self) -> None:
        if not self._received_meta:
            self._send(build_request_meta_info())
            self.define_timeout.emit("meta")
        if not self._received_channel:
            self._send(build_request_channel_define())
            self.define_timeout.emit("channel")
        if not self._received_state:
            self._send(build_request_state_define())
            self.define_timeout.emit("state")
        if not self._received_event:
            self._send(build_request_event_define())
            self.define_timeout.emit("event")
