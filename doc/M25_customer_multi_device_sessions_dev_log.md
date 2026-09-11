# M25 客户多设备共享会话开发记录

> 2026-09-03 M25R 更新：下文关于永久恢复台账、全局恢复阻断和 Settings 恢复中心的实施记录已被 M25R 取代，仅保留为历史。当前实现对未确认终态采用 endpoint 本地有界收口，不创建跨重启恢复状态。

## 2026-09-01：用户确认并进入实施

- 用户确认按 M25 共享架构实施，并授权先单独收口 `TRACKING_SIMULATION=0x11` 规范/测试漂移。
- `0x11` 合同已根因收口：Debug 顶层合法集合精确扩展为 `0x01..0x11`，协议总表登记 `TRACKING_SIMULATION`，未知命令回归改用保留值 `0x12`，未放宽未知命令。
- 针对性验证：`76 passed in 0.24s`；进入 Broker/Runtime 与客户多设备实现。

## 2026-09-01：计划与源码审查

### 用户目标

- 客户页支持多设备同时在线，并在左侧设备列表切换。
- 目标示例为 AFD01C `192.168.1.13:4004` 与 ESA01 `192.168.1.12:4004` 同时连接。
- 用户进一步明确：试产已支持多设备，本功能应共用现有组件、功能和方法，不应独立再实现一套。

### 基线

- 上位机 HEAD：`c9b44da feat(afd01c): 完善状态展示并增加Tracking仿真`。
- 计划创建前工作树干净，`master` 领先 `origin/master` 14 个提交。
- 本轮按 `AGENTS.md` 仅创建计划、验收标准和开发记录，用户确认前不修改业务代码。

### 当前事实

- 客户/工程共用一个 `LiveView`、一个 `UdpWorker` 和一个 `DeviceSessionCore`；当前左栏只有 Overview/RF/Playback/Maintenance。
- `SessionRegistry` 已实现一个 endpoint 一个权威 Core，但没有多 endpoint UDP ingress、在线阶段或 active selection。
- M19 已证明多 worker 竞争一个本地端口存在接收归属风险，并实现单 `UdpFleetHub` 按 source endpoint 分流。
- `FleetController`、`DeviceSession` 和 `UdpFleetHub` 当前混有 CIDR 探测、生产准入、slot、UID/SN/MAC、批次 recorder 和 SNR 等生产语义，不能整体直接作为客户 manager。
- `production_product_policy()` 不接纳 ESA01；`customer_product_policy()` 已分别登记 AFD01C protocol 8 和 ESA01 protocol 6。
- 当前 Fleet `send_to()` 成功表示本地出队请求已入队，不是同步 OS `sendto()` 完成，不能直接作为客户 RF/OTA sender 合同。
- 客户页面和工程控制器在构造时绑定具体 Core/Store；安全方案是每 endpoint lazy 页面实例，不做动态 rebind。

### 架构决策

- 从试产抽出进程级 `UdpEndpointBroker` 和 `EndpointSessionDirectory/Runtime`，由 Customer/Engineering/Production 共同消费。
- 同一 endpoint raw datagram 只解析一次、Core 只创建一次、Product 订阅只保留一个 owner。
- `FleetController` 继续保留生产编排，改为通用 Runtime 上的生产 facet；不增加 customer mode 分支。
- 客户显式列表、active endpoint、客户录制和页面导航保留在客户域。
- 设备切换只切页面 stack；连接、Store、录制、OTA 和已开始事务继续。

### 交叉复核后的合同收口

