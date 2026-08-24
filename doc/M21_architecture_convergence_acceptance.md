# M21 单进程架构收敛与长时运行治理验收标准

## A. 生命周期与切换

- [x] Customer、Engineering、Production 和高频子页面均实现统一生命周期。
- [x] 任一隐藏页面连续 10 秒的曲线、3D、表格和夹具绘图回调计数为 0。
- [x] 页面隐藏期间，当前连接仍接收帧，握手、录制、OTA、批次执行和 MS-6222 采集按业务状态继续。
- [x] 页面激活立即呈现最新 Store 快照，随后恢复可见刷新定时器。
- [x] 连续切换客户/工程/试产 200 次不重连、不清空 Store、不改变录制或设备事务状态。
- [x] 已构建页面的切换耗时 p95 小于 100 ms；首次按需构建目标小于 500 ms。
- [x] 多个 3D 视图复用 STL 网格缓存，不重复解析同一模型文件。

## B. 页面按需构建

- [x] 默认启动只构建客户总览和共享会话。
- [x] 客户其他页面、工程页面和试产页面首次访问时构建，后续访问复用原实例。
- [x] 首次构建前到达的数据可在页面创建后立即显示。
- [x] 按需构建期间连接、录制、OTA 和批次状态保持不变。

## C. 会话与产品状态

- [x] 一个 endpoint 在进程内只有一个 `DeviceSessionCore` 权威实例。
- [x] 客户、工程和试产视图不再各自创建重复组帧器或握手状态。
- [x] Debug、参数、OTA 和产品控制通过独立控制器运行，界面不直接操作 worker 私有状态。
- [x] 产品字段可区分 PENDING、VALID、STALE 和 UNSUPPORTED，并携带来源与时间质量。
- [x] 单个产品快照只来自 Product Service 或 legacy 投影之一，不混合来源。
- [x] 含产品记录的回放使用产品投影；旧录制使用 legacy 投影。

## D. 协议兼容

- [x] DEBUG v2、Product Service 和 Orbit 领域实现完成拆分。
- [x] 原 `core.protocol` 公开 import 继续可用。
- [x] 现有协议 golden vectors 的编码字节、CRC、解码模型和异常恢复完全一致。
- [x] `FrameReceiverV2` 只承担包络解析、CRC、恢复和领域分发。

## E. SDB 与长时证据

- [x] Recorder 与 Importer 共用唯一 SDB schema。
- [x] 大型 SDB 采用流式读取和稀疏索引，主线程不执行整文件读取。
- [x] 回放仅加载当前查询窗口，进程内存不随文件总时长线性增长。
- [ ] 四台 20 Hz 设备加 100 Hz MS-6222 的 5 小时合成测试中，常驻内存相对空闲增量目标不超过 256 MB。
- [x] 夹具界面只保留最近 5 分钟显示数据，完整证据持续写盘并在线累计指标。
- [x] 证据队列有界；满队列、写入失败和采样缺口均形成 INCOMPLETE 证据，不静默丢失。
- [x] 文件哈希按块计算，长时会话完成时无全文件内存复制。

## F. 试产协调与界面职责

- [x] BatchCoordinator 与 FixtureSessionCoordinator 是试产状态迁移和资源租约的权威来源。
- [x] 两个试产页面只绑定类型化状态并发出意图，不直接编排长流程。
- [x] M19-A 与 M19-A.2 现有行为和记录格式保持兼容。
- [x] 页面切换和隐藏不会中断正在执行的批次或夹具会话。

## G. 国际化、样式与自动化

- [x] 语言切换使用稳定翻译键，不通过可见文本反向决定业务状态。
- [x] 高频重复内联样式已收敛到共享语义样式。
- [x] 架构测试覆盖隐藏渲染、单一会话、协议依赖、有限队列和 UI/业务边界。
- [x] 翻译 update/check 通过，无 unfinished、空翻译或占位符不一致。
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q` 全绿且无 warning。
- [x] `python3 -m compileall -q satellite_debug_tool` 与 `git diff --check` 通过。

## 软件验收证据

- Offscreen 200 次热切换：p50 `1.076 ms`、p95 `1.853 ms`、最大 `2.176 ms`；会话与 Store 实例保持不变。
- 首次访问：Engineering `228.739 ms`（Live 呈现构建 `149.383 ms`），Production `165.090 ms`（页面构建 `133.563 ms`）。
- 三组 10 秒隐藏呈现观测：工程 Live、客户总览、夹具绘图的隐藏回调增量均为 `0`。
- 自动化覆盖见 `test_view_lifecycle.py`、`test_device_session_core.py`、`test_playback_series_provider.py`、`test_fixture_session.py` 与 `test_m21_architecture.py`。

## 待执行边界

- 5 小时、四 DUT 加 MS-6222 的 RSS 验收需要长时合成或硬件台架运行；当前只确认各内存容器、查询窗口与证据队列具有固定上限。
- 真实设备连接、录制、OTA、批次和 MS-6222 采集在工作区切换期间的连续性属于硬件验收，不以 offscreen 测试替代。
