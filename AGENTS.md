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

# 测试（无 editable install 时需要 PYTHONPATH；89 个测试文件，conftest 已设 offscreen + 静默更新）
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
| 0 | **Customer Workspace**（默认） | 直接进 | 面向已注册产品（AFD01 / AFD01A / AFD01B2 / AFD01C / ESA01）：左侧最多 4 台显式 UDP 设备，Overview / RF control / Network test / Playback / Maintenance。每个 endpoint 使用固定会话页面 bundle；Network test 是全局页面。 |
| 1 | **Engineering Tabs**（Live / Playback / Log / Device） | `Ctrl+Shift+E` 首次确认后本会话解锁 | 内部诊断、协议解码、设备参数读写、OTA。 |
| 2 | **Production Workspace**（批量试产） | `Ctrl+Shift+P` 首次确认解锁；或启动加 `--production` | 批次测试 / 摇摆台测试 / MS-6222 测试 / 电源测试四页面；已有单设备 DOCX、证据 ZIP 和 V2 审核，完整自动测试编排仍在后续里程碑。 |

- Customer 与 Production 共用一个进程级 `UdpEndpointBroker` 和一个 `EndpointSessionDirectory`；同一 endpoint 只有一个 Runtime/Core。工程工作区显式选择“共享客户 UDP”时观察当前客户 endpoint，选择“工程串口”时使用独立串口 Core。
- 默认启动只构建客户总览和 `LiveView` 会话壳；工程 Live 呈现、工程其他 Tab、客户其他页面和试产工作区均在首次访问时构建并复用。
- `MainWindow` 负责顶层 `activate_view()` / `deactivate_view()`；各工作区只负责当前子页面。隐藏页面停止曲线、3D、表格和夹具绘图，连接、录制、OTA、批次与采集状态继续运行。
- OTA、参数写入和 Product 控制通过 `DeviceSessionCore` 的单一设备事务租约互斥；控制器在完成、失败、断线或连接代际变化时释放各自 owner。
- Product Service `result=0` 只表示设备已接受请求；只有更新且匹配的遥测回读才表示状态已应用，物理 RF 证据继续独立。
- 客户 OTA 使用不可变的已认证解密 artifact token，绑定解密后的包字节、加密 manifest 和当前设备会话；工程 raw BIN 与客户加密包不共用授权，传输期间不允许替换 artifact。
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
Production   Fleet + EndpointSessionDirectory → ProductionConfigurationStore → BatchCoordinator / FixtureSessionCoordinator → MotionPlatformController + ResultStore + SDB
```

### 包结构（已演进出新模块，AGENTS 要跟得上）

- `core/protocol/` — v2 包络、CRC、Handshake 与领域注册表；`domains/{debug,product,orbit}.py` 独占各自命令解码，`FrameReceiverV2` 只做包络解析和领域分发
- `core/session/` — **M21/M25** `DeviceSessionCore`、`EndpointSessionDirectory`、`EndpointSessionRuntime` 与 Debug/参数/OTA/Product 控制器；一个 UDP endpoint 只有一个权威会话
- `core/customer/` — **M25** 客户显式设备目录、attached 意图、固定 endpoint binding；选择设备只切换呈现，不重绑 Core 或控制目标
- `core/external_power_monitor.py` — **M28** 每个客户设备独立的 PSW80-27 串行监测/控制 owner 与 Store；同一 TCP 会话完成轮询、设参、启停和回读闭环，保留 30 分钟 V/A 历史
- `core/iperf_test.py` — **M28** 每个客户设备独立的 iperf3 客户端 owner/Store；双 QProcess 独立运行 UL/DL，解析 3.18+ JSON-stream，并将网络指标与该设备的 PSW 样本写入同一测试会话
- `core/comm/udp_endpoint_broker.py` — **M25** Customer/Production 共用的进程级单 UDP socket、admission claim、来源 endpoint 分流与定向发送
- `core/product/` — **M18+** 客户 Product Service 模型、注册策略、Store、回放/legacy 投影与整快照来源状态机；每个 `ProductValue` 携带可用性、来源、接收时间和质量
- `core/playback/` — **M21** 后台 SDB 构建线程与磁盘型 `PlaybackSeriesProvider`，按时间窗口和像素预算查询曲线数据
- `core/comm/` — QThread worker：`BaseWorker`（QThread 基类）→ `SerialWorker` / `UdpWorker`
- `core/data/` — `ChannelBuffer`、`DataStore`、`TelemetrySeriesStore`、`StateStore`、`EventLog` 与 GNSS/Orbit Store
- `core/profile/` — `ProfileStore` + `ProfileCache`，设备 profile 驱动 UI；`semantics.py` 通道语义；`ins_yaw_display.py` 内部 INS 航向处理
- `core/production/` — **M19/M21** 试产与夹具领域；`BatchCoordinator` / `FixtureSessionCoordinator` 统一状态迁移、资源租约和证据收尾，页面只发意图并呈现类型化状态
- `core/production/configuration.py` — **M32** 生产配置 v2；产品、电源、摇摆台、MS-6222 与工位引用统一解析，正式批次冻结完整夹具 payload、revision、标定和 SHA-256；旧 `production.ms6222_port` 仅作为一次迁移输入
- `core/production/run_coordinator.py` — **M19-A.13** 首轮正式自动工况的唯一 attempt 状态机；只依据前置证据、硬件启动回执和逐设备分析结果推进，不用超时推断动作成功
- `core/production/fixture_runtime.py` — **M19-A.13** 进程级摇摆台/MS-6222 所有权根；Customer 弹窗与 Production 共用唯一租约、夹具档案和坐标标定 Store
- `core/production/fixture_telemetry.py` — **M19-A.15** 进程级夹具姿态事实总线；平台已发送目标、MS-6222 标定后相对 FRD 与真实航向保持独立来源，活动 SDB v3 recorder 各自订阅并保存
- `core/production/ms6222_debug.py` — **M19-A.5/M19-A9** MS-6222 独立串口租约与工程证据会话；独立 MS 页面只采集参考，摇摆台自动资格测试显式获取摇摆台与 MS 两项所有权并生成联合诊断包
- `core/motion_platform/` — **M19-A.11** 可复用运动平台直接控制组件；独占本机 `192.168.15.101:8410` 反馈端点并向控制器 `192.168.15.201:7408` 发送二进制帧，统一拥有严格逆运动学、连接代际、反馈闭环完成、回原点、轨迹调度和证据模型
- `core/production/fixture_profile.py` — **M19-A.11** 摇摆台型号模板、v4 工作站档案、运行包线和控制租约；型号预置直接控制端点与运动学参数，旧 `192.168.15.101:9800` 厂家服务端点不解释为控制器地址，现场只确认轴符号并由引导标定生成真实 calibration ID
- `core/log_parser/` — WindTerm 日志解析
- `core/security/` — 客户 OTA 固件包 AES-256-GCM 认证解密（`firmware_package.py`）
- `core/link_trace.py` — 帧 trace 日志
- `core/config.py` — `Settings` 类，JSON 存 `~/.satellite_debug_tool/settings.json`
- `ui/` — PySide6 呈现层；`view_lifecycle.py` 定义页面生命周期，`lazy_view_host.py` 提供首次访问构建，`semantic_style.py` 只在语义属性变化时刷新样式
- `ui/assets/` — Leaflet 离线地图 bundle + STL 模型
- `i18n/` — `TranslationManager` + TS/QM 翻译资源（`translations/satellite_debug_tool_zh_CN.{ts,qm}`）
- `io/` — `DataRecorder`、流式 `DataImporter` 与唯一 `sdb_schema.py`；大型 SDB 在原文件旁生成可重建的稀疏 `.sdbi` 索引
- `tools/` — `tile_downloader.py`（OSM 离线 tile CLI）、`build_encrypted_firmware_package.py`（客户 OTA 加密固件打包工具）
- `updater/` — 自动升级模块：`ReleaseChecker / Downloader / Applier`；`UpdateDialog` 只冻结安装请求，`MainWindow.closeEvent()` 完成受控收尾后才启动单文件 updater；updater 递归验证唯一载荷根并持久化阶段状态
- `platform/macos/{en,zh_CN}.lproj/InfoPlist.strings` — macOS `CFBundleLocalizations` 用的本地化 InfoPlist 字符串

### 关键约定

- 所有 import 用 `satellite_debug_tool.` 前缀（绝对导入；这是为什么必须 `python3 -m ...`）
- UI 在 `ui/`，业务在 `core/`，持久化在 `io/`，**试产业务在 `core/production/`，客户产品服务领域在 `core/product/`**
- 运动平台协议、运动学、UDP socket、状态机和轨迹调度只存在于 `core/motion_platform/`；试产页面、批次编排和后续场景仿真只调用其公开领域接口。MS-6222 保持独立参考源，由上层联合测试显式组合。
- worker 线程（`BaseWorker` 子类）**严禁**直接操作 widget；必须 emit Qt 信号，主线程消费
- 页面构造只建立呈现对象；协议解析、连接代际、参数/OTA 状态机、试产状态迁移和证据收尾属于 `core/`。
- 高频页面必须实现幂等 `activate_view()` / `deactivate_view()`；隐藏时停止呈现定时器，恢复时先从 Store 即时刷新一次。
- 新增重量级页面使用 `LazyViewHost` 首次构建并保留实例；首次构建前到达的数据必须由权威 Store 在激活时补齐。
- **每个离线 Tab 使用独立 `DataStore` / `ProfileStore`**；共享 UDP 的 Customer 与 Engineering 通过当前 endpoint bundle 消费同一个 Runtime Store，工程串口和 Playback/Log 分别独立，切换不污染
- 协议帧格式：`AA 55 0D` + cmd_type(1B) + len(2B LE) + data + CRC16-CCITT(2B LE) + `EE`；命令仅分配 `0x01..0x11`、`0x20..0x2C`、`0x30..0x31` 三段；DATA 段上限 `MAX_DATA_LENGTH=1536`，`MAX_FRAME_LENGTH=1548` 是设备端保守缓冲值（实际线上帧开销 9 B、最大 1545 B），DATA_REPORT 单帧最大 64 通道
- 通用长帧扩容不改变专用上传分片合同：`OTA_DATA` 每片 1..1021 B（UI 通常发送 512 B），Orbit `UPLOAD_CHUNK` 每片 1..1012 B
- 测试在 `satellite_debug_tool/tests/`（89 个文件），名称和注释多为中文；`conftest.py` 的 session fixture 保持唯一 QApplication，UI 测试通过 `qapp` fixture 复用它
- `conftest.py` 自动设 `QT_QPA_PLATFORM=offscreen` + `SATELLITE_UPDATE_CHECK=0` + `SATELLITE_DEBUG_LOCALE=zh_CN`，**绝不要**在测试代码里访问 Gitee/GitHub API
- 字号已固化 `small`（`base_px=13`，`main.py` 调 `S.apply_global_font(app, scale="small", base_px=13)`）；`styles.FONT_SCALES` / `FontScale` API 仅保留兼容 `test_styles.py`，UI 不再暴露
- 主题三档 `dark / dark_hc / light`，由 `S.palette()` 出语义色键（兼容键 + Mission Console 新语义键），顶栏图标按钮循环切换
- 离线地图约定 GPS channel 名 `gps_lat` / `gps_lon`（可选 `gps_alt`），Playback / Log 检测到自动启用"地图"按钮
- **客户多设备页面按 endpoint 固定绑定**——每个 endpoint bundle 复用 Directory 的唯一 Runtime/Core/Store；禁止把已有 widget/controller 动态 rebind 到另一 endpoint。工程“共享客户 UDP”复用当前 bundle，工程串口保持独立。
- **AFD01 新型号身份**——后续出货的 AFD01A 沿用原 AFD01 的产品使用方式与 3D 几何，但 `afd01` / `afd01a` / `afd01b2` / `afd01c` 各自独立注册，按 Debug hw_type、Product Identity 与 service protocol 精确准入；recipe 和加密 OTA 包不互作别名。A/B2 仅接受 protocol 8，B2 未提供独立 STL 时使用占位模型。
- **电源与网络测试按客户设备固定绑定**——`customer.devices[]` 使用稳定 `device_id` 保存各自电源和 iperf profile；编辑 endpoint 不改变归属。外接电源弹窗配置 IP/设定值并执行闭环控制，端口固定 2268；SDB 的 `external_power_sample/v1` 只写入对应设备并携带 `device_id`/endpoint。
- **摇摆台对外统一设备 FRD**——夹具界面、轨迹、自动资格测试和报告使用 Roll+ 右翼下沉、Pitch+ 机头上抬、Yaw+ 向右/俯视顺时针；灵境 A6 型号在运动组件边界固定转换为供应商控制符号 `(-1,+1,-1)`，型号档案只读。平台静止与 MS-6222 标定后姿态精度是两个独立结论。
- **正式工位夹具绑定**——电源 TCP、摇摆台控制/反馈 UDP 和 MS-6222 COM 均来自所选工位依赖档案；型号预置拥有几何、运行包线和 FRD 符号，现场档案拥有实际端点。正式批次启动要求摇摆台反馈与 MS-6222 有效帧均进入 `READY`，`CONFIGURED` 不表示已连接。
- **夹具姿态按设备录制并相关回放**——活动 SDB v3 以 `fixture_attitude_sample/v1` metadata 保存共享夹具事实；回放只在同一设备启动代际的有效主机接收时间/设备 uptime 锚点内插值。设备姿态、平台目标和 MS 相对 FRD 在同一姿态区域按颜色与线型区分；无效或缺口用 NaN 断开，未对齐样本不进入曲线。
- **客户网络测试是设备级状态**——每个 endpoint bundle 拥有固定 `IperfTestController/Store`，切换页面不重绑、不停止后台测试；不同设备只在本地 IPv4和服务端端口不冲突时并行，应用退出前逐个完成进程和证据收尾。
- 全局设置弹窗按 Customer / Engineering / Production scope 呈现；客户和工程 scope 不显示试产配置与试产报告，Production scope 保留这些业务设置。
- Customer 侧栏与 Engineering 顶栏的“摇摆台…”入口复用同一个弹窗实例和进程级 `FixtureRuntime`；提供日常相对姿态、回中、组合连续摇摆、受控停止、MS 绝对航向和有界姿态修正，目标/MS 实测/误差共用一张姿态图。回原点、档案编辑和资格标定仍由 Production 工程页面负责。
- 第一方可翻译复合控件显式实现 `retranslate_ui()`；语言切换使用稳定源键，禁止扫描对象树或根据当前可见文本反查业务状态。

## 持久化路径（`~/.satellite_debug_tool/`）

| 路径 | 用途 |
|------|------|
| `settings.json` | `core/config.Settings` 用户配置（连接参数、UI 偏好、路径、试产参数） |
| `settings.json.bak` | 自校验 last-known-good 设置备份；主配置损坏时用于原子恢复 |
| `production_configuration.json` | 产品测试模板、电源档案和工作站配置的单一原子目录；批次只保存所选稳定 ID |
| `profiles/{hw_type}.json` | ProfileCache 缓存的设备 profile |
| `tiles/{region}/{z}/{x}/{y}.png` | M8 OSM 离线 tile（按区域分组） |
| `updates/<tag>/updater.log`、`update-status.json` | 自动升级持久日志与原子事务状态；临时解压目录独立清理 |
| `production_batches/<batch_id>/` | M19-A 批次原始数据库、SDB、配方和夹具证据 |
| 设置的报告根目录/`日期/型号/SN/批次/` | 单设备 V1/V2 DOCX、`evidence.zip` 与 SHA-256 `manifest.json` |
| `fixture_profiles/` | 带 revision 与 SHA-256 的工作站夹具档案 |
| `fixture_calibrations/` | MS-6222 坐标标定结果 |
| `fixture_sessions/<session_id>/` | 夹具命令、MS 原始帧、解析结果、事件、指标与 manifest |
| `ms6222_sessions/<session_id>/` | MS-6222 独立调试的配置、原始帧、解析 CSV、统计、事件、summary 与 manifest |
| `power_supply_sessions/<session_id>/` | PSW 独立工程调试的配置、身份、SCPI 记录、事件与 manifest |
| `iperf_sessions/<device_id>/<session_id>/` | 设备级 iperf3 配置、UL/DL 原始 JSONL、网络/功耗 CSV、事件与 summary |

## 构建与发版

- **macOS 本地打包**：`./scripts/build_macos.sh`（自动 venv + 装依赖 + 跑 `update_translations.py check` + PyInstaller 主程序+updater + 嵌入 `.app/Contents/MacOS/updater` + `codesign --force --deep --sign -` ad-hoc 签名 + `xattr -cr` 清 quarantine + `ditto -c -k` 出 zip）
- **Windows 一键打包**：`scripts\build_windows.bat`（PyInstaller + `verify_model_assets.py` 校验 3D 模型 + updater 嵌入 + `Compress-Archive` 出 zip）
- **Windows 自动更新包合同**：GitHub Release `.7z` 的归档根直接包含 `SatelliteDebugTool.exe`、`updater.exe` 与 `_internal/`，兼容旧 updater 安装修复版；上传前由 `scripts/verify_update_archive.py` 验证唯一完整载荷
- **CI**：`.github/workflows/build.yml` 只跑 Windows（`prepare-release` + `build-windows`，7z 分卷上传 GitHub Release；推 master/main 触发 `ci-only-build` 上传 artifact 不发版）
- **mac 不走 CI**：PyInstaller .app 含 1500+ symlinks + 7z 兼容性问题，强制本地脚本。updater 二进制仍嵌入但 mac 平台不发版，触发频率为零
- **版本号唯一来源**：`satellite_debug_tool/__init__.py.__version__`（当前 `1.4.0`）；mac `Info.plist` 的 `CFBundleVersion` 由 `satellite_debug_tool.spec` 自动读这个值
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
- `doc/M29_AFD01A_B2_upper_pc_*.md` — AFD01A/B2 接入计划、验收及真机边界
- `doc/upper_pc_function_definition_vnext.md` — M7–M16 历史功能定义与路线基线，不代表当前架构
- `doc/optimization_plan.md` — M1–M6 整体优化计划（v1.2）
- `doc/M7_*.md` ~ `doc/M29_*.md` — 各里程碑 plan/acceptance/dev_log（M7 Tab 化、M8 离线地图、M10/M11 升级、M12 归一化、M13 通道语义、M14 ESA01、M15 GNSS truth、M16 i18n English、M17 内置 3D 模型、M18 客户工作台 + Product Service、M19 批量试产与夹具调试、M20 根因修复与状态完整性、M21 单进程架构收敛、M22 AFD01C 适配、M23 部件温度窗口、M24 Tracking 仿真、M25 客户多设备共享会话、M25R 会话恢复简化、M26 外接电源监测、M27 iperf3 功耗测试、M28 客户设备独立电源与 iperf3、M29 AFD01A/B2 产品接入）
- `doc/development_log.md` — M1–M6 实施日志
- `doc/acceptance_log.md` — F-/A- 系列验收跟踪
- `doc/i18n_terms.md` — 中英术语表
- `doc/user_manual.md` / `doc/user_manual_en.md` — M16 工程工作区历史操作手册；当前三工作区事实以本文件为准
- `doc/production_automation_operator_guide.md` — 当前试产工程预览的配置、夹具检查、批次、证据和报告操作指南，明确尚未接通的正式自动工况边界
- `doc/M32_formal_production_fixture_closure_*.md` — 电源、摇摆台、MS-6222 完整工作站配置、M31 出货整备和正式自动工况收口计划、验收与开发记录
- `doc/RELEASING.md` — 发版 SOP（含 tag 重发、hotfix、rc 预发）
- `doc/AFD01_encrypted_firmware_package.md` — 客户 OTA 加密固件包格式
- `BUILDING.md` — 打包可执行文件完整指南
