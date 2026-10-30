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

# 测试（无 editable install 时需要 PYTHONPATH；100 个测试文件，conftest 已设 offscreen + 静默更新）
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

## 构建版本与工作区

- **用户版 `main_user.py` / `UserWindow`**：固定总览、RF 控制、客户回放、独立加密固件升级四页；使用客户固定 endpoint 会话。总览的 SNR 五分钟曲线和三模块温度属于始终装配的公共面板；侧栏保留按需创建的 Shell（TCP、串口及 YMODEM）；电源、iperf、夹具、安装标定及工程/试产页面由运维版提供。用户版入口拒绝运维命令行，构建产物裁剪运维专属模块。
- **运维版 `main.py` / `MainWindow`**：现有全量功能；源码 `python -m satellite_debug_tool.main` 默认启动运维版。两版共用领域模型、会话、录制、OTA 状态机及更新事务；构建身份来自打包的 `build_identity.json`。
- 用户设置/日志/更新事务位于 `~/.satellite_debug_tool/user/`；运维版保留原根目录。已有模型覆盖和 profile 缓存继续按原路径复用。

### 运维版工作区（顶层 `MainWindow`）

`MainWindow` 是个 `QStackedWidget`，三页：

| 索引 | 工作区 | 入口 | 备注 |
|------|--------|------|------|
| 0 | **Customer Workspace**（默认） | 直接进 | 面向已注册产品（AFD01 / AFD01A / AFD01B2 / AFD01C / AFD01D / ESA01）：左侧最多 4 台显式 UDP 设备，Overview / RF control / Network test / Playback / Maintenance。每个 endpoint 使用固定会话页面 bundle；Network test 是全局页面。 |
| 1 | **Engineering Tabs**（Live / Playback / Log / Device） | `Ctrl+Shift+E` 首次确认后本会话解锁 | 内部诊断、协议解码、设备参数读写、OTA。 |
| 2 | **Production Workspace**（批量试产） | `Ctrl+Shift+P` 首次确认解锁；或启动加 `--production` | 批次测试 / 摇摆台测试 / MS-6222 测试 / 电源测试四页面；已有单设备 DOCX、证据 ZIP 和 V2 审核，完整自动测试编排仍在后续里程碑。 |

- Customer 与 Production 共用一个进程级 `UdpEndpointBroker` 和一个 `EndpointSessionDirectory`；同一 endpoint 只有一个 Runtime/Core。工程工作区显式选择“共享客户 UDP”时观察当前客户 endpoint，选择“工程串口”时使用独立串口 Core。
- 默认启动只构建客户总览和 `LiveView` 会话壳；工程 Live 呈现、工程其他 Tab、客户其他页面和试产工作区均在首次访问时构建并复用。
- `MainWindow` 负责顶层 `activate_view()` / `deactivate_view()`；各工作区只负责当前子页面。隐藏页面停止曲线、3D、表格和夹具绘图，连接、录制、OTA、批次与采集状态继续运行。
- OTA、参数写入和 Product 控制通过 `DeviceSessionCore` 的单一设备事务租约互斥；控制器在完成、失败、断线或连接代际变化时释放各自 owner。
- Product Service `result=0` 只表示设备已接受请求；只有更新且匹配的遥测回读才表示状态已应用，物理 RF 证据继续独立。
- 客户 OTA 使用不可变的已认证解密 artifact token，绑定解密后的包字节、加密 manifest 和当前设备会话；工程 raw BIN 与客户加密包不共用授权，传输期间不允许替换 artifact。
- AFD01 系列低于 `0.0.131_beta` 的已认证解密客户包使用有界旧 OTA 响应合同（`OK` / `dup OK` / `OK, rebooting`）；`0.0.131_beta` 及以后、工程 raw BIN、未知型号或未知版本只接受带阶段和序号的正式响应。
- 新生成的客户 `.sfpkg` 默认使用 `allow_downgrade`，允许升级、同版本重装和降级；上位机仍严格执行经过 AES-GCM 认证的 manifest 策略，旧包不会被旁路。
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
- `core/external_power_monitor.py` — **M28** 每个客户设备独立的 PSW80-27 串行监测/控制 owner 与 Store；同一 TCP 会话完成轮询、设参、启停和回读闭环，保留 30 分钟 V/A 历史。底层 `GwInstekPswAdapter` 独占连接内配置验证事实，正常轮询保留已验证关闭状态；开启前重新回读输出、保护及电压/电流设定值，通信失败、保护、参数漂移或重连撤销相应准入证据。修复计划与证据见 `doc/M26_power_enable_readiness_fix_*.md`。
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
- `core/tcp_shell/` — **M37** Shell 弹窗（TCP 1235 / 串口）+ YMODEM Batch Sender：`transport.py` 字节流 link 层（`TcpShellLink` / `SerialShellLink`，UDP 明确拒绝——设备侧无 UDP shell 服务；单次读取统一 ≤ `MAX_READ_CHUNK` 64 KiB）、`ymodem_sender.py` 纯状态机（C 握手、SOH/128 头包与批结束 + STX/1024 数据块、CRC16-CCITT、EOT 两阶段、空文件名批结束）、`worker.py` 单 link 读写 owner（QThread，target dict 驱动；shell 字节经 ring 入界、YMODEM 二进制 recv 不经 ring）、`rx_bridge.py` **M45** 有界接收环（单会话单生产者单消费者：分段接纳、有界等待片段、`push_final` 残余移交、清屏代际与锁内通知边沿）、`controller.py` YMODEM 准入租约与最小版本回读、`recorder.py` 会话证据
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

