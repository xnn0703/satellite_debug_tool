# 打包可执行文件

本工具使用一套源码生成两个独立桌面产物。用户无需安装 Python；两版版本号统一读取 `satellite_debug_tool/__init__.py.__version__`。

## 版本与产物

| 内容 | 用户版 `user` | 运维版 `ops` |
|---|---|---|
| 功能 | 总览、RF 控制、回放、加密固件升级、Shell（TCP/串口/YMODEM） | 全量客户、工程、试产、测试与夹具工具 |
| 源码启动 | `python3 -m satellite_debug_tool.main_user` | `python3 -m satellite_debug_tool.main` |
| Windows 安装目录 | `dist/user/SatelliteTerminalToolUser/` | `dist/ops/SatelliteDebugTool/` |
| macOS app | `dist/user/SatelliteTerminalToolUser.app` | `dist/ops/SatelliteDebugTool.app` |
| macOS bundle identifier | `com.satellite-debug-tool.user` | `com.satellite-debug-tool.desktop` |
| 设置、日志与更新目录 | `~/.satellite_debug_tool/user/` | `~/.satellite_debug_tool/` |

用户版首次启动使用空设备目录，由用户显式添加。运维版继续使用原全量版配置；两版独立设置，部分共享模型缓存仍沿用原路径。

Windows 两版均运行安装目录内的 `SatelliteDebugTool.exe`，并保留同层 `updater.exe` 与 `_internal/`。独立目录允许两版共存。构建身份保存在包内 `satellite_debug_tool/build_identity.json`，包含 edition、version、platform；运行时采用固定入口及该身份。

## 本地一键打包

```bash
# macOS；省略参数时构建 ops
./scripts/build_macos.sh user
./scripts/build_macos.sh ops
./scripts/build_macos.sh both
```

```bat
rem Windows；省略参数时构建 ops
scripts\build_windows.bat user
scripts\build_windows.bat ops
scripts\build_windows.bat both
```

平台脚本准备 venv、安装依赖，再调用 `scripts/build_editions.py`。该 Python 构建器统一执行翻译检查、主程序与 updater 打包、模型资源及模块裁剪审计；macOS 额外执行 ad-hoc 签名、签名验证、清检疫属性与 `ditto` 归档。

| 本地文件 | 用途 |
|---|---|
| `release/SatelliteDebugTool-user-macOS-<arch>.zip` | mac 用户版 |
| `release/SatelliteDebugTool-ops-macOS-<arch>.zip` | mac 运维版 |
| `release/SatelliteDebugTool-user-Windows-<arch>.zip` | Windows 用户版手动安装 |
| `release/SatelliteDebugTool-ops-Windows-<arch>.zip` | Windows 运维版手动安装 |
| `release/build-user.json`、`build-ops.json` | 包体积、路径与构建耗时 |

macOS zip 保留 app 内符号链接，可解压后拖入 Applications。Linux 当前没有受支持的发行构建流程。

## 裁剪与资源验证

主程序 spec 读取构建参数 `SATELLITE_BUILD_EDITION`，分别使用固定 user/ops 入口。用户版按 `scripts/edition_policy.py` 裁剪运维 UI、执行模块及专用依赖，保留 3D、曲线、录制/回放、认证 OTA 与完整 Shell 所需组件。Leaflet/map、QtWebEngine、python-docx 等运维依赖从用户包移除。历史 SDB 读取需要的纯数据模型仍共享。

构建器自动运行：

```bash
python3 scripts/verify_model_assets.py <payload>
python3 scripts/verify_edition_package.py <payload> --edition user --toc build/user/satellite_debug_tool/Analysis-00.toc
```

原生冻结包烟测使用临时设置、回环设备目录，验证总览 SNR/模块温度面板已入布局且可见、遍历本版页面、载入 3D 模型并验证 Shell 弹窗复用/关闭，不连接硬件：

```bash
QT_QPA_PLATFORM=cocoa SATELLITE_UPDATE_CHECK=0 dist/user/SatelliteTerminalToolUser.app/Contents/MacOS/SatelliteDebugTool --smoke-test --smoke-output=/tmp/user-smoke.json
```

另行检查两种语言、三种主题和正常退出。源码测试不能替代冻结包资源检查；软件烟测不能替代设备连接、RF、固件升级及夹具实测。

## Windows CI 与自动更新

`.github/workflows/build.yml` 的 `build-windows` 采用 user/ops 矩阵。每份载荷通过检查后上传 workflow artifact，由唯一 `publish-windows` job 在两版成功后共同发布。推送 master/main 仅运行双版 `ci-only-build`、上传 artifact；tag 或手动 dispatch 进入发布链路。提交、推送与发布需按用户明确指令执行。

同一 Release 的正式资产：

| edition | 资产名称 |
|---|---|
| user | `satellite_terminal_tool-user-windows-v<version>.7z` |
| ops | `satellite_debug_tool-win-v<version>.7z` |

7z 根目录直接包含 `SatelliteDebugTool.exe`、`updater.exe`、`_internal/`；从对应 Windows 安装目录的内容压缩，使用 `-mf=off`。CI 调用 `scripts/verify_update_archive.py <archive> --edition <user|ops> --version <tag>` 校验唯一载荷与身份。

用户资产名称避开旧全量更新器的 `-win-*.7z` 正则。当前更新器精确匹配 edition/platform/version，并在替换安装前验证载荷与已安装身份。只有历史 ops 目标版本 `<=1.7.0` 允许缺少身份文件；后续版本两版均要求身份。macOS 继续本地分发 zip，当前不提供应用内自动更新资产。

发布步骤见 [doc/RELEASING.md](doc/RELEASING.md)。

## 翻译与图标

修改第一方 UI 文案后同时更新 TS/QM：

```bash
python3 scripts/update_translations.py update
python3 scripts/update_translations.py check
```

`check` 检查 unfinished、空翻译、占位符、词条与 QM 同步。构建前必须通过。

应用显示名称为“卫星通信终端工具 / Satellite Terminal Tool”，窗口与包使用同一卫星图标。主图为 `satellite_debug_tool/ui/assets/icons/app_icon_master.png`；更换已确认图稿后运行 `python3 scripts/generate_application_icons.py`，生成 Windows ico、macOS icns 与 Qt PNG。

## 排查

- 构建缺模块：检查实际导入路径、spec 和 user 模块白名单，再重新执行本版构建与冻结包烟测。
- Mac 检疫提示：确认来源后，通过系统设置的隐私与安全性允许打开；必要时对实际解压 app 执行 `xattr -dr com.apple.quarantine <app>`。构建采用 ad-hoc 签名，尚未进行 Apple 公证。
- Release 上传失败：检查 `contents: write`、Actions 权限与两份验证后的资产。
- 用户版体积仍包含 Qt/OpenGL/numpy：这些组件服务于保留的 3D 和曲线功能；包体积以本次平台实际构建指标为准。