- `EndpointSessionDirectory` 直接演进取代 `SessionRegistry`，不增加平行 Runtime map；明确 Configuration、Transport、Facet、Subscription、Handshake 五类 lease。
- Runtime 只拥有共享 transport/presence/代际，客户目录单独拥有 attached/detached 连接意图。Production 已在线时客户未连接仍显示 DISCONNECTED；Customer 后附着/先断开均不重置 Production 持有的 transport epoch。
- 客户连接另有 `CustomerAttachmentScope` epoch；Customer detach 即使不改变 Core generation，也会原子取消客户 pending/租约/确认框并失效 OTA token 与草稿，迟到响应不能复活旧事务，Production 保持不变。
- Core connected 不是 facet 发送授权；Runtime 发放 owner-scoped send capability，Customer detached 时即使 Production 在线也不能借共享 sender 出包。OS 成功写出后只有一个类型化 sent-evidence 事件。
- Broker 使用 admission claim：Customer 为精确 endpoint，Production 为 CIDR+device port。是否走临时 candidate 取决于权威 feed/Production 准入状态，不取决于 Runtime 对象是否存在。
- 修正“Runtime 存在即跳过准入”的错误判据：仅配置 dormant Runtime、Customer-active Runtime 与未知 endpoint 使用三条明确 Production admission 路径；candidate receiver 有 endpoint 生命周期、数量/字节/超时上限，raw bytes 仍只解析一次。
- `send_to()` 采用 socket lock 下跨线程同步 `sendto()`，成功只表示完整数据报写出；删除现有 Fleet 仅入队成功的歧义语义。
- Runtime 增加 `acquire/update/release_subscription` 和 `acquire/release_handshake`；多个 facet 共用一个 Product 订阅状态机，Customer 后附着可在现有 transport 启动 Handshake，而不调用 `begin_connection()`。
- 统一本地端口为 `device_udp.local_port`，在默认合并前按原始 JSON 和迁移 marker 处理；新 key、marker 与移除旧 key 位于同一次原子文件 replace，损坏 marker、两个不同非默认旧值或无效值均 fail closed。
- 客户目录边界改为 0~4 台；新增不自动连接。编辑要求 detached、无录制/OTA/事务，先校验再原子替换；重复目标选择已有项且源项不变，最后一台删除后保留空状态和 Playback。
- 通用 Directory 维护已验证 UID/SN 重复事实，Customer 告警并 fail closed 变更操作；Production 保留 UID/SN/MAC、slot 和 endpoint 迁移处置。Production 拒绝 ESA01 不能污染共享 Runtime 或 Customer facet。
- 工程连接显式区分共享客户 UDP 与独立单串口；共享 UDP 仅观察 Customer attachment、地址只读并由客户目录管理，Settings/ProfileStore 跟随当前工程会话。
- 进程级录制路径 lease 防止客户/试产两个 writer 写同一 SDB；默认文件名加入 endpoint 和确定性唯一后缀。
- Runtime operation gate 仲裁客户变更与生产批次：冻结/运行批次阻止同设备客户变更，已有客户变更则阻止 Production 冻结并明确标记未就绪。
- `RuntimeOperationGate` 直接取代 Core transaction lock，所有 mutation 与 Production freeze 使用同一 CAS。客户录制因 full capture-profile 协商属于 mutation，与同 Runtime 的冻结/运行批次互斥，不增加被动录制旁路。
- active mutation 阻止直接 detach；当前身份 scope 有效时，原 controller 才可在旧 capability 下完成 OTA_ABORT/Tracking stop/capture restore 等终止。没有有效身份 scope 或终止证据时 gate 转 RECOVERY_REQUIRED/NOT_READY，继续阻止 Production freeze，而不是本地 clear 后伪报恢复。
- RECOVERY_REQUIRED 由 Directory 级原子 `SessionRecoveryLedger` 持久化；Runtime 销毁、客户 Edit/Delete 和应用重启都不清 tombstone，恢复专用 capability + 匹配遥测才可删除。
- 共享 UDP attach/admission 初始只发 identity/Handshake/Subscription/read-only 受限 capability；普通 mutation/freeze 必须由当前 `PresenceEpoch` 新鲜 UID/SN 且已出现来源一致后，通过 `IdentityAuthorizationScope` 原子提升。presence stale、身份变化/冲突或来源失效立即撤权，即使 Core generation 不变也不能沿用旧 Store/ProfileCache 控制同 endpoint 的新设备。
- 撤权时 active mutation 先进入 TERMINATING，但身份未重新确认前不发送 terminal；只有新 PresenceEpoch 确认仍是原 UID/SN 才开放缩限 abort/stop/restore，发现新身份、冲突或超时则直接保留 RecoveryLedger 未知状态，避免把旧操作的终止命令发给同 endpoint 的新设备。
- 同一身份换 endpoint 仍恢复 NOT_READY。endpoint-only tombstone 触发全局 unresolved fail-closed，避免改址绕过。
- endpoint-only 物理退役只能在零活动 demand/recorder/operation 下通过精确 endpoint、操作者/原因和动态短语强确认，原子转为保留 hash 的 RETIRED 审计记录；不是无证据删除旁路。
- recovery clear 冻结 request/scope/telemetry cursor，只接受 sent event 后同 endpoint/identity/generation 的新响应与遥测；ledger 原子删除/clearance 是转 IDLE 的 commit point。
- lease/claim 顺序、逆序回滚、Qt owner 线程、Broker queued ingress 与关闭 lock order 已冻结；presence 的有效记录/3 s 时效和 Product/Handshake demand 边界进入验收。
- 工程共享 UDP 只观察 Customer attachment，不拥有独立 UDP transport；0 台/detached 显示空状态，模式切换对 recorder/OTA/事务 fail closed。所有消息和控制页面显示冻结目标。
- 共享工程 recorder 明确为不发送 capture-profile 的 endpoint current-stream 录制；客户 full-support recorder 与 Production batch 互斥，二者不混用证据语义。
- customer devices migration 补齐新/旧/marker/部分缺失/损坏/保存失败矩阵，与 local-port 一样在默认合并前原子处理。
- 整份 `settings.json` 在默认合并前验证；主坏备好恢复，主备皆坏保留证据并阻止 Customer UDP/Broker/Production，不能沿用当前 JSONDecodeError 静默 defaults 行为。
- settings 双损坏进入 read-only recovery，主题/语言/普通 preference/退出均不得覆盖原文件；三个工作区同步显示 identity/global-recovery 禁用原因。
- 录制路径使用跨平台 canonical key、进程独占 lease 和 `O_CREAT|O_EXCL` `RecordingReservation`；`DataRecorder.start()` 一次性接管 fd/lease，启动失败删除或隔离半成品并删除 `wb` 覆盖入口。性能测试固定采样事件、四路录制 soak 和运行环境记录。
- 状态文字不只依赖颜色；所有设备消息和辅助浮窗带冻结 endpoint 归属。Playback 保持全局单实例，主题/语言、布局和浮窗删除/退出生命周期进入验收。
- Host 压测使用 loopback/注入 endpoint；固定 LAN IP 只用于真机。Windows DPI/打包与真实混合设备均作为独立验收边界，不用 host PASS 代替。

