# 上位机功能定义 vNext

## 0. 文档定位

| 项目 | 内容 |
|------|------|
| 日期 | 2026-08-05 |
| 适用项目 | `satellite_debug_tool` |
| 适用协议 | DEBUG protocol v2 |
| 当前定位 | 当前产品功能定义 + 后续优化路线 |
| 历史关系 | `optimization_plan.md` 保留为 M1-M6 历史蓝图 |

本文件用于回答两个问题：

1. 当前上位机到底已经定义成什么产品。
2. 下一阶段优化应补哪些边界、语义和验收闭环。

## 1. 产品定位

Satellite Debug Tool 当前是一套同时服务客户操作与内部工程诊断的 AFD01 桌面工具。默认入口面向
客户，提供稳定产品语义、受保护控制、全量录制、受限回放和签名 OTA；工程人员在当前会话确认
解锁后，继续使用原有动态 Debug Profile 与全量分析能力。

优先级按以下顺序排列：

1. 客户看到的状态、控制结果和故障信息真实且稳定。
2. 发射与射频控制有能力约束、确认和设备读回。
3. 录制、回放、日志解析可追溯，客户可把全量现场数据回传支持团队。
4. 工程诊断信息密度和动态扩展能力不退化。
5. 客户 OTA 只接受授权签名包，发布与回退策略可审计。

当前应用包含两个共享同一设备会话的工作区：

| 工作区/页面 | 主要用户 | 用户目标 | 主要输出 |
|-------------|----------|----------|----------|
| 客户 / Overview | 客户操作员 | 连接设备、看整机状态、录制现场 | 跟踪/INS/GNSS、姿态/波束、3D、SNR、部件健康 |
| 客户 / RF control | 授权客户操作员 | 自动/手动切换与手动射频控制 | 精确响应和设备最终读回 |
| 客户 / Playback | 客户/支持人员 | 按时间复现客户可见状态 | 只读 Overview、时间轴、质量摘要、GNSS 弹窗 |
| 客户 / Maintenance | 客户维护人员 | 查看设备清单和升级 | 设备/部件信息、签名 `.sfpkg` OTA |
| 工程 / Live、Playback、Log、Device | 内部工程人员 | 全量调试与故障分析 | 动态通道、状态、事件、参数和开发 OTA |

工作区切换不得重连设备、重建 worker 或清空 Store。工程入口默认隐藏，通过 `Ctrl+Shift+E`
确认后仅在当前进程内开放，不持久化解锁状态。M18 客户功能只实现 AFD01；ESA01 保留已有工程
兼容，不属于本阶段交付范围。

## 2. 当前功能基线

### 2.1 客户工作台

客户 Overview 消费 AFD01 稳定产品服务，不依赖动态 Debug 字段名：

- 显示型号、序列号、连接状态、控制模式、跟踪阶段、锁定、组合导航和 GNSS 状态。
- 显示波束角、姿态角、3D 整机、经纬高、当前 SNR 和 60 秒历史曲线。
- 显示变频板、发射阵列和接收阵列的在线、温度、电压和版本；没有权威来源的字段显示 `—`。
- GNSS 天空图和信号详情继续复用现有弹窗。
- 客户连接只订阅产品服务，不自动打开动态工程 Debug。

RF control 只在 AFD01 能力声明和设备读回有效时开放。手动参数必须等 MANUAL 模式读回确认；
收发频点和极化作为一个事务原子下发；发射开关独立确认。AFD01 当前只支持左/右旋圆极化。
所有成功提示都要求匹配 `request_id + operation`，并等待后续产品遥测与目标值一致。

Maintenance 不显示任意参数编辑，只接受通过内置 Ed25519 公钥验签、产品/硬件/版本策略匹配的
`.sfpkg`。工程 Device 页仍保留 raw `.bin` 与调试回退能力。

### 2.2 工程 Live

Live 是核心工作台，负责连接真实设备或仿真链路。

已定义能力：

