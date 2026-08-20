# AGENTS.md

## 运行命令

```bash
# 启动 GUI（必须用 -m，包内绝对 import）
python3 -m satellite_debug_tool.main
python3 -m satellite_debug_tool.main --production   # 解锁批量试产工作区（启动即进）

# 测试（无 editable install 时需要 PYTHONPATH；62 个测试，conftest 已设 offscreen + 静默更新）
PYTHONPATH=. pytest satellite_debug_tool/tests
pytest satellite_debug_tool/tests/test_frame_v2.py -v      # 单文件
pytest satellite_debug_tool/tests -k "crc"                 # 按 pattern
pytest satellite_debug_tool/tests -k "production"          # 仅试产相关

# 依赖安装
pip3 install -r satellite_debug_tool/requirements.txt
pip3 install -e satellite_debug_tool                       # editable（IDE 跳转需要）
pip3 install pyinstaller                                   # 仅打包需要

# 国际化资源（修改文案后必须 update + check；构建脚本都会跑 check）
python3 scripts/update_translations.py update
python3 scripts/update_translations.py check
```

**没有 linter / typecheck / formatter 配置。** pytest 是当前唯一自动化验证手段；当前没有固定允许失败的 baseline，全量测试失败按真实回归调查。

## 工作区（顶层 `MainWindow`）

`MainWindow` 是个 `QStackedWidget`，三页：

| 索引 | 工作区 | 入口 | 备注 |
|------|--------|------|------|
| 0 | **Customer Workspace**（默认） | 直接进 | 面向 AFD01 终端用户：Overview / RF control / Playback / Maintenance。共享 `LiveView` 实例。 |
| 1 | **Engineering Tabs**（Live / Playback / Log / Device） | `Ctrl+Shift+E` 首次确认后本会话解锁 | 内部诊断、协议解码、设备参数读写、OTA。 |
| 2 | **Production Workspace**（批量试产） | `Ctrl+Shift+P` 首次确认解锁；或启动加 `--production` | M19-A 工程预览证据采集底座，未实现正式试产放行流程。 |

- 三个工作区共享同一个 `LiveView`（worker、握手、`frame_received` 广播）；切换不重建连接。
- 设备 OTA / 参数表读写时 `LiveView` 会 `set_device_transaction_active(True)`，期间禁用握手重试。
- 工程 Tab 内部仍是 `LiveView / PlaybackView / LogView / DeviceView` 四张卡（`QTabWidget`，tabBar 隐藏，顶栏"药丸"接管）。

## 架构

PySide6 桌面应用，调试相控阵卫星通信终端。基于 **DEBUG v2 协议**（`doc/DEBUG设备协议接口规范_v2.md` 是权威规范）；M18 起加入 AFD01 专用 **Product Service 协议**（cmd 0x20–0x2B），XESA01 Orbit 使用独立 0x30–0x31，envelope 仍兼容 v2。

### 数据流

```
Live Tab     Device → SerialWorker/UdpWorker → FrameReceiverV2 → records
                                                       ├→ Handshake 消费 META/DEFINE/HEARTBEAT → ProfileStore (Live)
                                                       ├→ DataReport → DataStore (ring 30000) → GroupedChart @5Hz
                                                       ├→ StateReport/EventReport → StateStore/EventLog
                                                       └→ Service* records (M18) → ProductServiceStore → CustomerWorkspace

Playback     .sdb v2/v3 → DataImporter → DataStore (无界) + 内嵌 profile
Log          .log   → WindTermLogParser → 虚拟 ProfileStore (hw_type="windterm_log") + DataStore (无界, max=128)
Device       共享 Live worker + frame stream → 参数表 / COMMAND_RESPONSE / OTA 状态机
Production   Product Service 单播 SUBSCRIBE → 参与设备冻结 → ResultStore + SDB（M19-A）
```

### 包结构（已演进出新模块，AGENTS 要跟得上）

