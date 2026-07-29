#!/usr/bin/env bash
# macOS 一键打包脚本
#
# 自动：venv 创建 + 依赖安装 + PyInstaller 打包 + updater 嵌入
# 自动清 quarantine，打完即可双击运行
#
# 用法：  ./scripts/build_macos.sh
# 产物：  dist/SatelliteDebugTool.app                       ← 双击运行
#         release/SatelliteDebugTool-macOS-<arch>.zip       ← 分发用
#         release/DeviceSimulator-macOS-<arch>.zip
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

ARCH="$(uname -m)"
RELEASE_DIR="$HERE/release"
mkdir -p "$RELEASE_DIR"

# ---- 读 __version__ 仅用于打印 ----
VERSION="$(grep -E '^__version__\s*=' "$HERE/satellite_debug_tool/__init__.py" \
  | sed -E 's/.*"([^"]+)".*/\1/')"
echo "[build_macos] 版本: ${VERSION}，架构: ${ARCH}"

# ---- 自动 venv ----
VENV="$HERE/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[build_macos] 没有 .venv，自动创建..."
  python3 -m venv "$VENV"
fi
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

# ---- 自动装依赖 ----
echo "[build_macos] 升级 pip + 装依赖（首次可能要 1-2 分钟）..."
$PIP install --quiet --upgrade pip
$PIP install --quiet -r "$HERE/satellite_debug_tool/requirements.txt"
$PIP install --quiet pyinstaller

# ---- 国际化资源完整性 ----
echo "[build_macos] 校验 TS/QM 翻译资源..."
$PY "$HERE/scripts/update_translations.py" check

# ---- 清旧产物 ----
rm -rf "$HERE/build" "$HERE/dist"

echo "[build_macos] 1/3 打包主程序..."
$PY -m PyInstaller --noconfirm satellite_debug_tool.spec

echo "[build_macos] 2/3 打包 updater..."
$PY -m PyInstaller --noconfirm updater.spec

echo "[build_macos] 3/3 打包 device_simulator..."
$PY -m PyInstaller --noconfirm device_simulator.spec

# ---- 嵌入 updater 到 .app ----
APP_PATH="$HERE/dist/SatelliteDebugTool.app"
if [[ -d "$APP_PATH" && -d "$HERE/dist/updater" ]]; then
  APP_MACOS="$APP_PATH/Contents/MacOS"
  # ditto 保留 symlink + 资源 fork（mac native）
  ditto "$HERE/dist/updater/_internal" "$APP_MACOS/_internal"
  cp "$HERE/dist/updater/updater" "$APP_MACOS/updater"
  chmod +x "$APP_MACOS/updater"
  echo "[build_macos] ✅ updater 已嵌入 .app/Contents/MacOS/"
fi

# ---- 清 quarantine（本机直接打开不再报"无法验证开发者"） ----
xattr -cr "$APP_PATH" 2>/dev/null || true

# ---- 分发用 zip（ditto 保 symlink，避免之前 7z 那种 symlink 被毁的坑）----
APP_ZIP="$RELEASE_DIR/SatelliteDebugTool-macOS-${ARCH}.zip"
SIM_ZIP="$RELEASE_DIR/DeviceSimulator-macOS-${ARCH}.zip"
rm -f "$APP_ZIP" "$SIM_ZIP"
( cd "$HERE/dist" && ditto -c -k --sequesterRsrc --keepParent "SatelliteDebugTool.app" "$APP_ZIP" )
( cd "$HERE/dist" && zip -rq "$SIM_ZIP" "DeviceSimulator" )

echo ""
echo "==== ✅ 打包完成 ===="
echo "本机直接跑：  open '$APP_PATH'"
echo "分发 zip：    $APP_ZIP"
echo "模拟器 zip：  $SIM_ZIP"
echo ""
ls -lh "$APP_ZIP" "$SIM_ZIP"
echo ""
echo "对方拿到 zip 解压后首次双击若报"无法验证开发者"，让对方终端跑："
echo "    xattr -cr <解压目录>/SatelliteDebugTool.app"