- 串口/UDP 连接，连接后由 `Handshake` 请求 META、CHANNEL_DEFINE、STATE_DEFINE、EVENT_DEFINE。
- `ProfileStore` 根据 `hw_type` 聚合三张表；UI 按 profile 重建。
- `StatusStrip` 显示链路、录制、心跳和 critical 状态/通道。
- `Dashboard` 显示 critical KPI 卡片和 critical ENUM 模式按钮。
- `GroupedChartWidget` 支持单图/分组、隐藏全部、通道显隐、归一化、子图独立归一化、事件标记。
- `ChannelPanel` 复选框语义是“控制曲线显隐”，不是只隐藏标签。
- `StatePanel` 显示状态字，支持分组和变化高亮。
- `EventTimeline` 显示事件，支持过滤、搜索、双击/右键跳转曲线。
- `AttitudeWidget` 自动按 profile 通道名绑定 roll/pitch/yaw/ant_az/ant_el，显示机体、波束、扫描轨迹，并可加载本地 STL。
- `ControlPanel` 支持采样率、用户标记、通道 enable mask、复位统计。
- 工程录制保持 `.sdb v2`；客户“全量录制”使用 `.sdb v3`，写盘均为后台线程。
- 仿真入口集成在 Live，当前通过 MockModem/fake-device 验证对星闭环与 SNR 模型。

当前限制：

- 模式切换目前只对 `state_id == 0` 下发 `SET_TRACE_MODE`，其它 ENUM 状态字显示“协议待扩展”。
- 3D 绑定当前是自动绑定，不提供手动持久化覆盖。
- 仿真模式下 Chart 数据注入仍是后续项，当前主要显示仿真面板 metrics。
- Worker 仍按解析到的单帧 emit，旧 plan 中“20ms 批量 emit”没有作为当前事实实现。

### 2.3 Playback

工程 Playback 用于离线复盘 `.sdb v2/v3`；客户 Playback 只投影稳定产品字段。

已定义能力：

- 接受 SDB v2/v3；打开时读取 header、profile/session JSON 和记录区。
- 每个 Playback Tab 有独立 `ProfileStore` 和无界 `DataStore`，不污染 Live。
- 回放曲线支持时间窗、事件标记、跳转曲线。
- 如果 profile 中存在 `gps_lat` / `gps_lon`，可启用地图浮窗显示轨迹。
- SDB v3 保存每个 RX chunk 的主机时间、控制请求、metadata、gap marker 和质量 summary。
- 客户回放按记录时间驱动独立 ProductServiceStore，支持播放、暂停、拖动和倍速，不提供控制。

当前限制：

- SDB v1 不在主程序兼容范围；如确有历史数据需求，应另做一次性转换脚本。
- CSV import 未实现；README/手册不得宣称 CSV 已支持。
- 地图 GPS 识别当前依赖通道名约定，后续应迁移到 profile 语义角色。

### 2.4 Log

Log 用于把 WindTerm 文本日志转成可视化曲线。

已定义能力：

- `WindTermLogParser` 识别表头和数据行，非数值列整列剔除。
- 解析结果生成虚拟 profile，`hw_type="windterm_log"`。
- 使用无界 `DataStore`，按行号生成占位时间戳。
- 若列名包含 `gps_lat` / `gps_lon`，可启用地图轨迹。

当前限制：

- 日志时间轴目前是占位时间戳，不等同于真实设备时间。
- 日志列名同样存在约定式语义，后续可引入导入配置或语义映射。

### 2.5 工程 Device

Device 是设备管理工作台，复用 Live 的连接和帧广播。

已定义能力：

- 显示 META_INFO 中的设备类型、固件版本、序列号、协议版本。
- 请求参数表，展示参数类型、当前值、范围、只读/需重启标志。
- 参数写入和恢复默认通过 COMMAND_RESPONSE 闭环。
- OTA 支持选择固件、BEGIN/DATA/END/ABORT、进度、速度、剩余时间、等待重启上线。
- OTA 期间会暂停 debug 输出，结束后恢复。

当前限制：

- Device 与 Live 共享连接，当前不支持独立第二设备连接。
- OTA 失败恢复依赖设备端 bootloader 和通信链路行为，仍需要真机 SOP 固化。

## 3. 跨域能力定义

### 3.1 Profile 与语义

当前 profile 由三张 DEFINE 表组成：