- `core/protocol/` — v2 协议帧编解码（`frame_v2` 常量、`codec_v2` 解码、`frame_receiver_v2` 状态机、`handshake`、`crc16`）
- `core/product/` — **M18+** AFD01 Product Service 协议（`models.py` 数据类、`service_store.py`、`playback_projection.py`、`recording_state.py`、`timestamps.py`、`legacy_v2.py` 兼容映射）
- `core/comm/` — QThread worker：`BaseWorker`（QThread 基类）→ `SerialWorker` / `UdpWorker`
- `core/data/` — `ChannelBuffer`（环形 ndarray 或无界 list）、`DataStore`、`StateStore`、`EventLog`、`gnss_store.py`
- `core/profile/` — `ProfileStore` + `ProfileCache`，设备 profile 驱动 UI；`semantics.py` 通道语义；`ins_yaw_display.py` 内部 INS 航向处理
- `core/production/` — **M19** 试产夹具：`recipe.py`（配方）、`fleet.py`（多设备并发）、`fixtures.py`（GW Instek PSW 电源 + 运动平台抽象）、`motion_platform.py`、`power_supply.py`、`result_store.py`（SQLite 落盘）
- `core/log_parser/` — WindTerm 日志解析
- `core/security/` — 客户 OTA 固件包 Ed25519 验签（`firmware_package.py`）
- `core/link_trace.py` — 帧 trace 日志
- `core/config.py` — `Settings` 类，JSON 存 `~/.satellite_debug_tool/settings.json`
- `ui/` — PySide6 组件；`MainWindow` 顶层；`live_view / playback_view / log_view / device_view / customer_workspace / production_workspace / map_widget / chart_widget / grouped_chart_widget / attitude_widget / device_view / settings_dialog / update_dialog`
- `ui/assets/` — Leaflet 离线地图 bundle + STL 模型
- `i18n/` — `TranslationManager` + TS/QM 翻译资源（`translations/satellite_debug_tool_zh_CN.{ts,qm}`）
- `io/` — `DataRecorder`（异步 threading.Thread + queue；支持 SDB v2/v3，文件头内嵌 profile JSON）、`DataImporter`
- `tools/` — `tile_downloader.py`（OSM 离线 tile CLI）、`build_signed_firmware_package.py`（客户 OTA 固件打包工具）
- `updater/` — 自动升级模块：`ReleaseChecker / Downloader / Applier`，单文件 `updater` 二进制在 PyInstaller 后嵌入 `.app/Contents/MacOS/updater`
- `platform/macos/{en,zh_CN}.lproj/InfoPlist.strings` — macOS `CFBundleLocalizations` 用的本地化 InfoPlist 字符串

### 关键约定

- 所有 import 用 `satellite_debug_tool.` 前缀（绝对导入；这是为什么必须 `python3 -m ...`）
- UI 在 `ui/`，业务在 `core/`，持久化在 `io/`，**试产业务在 `core/production/`，AFD01 产品服务协议在 `core/product/`**
- worker 线程（`BaseWorker` 子类）**严禁**直接操作 widget；必须 emit Qt 信号，主线程消费
- **每个 Tab/工作区独立 `DataStore` / `ProfileStore`**（M7 引入），切换不污染；CustomerWorkspace 和 LiveView 共享的是同一个 `LiveView` 实例，所以底层 DataStore 实际同一份
- 协议帧格式：`AA 55 0D` + cmd_type(1B) + len(2B LE) + data + CRC16-CCITT(2B LE) + `EE`；命令仅分配 `0x01..0x10`、`0x20..0x2B`、`0x30..0x31` 三段；DATA 段上限 `MAX_DATA_LENGTH=1536`，`MAX_FRAME_LENGTH=1548` 是设备端保守缓冲值（实际线上帧开销 9 B、最大 1545 B），DATA_REPORT 单帧最大 64 通道
- 通用长帧扩容不改变专用上传分片合同：`OTA_DATA` 每片 1..1021 B（UI 通常发送 512 B），Orbit `UPLOAD_CHUNK` 每片 1..1012 B
- 测试在 `satellite_debug_tool/tests/`（62 个文件），名称和注释多为中文；UI 测试用 `qapp` fixture 复用 QApplication
- `conftest.py` 自动设 `QT_QPA_PLATFORM=offscreen` + `SATELLITE_NO_UPDATE_CHECK=1` + `SATELLITE_DEBUG_LOCALE=zh_CN`，**绝不要**在测试代码里访问 Gitee/GitHub API
- 字号已固化 `small`（`base_px=13`，`main.py` 调 `S.apply_global_font(app, scale="small", base_px=13)`）；`styles.FONT_SCALES` / `FontScale` API 仅保留兼容 `test_styles.py`，UI 不再暴露
- 主题三档 `dark / dark_hc / light`，由 `S.palette()` 出语义色键（兼容键 + Mission Console 新语义键），顶栏图标按钮循环切换
- 离线地图约定 GPS channel 名 `gps_lat` / `gps_lon`（可选 `gps_alt`），Playback / Log 检测到自动启用"地图"按钮
- **客户工作台 `CustomerWorkspace` 共享 `LiveView` 实例**——改 Customer view 时不要新建自己的 DataStore/ProfileStore，否则与 Live Tab 状态分裂

