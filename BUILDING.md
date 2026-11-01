# 卫通终端管理工具构建指南

产品名称为“卫通终端管理工具 / Satellite Terminal Manager”。同一源码生成 user、ops 两个独立产物；客户解压完整包即可使用，无需 Python。版本唯一来源为 `satellite_debug_tool/__init__.py.__version__`，当前发行版本为 v1.9.0；逐包发布状态、源码 SHA 和校验证据见 [发行验证记录](doc/M53_release_validation.json)。

## 产物与身份

| 内容 | 用户版 user | 运维版 ops |
|---|---|---|
| 功能 | 总览、RF 控制、回放、加密固件升级、Shell | 全量客户、工程、试产、测试与夹具工具 |
| 源码入口 | `python3 -m satellite_debug_tool.main_user` | `python3 -m satellite_debug_tool.main` |
| Windows 目录 | `dist/user/SatelliteTerminalManager/` | `dist/ops/SatelliteTerminalManagerOps/` |
| macOS app | `dist/user/SatelliteTerminalManager.app` | `dist/ops/SatelliteTerminalManagerOps.app` |
| macOS bundle identifier | `com.satellite-debug-tool.user` | `com.satellite-debug-tool.desktop` |
| 设置与日志目录 | `~/.satellite_debug_tool/user/` | `~/.satellite_debug_tool/` |
| 应用更新 | 人工下载完整包 | TUF 可信在线更新；Windows 自动安装、Mac 人工安装 |

目录、主程序和归档名称的唯一代码合同是 `satellite_debug_tool/package_names.py`。Windows 主程序为 `SatelliteTerminalManager.exe`，macOS app 内主程序为 `Contents/MacOS/SatelliteTerminalManager`。ops 同时包含 updater，user 裁剪应用 updater 与在线源配置。两版保留完整 Shell（TCP/串口/YMODEM）。

客户版归档格式为 `satellite_terminal_manager-<platform>-<arch>-v<version>.<extension>`，运维全量版为 `satellite_terminal_manager-ops-<platform>-<arch>-v<version>.<extension>`，安装目录及 app 名称不含版本号。Windows 候选完整包使用 `.7z`，macOS 使用保留 app 符号链接的 `.zip`；架构为 `x86_64` 或 `arm64`。Windows 归档根直接包含主程序及 `_internal/`，ops 还包含 `updater.exe`。

内部 Python 包、app/repo 信任身份、bundle identifier、配置目录和设备固件 AES-GCM 认证域保持原合同。旧版本 <=1.8.1 首次人工取得 v1.9.0 完整包；历史归档保持原样。

## 本地构建

源码最低 Python 3.10，打包工具链固定 Python 3.11。先使用 Python 3.11 建立构建 venv，再运行平台脚本；已有 venv 应先确认解释器版本。

```bash
python3.11 -m venv .venv
.venv/bin/python --version
./scripts/build_macos.sh both
```

```bat
py -3.11 -m venv .venv
.venv\Scripts\python --version
scripts\build_windows.bat both
```

两平台脚本支持 `user|ops|both`，省略参数时为 ops。它们安装平台约束依赖，由 `scripts/build_editions.py` 完成翻译检查、PyInstaller 构建、模型与 edition 审计、归档。普通构建仅写本地；正式发行使用下节候选流程。

v1.9.0 发行目录声明 Windows 最低系统为 `10.0.19041`；macOS spec 声明 `LSMinimumSystemVersion=14.0`。实际冻结依赖的最低系统要求仍须检查，不把此声明视为兼容性实测。当前签名为 ad-hoc，构建包含签名验证，Apple 公证未完成。Linux 没有受支持的发行流程。

## 验证与正式候选

```bash
PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q
.venv/bin/python scripts/update_translations.py check
.venv/bin/python scripts/check_frozen_application.py --edition user --version 1.9.0
.venv/bin/python scripts/check_frozen_application.py --edition ops --version 1.9.0
```

冻结 smoke 使用临时设置和回环设备目录，检查页面装配、总览面板、3D 资源、Shell 与受控退出。包内 `build_identity.json` 必须匹配 edition、版本、平台、架构及源 SHA。Windows 候选由 Actions 双版构建、验证后自动上传 ECS；macOS 在本机使用 Python 3.11 调用 `scripts/release_macos.py`，完成同一指定干净 SHA 的构建、冻结 smoke、候选校验和上传。

正式命令与签名晋升步骤见 [发行 SOP](doc/RELEASING.md)。user 完整包公开供人工下载，ops 完整包经签名晋升 stable。首份可信正式版本没有上一基线，只发布完整包。发布后提供服务器 `/downloads/v1.9.0/<完整归档文件名>` 直链，不要求客户取得旧版本。

## 翻译与图标

修改第一方 UI 文案后同步更新 TS/QM：

```bash
.venv/bin/python scripts/update_translations.py update
.venv/bin/python scripts/update_translations.py check
```

图标母版为 `satellite_debug_tool/ui/assets/icons/app_icon_master.png`，资源生成入口为 `scripts/generate_application_icons.py`。用户版模块边界由 `scripts/edition_policy.py` 定义；缺模块应检查实际导入、spec 与白名单，重新构建并执行冻结 smoke。

源码测试、冻结启动、签名及归档校验分别提供软件证据。Windows 原生显卡渲染、实际安装更新和物理设备/RF/夹具验收独立记录。
