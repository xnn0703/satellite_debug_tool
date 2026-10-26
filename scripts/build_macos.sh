#!/usr/bin/env bash
# macOS 一键打包脚本
#
# 自动：venv 创建 + 依赖安装 + PyInstaller 打包 + updater 嵌入
# 自动清 quarantine，打完即可双击运行
#
# 用法：  ./scripts/build_macos.sh user|ops|both（默认 ops）
# 产物：  dist/user/SatelliteTerminalToolUser.app、dist/ops/SatelliteDebugTool.app
#         release/SatelliteDebugTool-{user,ops}-macOS-<arch>.zip
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

# Two immutable product editions; separate work/output directories.
"$PY" "$HERE/scripts/build_editions.py" "${1:-ops}"