## 持久化路径（`~/.satellite_debug_tool/`）

| 路径 | 用途 |
|------|------|
| `settings.json` | `core/config.Settings` 用户配置（连接参数、UI 偏好、路径、试产参数） |
| `profiles/{hw_type}.json` | ProfileCache 缓存的设备 profile |
| `tiles/{region}/{z}/{x}/{y}.png` | M8 OSM 离线 tile（按区域分组） |
| `updates/<tag>/updater.log` | 自动升级日志；升级失败时排查用 |
| `production_batches/<batch_id>/` | M19-A 工程预览批次产物：数据库 + SDB；正式报告仍待后续里程碑实现 |

## 构建与发版

- **macOS 本地打包**：`./scripts/build_macos.sh`（自动 venv + 装依赖 + 跑 `update_translations.py check` + PyInstaller 主程序+updater + 嵌入 `.app/Contents/MacOS/updater` + `codesign --force --deep --sign -` ad-hoc 签名 + `xattr -cr` 清 quarantine + `ditto -c -k` 出 zip）
- **Windows 一键打包**：`scripts\build_windows.bat`（PyInstaller + `verify_model_assets.py` 校验 3D 模型 + updater 嵌入 + `Compress-Archive` 出 zip）
- **CI**：`.github/workflows/build.yml` 只跑 Windows（`prepare-release` + `build-windows`，7z 分卷上传 GitHub Release；推 master/main 触发 `ci-only-build` 上传 artifact 不发版）
- **mac 不走 CI**：PyInstaller .app 含 1500+ symlinks + 7z 兼容性问题，强制本地脚本。updater 二进制仍嵌入但 mac 平台不发版，触发频率为零
- **版本号唯一来源**：`satellite_debug_tool/__init__.py.__version__`（当前 `1.1.1`）；mac `Info.plist` 的 `CFBundleVersion` 由 `satellite_debug_tool.spec` 自动读这个值
- **打 tag 发版**：`git tag v<__version__>` + `git push github v<__version__>` 触发 CI；mac 本地手动 `./scripts/build_macos.sh`
- **`release.config.json`** 配 GitHub owner/repo/api；updater 用 `release_config.py` 读取
- **修改第一方 UI 文案后**：`python3 scripts/update_translations.py update` + commit TS+QM；只交 TS 不交 QM 会让 `check` 失败

### Spec 关键钩子

- `satellite_debug_tool.spec` — 主程序 spec；`collect_all(PySide6/pyqtgraph/OpenGL)`；过滤掉 PySide6 自带但不需要的 Designer/Assistant/Linguist.app（保留 `QtWebEngineProcess.app`）；`tests` 子模块不进发行包
- `updater.spec` — 单文件 updater 二进制
- 新依赖若 PyInstaller 找不到：`hiddenimports` 加一行；常见坑：GLU 子模块（`collect_all("OpenGL")` 已覆盖）

## 关键文档（按重要性）

- `doc/DEBUG设备协议接口规范_v2.md` — **协议权威规范**
- `doc/upper_pc_function_definition_vnext.md` — 当前上位机功能定义与路线
- `doc/optimization_plan.md` — M1–M6 整体优化计划（v1.2）
- `doc/M7_*.md` ~ `doc/M19_*` — 各里程碑 plan/acceptance/dev_log（M7 Tab 化、M8 离线地图、M10/M11 升级、M12 归一化、M13 通道语义、M14 ESA01、M15 GNSS truth、M16 i18n English、M17 内置 3D 模型、M18 客户工作台 + Product Service、M19 批量试产）
- `doc/development_log.md` — M1–M6 实施日志
- `doc/acceptance_log.md` — F-/A- 系列验收跟踪
- `doc/i18n_terms.md` — 中英术语表
- `doc/user_manual.md` / `doc/user_manual_en.md` — 用户手册
- `doc/RELEASING.md` — 发版 SOP（含 tag 重发、hotfix、rc 预发）
- `doc/AFD01_signed_firmware_package.md` — 客户 OTA 固件包签名格式
- `BUILDING.md` — 打包可执行文件完整指南
