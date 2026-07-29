"""Stable updater errors shared by the core and translated UI."""

from __future__ import annotations

from typing import Any


class UpdaterError(Exception):
    """Error with a stable code, raw detail, and structured context."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        **context: Any,
    ) -> None:
        self.code = str(code)
        self.detail = str(detail)
        self.context = dict(context)
        super().__init__(self._english_message())

    def _english_message(self) -> str:
        message = self.code.replace("_", " ")
        if self.detail:
            message = f"{message}: {self.detail}"
        if self.context:
            rendered = ", ".join(f"{key}={value}" for key, value in self.context.items())
            message = f"{message} ({rendered})"
        return message
