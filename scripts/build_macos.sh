#!/usr/bin/env bash
# macOS 一键打包脚本。
# 前提：项目根目录下有 .venv（或当前已激活的 venv），安装了 requirements.txt + pyinstaller。
#
# 用法：  ./scripts/build_macos.sh
# 产物：  release/SatelliteDebugTool-macOS-<arch>.zip
#         release/DeviceSimulator-macOS-<arch>.zip
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

ARCH="$(uname -m)"                           # arm64 / x86_64
RELEASE_DIR="$HERE/release"
mkdir -p "$RELEASE_DIR"

# ---- 挑 Python ----
if [[ -x "$HERE/.venv/bin/python" ]]; then
    PY="$HERE/.venv/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" ]]; then
    PY="$VIRTUAL_ENV/bin/python"
else
    PY="$(command -v python3)"
fi
echo "[build_macos] 使用 Python: $PY ($($PY --version))"

# ---- 依赖自检 ----
if ! $PY -c "import PyInstaller" >/dev/null 2>&1; then
    echo "[build_macos] 安装 pyinstaller..."
    $PY -m pip install pyinstaller
fi
$PY -c "import PySide6, pyqtgraph, serial, numpy, py7zr, psutil, OpenGL" || {
    echo "[build_macos] 运行依赖缺失，执行 pip install -r satellite_debug_tool/requirements.txt 后重试"
    exit 1
}

# ---- 清旧产物 ----
rm -rf build dist

echo "[build_macos] 打包 主程序..."
$PY -m PyInstaller --noconfirm satellite_debug_tool.spec

echo "[build_macos] 打包 updater（M11 升级器）..."
$PY -m PyInstaller --noconfirm updater.spec

echo "[build_macos] 打包 模拟器..."
$PY -m PyInstaller --noconfirm device_simulator.spec

# ---- M11：把 updater 二进制塞进 .app 的 Contents/MacOS 旁边 ----
# updater 自带 _internal/，整个目录复制到 Contents/MacOS/ 下
if [[ -d "dist/SatelliteDebugTool.app" && -d "dist/updater" ]]; then
    APP_MACOS="dist/SatelliteDebugTool.app/Contents/MacOS"
    cp -R dist/updater/_internal "$APP_MACOS/" 2>/dev/null || true
    cp dist/updater/updater "$APP_MACOS/updater"
    chmod +x "$APP_MACOS/updater"
    echo "[build_macos] ✅ updater 已嵌入 $APP_MACOS/"
fi

# ---- 归档 ----
APP_ZIP="$RELEASE_DIR/SatelliteDebugTool-macOS-$ARCH.zip"
SIM_ZIP="$RELEASE_DIR/DeviceSimulator-macOS-$ARCH.zip"
rm -f "$APP_ZIP" "$SIM_ZIP"

# 主程序：打包 .app（双击可运行）而不是整个 onedir，体积更小
if [[ -d "dist/SatelliteDebugTool.app" ]]; then
    ( cd dist && ditto -c -k --sequesterRsrc --keepParent "SatelliteDebugTool.app" "$APP_ZIP" )
    echo "[build_macos] ✅ $APP_ZIP"
else
    ( cd dist && zip -rq "$APP_ZIP" "SatelliteDebugTool" )
    echo "[build_macos] ⚠️ 未生成 .app bundle，回退到 onedir；$APP_ZIP"
fi

( cd dist && zip -rq "$SIM_ZIP" "DeviceSimulator" )
echo "[build_macos] ✅ $SIM_ZIP"

echo ""
echo "==== 完成。产物在 $RELEASE_DIR ===="
ls -lh "$RELEASE_DIR"
echo ""
echo "提示：首次打开 .app 系统会提示『无法验证开发者』，"
echo "      在 系统设置 → 隐私与安全性 → 仍要打开 即可；"
echo "      或终端执行 xattr -dr com.apple.quarantine dist/SatelliteDebugTool.app"