- **M46 电源面板交还与 MS-6222 对照**：客户外接电源页“退出遥控（恢复面板）”由同一 worker 完成当前操作、发送 `SYST:COMM:RLST LOC` 并关闭会话；monitor 保留面板控制意图，后续页面活动变化保持停止监测，显式“保存并连接”恢复监测。写入成功只呈现“已发送返回本地指令”，输出与设定值保持。`FixtureTelemetryBus` 同时拥有当前参考姿态快照，独立 MS 页和摇摆台入口发布 INS 样本与连接生命周期；总览在设备 3D 角度下方显示共享 MS 参考，固定显示原始 INS/真实航向（三位小数），摇摆台继续显示标定后的设备 FRD/相对航向，3 秒过期及断开时清除实时数字。原始快照仅服务实时对照，SDB 姿态记录继续使用既有类型化总线合同。见 `doc/M46_power_local_ms6222_readout_*.md`。

- **M40 中性产品命名**：客户可见名称统一为“卫星通信终端工具 / Satellite Terminal Tool”，顶栏简称“终端工具 / Terminal Tool”；应用元数据与 macOS 包信息使用中性标识。报告默认公司名为空，由用户工位配置提供；已发布固件包的 AES-GCM 认证域按格式合同保留固定字节。
- **M41 下拉框排版**：全局 QComboBox 与其弹出视图使用同一 UI 字体角色，数值输入保留等宽字体；回放/日志的 TimeRangeControl 使用标准列表委托和按内容调整宽度，语言切换由 Qt 重新计算尺寸，保持当前预设与范围。两页面将文件信息和时间范围分行，保证英文选项在页面布局中完整显示。