- CHANNEL_DEFINE：`channel_id`、类型、group、flags、name、unit、display range。
- STATE_DEFINE：`state_id`、类型、flags、name、enum items。
- EVENT_DEFINE：`event_id`、level、name。

这足以驱动基础 UI，但不足以表达所有高级语义。当前仍存在以下约定：

| 语义 | 当前做法 | 风险 |
|------|----------|------|
| 地图轨迹 | 通道名必须是 `gps_lat` / `gps_lon` | 新设备改名后地图不可用 |
| 3D 姿态 | 按 roll/pitch/yaw/ant_az/ant_el 名称自动匹配 | 多套姿态源或不同命名时不可控 |
| 模式切换 | `state_id == 0` 视作 trace mode | 新设备 state_id 规划不同会失效 |
| Dashboard 合并 | 依赖 critical flag 和名称模式 | 复杂 KPI 布局表达力有限 |

vNext 应补 profile 语义层，方向是协议 v2.x 或 profile cache schema 增量字段：

- `semantic_role`: `gps_lat`、`gps_lon`、`roll`、`pitch`、`yaw`、`antenna_az`、`antenna_el`、`snr` 等。
- `control_binding`: ENUM 状态字绑定到哪个 CONTROL 子命令和参数编码。
- `ui_role`: critical、dashboard、status、map、attitude、hidden/default visible 等更明确的 UI 角色。
- `capabilities`: 设备是否支持参数表、OTA、采样率调整、通道 mask、仿真模式。

### 3.2 数据格式

当前正式数据格式：

- 工程实时协议：DEBUG v2 自描述二进制帧。
- 客户实时协议：同一包络内的 AFD01 产品服务 `0x20..0x26`。
- 工程录制/回放：`.sdb v2`，header 内嵌 profile JSON。
- 客户全量录制/回放：`.sdb v3`，记录级主机时间、会话 metadata、控制和质量信息。
- 日志导入：WindTerm 文本日志解析为虚拟 profile。

当前边界：

- SDB v1 不再直接支持。
- CSV import 不支持。
- CSV export 如需产品化，必须定义列头、时间戳、profile sidecar 和多设备语义，而不是简单 dump 曲线。

### 3.3 地图

地图用于 Playback/Log 的离线轨迹复盘。

- 默认读 `~/.satellite_debug_tool/tiles/<region>/<z>/<x>/<y>.png`。
- 可配置天地图 token。
- 轨迹、事件 marker、时间窗高亮通过 JS API 更新。

当前 GPS 识别仍依赖通道/列名，属于 M13 待收口项。

### 3.4 自动更新与发版

当前定义：

- 版本号来自 `satellite_debug_tool.__version__`。
- UI 提供“检查更新”和后台静默检查。
- updater 从 GitHub Release 下载单个 `.7z` 资产，解压并替换；仍兼容旧版分卷资产。
- `~/.satellite_debug_tool/` 位于安装目录外，升级时保留用户配置、profile 和地图缓存。

当前边界：

- macOS 不签名、不公证。
- Windows 发布流程以 GitHub Release 为唯一自动发布目标；macOS 仍由本地打包并单独分发。

### 3.5 主题与字体

当前定义：

- 主题：`dark`、`dark_hc`、`light`。
- UI 不再暴露字号档位，当前基准固化为 `small`。
- `styles.py` 仍保留 `FONT_SCALES` API 兼容旧测试和组件内部调用。

## 4. 明确不做

当前阶段不做：

- 多设备同时连接。
- 主程序内直接兼容 SDB v1。
- 未定义 schema 的 CSV import。
- Web/移动端。
- 操作系统应用代码签名、公证、灰度发布（客户固件包 Ed25519 验签已实现）。
- 自动短信/钉钉告警。
- 在 M18 为 ESA01 增加客户投影或控制；后续有明确交付需求时单独接入。

## 5. 已知欠缺

