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

### 国际化资源

代码中的英文是源文案，简体中文维护在：

```text
satellite_debug_tool/i18n/translations/satellite_debug_tool_zh_CN.ts
satellite_debug_tool/i18n/translations/satellite_debug_tool_zh_CN.qm
```

修改第一方界面文案后先更新并校验：

```bash
python3 scripts/update_translations.py update
python3 scripts/update_translations.py check
```

`check` 会拒绝 `unfinished`、空翻译、占位符不一致、过期词条和不同步的 QM。
macOS/Windows 构建脚本都会在 PyInstaller 前执行该检查。不要只提交 TS 而遗漏 QM。

---

## 方式二：GitHub Actions 自动构建与发版

`.github/workflows/build.yml` 包含以下 job：

| Job | 触发条件 | 用途 |
|------|---------|------|
| `prepare-release` | tag `v*` / 手动 dispatch | 解析发布 tag |
| `build-windows` | tag / dispatch | windows-latest 生成单个 7z 并上传 GitHub Release |
| `ci-only-build` | 推 master/main | 仅 win 本地构建验证，结果上传 GH artifact，不发版 |

> **mac 不走 CI**：PyInstaller .app 含 1500+ symlinks + 7z 压缩兼容性问题，
> 改本地脚本 `./scripts/build_macos.sh` 自动 venv + 装依赖 + ditto 打包。
> 详见下方"本地 mac 打包"章节。

### 发版流程（标准）

```bash
# 1. 改 satellite_debug_tool/__init__.py.__version__
vim satellite_debug_tool/__init__.py    # 例如 "1.0.0" → "1.1.0"

# 2. commit + 打 tag（tag 名要与 __version__ 一致 + 加 v 前缀）
git add satellite_debug_tool/__init__.py
git commit -m "chore: bump version to 1.1.0"
git tag v1.1.0
git push origin master
git push github master
git push origin v1.1.0      # 同步 Gitee 源码 tag
git push github v1.1.0      # 推 GitHub tag 触发 release CI

# 3. 在 GitHub Actions 页面看进度
#    - prepare-release   ~30s
#    - build-windows     ~10min

# 4. 完成后查 GitHub Releases
#    https://github.com/xnn0703/satellite_debug_tool/releases
#    应能看到一个 Windows .7z 资产

# 5. mac 包本地出（仅 win 走 CI；mac 见下一节"本地 mac 打包"）
./scripts/build_macos.sh
# 产物 release/SatelliteDebugTool-macOS-<arch>.zip 自行分发给 mac 用户
```

---

## 方式三：本地 mac 打包（mac 不走 CI）

```bash
./scripts/build_macos.sh
```

脚本自动：
- 创建 `.venv`（如无）
- 装 requirements.txt + pyinstaller
- 跑 PyInstaller（主程序 + updater + simulator）
- 把 updater 嵌入 `.app/Contents/MacOS/`
- `xattr -cr` 清 quarantine（本机直接双击就能开）
- `ditto -c -k` 出单文件 zip 给同事分发

产物：
- `dist/SatelliteDebugTool.app` — 本机直接 `open` 打开
- `release/SatelliteDebugTool-macOS-<arch>.zip` — 分发给同事

对方拿到 zip 解压后首次打开若报"无法验证开发者"，让对方终端跑：
```bash
xattr -cr <解压目录>/SatelliteDebugTool.app
```

### 失败排查

- **build-windows/mac 失败**：查 PyInstaller 输出；通常是新依赖没在 spec 的 `hiddenimports` 里
- **GitHub Release 上传失败**：检查 workflow 的 `permissions: contents: write` 和仓库 Actions 权限
- **资产缺失**：确认 `Create 7z archive` 生成的文件名与 `softprops/action-gh-release` 的 `files` 完全一致

### 仅 CI 构建（不发版）

push 到 master/main 不打 tag → 跑 `ci-only-build`，产物上传 GH artifact（7 天保留）。
Actions → run → Artifacts 卡片下载。

### 手动触发

Actions 页面 → Build & Release → Run workflow → 填 tag（例 `v0.1.0-rc1`）→ Run。
适合预发版本测试，不需要打真实 git tag。

---

## 手动调用 PyInstaller

如果想改细节（加 icon、嵌入 data 文件等），直接编辑 `satellite_debug_tool.spec` / `device_simulator.spec` 再：

```bash
pyinstaller --noconfirm satellite_debug_tool.spec
pyinstaller --noconfirm device_simulator.spec
```

主程序 spec 显式包含 TS/QM、Leaflet/map 资源和 macOS `InfoPlist.strings`。
打包后至少检查：

```text
SatelliteDebugTool/_internal/satellite_debug_tool/i18n/translations/
SatelliteDebugTool/_internal/satellite_debug_tool/ui/assets/map.html
```

macOS `.app` 还应包含 `Contents/Resources/en.lproj` 和
`Contents/Resources/zh_CN.lproj`。最终验收必须分别以 `en_US`、`zh_CN`
启动产物，源码运行成功不能替代打包资源检查。

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `satellite_debug_tool.spec` | PyInstaller 主程序 spec，含 `collect_all(PySide6/pyqtgraph)` |
| `device_simulator.spec` | 模拟器 spec，轻量（排除 PySide6/pyqtgraph） |
| `scripts/build_macos.sh` | macOS/Linux 一键脚本 |
| `scripts/build_windows.bat` | Windows 一键脚本 |
| `.github/workflows/build.yml` | Windows CI 构建与可选 Release |

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