- 所有 import 用 `satellite_debug_tool.` 前缀（绝对导入；这是为什么必须 `python3 -m ...`）
- UI 在 `ui/`，业务在 `core/`，持久化在 `io/`，**试产业务在 `core/production/`，客户产品服务领域在 `core/product/`**
- 运动平台协议、运动学、UDP socket、状态机和轨迹调度只存在于 `core/motion_platform/`；试产页面、批次编排和后续场景仿真只调用其公开领域接口。MS-6222 保持独立参考源，由上层联合测试显式组合。
- worker 线程（`BaseWorker` 子类）**严禁**直接操作 widget；必须 emit Qt 信号，主线程消费
- 页面构造只建立呈现对象；协议解析、连接代际、参数/OTA 状态机、试产状态迁移和证据收尾属于 `core/`。
- 高频页面必须实现幂等 `activate_view()` / `deactivate_view()`；隐藏时停止呈现定时器，恢复时先从 Store 即时刷新一次。
- 新增重量级页面使用 `LazyViewHost` 首次构建并保留实例；首次构建前到达的数据必须由权威 Store 在激活时补齐。
- **每个离线 Tab 使用独立 `DataStore` / `ProfileStore`**；共享 UDP 的 Customer 与 Engineering 通过当前 endpoint bundle 消费同一个 Runtime Store，工程串口和 Playback/Log 分别独立，切换不污染
- 协议帧格式：`AA 55 0D` + cmd_type(1B) + len(2B LE) + data + CRC16-CCITT(2B LE) + `EE`；命令仅分配 `0x01..0x12`、`0x20..0x2C`、`0x30..0x31` 三段；DATA 段上限 `MAX_DATA_LENGTH=1536`，`MAX_FRAME_LENGTH=1548` 是设备端保守缓冲值（实际线上帧开销 9 B、最大 1545 B），DATA_REPORT 单帧最大 64 通道
- 通用长帧扩容不改变专用上传分片合同：`OTA_DATA` 每片 1..1021 B（UI 通常发送 512 B），Orbit `UPLOAD_CHUNK` 每片 1..1012 B
- 测试在 `satellite_debug_tool/tests/`（100 个文件），名称和注释多为中文；`conftest.py` 的 session fixture 保持唯一 QApplication，UI 测试通过 `qapplication_session` fixture 复用它
- `conftest.py` 自动设 `QT_QPA_PLATFORM=offscreen` + `SATELLITE_UPDATE_CHECK=0` + `SATELLITE_DEBUG_LOCALE=zh_CN`，**绝不要**在测试代码里访问 Gitee/GitHub API
- 字号已固化 `small`（`base_px=13`，`main.py` 调 `S.apply_global_font(app, scale="small", base_px=13)`）；`styles.FONT_SCALES` / `FontScale` API 仅保留兼容 `test_styles.py`，UI 不再暴露
- **M38 配色**：`dark / dark_hc / light` 三主题由 `ui/styles.py` 统一提供石墨灰/中性浅灰、蓝色操作与选择、独立状态色及 Qt 原生调色板。实底按钮、选中项、焦点和进度条分别使用成对角色；曲线/GNSS/姿态/ANSI 保持类别含义并集中调整主题亮度。常规数值使用正文色；切主题立即重绘已有状态和终端格式，顶栏图标按钮循环切换。
- 离线地图约定 GPS channel 名 `gps_lat` / `gps_lon`（可选 `gps_alt`），Playback / Log 检测到自动启用"地图"按钮
- **客户多设备页面按 endpoint 固定绑定**——每个 endpoint bundle 复用 Directory 的唯一 Runtime/Core/Store；禁止把已有 widget/controller 动态 rebind 到另一 endpoint。工程“共享客户 UDP”复用当前 bundle，工程串口保持独立。
- **AFD01 新型号身份**——AFD01A 沿用原 AFD01 的产品使用方式与 3D 几何；`afd01` / `afd01a` / `afd01b2` / `afd01c` / `afd01d` 各自独立注册，按 Debug hw_type、Product Identity 与 service protocol 精确准入，recipe 和加密 OTA 包不互作别名。A/B2/C/D 仅接受 protocol 8；B2 未提供独立 STL 时使用占位模型，C/D 共用外观几何但分别打包为 `afd01c.stl` / `afd01d.stl`，CAD 轴按 `+Z=机头、+X=左翼、+Y=天顶` 映射到 FLU。
- **电源与网络测试按客户设备固定绑定**——`customer.devices[]` 使用稳定 `device_id` 保存各自电源和 iperf profile；编辑 endpoint 不改变归属。外接电源弹窗配置 IP/设定值并执行闭环控制，端口固定 2268；SDB 的 `external_power_sample/v1` 只写入对应设备并携带 `device_id`/endpoint。
- **摇摆台对外统一设备 FRD**——夹具界面、轨迹、自动资格测试和报告使用 Roll+ 右翼下沉、Pitch+ 机头上抬、Yaw+ 向右/俯视顺时针；灵境 A6 型号在运动组件边界固定转换为供应商控制符号 `(-1,+1,-1)`，型号档案只读。平台静止与 MS-6222 标定后姿态精度是两个独立结论。
- **正式工位夹具绑定**——电源 TCP、摇摆台控制/反馈 UDP 和 MS-6222 COM 均来自所选工位依赖档案；型号预置拥有几何、运行包线和 FRD 符号，现场档案拥有实际端点。正式批次启动要求摇摆台反馈与 MS-6222 有效帧均进入 `READY`，`CONFIGURED` 不表示已连接。
- **夹具姿态按设备录制并相关回放**——活动 SDB v3 以 `fixture_attitude_sample/v1` metadata 保存共享夹具事实；回放只在同一设备启动代际的有效主机接收时间/设备 uptime 锚点内插值。设备姿态、平台目标和 MS 相对 FRD 在同一姿态区域按颜色与线型区分；无效或缺口用 NaN 断开，未对齐样本不进入曲线。
- **客户网络测试是设备级状态**——每个 endpoint bundle 拥有固定 `IperfTestController/Store`，切换页面不重绑、不停止后台测试；不同设备只在本地 IPv4和服务端端口不冲突时并行，应用退出前逐个完成进程和证据收尾。
- 全局设置弹窗按 Customer / Engineering / Production scope 呈现；客户和工程 scope 不显示试产配置与试产报告，Production scope 保留这些业务设置。
- Customer 侧栏、Engineering 顶栏与 Production 导航的“Shell…”入口复用同一个弹窗实例（`CustomerWorkspace.open_tcp_shell_dialog` 懒创建缓存）；弹窗连接类型支持 TCP（默认端口 1235）与串口（8N1，波特率默认 115200，枚举 + 手填；对应 letter shell 串口 / RT-Thread msh 控制台），内置哑终端与 YMODEM Sender，跨工作区切换存活，关闭即断开会话并收尾证据。UDP 不是支持类型——两个固件家族的 shell 都只在 TCP 与串口上，`create_link` 明确拒绝。
- Customer 侧栏与 Engineering 顶栏的“摇摆台…”入口复用同一个弹窗实例和进程级 `FixtureRuntime`；提供日常相对姿态、回中、组合连续摇摆、受控停止、MS 绝对航向和有界姿态修正，目标/MS 实测/误差共用一张姿态图。回原点、档案编辑和资格标定仍由 Production 工程页面负责。
- 第一方可翻译复合控件显式实现 `retranslate_ui()`；语言切换使用稳定源键，禁止扫描对象树或根据当前可见文本反查业务状态。
- **M43 Shell 输入/回显合同**：普通命令文字采用 ASCII，键盘、输入法 commit 与粘贴统一原子校验，非 ASCII/非法控制字节在发送前明确提示。接收流由 `core/tcp_shell/terminal.py` 单次解析；`TerminalScreen` 独占行、单元格、光标和颜色，Qt 文档仅投影。BS 左移后覆盖，水平游标跨包保持；正文/HEX 使用独立文档并持续消费同一接收流。原始字节与 YMODEM 文件名保持既有合同。
- **M45 Shell 有界消费与按帧投影**：shell 接收字节由 worker 独占推入每连接一个的有界 `RxRing`（512 KiB，满载有界等待片段 ≤20ms 并在片段间泵命令，YMODEM 启动不进入环等待栈）；`rx_pending` 仅在锁内"待观察态"翻转边沿发射，UI 以 33ms 单次定时器按帧消费——每帧 16 KiB 预算、4 KiB 分段、8ms 解析软预算，帧内多个 `TerminalUpdate` 用平移公式合并后单 edit block 投影。TCP/串口单次读取统一 ≤64 KiB；停止/退出把已读未入环残余 `push_final` 非阻塞移交，UI 分帧终排空后 `decoder.finish`、文本日志最后关闭；应用退出与关闭弹窗共用该收尾状态机。文档物理淘汰由 `maximumBlockCount(=max_rows)` 拥有（手动从头删除是与块数无关的 O(文档) ~160ms，该路径已删除），寻址偏移只计帧前已存在的被淘汰行；清屏以 ring 代际划界，已入环与在途旧字节不进入新屏幕。controller `disconnected/ymodem_finished` 直连转发且私有状态先更新后外发。实测：满载 5000 行持续消费 92,160 B/s、洪水中键→命令入队 p99 ~24ms、按帧可持续 ~756 KiB/s。
- **M44 采集时间代际**：`DeviceSessionCore` 在入库前使用 `TelemetryClock` 统一准入；周期流连续三个推进的反向时间样本确认采集新段（256 帧/5 秒有界候选），u32 回绕结合主机经过时间，单个旧包保留原事实。新段重建 Product 动态事实、Debug 与相关历史锚点；移动时间围栏隔离迟到旧段包。`telemetry_epoch` 与连接 generation 独立，旧操作 scope 失效，参数/控制与 OTA 重启验证消费同一代际事件，Runtime 身份授权重建，endpoint 传输继续。显示清空保留时间锚点。SDB 原始帧完整留存，派生回放分段并插入 NaN；boot ID 缺失与 SDB v2 缺少逐帧主机时间的辨识边界见 M44 计划。
- **Shell/YMODEM 是两版共享工具路径**——Shell 观察与输入不持设备事务租约；YMODEM 开始前对有管理会话的 endpoint 以 `tcp-shell-ymodem` purpose 抢占操作网关（客户/工程走 attachment 网关，批次外设备要求 gate 空闲；仅 TCP 目标可命中设备目录），手填 TCP 目标与串口目标允许直传并在证据记录 `managed_session: false`。YMODEM 文件名原样保留：`<target>_app_X.Y.Z_release|beta.bin` → 设备 DOWNLOAD 区、`app_factory.bin` → FACTORY 区、其他 → LittleFS、跨产品被设备拒绝；接收端准入（如 XESA01 RT-Thread `ry`/`ymodem_ota` 或 AFD01 系文件名路由）由各设备固件权威决定。YMODEM 帧块类型合同：头包与批结束帧 SOH/128、数据块 STX/1024——RT-Thread rym 接收端（4-xesa01）对批结束头固定按 128 字节校验，AFD01 接收端按帧头自适应两种块长；超长头包（>128B）回退 STX，两类接收端都接受。头包 ACK 与最终 EOT ACK 都等待在存储操作窗口（`final_ack_timeout_s` 20 s）：RT-Thread `yota` 在头包 ACK 前同步擦除 download 分区（XESA01 552 KiB 实测 ~7 s，分区 1 MiB），AFD01 在最终 EOT ACK 前落盘 flash；数据块 ACK 用短块窗口。XESA01 设备侧接收命令是 `yota`（每次开始前手动输入），`rb` 仅存在于 AFD01 letter-shell。YMODEM 完成只证明字节已被设备接收校验；升级生效由重启后握手回读独立呈现，不自动判 PASS。

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
| `tcp_shell_sessions/<session_id>/`（用户版为 `user/tcp_shell_sessions/`） | M37 TCP Shell 会话证据：config、TX/RX transcript、YMODEM 记录、事件、summary 与 manifest |

