"""冻结程序烟测的阶段与失败证据；普通启动沿用原异常处理。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback


def output_path() -> Path | None:
    if "--smoke-test" in sys.argv[1:]:
        value = next((arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--smoke-output=")), "")
        if value:
            return Path(value)
    return None


def stage(name: str) -> None:
    destination = output_path()
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.with_suffix(".stage.txt").write_text(name, encoding="utf-8")


def install() -> None:
    """烟测失败写入完整异常并返回失败码，供无控制台的 CI 调用方读取。"""
    if "--smoke-test" not in sys.argv[1:]:
        return
    stage("python-runtime-ready")

    def report_exception(error_type, error, tb) -> None:
        destination = output_path()
        if destination is not None:
            proof = {
                "status": "failed", "error": str(error),
                "traceback": "".join(traceback.format_exception(error_type, error, tb)),
            }
            try:
                from PySide6.QtGui import QOpenGLContext
                context = QOpenGLContext.currentContext()
                if context is not None:
                    from OpenGL import GL
                    proof["graphics"] = {
                        "format_version": context.format().version(),
                        "profile": context.format().profile().name,
                        "is_opengl_es": context.isOpenGLES(),
                    }
                    import psutil
                    proof["graphics"]["libraries"] = [item.path for item in psutil.Process().memory_maps()
                        if any(name in item.path.lower() for name in ("opengl", "egl", "gles"))]
                    for name, token in (("version", GL.GL_VERSION), ("renderer", GL.GL_RENDERER), ("vendor", GL.GL_VENDOR)):
                        value = GL.glGetString(token)
                        proof["graphics"][name] = value.decode(errors="replace") if value else None
            except Exception as probe_error:
                proof["graphics_probe_error"] = str(probe_error)
            destination.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
        os._exit(1)

    sys.excepthook = report_exception
