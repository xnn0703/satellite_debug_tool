# 打包可执行文件

本工具基于 PySide6，打包成桌面可执行文件后用户无需安装 Python/依赖，双击即可使用。

## 产物

每次构建会在 `release/` 下生成两个 zip：

| 文件 | 内容 |
|------|------|
| `SatelliteDebugTool-<OS>-<arch>.zip` | 上位机主程序（GUI） |
| `DeviceSimulator-<OS>-<arch>.zip` | 模拟器命令行工具（离线 demo 用） |

目录结构（onedir 模式）：

```
SatelliteDebugTool/
├── SatelliteDebugTool(.exe)       ← 双击运行
└── _internal/                      ← Python/Qt 运行时，勿改
```

macOS 额外产出 `SatelliteDebugTool.app`，可拖进 /Applications。

---

## 方式一：本地一键打包

### macOS / Linux

```bash
# 先确保 venv 已装好依赖（pip install -r satellite_debug_tool/requirements.txt）
./scripts/build_macos.sh
```

生成 `release/SatelliteDebugTool-macOS-<arm64|x86_64>.zip`。

**首次启动系统提示"无法验证开发者"**：

- 系统设置 → 隐私与安全性 → "仍要打开"
- 或终端执行：`xattr -dr com.apple.quarantine dist/SatelliteDebugTool.app`

### Windows

双击 `scripts\build_windows.bat`（或 cmd 里运行）。

生成 `release\SatelliteDebugTool-Windows-x86_64.zip`。

**首次启动提示 SmartScreen 未签名**：

- 点 "更多信息" → "仍要运行"

### 依赖

脚本会自动检查 `PySide6 / pyqtgraph / pyserial / numpy / pyinstaller`，缺了会 `pip install`。
如果系统 Python 干净建议先创建 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r satellite_debug_tool/requirements.txt
pip install pyinstaller
```

---

## 方式二：GitHub Actions 自动构建

已内置 `.github/workflows/build.yml`，**push/tag/手动** 任一方式触发。

### 触发方式

- **Push 到 main/master** 或打 tag `v*.*.*`：自动跑所有平台
- 在 Actions 页面手动：点 `Run workflow`
  - `mark_release=true` 会同时创建**草稿 Release**，把 3 份 zip 挂上去

### 构建矩阵

| Runner | 产物 |
|--------|------|
| macos-14 (Apple Silicon) | `SatelliteDebugTool-macOS-arm64.zip` + 模拟器 |
| macos-13 (Intel) | `SatelliteDebugTool-macOS-x86_64.zip` + 模拟器 |
| windows-latest | `SatelliteDebugTool-Windows-x86_64.zip` + 模拟器 |

### 下载产物

- **构建完成**：Actions → 选中该 run → 下拉滚动找 **Artifacts** 卡片 → 点 zip 下载（保留 30 天）
- **打 tag 后**：Releases 页面找到对应版本的草稿 Release，把所有 zip 都下下来

### tag 一次性出 3 平台正式 release

```bash
git tag v0.1.0
git push origin v0.1.0
# CI 构建完成后，到 Releases 页把草稿发布即可
```

---

## 手动调用 PyInstaller

如果想改细节（加 icon、嵌入 data 文件等），直接编辑 `satellite_debug_tool.spec` / `device_simulator.spec` 再：

```bash
pyinstaller --noconfirm satellite_debug_tool.spec
pyinstaller --noconfirm device_simulator.spec
```

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `satellite_debug_tool.spec` | PyInstaller 主程序 spec，含 `collect_all(PySide6/pyqtgraph)` |
| `device_simulator.spec` | 模拟器 spec，轻量（排除 PySide6/pyqtgraph） |
| `scripts/build_macos.sh` | macOS/Linux 一键脚本 |
| `scripts/build_windows.bat` | Windows 一键脚本 |
| `.github/workflows/build.yml` | CI workflow（macos+windows 矩阵 + 可选 Release） |

---

## 常见问题

### Q：.app 体积 228 MB 太大？

A：onedir 自带完整 PySide6 + Qt runtime + pyqtgraph。想要小可以换 `onefile` 模式
（改 spec 把 `COLLECT/BUNDLE` 去掉，改 `EXE` 的 `exclude_binaries=False`），
单文件 ~120MB，但启动慢且 Qt 插件路径偶有坑。目前保持 onedir。

### Q：打出的包在另一台 Mac 提示"已损坏"

A：未签名 + 沙箱检疫。在目标机上执行：
```bash
xattr -dr com.apple.quarantine /Applications/SatelliteDebugTool.app
```

### Q：Windows 版能在 XP / Win7 上跑吗？

A：不能。PyInstaller 打包的 Python 3.11 + Qt6 最低支持 **Windows 10**。

### Q：Linux 产物呢？

A：CI 没加 Linux runner，本地 `build_macos.sh` 在 Linux 上能直接跑（Ubuntu/Debian 预装 Qt 依赖即可）。
如果要加 CI，在 `.github/workflows/build.yml` 里复制一条 `ubuntu-latest` matrix item、
脚本也用 `bash scripts/build_macos.sh`（文件名不贴切，里面代码 Linux 通用）。