| 编号 | 欠缺 | 影响 | 建议归属 |
|------|------|------|----------|
| G-01 | profile 缺语义角色 | 地图/3D/控制仍靠名称和 state_id 约定 | M13 |
| G-02 | 真机/外场验收散落 | 已实现项难以转为可签收基线 | M15 |
| G-03 | 仿真 Chart 不滚动 | 演示和回归体验不完整 | M14 |
| G-04 | SDB/CSV 数据互操作未定案 | 外部分析和历史数据迁移容易反复 | M16 |
| G-05 | OTA 失败恢复 SOP 不够细 | 现场升级风险无法量化关闭 | M16 |
| G-06 | Worker 批量 emit 未实现或未重新决策 | 高频性能优化路线不清晰 | M15 |
| G-07 | 文档历史状态和当前状态容易混读 | 新开发者容易按旧计划改错方向 | 本次已处理一部分 |

## 6. 后续路线

### M13 — Profile 语义层

目标：让“profile 驱动”从名称约定升级为显式语义。

建议交付：

- profile schema 草案：semantic role、control binding、capabilities。
- 协议 v2.x 兼容策略：旧设备仍按名称约定 fallback，新设备优先使用语义字段。
- Live/Playback/Log 地图和 3D 改为优先读 semantic role。
- Dashboard 模式按钮不再硬绑 `state_id == 0`。

验收锚点：

- 新设备通道名不用 `gps_lat/gps_lon`，只要 semantic role 正确，地图仍启用。
- trace mode 不在 state_id 0 时，Dashboard 仍能下发正确控制。

### M14 — 仿真产品化

目标：把仿真从“面板可看”推进到“完整模拟设备数据流”。

建议交付：

- MockModem 或 fake-device 将 SNR、扫描角、失指等注入 DataStore/Chart。
- 清理旧仿真模块或明确标为 legacy。
- 固化 PC fake-device 与设备端 simulate_modem 契约。
- 增加仿真演示 SOP。

验收锚点：

- 开启仿真后 Live Chart 自动滚动，Dashboard/Event/State 有可解释数据。
- 遮挡、雨衰、航向偏差能在曲线和事件中同步体现。

### M15 — 真机/外场验收闭环

目标：把当前黄色验收项转为可执行测试记录。

建议交付：

- `doc/field_validation_sop.md`：握手、100Hz 10min、1h 录制、事件延迟、CPU/内存/FPS、1366x768、500cd/m2 可读性。
- 抓包/日志/截图命名规范。
- 性能采样脚本或手工步骤。
- `acceptance_log.md` 按证据回填。

验收锚点：

- 每个黄色项至少有“如何测、用什么设备、通过阈值、证据路径”。
- 完成一次 afd01 真机记录。

### M16 — 数据互操作与发布治理

目标：减少交付和历史数据反复。

建议交付：

- 明确 CSV 是“不做 / 只导出 / 导入导出都做”。
- 如果保留历史数据需求，提供 `sdb_v1_convert.py`，但不把 v1 兼容塞回主路径。
- OTA 失败恢复 SOP：断电、半包、校验失败、重启超时、同版本重刷。
- 发布 SOP 与 updater 真流程联调记录。

验收锚点：

- README/用户手册/RELEASING/BUILDING 对数据格式和升级流程说法一致。
- 做一次测试 tag release，并记录 updater 行为。

## 7. 当前文档索引

| 文档 | 定位 |
|------|------|
| `doc/upper_pc_function_definition_vnext.md` | 当前功能定义与后续路线 |
| `doc/optimization_plan.md` | M1-M6 历史优化蓝图 |
| `doc/acceptance_log.md` | M1-M6 及后续验收状态跟踪 |
| `doc/M7_plan.md` ~ `doc/M12_plan.md` | 各里程碑局部计划 |
| `doc/M13_profile_semantics_plan.md` | M13 profile 语义层计划 |
| `doc/simulation_delivery.md` | 当前仿真交付和遗留 |
| `doc/DEBUG设备协议接口规范_v2.md` | 协议权威规范 |
| `doc/user_manual.md` | 用户操作手册 |
| `doc/RELEASING.md` | 发版流程 |

## 8. 需要用户决策的问题

当前没有阻塞本次文档开发的问题。后续进入 M13/M16 前，需要用户确认：

- profile 语义扩展是否允许同步修改下位机协议/profile。
- CSV 是否仍是产品需求，还是只保留 SDB v2。
- OTA 失败恢复是否要纳入上位机 UI，还是只做 SOP。
