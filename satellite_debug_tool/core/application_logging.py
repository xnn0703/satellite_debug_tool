"""Persistent application logging for field diagnostics."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from typing import Optional


_HANDLER_MARKER = "satellite_debug_tool_file_handler"


def default_application_log_path() -> Path:
    return Path.home() / ".satellite_debug_tool" / "logs" / "application.log"


def configure_application_logging(log_path: Optional[Path] = None) -> Path:
    path = Path(log_path or default_application_log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    existing = next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, _HANDLER_MARKER, False)
        ),
        None,
    )
    if existing is None:
        handler = RotatingFileHandler(
            path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    previous_hook = sys.excepthook

    def _log_unhandled(exc_type, exc_value, exc_traceback) -> None:
        logging.getLogger("satellite_debug_tool.unhandled").critical(
            "unhandled exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        previous_hook(exc_type, exc_value, exc_traceback)

    if not getattr(sys.excepthook, _HANDLER_MARKER, False):
        setattr(_log_unhandled, _HANDLER_MARKER, True)
        sys.excepthook = _log_unhandled
    logging.getLogger("satellite_debug_tool").info(
        "application logging initialized: %s", path
    )
    return path


__all__ = ["configure_application_logging", "default_application_log_path"]
