"""从已确认的 PNG 母版生成桌面图标；依赖项目现有 PySide6。"""

from __future__ import annotations

import os
from pathlib import Path
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "satellite_debug_tool" / "ui" / "assets" / "icons"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
# 标准尺寸以及 macOS Retina 的 16/32/128/256 逻辑像素表示。
ICNS_SIZES = {
    b"icp4": 16, b"icp5": 32, b"icp6": 64,
    b"ic07": 128, b"ic08": 256, b"ic09": 512, b"ic10": 1024,
    b"ic11": 32, b"ic12": 64, b"ic13": 256, b"ic14": 512,
}


def png_bytes(master: QImage, size: int) -> bytes:
    """每个尺寸直接从母版缩放，保留透明度。"""
    scaled = master.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not scaled.save(buffer, "PNG"):
        raise RuntimeError(f"PNG 编码失败：{size}")
    return bytes(data)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    master = QImage(str(ASSETS / "app_icon_master.png"))
    if master.isNull() or master.width() != master.height() or master.width() < 1024:
        raise ValueError("母版必须为边长至少 1024 像素的方形 PNG")
    frames = {size: png_bytes(master, size) for size in set(ICO_SIZES) | set(ICNS_SIZES.values())}
    offset = 6 + 16 * len(ICO_SIZES)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(ICO_SIZES)))
    for size in ICO_SIZES:
        data = frames[size]
        directory.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    (ASSETS / "app_icon.ico").write_bytes(bytes(directory) + b"".join(frames[s] for s in ICO_SIZES))
    chunks = b"".join(kind + struct.pack(">I", len(frames[size]) + 8) + frames[size]
                      for kind, size in ICNS_SIZES.items())
    (ASSETS / "app_icon.icns").write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)

    # 实际像素大小，分别覆盖明暗桌面；文字仅出现在预览，资源保持纯图形。
    preview = QImage(720, 360, QImage.Format.Format_ARGB32)
    painter = QPainter(preview)
    sizes = (16, 24, 32, 48, 64, 128)
    for row, (background, foreground) in enumerate((("#F4F5F7", "#25282D"), ("#18191B", "#E8E9EC"))):
        painter.fillRect(0, row * 180, 720, 180, QColor(background))
        painter.setPen(QColor(foreground))
        for column, size in enumerate(sizes):
            x = 60 + column * 120
            painter.drawImage(x - size // 2, row * 180 + 75 - size // 2, QImage.fromData(frames[size]))
            painter.drawText(x - 40, row * 180 + 148, 80, 24, Qt.AlignmentFlag.AlignCenter, f"{size} px")
    painter.end()
    path = ROOT / "doc" / "screenshots" / "M39_application_icon_sizes.png"
    if not preview.save(str(path)):
        raise RuntimeError(f"预览保存失败：{path}")
    print(f"图标已生成：{ASSETS}；预览：{path}")


if __name__ == "__main__":
    main()