## 构建与发版

- **发布目标（2026-10-26 用户确认）**：源码、发行标签及 Release 使用 GitHub；使用 `github` 远端执行发布。

- **开发验证方式（2026-10-23 用户确认）**：每轮修复默认验证源码并提供运行命令；用户完成功能验证、明确要求打包后，再执行本地打包或主动触发发行构建。

- **M39 应用图标**：`ui/assets/icons/app_icon_master.png` 是已确认图稿，`scripts/generate_application_icons.py` 从母版生成 Windows 多尺寸 ICO、macOS ICNS 和实际尺寸预览。主程序/updater 的 Windows spec、macOS BUNDLE、QApplication 与顶栏共用该资源；运行时入口为 `ui/application_icon.py`。

- **macOS 本地打包**：`./scripts/build_macos.sh user|ops|both`（默认 ops）（自动 venv + 装依赖 + 跑 `update_translations.py check` + PyInstaller 主程序+updater + 嵌入 `.app/Contents/MacOS/updater` + `codesign --force --deep --sign -` ad-hoc 签名 + `xattr -cr` 清 quarantine + `ditto -c -k` 出 zip）
- **Windows 一键打包**：`scripts\build_windows.bat user|ops|both`（默认 ops）；两平台均由 `scripts/build_editions.py` 调用 spec、验证模型与构建身份、嵌入 updater 并生成独立 zip。用户 Windows 安装目录为 `SatelliteTerminalToolUser`，EXE 保留 `SatelliteDebugTool.exe`。
- **Windows 自动更新包合同**：GitHub Release `.7z` 的归档根直接包含 `SatelliteDebugTool.exe`、`updater.exe` 与 `_internal/`，兼容旧 updater 安装修复版；上传前由 `scripts/verify_update_archive.py` 验证唯一完整载荷
- **CI**：`.github/workflows/build.yml` 只跑 Windows（`prepare-release` + `build-windows`，user/ops 矩阵完成冻结离屏启动/总览/Shell/退出烟测，再生成并验证单文件 7z，两份构建成功后由唯一 publish job 创建草稿，核对资产后发布；推 master/main 触发双版本 `ci-only-build` 上传 artifact 不发版）
- **mac 不走 CI**：PyInstaller .app 含 1500+ symlinks + 7z 兼容性问题，强制本地脚本。updater 二进制仍嵌入但 mac 平台不发版，触发频率为零
- **版本号唯一来源**：`satellite_debug_tool/__init__.py.__version__`（当前 `1.8.1`）；mac `Info.plist` 的 `CFBundleVersion` 由 `satellite_debug_tool.spec` 自动读这个值
- **打 tag 发版**：`git tag v<__version__>` + `git push github v<__version__>` 触发 CI；mac 本地手动 `./scripts/build_macos.sh both`
- **`release.config.json`** 配 GitHub owner/repo/api；updater 用 `release_config.py` 读取
- **修改第一方 UI 文案后**：`python3 scripts/update_translations.py update` + commit TS+QM；只交 TS 不交 QM 会让 `check` 失败

