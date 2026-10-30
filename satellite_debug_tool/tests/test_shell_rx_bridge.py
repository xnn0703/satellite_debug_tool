"""M45 RxRing 合同回归：容量、分段接纳、有界等待、唤醒、残余移交、锁内边沿与竞争。"""
from __future__ import annotations

import threading
import time

import pytest

from satellite_debug_tool.core.tcp_shell.rx_bridge import (
    MAX_READ_CHUNK,
    PushStatus,
    RxRing,
)


def test_capacity_bound_and_partial_acceptance():
    ring = RxRing(capacity_bytes=100)
    accepted, status, notify = ring.push(b"a" * 60)
    assert (accepted, status, notify) == (60, PushStatus.ACCEPTED, True)
    accepted, status, notify = ring.push(b"b" * 60)
    assert accepted == 40
    assert status is PushStatus.PARTIAL
    assert notify is False
    assert ring.backlog_bytes == 100
    assert ring.capacity_bytes == 100


def test_push_rejects_invalid_construction():
    with pytest.raises(ValueError):
        RxRing(capacity_bytes=0)
    with pytest.raises(ValueError):
        RxRing(wait_segment_s=0)


def test_push_waits_one_bounded_segment_then_returns_retry():
    ring = RxRing(capacity_bytes=10, wait_segment_s=0.02)
    ring.push(b"0123456789")
    start = time.perf_counter()
    accepted, status, _ = ring.push(b"x")
    elapsed = time.perf_counter() - start
    assert accepted == 0
    assert status is PushStatus.RETRY
    assert 0.015 <= elapsed < 0.5


def test_push_wakes_when_drain_frees_space():
    ring = RxRing(capacity_bytes=10, wait_segment_s=5.0)
    ring.push(b"0123456789")

    def drain_soon():
        time.sleep(0.05)
        ring.drain(10)

    thread = threading.Thread(target=drain_soon)
    thread.start()
    start = time.perf_counter()
    accepted, status, _ = ring.push(b"abcdefghij")
    elapsed = time.perf_counter() - start
    thread.join(timeout=2.0)
    assert (accepted, status) == (10, PushStatus.ACCEPTED)
    assert elapsed < 1.0


def test_wake_stop_interrupts_capacity_wait_and_freezes_production():
    ring = RxRing(capacity_bytes=10, wait_segment_s=5.0)
    ring.push(b"0123456789")

    def stop_soon():
        time.sleep(0.05)
        ring.wake_stop()

    thread = threading.Thread(target=stop_soon)
    thread.start()
    start = time.perf_counter()
    accepted, status, _ = ring.push(b"x")
    elapsed = time.perf_counter() - start
    thread.join(timeout=2.0)
    assert status is PushStatus.STOPPED
    assert elapsed < 1.0
    assert ring.push(b"y").status is PushStatus.STOPPED


def test_push_final_appends_residual_beyond_capacity_then_freezes():
    ring = RxRing(capacity_bytes=16)
    ring.push(b"ring-data")
    assert ring.push_final(b"residual") is False  # 已欠通知，残余随既有帧排空
    assert ring.backlog_bytes == len(b"ring-data") + len(b"residual")
    data, epoch = ring.drain(len(b"ring-data"))
    assert data == b"ring-data"
    data, epoch = ring.drain(64)
    assert data == b"residual"
    assert ring.backlog_bytes == 0
    with pytest.raises(RuntimeError):
        ring.push(b"more")
    with pytest.raises(RuntimeError):
        ring.push_final(b"more")


def test_push_final_keeps_residual_after_existing_pending():
    ring = RxRing(capacity_bytes=64)
    ring.push(b"queued")
    ring.drain(64)  # pending 清零
    assert ring.push_final(b"residual") is True  # 残余需要新的通知
    ring2 = RxRing(capacity_bytes=64)
    ring2.push(b"queued")  # pending 已置位
    assert ring2.push_final(b"residual") is False  # 已有通知，无需重复


def test_push_final_rejects_oversized_block():
    ring = RxRing()
    with pytest.raises(ValueError):
        ring.push_final(b"x" * (MAX_READ_CHUNK + 1))


def test_drain_respects_budget_and_pending_clears_when_empty():
    ring = RxRing(capacity_bytes=1000)
    ring.push(b"a" * 300)
    ring.push(b"b" * 300)
    data, epoch = ring.drain(500)
    assert data == b"a" * 300 + b"b" * 200
    assert epoch == 0
    data, _ = ring.drain(500)
    assert data == b"b" * 100
    data, _ = ring.drain(10)
    assert data == b""
    assert ring.backlog_bytes == 0


def test_clear_discards_queued_and_advances_epoch():
    ring = RxRing(capacity_bytes=1000)
    ring.push(b"old-bytes")
    ring.drain(64)  # 排空，通知边沿复位
    assert ring.clear() is True
    data, epoch = ring.drain(1000)
    assert data == b""
    assert epoch == 1
    ring.push(b"new-bytes")
    data, epoch = ring.drain(100)
    assert data == b"new-bytes"
    assert epoch == 1


def test_clear_keeps_single_notification_when_frame_already_owed():
    ring = RxRing(capacity_bytes=1000)
    ring.push(b"old-bytes")  # 已欠一次 flush 帧
    assert ring.clear() is False
    data, epoch = ring.drain(1000)
    assert data == b""
    assert epoch == 1


def test_clear_returns_false_after_finalization():
    ring = RxRing()
    ring.push_final(b"done")
    assert ring.clear() is False


def test_pending_edge_fires_once_until_drained():
    ring = RxRing()
    assert ring.push(b"one")[2] is True
    assert ring.push(b"two")[2] is False
    ring.drain(2)  # 部分排空，pending 保持
    assert ring.push(b"three")[2] is False
    ring.drain(1024)  # 排空
    assert ring.push(b"four")[2] is True


def test_producer_consumer_interleave_preserves_stream_and_edge_state():
    ring = RxRing(capacity_bytes=4096, wait_segment_s=0.002)
    payload = bytes(range(256))
    total_segments = 300
    expected = payload * total_segments
    drained: list[bytes] = []
    producer_done = threading.Event()

    def produce():
        for _ in range(total_segments):
            remaining = payload
            while remaining:
                result = ring.push(remaining)
                assert result.status is not PushStatus.STOPPED
                remaining = remaining[result.accepted :]
        producer_done.set()

    def consume():
        while True:
            data, _ = ring.drain(37 * (len(drained) % 7 + 1))
            if data:
                drained.append(data)
            elif producer_done.is_set() and ring.backlog_bytes == 0:
                if not ring.drain(1)[0]:
                    return

    producer = threading.Thread(target=produce)
    consumer = threading.Thread(target=consume)
    producer.start()
    consumer.start()
    producer.join(timeout=10.0)
    consumer.join(timeout=10.0)
    assert not producer.is_alive()
    assert not consumer.is_alive()
    assert b"".join(drained) == expected
    # 排空且无代际变化后 pending 必须已清零：下一次入环重新产生通知边沿
    assert ring.push(b"tail")[2] is True
    ring.drain(64)
