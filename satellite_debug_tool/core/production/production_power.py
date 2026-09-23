"""Formal batch power owner using the verified PSW closed-loop adapter."""

from __future__ import annotations

import threading
from typing import Callable, Optional
import uuid

from PySide6.QtCore import QThread, Signal

from .power_supply import (
    GwInstekPswAdapter,
    PowerCommandRecord,
    PowerSupplyConfig,
    PowerSupplyState,
)


class ProductionPowerWorker(QThread):
    """Own one formal-batch PSW connection from identity through verified OFF."""

    power_ready = Signal(object, object, object, str)
    power_failed = Signal(str, object, str)
    power_stopped = Signal(object, object, str)

    def __init__(
        self,
        config: PowerSupplyConfig,
        *,
        adapter_factory: Callable[[PowerSupplyConfig], GwInstekPswAdapter] = GwInstekPswAdapter,
        parent=None,
    ) -> None:
        super().__init__(parent)
        config.validate()
        self._config = config
        self._adapter_factory = adapter_factory
        self._stop_requested = threading.Event()
        self._action_id = f"BATCH-PWR-{uuid.uuid4().hex[:12]}"

    @property
    def action_id(self) -> str:
        return self._action_id

    def request_verified_off(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        adapter: Optional[GwInstekPswAdapter] = None
        emitted_failure = False
        try:
            adapter = self._adapter_factory(self._config)
            identity = adapter.connect()
            if self._stop_requested.is_set():
                result = adapter.disable_output(fixture_action_id=self._action_id)
                self.power_stopped.emit(result, adapter.records, self._action_id)
                return
            adapter.prepare_output_off(fixture_action_id=self._action_id)
            result = adapter.enable_output(fixture_action_id=self._action_id)
            self.power_ready.emit(identity, result, adapter.records, self._action_id)
            while not self._stop_requested.wait(0.1):
                continue
            result = adapter.disable_output(fixture_action_id=self._action_id)
            self.power_stopped.emit(result, adapter.records, self._action_id)
        except Exception as exc:
            emitted_failure = True
            records: tuple[PowerCommandRecord, ...] = ()
            if adapter is not None:
                records = adapter.records
                if adapter.state not in {
                    PowerSupplyState.DISCONNECTED,
                    PowerSupplyState.OFF_CONFIRMED,
                    PowerSupplyState.READY_OFF,
                }:
                    try:
                        adapter.disable_output(fixture_action_id=self._action_id)
                        records = adapter.records
                    except Exception:
                        records = adapter.records
            self.power_failed.emit(
                f"{type(exc).__name__}: {exc}",
                records,
                self._action_id,
            )
        finally:
            if adapter is not None:
                adapter.close()
            if self._stop_requested.is_set() and not emitted_failure and adapter is None:
                self.power_stopped.emit(None, (), self._action_id)


__all__ = ["ProductionPowerWorker"]