- **双版更新合同**：用户包名 `satellite_terminal_tool-user-windows-vX.Y.Z.7z`，运维包名保留 `satellite_debug_tool-win-vX.Y.Z.7z`。更新选择匹配 edition/platform/version，安装前验证载荷身份与当前安装；仅截至 1.7.0 的历史全量 ops 包可采用旧无身份清单合同。macOS 继续通过独立签名 app 手动分发。
- **用户包模块边界**：`scripts/edition_policy.py` 明确共享 UI 模块；其余 UI、生产执行模块和 QtWebEngine 由运维版提供，Shell UI/传输/YMODEM 两版共享，`verify_edition_package.py` 审计冻结 TOC 并验证用户包完整包含 Shell 模块。`core.production` 与 `ui` 的包导出按需加载，保留公共 API 并消除聚合 import 带来的运维依赖。

### Spec 关键钩子

- `satellite_debug_tool.spec` — 主程序双版 spec；ops 保留全量 Qt/WebEngine 与运维依赖，user 使用 Qt hooks 和明确模块白名单裁剪；两版保留 pyqtgraph/OpenGL、翻译、图标与模型，`tests` 子模块不进发行包。运维包过滤掉 Designer/Assistant/Linguist.app，保留其地图需要的 QtWebEngineProcess.app。
- `updater.spec` — 单文件 updater 二进制
- 新依赖若 PyInstaller 找不到：`hiddenimports` 加一行；常见坑：GLU 子模块（`collect_all("OpenGL")` 已覆盖）