### 基线测试异常

- 计划阶段执行全量测试：`1196 passed, 1 failed in 48.33s`。
- 失败为 `test_frame_v2.py::TestProtocolConstants::test_cmd_values_match_spec`：当前 `CmdType.TRACKING_SIMULATION=0x11`，测试仍断言所有 Debug cmd 必须 `<=0x10`。
- 该失败存在于干净基线且与 M25 文档无关，但项目没有允许失败 baseline；进入业务实现前需要单独确认并收口规范/测试漂移，不能把它记为 M25 可忽略失败。

### 当前进度

- [x] 当前代码、M18/M19/M21/M22/M24、产品策略和试产 Fleet 只读审查。
- [x] 共用组件边界与客户/试产独立语义确定。
- [x] M25 plan、acceptance 和 dev log 创建。
- [x] 架构、历史合同、UI/测试三路交叉复核并收口 P0/P1。
- [x] 用户确认计划。
- [x] 通用 Broker/Runtime 抽取与试产回归。
- [x] 客户多设备目录、页面和安全隔离实现。
- [ ] Host 自动化最终复跑、双平台和双真机验收。

## 2026-09-01：配置恢复与录制路径所有权

- `Settings` 在默认合并前校验原始 JSON object，客户设备列表与统一
  `device_udp.local_port` 使用一次原子迁移；迁移冲突、marker 损坏和保存
  不确定性均阻止设备路径。
- 主配置保存前生成单文件、自校验备份；主坏备好时先原子恢复，主备皆坏
  时进入 read-only recovery，严格 `save()` 不写任何恢复证据。
- 新增恢复证据导出和 `REBUILD_DEVICE_SETTINGS` 专用重建：先把主/备原始
  bytes、SHA-256 和诊断隔离为 content-addressed evidence，再从安全默认值
  构造统一端口、0~4 台客户设备及迁移 marker。
- 普通偏好改走 `PERSISTED / MEMORY_ONLY / FAILED` 肯定式持久化边界；恢复
  状态下主题、语言、工程 Tab 和串口意图可继续使用内存值，不能覆盖损坏文件。
- 新增进程级 `RecordingPathRegistry` 与 `RecordingReservation`。路径按真实父目录、
  NFC 及 macOS/Windows case-fold 归一化，使用 `O_CREAT|O_EXCL` 创建并把 fd
  一次性交给 `DataRecorder`；已有文件和活动等价路径不再通过 `wb` 覆盖。
- 配置/恢复专项当前 `31 passed`；SettingsDialog 合并专项 `41 passed`；录制路径、
  Recorder 与回放相关专项 `24 passed`。完整目录与跨工作区回归待主窗口集成后统一执行。

## 2026-09-01：共享 Runtime、客户目录与固定页面

- 从试产单 socket 事实中抽出进程级 `UdpEndpointBroker`；Customer 和 Production
  通过同一个 Broker 取得精确 endpoint/CIDR admission claim，完整 OS `sendto()`
  成功后才产生唯一 sent event。
