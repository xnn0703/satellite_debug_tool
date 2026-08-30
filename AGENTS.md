# AGENTS.md

## 最高优先级：根因修复与肯定式逻辑

- 根因级修复是默认交付方式：先用稳定复现、调用链和状态证据定位根因，再修改根因所属的状态源、协议合同或数据流。超时放宽、重复重试、额外轮询、吞异常、硬编码设备特例和 UI 补偿只用于定位，不能作为最终修复。
- 根因属于错误模型时，单一正确模型是唯一交付物：直接替换旧实现，并删除被取代的旧分支、兼容层、临时开关、重复状态源、旁路、失效 fallback 和失效测试。一个业务事实只保留一个权威 owner。
- 条件、状态、API、变量、注释、日志、计划、验收语句和界面文案只陈述一个直接、肯定、单义、可验证的领域事实，例如 `ready`、`valid`、`connected`、`has_reference`。禁止 `not_disabled`、`not_invalid`、`no_error == false` 一类双重否定。
- 状态描述只陈述证据已经确认的事实。例如 UDP 写入成功表示“指令已发送”；设备回读确认后才表示“设备在线”“已接受”或“已应用”，物理 RF 仍需独立证据。
- 兼容迁移只服务于明确的产品版本合同，并同步冻结独立边界、期限、退出条件和专项验收证据。未知设备或未知版本使用明确的“待确认”或“不支持”状态。
- 每次修复都增加一项回归测试：旧实现稳定复现故障，新实现稳定通过；同时检查相邻入口，证明被替代逻辑已经完整移除。
- 评审先检查事实来源和状态流，再检查局部条件。发现多条路径表达同一事实时，统一到领域模型或 Store，由界面只负责呈现和发出意图。Review 只有在根因消除、旧模型清除、全链路语义一致且回归证据完整后才算完成。

## 运行命令

```bash
# 启动 GUI（必须用 -m，包内绝对 import）
python3 -m satellite_debug_tool.main
python3 -m satellite_debug_tool.main --production   # 解锁批量试产工作区（启动即进）

# 测试（无 editable install 时需要 PYTHONPATH；74 个测试文件，conftest 已设 offscreen + 静默更新）
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
| 0 | **Customer Workspace**（默认） | 直接进 | 面向已注册产品（AFD01 / AFD01C / ESA01）：Overview / RF control / Playback / Maintenance。共享 `LiveView` 实例。 |
| 1 | **Engineering Tabs**（Live / Playback / Log / Device） | `Ctrl+Shift+E` 首次确认后本会话解锁 | 内部诊断、协议解码、设备参数读写、OTA。 |
| 2 | **Production Workspace**（批量试产） | `Ctrl+Shift+P` 首次确认解锁；或启动加 `--production` | 批次测试 / 夹具调试双页面；正式试产放行流程仍在后续里程碑。 |

- 客户与工程工作区共享同一个 `DeviceSessionCore`、连接和 Store；试产 Fleet 通过同一个 `SessionRegistry` 取得 endpoint 会话。
- 默认启动只构建客户总览和 `LiveView` 会话壳；工程 Live 呈现、工程其他 Tab、客户其他页面和试产工作区均在首次访问时构建并复用。
- `MainWindow` 负责顶层 `activate_view()` / `deactivate_view()`；各工作区只负责当前子页面。隐藏页面停止曲线、3D、表格和夹具绘图，连接、录制、OTA、批次与采集状态继续运行。
- OTA、参数写入和 Product 控制通过 `DeviceSessionCore` 的单一设备事务租约互斥；控制器在完成、失败、断线或连接代际变化时释放各自 owner。
- Product Service `result=0` 只表示设备已接受请求；只有更新且匹配的遥测回读才表示状态已应用，物理 RF 证据继续独立。
- 客户 OTA 使用不可变的已验签 artifact token，绑定包字节、签名 manifest 和当前设备会话；工程 raw BIN 与客户签名包不共用授权，传输期间不允许替换 artifact。
- `OTA_END=VERIFIED` 只确认字节转移和校验已被设备接受；完成结果还必须证明同 endpoint 返回、本次会话捕获的每项不可变身份都重新匹配，且传输前捕获的每个固件来源都重新回报、相互一致并匹配目标版本。同版本包只能显示“应用未独立确认”。
- 工程 Tab 内部仍是 `LiveView / PlaybackView / LogView / DeviceView` 四张卡（`QTabWidget`，tabBar 隐藏，顶栏"药丸"接管）。

## 架构

PySide6 桌面应用，调试相控阵卫星通信终端。基于 **DEBUG v2 协议**（`doc/DEBUG设备协议接口规范_v2.md` 是权威规范）；M18 起加入已注册产品使用的 **Product Service 协议**（cmd 0x20–0x2C），XESA01 Orbit 使用独立 0x30–0x31，envelope 仍兼容 v2。

### 数据流

```
Live/Customer Device → SerialWorker/UdpWorker → DeviceSessionCore
                                                   ├→ FrameReceiverV2 → domain registry
                                                   ├→ Handshake → ProfileStore
                                                   ├→ DataReport → TelemetrySeriesStore (ring 30000)
                                                   ├→ State/Event/GNSS/Orbit canonical stores
                                                   └→ Service* → ProductServiceStore → ProductSnapshotResolver