## 关键文档（按重要性）

- 本文件（`AGENTS.md`）— **当前工程约定与架构的唯一权威入口**；代码、协议或里程碑演进后同步更新本文件
- `doc/DEBUG设备协议接口规范_v2.md` — **协议权威规范**
- `doc/M22_AFD01C_upper_pc_adaptation_*.md` — 当前 AFD01C 上位机适配范围、证据与未完成真机边界
- `doc/M29_AFD01A_B2_upper_pc_*.md` — AFD01A/B2 接入计划、验收及真机边界
- `doc/M34_AFD01D_upper_pc_adaptation_*.md` — AFD01D 独立身份、OTA、试产和 3D 模型适配计划、验收与开发记录
- `doc/M35_firmware_package_version_policy_*.md` — 客户固件包默认允许升级、同版本重装和降级的跨仓发布合同
- `doc/M36_debug_ota_response_contract_*.md` — 旧固件有界 OTA 响应合同（`OK` / `dup OK` / `OK, rebooting`）与 `0.0.131_beta` 起严格合同的迁移边界
- `doc/M37_tcp_shell_ymodem_*.md` — 三工作区 TCP Shell + YMODEM 弹窗计划、验收与开发记录；真机引导升级路径
- `doc/M38_ui_color_system_*.md` — 石墨灰与蓝色 UI 配色计划、验收与开发记录；三主题、三工作区、状态与数据颜色角色；Windows 原生视觉验收待补
- `doc/M39_application_icon_*.md` — 应用品牌图标、Windows/macOS/Qt 接入、资源生成与验收记录
- `doc/M40_neutral_product_naming_*.md` — 客户交付中性命名、应用/打包/翻译范围、认证格式边界与验收记录
- `doc/M41_combobox_text_layout_*.md` — 时间范围下拉框裁切根因、字体/列表委托/内容尺寸修复、回归与原生视觉证据
- `doc/M42_user_operations_editions_*.md` — 用户版/运维版功能矩阵、装配与打包、独立配置与更新合同；用户已确认保留用户版独立固件升级，已完成实现与 macOS 双版构建验证，Windows 冻结与真机验收待执行
- `doc/M43_shell_terminal_reliability_*.md` — Shell 单次流解码、终端状态模型与 ASCII 命令输入校验；现场输入法及设备交互待验证。
- `doc/M45_shell_terminal_throughput_*.md` — Shell 有界消费与按帧投影计划、验收与开发记录；含三轮评审合同修订与两次 P0 投影升级的实测依据（头删 O(文档) 归因、maximumBlockCount 接管、16 KiB 帧预算）。
- `doc/M44_snr_time_axis_recovery_*.md` — 客户 SNR/温度历史的同连接采集代际恢复、u32/乱序准入、事务授权与回放断点证据。
- `doc/upper_pc_function_definition_vnext.md` — M7–M16 历史功能定义与路线基线，不代表当前架构
- `doc/optimization_plan.md` — M1–M6 整体优化计划（v1.2）
- `doc/M7_*.md` ~ `doc/M36_*.md` — 各里程碑 plan/acceptance/dev_log（M7 Tab 化、M8 离线地图、M10/M11 升级、M12 归一化、M13 通道语义、M14 ESA01、M15 GNSS truth、M16 i18n English、M17 内置 3D 模型、M18 客户工作台 + Product Service、M19 批量试产与夹具调试、M20 根因修复与状态完整性、M21 单进程架构收敛、M22 AFD01C 适配、M23 部件温度窗口、M24 Tracking 仿真、M25 客户多设备共享会话、M25R 会话恢复简化、M26 外接电源监测、M27 iperf3 功耗测试、M28 客户设备独立电源与 iperf3、M29 AFD01A/B2 产品接入、M30 Windows 自动更新可靠性、M31 生产出货整备、M33 上位机解密固件包）
- `doc/development_log.md` — M1–M6 实施日志
- `doc/acceptance_log.md` — F-/A- 系列验收跟踪
- `doc/i18n_terms.md` — 中英术语表
- `doc/user_manual.md` / `doc/user_manual_en.md` — M16 工程工作区历史操作手册；当前三工作区事实以本文件为准
- `doc/customer_workspace_guide.md`（含 `_en` 版） — 当前客户工作区多设备操作指南：endpoint 目录、连接阶段语义与设备级测试边界
- `doc/production_automation_operator_guide.md` — 当前试产工程预览的配置、夹具检查、批次、证据和报告操作指南，明确尚未接通的正式自动工况边界
- `doc/M32_formal_production_fixture_closure_*.md` — 电源、摇摆台、MS-6222 完整工作站配置、M31 出货整备和正式自动工况收口计划、验收与开发记录
- `doc/RELEASING.md` — 发版 SOP（含 tag 重发、hotfix、rc 预发）
- `doc/AFD01_encrypted_firmware_package.md` — 客户 OTA 加密固件包格式
- `BUILDING.md` — 打包可执行文件完整指南
