# M21 单进程架构收敛与长时运行治理开发日志

## 2026-08-24：实施启动

### 基线

- 当前提交：`aafe469`。
- 工作区状态：干净。
- 全量测试：`977 passed in 55.11s`，`0 warnings`。
- 继续维持一个完整可执行程序，使用 `Ctrl+Shift+E` 与 `Ctrl+Shift+P` 切换工作区。

### 已确认根因

- `MainWindow` 启动时构建全部工作区，未建立顶层及子页面生命周期。
- `LiveView`、客户总览和夹具调试页的高频刷新定时器在隐藏后继续运行。
- `LiveView` 同时拥有通信会话、协议状态、录制与大量 UI 呈现职责；试产 Fleet 又建立另一套设备会话。
- Product Service 与 legacy 投影采用字段覆盖，来源状态无法表达待确认，单个快照可能混合来源。
- 通用、产品和 Orbit 协议集中在大型模块中，领域边界由静态映射维持。
- SDB 回放会整文件读入内存，客户回放存在重复导入路径。
- 夹具长时记录在内存保留大窗口，写入队列和文件哈希缺少明确容量治理。
- 试产页面同时承担界面、流程协调、资源租约和证据收尾职责。
- 国际化依赖对象树反查，重复内联样式增加运行时与维护成本。

### 实施状态

- [x] M21 计划、验收标准和开发日志落盘。
- [x] 工作区/子页面生命周期与隐藏渲染休眠。
- [x] 页面按需构建与 STL 网格缓存。
- [x] 流式 SDB、后台回放与夹具长时证据治理。
- [x] 统一设备会话、控制器与遥测序列 Store。
- [x] 协议领域拆分与产品来源状态机。
- [x] 试产协调层和页面职责收敛。
- [x] 国际化、样式与架构质量门禁。
- [x] 全量测试、性能验证和验收复核。

## 2026-08-24：页面生命周期完成

- 新增统一 `activate_view()` / `deactivate_view()` 调度接口。
- `MainWindow` 成为客户、工程和试产工作区的唯一顶层生命周期调度者。
- Customer 与 Production 只激活当前子页面；工程区只激活当前 Tab。
- Live 的 100 ms/200 ms 刷新、客户总览 100 ms 刷新和夹具 100 ms 绘图在隐藏后停止。
- 定时器激活只影响呈现层；worker、Handshake、录制、OTA、Fleet 和 MS-6222 会话保持独立业务生命周期。
- Live 的两个无父定时器已绑定到 `LiveView`，销毁时由 Qt 统一回收。
- 新增生命周期回归测试，覆盖客户、工程 Live、工程 Device 和夹具子页面切换。
- 阶段全量回归：`979 passed in 57.82s`，`0 warnings`。

## 2026-08-24：按需呈现与工作区切换收敛

- 新增 `LazyViewHost`，客户 RF/回放/维护、工程 Playback/Log/Device、Production 和夹具调试均在首次访问构建并保留实例。
- `LiveView` 改为两阶段结构：构造阶段建立 `DeviceSessionCore`、连接配置、Store、录制和控制器；首次进入工程 Live 时才建立通道树、曲线、Dashboard、3D、状态和事件组件。
- 客户页面可在工程呈现尚未创建时连接 UDP、预置录制并接收数据；首次进入工程 Live 后从现有 Store 补齐通道和值。
- 修复 `LazyViewHost` 的预构建激活根因：业务入口先调用 `ensure_view()` 后，页面切换会明确激活已存在的子页面。旧逻辑只更新宿主 active 状态，导致 Production 页面可见但子页面生命周期仍为 inactive。
- STL loader 增加进程级只读网格缓存，同一路径和文件状态只解析一次，调用方获得独立 ndarray 视图。
- 200 次三工作区切换保持同一会话和 Store；offscreen 热切换 p50 `1.076 ms`、p95 `1.853 ms`、最大 `2.176 ms`。
- 首次访问实测：Engineering `228.739 ms`，其中 Live 呈现构建 `149.383 ms`；Production `165.090 ms`，其中页面构建 `133.563 ms`。
- 客户总览、工程 Live 和夹具绘图分别执行 10 秒隐藏观测，隐藏回调增量均为 `0`。

## 2026-08-24：统一会话、控制器与产品来源

- 新增 `DeviceSessionCore`，统一持有 `FrameReceiverV2`、Handshake、Profile、遥测、状态、事件、GNSS、Orbit 和 Product Service Store。
- 新增 `SessionRegistry`，同一 endpoint 的客户/工程/试产消费者取得同一个权威 core；重复注册不同 core 会明确拒绝。
- Debug、参数、OTA、Product 控制、产品订阅和全量录制配置均进入独立控制器；请求上下文在发送前注册，支持同步响应且不产生竞态。
- 新增 `TelemetrySeriesStore`，以设备时间为权威时间轴，统一窗口查询与数据缺口语义。
- Product value 增加 `PENDING / VALID / STALE / UNSUPPORTED`、来源、主机接收时间和质量；Product Service 与 legacy v2 采用整快照来源选择，不做字段级混合。
- Fleet 首包只解码一次，再将解码结果应用到权威会话；Product Service 同设备时间戳下的不同报文保留各自接收时刻。

## 2026-08-24：协议、回放与证据链治理

- DEBUG v2、Product Service 和 Orbit 解码器拆入独立领域模块；命令注册表验证所有命令所有权互斥，`FrameReceiverV2` 只处理包络、CRC、恢复和领域分发。
- Recorder 与 Importer 共用 `io/sdb_schema.py`；SDB v3 采用流式 record envelope，生成可校验、可重建的稀疏 `.sdbi`。
- 新增后台 `PlaybackLoadThread` 和磁盘型 `PlaybackSeriesProvider`；回放只把当前时间窗口和像素预算内的极值点载入内存。
- 夹具原始证据使用固定容量队列写盘；队列满、写入失败和分析失败均形成 `INCOMPLETE`。比较 CSV 从落盘证据流式生成，内存指标样本有界，SHA-256 按块读取。
- 新增 `BatchCoordinator` 与 `FixtureSessionCoordinator`，集中批次/夹具状态迁移、租约、收尾与异常恢复；试产 UI 不再直接迁移 attempt 状态。

## 2026-08-24：国际化、样式与质量门禁

- `TranslationManager` 改为显式 `retranslate_ui()` 调度；删除 QObject 全树扫描、ChildAdded 跟踪和可见文本反向查询。
- 所有注册翻译的复合控件实现本类 `retranslate_ui()`；动态状态保存稳定源键和类型化值。
- 高频状态样式进入语义属性 helper，仅属性值变化时执行 repolish。
- 新增 M21 架构门禁，覆盖协议命令所有权、FrameReceiver 依赖方向、UI 解析器边界、试产状态迁移边界、显式翻译和语义样式幂等性。
- 软件验收完成；5 小时四 DUT 加 MS-6222 RSS 目标和真实硬件连续性保留为台架验收项。

## 2026-08-24：最终软件验证

- `python3 scripts/update_translations.py update`：`868 finished`，`0 unfinished`。
- `python3 scripts/update_translations.py check`：通过，目录与源码同步。
- `PYTHONPATH=. pytest satellite_debug_tool/tests -q`：`1011 passed in 32.16s`，无 warning。
- `python3 -m compileall -q satellite_debug_tool`：通过。
- `git diff --check`：通过。
- 本次未执行 Git 提交、打包或硬件台架操作。