Engineering Live       DeviceSessionCore stores → first-access presentation → visible timers
Playback     .sdb v2/v3 → streaming DataImporter → PlaybackSeriesProvider(SQLite) → window DataStore
Log          .log   → WindTermLogParser → 虚拟 ProfileStore (hw_type="windterm_log") + DataStore (无界, max=128)
Device       DeviceSessionCore + Parameter/Ota controllers → 参数表 / COMMAND_RESPONSE / OTA 状态机
Production   Fleet + SessionRegistry → BatchCoordinator / FixtureSessionCoordinator → ResultStore + SDB
```

### 包结构（已演进出新模块，AGENTS 要跟得上）

- `core/protocol/` — v2 包络、CRC、Handshake 与领域注册表；`domains/{debug,product,orbit}.py` 独占各自命令解码，`FrameReceiverV2` 只做包络解析和领域分发
- `core/session/` — **M21** `DeviceSessionCore`、`SessionRegistry` 与 Debug/参数/OTA/Product 控制器；一个 endpoint 只有一个权威会话
- `core/product/` — **M18+** 客户 Product Service 模型、注册策略、Store、回放/legacy 投影与整快照来源状态机；每个 `ProductValue` 携带可用性、来源、接收时间和质量
- `core/playback/` — **M21** 后台 SDB 构建线程与磁盘型 `PlaybackSeriesProvider`，按时间窗口和像素预算查询曲线数据
- `core/comm/` — QThread worker：`BaseWorker`（QThread 基类）→ `SerialWorker` / `UdpWorker`
- `core/data/` — `ChannelBuffer`、`DataStore`、`TelemetrySeriesStore`、`StateStore`、`EventLog` 与 GNSS/Orbit Store
- `core/profile/` — `ProfileStore` + `ProfileCache`，设备 profile 驱动 UI；`semantics.py` 通道语义；`ins_yaw_display.py` 内部 INS 航向处理
- `core/production/` — **M19/M21** 试产与夹具领域；`BatchCoordinator` / `FixtureSessionCoordinator` 统一状态迁移、资源租约和证据收尾，页面只发意图并呈现类型化状态
- `core/log_parser/` — WindTerm 日志解析
- `core/security/` — 客户 OTA 固件包 Ed25519 验签（`firmware_package.py`）
- `core/link_trace.py` — 帧 trace 日志
- `core/config.py` — `Settings` 类，JSON 存 `~/.satellite_debug_tool/settings.json`
- `ui/` — PySide6 呈现层；`view_lifecycle.py` 定义页面生命周期，`lazy_view_host.py` 提供首次访问构建，`semantic_style.py` 只在语义属性变化时刷新样式
- `ui/assets/` — Leaflet 离线地图 bundle + STL 模型
- `i18n/` — `TranslationManager` + TS/QM 翻译资源（`translations/satellite_debug_tool_zh_CN.{ts,qm}`）
- `io/` — `DataRecorder`、流式 `DataImporter` 与唯一 `sdb_schema.py`；大型 SDB 在原文件旁生成可重建的稀疏 `.sdbi` 索引
- `tools/` — `tile_downloader.py`（OSM 离线 tile CLI）、`build_signed_firmware_package.py`（客户 OTA 固件打包工具）
- `updater/` — 自动升级模块：`ReleaseChecker / Downloader / Applier`，单文件 `updater` 二进制在 PyInstaller 后嵌入 `.app/Contents/MacOS/updater`
- `platform/macos/{en,zh_CN}.lproj/InfoPlist.strings` — macOS `CFBundleLocalizations` 用的本地化 InfoPlist 字符串

### 关键约定

- 所有 import 用 `satellite_debug_tool.` 前缀（绝对导入；这是为什么必须 `python3 -m ...`）
- UI 在 `ui/`，业务在 `core/`，持久化在 `io/`，**试产业务在 `core/production/`，客户产品服务领域在 `core/product/`**
- worker 线程（`BaseWorker` 子类）**严禁**直接操作 widget；必须 emit Qt 信号，主线程消费
- 页面构造只建立呈现对象；协议解析、连接代际、参数/OTA 状态机、试产状态迁移和证据收尾属于 `core/`。
- 高频页面必须实现幂等 `activate_view()` / `deactivate_view()`；隐藏时停止呈现定时器，恢复时先从 Store 即时刷新一次。
- 新增重量级页面使用 `LazyViewHost` 首次构建并保留实例；首次构建前到达的数据必须由权威 Store 在激活时补齐。
- **每个 Tab/工作区独立 `DataStore` / `ProfileStore`**（M7 引入），切换不污染；CustomerWorkspace 和 LiveView 共享的是同一个 `LiveView` 实例，所以底层 DataStore 实际同一份
- 协议帧格式：`AA 55 0D` + cmd_type(1B) + len(2B LE) + data + CRC16-CCITT(2B LE) + `EE`；命令仅分配 `0x01..0x10`、`0x20..0x2C`、`0x30..0x31` 三段；DATA 段上限 `MAX_DATA_LENGTH=1536`，`MAX_FRAME_LENGTH=1548` 是设备端保守缓冲值（实际线上帧开销 9 B、最大 1545 B），DATA_REPORT 单帧最大 64 通道
- 通用长帧扩容不改变专用上传分片合同：`OTA_DATA` 每片 1..1021 B（UI 通常发送 512 B），Orbit `UPLOAD_CHUNK` 每片 1..1012 B
- 测试在 `satellite_debug_tool/tests/`（74 个文件），名称和注释多为中文；`conftest.py` 的 session fixture 保持唯一 QApplication，UI 测试通过 `qapp` fixture 复用它
- `conftest.py` 自动设 `QT_QPA_PLATFORM=offscreen` + `SATELLITE_UPDATE_CHECK=0` + `SATELLITE_DEBUG_LOCALE=zh_CN`，**绝不要**在测试代码里访问 Gitee/GitHub API
- 字号已固化 `small`（`base_px=13`，`main.py` 调 `S.apply_global_font(app, scale="small", base_px=13)`）；`styles.FONT_SCALES` / `FontScale` API 仅保留兼容 `test_styles.py`，UI 不再暴露
- 主题三档 `dark / dark_hc / light`，由 `S.palette()` 出语义色键（兼容键 + Mission Console 新语义键），顶栏图标按钮循环切换
- 离线地图约定 GPS channel 名 `gps_lat` / `gps_lon`（可选 `gps_alt`），Playback / Log 检测到自动启用"地图"按钮
- **客户工作台 `CustomerWorkspace` 共享 `LiveView` 实例**——改 Customer view 时不要新建自己的 DataStore/ProfileStore，否则与 Live Tab 状态分裂
- 第一方可翻译复合控件显式实现 `retranslate_ui()`；语言切换使用稳定源键，禁止扫描对象树或根据当前可见文本反查业务状态。

## 持久化路径（`~/.satellite_debug_tool/`）

| 路径 | 用途 |
|------|------|
| `settings.json` | `core/config.Settings` 用户配置（连接参数、UI 偏好、路径、试产参数） |
| `profiles/{hw_type}.json` | ProfileCache 缓存的设备 profile |
| `tiles/{region}/{z}/{x}/{y}.png` | M8 OSM 离线 tile（按区域分组） |
| `updates/<tag>/updater.log` | 自动升级日志；升级失败时排查用 |
| `production_batches/<batch_id>/` | M19-A 工程预览批次产物：数据库 + SDB；正式报告仍待后续里程碑实现 |
| `fixture_profiles/` | 带 revision 与 SHA-256 的工作站夹具档案 |
| `fixture_calibrations/` | MS-6222 坐标标定结果 |
| `fixture_sessions/<session_id>/` | 夹具命令、MS 原始帧、解析结果、事件、指标与 manifest |

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

- 本文件（`AGENTS.md`）— **当前工程约定与架构的唯一权威入口**；代码、协议或里程碑演进后同步更新本文件
- `doc/DEBUG设备协议接口规范_v2.md` — **协议权威规范**
- `doc/M22_AFD01C_upper_pc_adaptation_*.md` — 当前 AFD01C 上位机适配范围、证据与未完成真机边界
- `doc/upper_pc_function_definition_vnext.md` — M7–M16 历史功能定义与路线基线，不代表当前架构
- `doc/optimization_plan.md` — M1–M6 整体优化计划（v1.2）
- `doc/M7_*.md` ~ `doc/M22_*` — 各里程碑 plan/acceptance/dev_log（M7 Tab 化、M8 离线地图、M10/M11 升级、M12 归一化、M13 通道语义、M14 ESA01、M15 GNSS truth、M16 i18n English、M17 内置 3D 模型、M18 客户工作台 + Product Service、M19 批量试产与夹具调试、M20 根因修复与状态完整性、M21 单进程架构收敛、M22 AFD01C 适配）
- `doc/development_log.md` — M1–M6 实施日志
- `doc/acceptance_log.md` — F-/A- 系列验收跟踪
- `doc/i18n_terms.md` — 中英术语表
- `doc/user_manual.md` / `doc/user_manual_en.md` — M16 工程工作区历史操作手册；当前三工作区事实以本文件为准
- `doc/RELEASING.md` — 发版 SOP（含 tag 重发、hotfix、rc 预发）
- `doc/AFD01_signed_firmware_package.md` — 客户 OTA 固件包签名格式
- `BUILDING.md` — 打包可执行文件完整指南