- `EndpointSessionDirectory` 成为唯一 endpoint→Runtime/Core owner；Runtime 分离
  Configuration、Transport、Observer、Subscription、Handshake lease，并统一
  PresenceEpoch、IdentityAuthorizationScope、CustomerAttachmentScope 和 mutation CAS。
- Production Fleet 改为通用 Runtime 上的 production facet。Customer-active admission
  消费同一解码流，dormant/unknown candidate 保留有界 receiver；ESA01 production
  不支持结论不再污染 Customer Runtime。
- 新增 Settings-backed `CustomerDeviceDirectory`：稳定保存 0~4 个 endpoint 和 active
  selection；Add/Edit/Delete、重复项、容量、原子持久化回滚、detached/busy 前置条件均
  由目录处理。仅配置不启动 socket，新设备不自动连接。
- 左侧设备列表显示 endpoint、当前在线身份、DISCONNECTED/WAITING/ONLINE/
  RECONNECTING、录制、事务、身份冲突和恢复事实。Overview/RF/Maintenance 使用
  endpoint 固定 lazy bundle；选择设备只切 QStack，不 rebind Core/controller。
- 工程工作区新增显式“工程串口/共享客户 UDP”模式。共享模式的 Live/Device/Tracking
  跟随 active customer attachment 并复用同一 bundle；串口使用独立 Core，不进入 IPv4
  Directory。Playback/Log 保持离线单实例。
- MainWindow 双 loopback 集成验证已覆盖：一个 socket、两个 Runtime/Core、来源分流各
  解析一次、200 次固定页面热切换不增加 generation、工程共享模式跟随选择、有界退出。
  当前专项 `2 passed`；最终性能数值在稳定全量后记录。

## 2026-09-01：安全对抗复核进行中

- 复核确认 Settings 运行期进入 read-only/device-blocked 后也必须动态阻断 Runtime
  mutation 和 Production start；Main recovery policy 与 Production start gate 已补齐。
- 工程串口曾因沿用 IPv4 Directory 产生无效 `serial:*:0` endpoint；根因修复为 unmanaged
  Live 独立 Core，只有 customer binding 使用共享 UDP Directory。连接/录制/生命周期
  相邻 `22 passed`。
- 客户 full-capture recorder 的 gate 原先在 profile ACK 后过早释放；正在把 recorder
  ownership 收敛到 Runtime，从协商前持续到 writer 收尾和 restore 证据，确保同 endpoint
  Production freeze 无法旁路。
- 恢复台账增加独立 anchor、revision 仲裁、command fingerprint 和 trusted retirement
  coordinator 合同；Runtime 的未知终态 durable 写入链路、强风险 ledger rebuild 与恢复 UI
  仍在合并验证，未标记完成。
- 第一轮全量为 `1284 passed, 5 failed`：4 项为旧空目录/事务旁路/typed sender 测试假设，
  已逐项更新并专项通过；剩余翻译 catalog 待全部 UI 文案稳定后统一 update/check。

## 2026-09-01：共享所有权与恢复链路收口

- Customer、Engineering shared UDP 与 Production 已统一使用一个进程级
  `UdpEndpointBroker`、一个 `EndpointSessionDirectory` 和每 endpoint 唯一 Runtime/Core。
  Production 的 CIDR 准入、slot、配方和批次语义仍留在 production facet；Customer 没有
  新建第二套 Hub、receiver 或 endpoint session map。
- 客户 full-capture 从 profile 协商前到 writer 收尾及 restore 确认持续持有同一个 Runtime
  recorder lease + mutation gate；同 endpoint 的 Production freeze 无法在 ACK 后旁路。
  工程 shared recorder 使用独立 `engineering_current_stream` 证据语义，可与 batch freeze
  并存，但仍计入 detach/retirement/close 的活动资源。
- `DataRecorder.stop()` 改为可重入有界 finalize。首次 join 超时时保留 thread、reservation、
  recorder pointer 和 Runtime lease，后续 stop 可继续收尾；不会因第一次超时永久失去释放入口。
- Bundle → CustomerWorkspace → MainWindow 关闭结果逐层返回。durable recovery 写失败或 worker
  未停止时保留 binding/runtime/directory owner 并拒绝退出，不再先拆页面和 Directory 后忽略失败。
- `SessionRecoveryLedger` 已接入 Runtime 的未知终态写入，并以独立 anchor、主备 revision、
  tombstone hash、sent fingerprint 和新遥测 cursor 形成持久证据。Settings Recovery 提供两条
  独立强风险入口：endpoint-only 物理退役，以及先导出三份损坏证据再输入
  `REBUILD_RECOVERY_LEDGER` 的账本重建。重建后按稳定 UID/SN 逐设备 clearance，不直接放行。
- Settings、Customer 列表、Runtime mutation 和 Production start gate 都动态消费同一恢复事实；
  Edit/Delete/改址/断开/重启不能清 tombstone 或绕过 global recovery。

验证快照：

- Runtime、Production、真实 controller、Live/Bundle 与 Main 目标组：`231 passed`。
- Settings recovery、ledger、Runtime 与 Main 组合：`55 passed`。
- 客户目录、固定页面、双 endpoint 实际 UDP 分流与切换：`56 passed`；冷启动调度改为等待
  明确两路收帧条件，不再依赖固定 150 ms 机器时序。
- i18n：`1037 finished / 0 unfinished`，placeholder 一致，`update_translations.py check` PASS，
  `test_i18n.py` 为 `16 passed`。
- 第一轮最终全量：`1315 passed, 1 failed`；唯一失败是新增测试硬编码英文子串，而运行 locale
  已正确切换为中文。测试已改为断言语义状态，专项复跑 PASS；完整干净复跑待长时 host soak
  工具稳定后执行。
- AFD01C `192.168.1.13` + ESA01 `192.168.1.12` 真机、macOS 原生包和 Windows DPI/打包
  仍分别为 `BLOCKED`，host 结果不替代设备接受、遥测应用或物理 RF 证据。

## 2026-09-01：最终安全复核与 Host 验收完成

- 客户设备目录最终支持 0~4 个显式 IPv4 UDP endpoint；左侧列表切换只改变 active
  endpoint 和固定 lazy 页面，不重连、不增加 Core generation，也不停止未选中设备的
  接收、订阅、录制或已开始事务。
- Customer、Engineering shared UDP 与 Production 复用同一个进程级 Broker、Directory 和
  endpoint Runtime/Core。Production 自己的运行事实已与共享 socket 线程状态分离；Customer
  先在线后打开 Production 仍会建立 Production CIDR claim/discovery/demand。
- Production stop 后不再消费仍由 Customer exact claim 接收的数据报，避免旧 Production
  session 对共享 Core 二次 `feed_bytes()`。批次 gate/attachment 释放失败时保留 owner、gateway
  与 Hub 供精确重试；关闭预检在任何不可逆 teardown 前 fail closed。
- Product、Parameter、Mount、OTA、Debug、Tracking 与客户 capture-profile 的已发送 mutation
  均按匹配响应/新鲜遥测/terminal evidence 收口；终态未知写入持久 RecoveryLedger。普通
  Customer/Production capability 不能发送 recovery，Settings 只能使用一次性、精确指纹绑定的
  `SET_TX_ENABLE(false)` 恢复能力；其证据只证明 MCU TX gate 关闭，不证明物理 RF 输出。
- 首轮 30 分钟 host soak 的数据面全部通过，但脚本在 restore 前同步离线校验 158400 条 SDB
  记录，阻塞 Qt 事件超过 presence timeout，导致 1 个 gate 未释放，结果按 `FAIL` 保留。根因
  修复为 writer 停止后先完成 capture-profile restore/owner 释放，再离线校验 SDB；新增执行顺序
  回归，未放宽 Runtime fail-closed 合同。
- 修复后正式独立 host soak 为 `PASS_30_MIN_HOST_SOAK`：4 个 loopback endpoint 各 20 Hz，
  `1800.001 s`；1 个 socket、4 个 Runtime/Core 全程稳定；telemetry sent/parsed 均为 `144003`，
  raw routed/recorder written 均为 `151203`，SDB 总记录 `158403`；跨路由、重复解析、recorder
  drop 均为 0。主线程 marker `17999` 个，p95 `0.923833 ms`、max `4.9105 ms`；4 个 SDB v3
  均完整，attachment、Directory、Broker、模拟器和 latency thread 全部干净释放。报告：
  `/private/var/folders/y6/f1d17ljn46jgn8kjhlfkv2xc0000gn/T/tmp.f8o7HuYlXu/m25_multi_device_soak_20260901T113900480556Z.json`。
- 最终自动化：`PYTHONPATH=. pytest satellite_debug_tool/tests -q` 为 `1345 passed in 53.85s`；
  翻译目录 `1067/1067` 且 check PASS；`python3 -m compileall -q satellite_debug_tool` 与
  `git diff --check` PASS。
- Host A~H 完成。真实 AFD01C `192.168.1.13:4004` + ESA01 `192.168.1.12:4004` 台架、设备
  接受/遥测应用/物理 RF，以及 macOS 原生包和 Windows DPI/打包仍为 `BLOCKED`。
