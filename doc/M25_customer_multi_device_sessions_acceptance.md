# M25 客户多设备共享会话验收标准

## 2026-09-01 执行状态

| 范围 | 状态 | 当前证据 |
|------|------|----------|
| A~F 功能/安全 | PASS（host） | 共享 Broker/Directory/Runtime、客户目录、固定页面、受限恢复链与控制/录制隔离均有自动化覆盖；最终全量 `1345 passed` |
| G 生命周期/性能 | PASS（host） | 200 次热切换 PASS；四 endpoint × 20 Hz、四 recorder、1800.001 s：144003 条遥测、151203 条 raw、零串路/重复/drop，p95 `0.924 ms`，干净退出 |
| H 自动化/文档 | PASS（host） | 全量 `1345 passed`；i18n `1067/1067`、check PASS；compileall 与 `git diff --check` PASS；架构和中英文客户指南已同步 |
| I 双真机台架 | BLOCKED | 当前未接入 AFD01C `192.168.1.13` 与 ESA01 `192.168.1.12`，不具备设备接受/遥测应用/物理 RF 证据 |
| J 发布平台 | BLOCKED | 尚未执行 macOS 原生包与 Windows DPI/打包冒烟 |

下面的复选项保留为逐条审计清单；表中 PASS 仅表示当前 host 证据覆盖的边界，不替代 I/J。

> 日期：2026-09-01
>
> 状态：A~H Host 软件验收完成；I 双真机与 J 发布平台保持 BLOCKED
>
> 计划：`doc/M25_customer_multi_device_sessions_plan.md`

## A. 共用架构与唯一事实源

- [x] 客户与试产共同使用同一个进程级 `UdpEndpointBroker`；主程序内不存在 customer-only 第二 socket 或第二 endpoint ingress。
- [x] `EndpointSessionDirectory` 演进取代 `SessionRegistry`，是唯一 endpoint→Runtime/Core 映射；不存在并行 Registry、Runtime map 或页面私有 Core。
- [x] 同一 endpoint 的客户、工程和试产消费者取得同一个 `EndpointSessionRuntime` 和 `DeviceSessionCore`。
- [x] 每个原始数据报只由该 endpoint 的 `FrameReceiverV2` 解析一次；Production facet 不再重复 `feed_bytes()`。
- [x] `ConfigurationLease`、`TransportDemand`、`FacetObserver`、`SubscriptionDemand`、`HandshakeDemand` 生命周期分别测试；配置或页面存在不启动 socket，最后一个 TransportDemand 释放才结束 transport，所有 lease 释放才销毁 Runtime/Core。
- [x] 第一个 TransportDemand 只建立一次 transport epoch；同一 epoch 增减客户/试产 owner 不调用 `begin_connection()`、不增加 generation、不清空 receiver/Store/request ID。
- [x] Production 先使 Runtime ONLINE、Customer 后连接时立即复用已有 Runtime/Core/Store；Customer 断开后客户行显示 DISCONNECTED，而 Production transport/订阅/批次继续；最后一个 transport owner 释放才停止。
- [x] 共享 presence 与客户 attached/detached 意图分别归 Runtime 和客户目录；未连接的客户条目不会因 Production 在线而显示 ONLINE。
- [x] Runtime 维护单调 `PresenceEpoch`：WAITING/STALE 后第一条有效记录开启新 epoch；attach/Production admission 初始只取得 identity/Handshake/Subscription/read-only 受限 capability，旧 Store/ProfileCache、上一 epoch 身份或仅凭 endpoint 均不能授权普通 mutation/freeze。
- [x] 只有当前 `PresenceEpoch` 新收到的稳定 UID/SN 满足产品策略，且该 epoch 已出现的 Debug/Product 身份来源全部一致，才原子建立 `IdentityAuthorizationScope` 并提升对应 owner 的普通 mutation/freeze 权限；scope 冻结 endpoint、Core generation、PresenceEpoch、身份事实和来源 cursors。
- [x] 每次客户 attach/detach 产生新的 `CustomerAttachmentScope` epoch；客户操作同时校验它与 Core generation，Production 继续持有 transport 时重新 attach 也不能复用旧 attachment 授权。
- [x] Customer detach 在 Production 保持 ONLINE 时，仅当当前 IdentityAuthorizationScope 仍有效或本 PresenceEpoch 已重新确认原身份，才用旧 capability/gate owner 完成协议级 terminal 收口；证据确认后才释放 gate、增加 attachment epoch、撤销 capability并清 pending/artifact/草稿，Production generation/Store 不变。
- [x] 客户/共享工程 recorder 活动时 detach 被拒绝并要求先收尾；身份 scope 有效时 OTA/Tracking/capture 等分别发送 OTA_ABORT/stop/restore 并等待匹配证据，scope 已撤销且原身份未复核时 terminal 保持 0 datagram，不能被设备切换或后台事件隐式中止。
- [x] M25R 已取代早期永久恢复模型：终止明确完成或达到有界超时后释放当前 endpoint 的本地 owner；未确认结果不伪报成功，也不形成跨重启或跨设备阻断。
- [x] 一个 endpoint 只有一个连接代际、一个请求 ID 空间和一个 Product Service 订阅状态机。
- [x] `acquire_subscription`/`update_subscription`/`release_subscription` 只改变同一状态机的最大目标速率；generation 恢复时只有一个 keepalive/订阅流。
- [x] 订阅覆盖 0→1 demand、最大 rate 升/降、旧 pending ACK 被新 request 取代、1→0 停止且不伪造 unsubscribe、暂时无 transport pending 和新 epoch 恢复；rate 只接受 1..20 Hz。
- [x] `acquire_handshake`/`release_handshake` 能在既有 transport 上启停 Handshake；Production-first、Customer-later 不需要重建连接。
- [x] HandshakeDemand 在无 transport 时 pending、每个新 epoch 只启动一个状态机、最后 demand 释放时停止；额外 owner 不重启 Handshake。
- [x] 客户精确 endpoint claim 与生产 CIDR+port claim 共存；未被任何活动 claim 接纳的数据报被丢弃并计数。
- [x] Broker datagram 携带 matched-claim/facet 上下文；仅持 ConfigurationLease 的 dormant Runtime、Customer 已 attached 的 active Runtime、完全未知 endpoint 三种 Production 准入路径均按计划路由。
- [x] dormant configured Runtime 的 Production 候选在接纳前不污染 Core，接纳后复用同一 Runtime 建首个 epoch；Customer-active Runtime 的 Production admission 观察同一解码流且不增加 generation；未知 endpoint 接纳后才创建 Runtime。
- [x] endpoint candidate receiver 跨 datagram 保持，接纳/拒绝/10 s 超时即释放；已有 64 个时拒绝第 65 个且不驱逐旧候选，单 source 超过 64 KiB 只清该 source 并冷却 10 s；超限计数，接纳不重喂 raw bytes。
- [x] Customer-active Runtime 的 `ProductionAdmissionLease` 只含临时 observer/subscription demand；接纳时提升，拒绝/10 s 超时/Production stop/epoch 结束时全部逆序释放，订阅 rate/keepalive 回到剩余 owner，且不残留 slot/pending policy。
- [x] `send_to()` 在 socket lock 内同步执行 OS `sendto()`，与 close 并发安全；成功只表示完整 UDP 数据报已写出，不表示设备接受、遥测应用或物理 RF。
- [x] 页面/controller 只持 owner-scoped `EndpointSendCapability`，不接触裸 Core/Broker sender；每次发送校验 owner 或派生 capability 的父 demand/claim、endpoint、Core generation、适用的 customer attachment epoch 和 operation class；普通 mutation/freeze/terminal 额外校验 PresenceEpoch 与 IdentityAuthorizationScope/原身份 scope，共享 Engineering 不增加 TransportDemand。
- [x] Production 保持 ONLINE 而 Customer detached 时，客户与共享工程 RF/OTA/参数/Debug/read 请求均产生 0 个 datagram；重新 attach 只发受限 capability，当前 PresenceEpoch 的新鲜身份完成授权后才恢复普通 mutation，且 Core generation 不变。
- [x] 成功 OS 写出只产生一次含 endpoint/host time/generation/owner/operation class/frame 的 `DatagramSentEvent`；失败不产生 sent evidence，客户/试产 recorder 不重复记录。
- [x] claim/lease 获取按计划顺序提交，任一步失败逆序回滚；Customer connect/detach、Production start/accept/stop 及 exact+CIDR 重叠引用计数均有故障注入测试。
- [x] Directory/Runtime/Core/Store/request ID/lease/gate 只在 Qt owner 线程更新；Broker 接收线程不持 socket lock emit，queued datagram 不可变；send-vs-close、recv-vs-close 和 shutdown lock-order 测试通过。
- [x] shared presence 只由完整、CRC 正确且领域解码成功的注册类型记录刷新；3.0 s 单调时钟边界准确驱动 WAITING/ONLINE/RECONNECTING，错误帧不续期。进入 stale、身份来源变化/冲突或必需来源失效时立即撤销 IdentityAuthorizationScope，即使 Core generation 不变也不能继续普通 mutation/freeze。
- [x] Production Fleet 的槽位、CIDR、UID/SN/MAC、批次录制和生产策略仍由生产 facet 拥有，没有 customer mode 条件污染。
- [x] 现有试产多设备行为和结果语义回归通过。

## B. 客户设备目录与配置

- [x] 整个原始 `settings.json` 在默认合并/迁移前必须是 JSON object；文件不存在是新安装，语法/顶层类型损坏不能静默落到 defaults 或写 marker。
- [x] settings 保存维护 checksummed `.bak`；主坏备好时原子恢复并记录诊断。主备皆坏时保留原文，Customer UDP/Broker/Production fail closed，非设备 UI/串口可用内存默认但不能覆盖原文件。
- [x] 双损坏触发 `read_only_recovery`；主题/语言、普通 preference、关闭窗口和应用退出路径只更新内存，`save()` 类型化失败且主/备文件逐字节不变，只有 `REBUILD_DEVICE_SETTINGS` 可退出该模式并写文件。
- [x] 双损坏时 Settings 可导出原文/hash/错误；重新输入 unified port/devices 并输入 `REBUILD_DEVICE_SETTINGS` 后才原子构造 current keys/markers 且无旧 UDP keys。malformed JSON 测试证明不会变成空 devices/45678 后自动启动。
- [x] `customer.devices` 可保存 0~4 个规范化 IPv4 UDP endpoint 并保持稳定顺序，`customer.active_endpoint` 只保存当前选择；attached/online/busy 不持久化，0 台时显示空状态且 Playback 仍可用。
- [x] 新增 endpoint 只创建配置并选择条目，不自动连接；重复 endpoint 不创建第二条目，而是选择已有条目并给出明确提示。
- [x] 仅配置未 attached 的设备显示 DISCONNECTED；只有明确 connect 后才进入 WAITING，不能因 Production presence 或设备未上电描述误判连接意图。
- [x] 第五台设备被明确拒绝，已有四台连接和状态不受影响。
- [x] 原始 `customer.devices` 只接受 0~4 个唯一有效 `{IPv4 unicast, port 1..65535}`；active 只接受 null/缺失或列表成员，失配确定性重置 null 并提示。
- [x] customer-device 迁移矩阵覆盖：无新旧→空列表；marker absent + 有效新列表（含空）→新列表优先；完整有效旧 IP+port→一条 active；旧 key 部分缺失/无效→Customer fail closed；marker+新列表缺失/损坏→不回退；marker+旧 key 残留→原子清理。
- [x] 成功 customer-device 迁移的一次原子 replace 同时写 devices/active/marker并删除两个旧 remote key；失败保持原文件字节，Settings 修正入口可在空列表/Customer blocked 时使用，重复启动幂等。
- [x] `device_udp.local_port` 是迁移后的唯一权威本地端口，`config_migrations.device_udp_port_v1` 使重复启动幂等；迁移在默认值合并前检查原始 JSON。
- [x] 端口迁移矩阵覆盖：无配置→45678、新 key 优先、双历史默认→45678、仅一个旧值、两个相同值、一个历史默认+一个自定义→自定义、两个不同非默认值、显式无效值、marker+缺失/无效新 key、marker+旧 key 残留和保存失败恢复。
- [x] 两个不同非默认旧端口或显式无效值时 Broker fail closed，设置页提供选择/修正入口；确认并原子保存成功前不写 marker、不删除旧 key。
- [x] 成功迁移只执行一次原子文件 replace；最终原始 JSON 同时包含有效 `device_udp.local_port` 和 marker，且不含两个旧 local-port key。写入失败保留原文件字节不变。
- [x] 0 台设备或 Broker 因迁移冲突未启动时仍可进入 `SettingsDialog` 修正端口；存在任何 transport/discovery demand 时端口只读，全部 demand 释放后才允许修改且下次 bind 生效。
- [x] 应用启动不把上次在线状态当成当前在线；用户重新发出连接意图后才进入 WAITING。
- [x] endpoint 只有在客户 detached、录制停止且无 OTA/设备事务时才可编辑或删除；违规操作给出明确归属提示。
- [x] 编辑先验证后原子替换并创建新 session scope；已有四台时可直接替换当前项，不需要第五个临时槽位；旧 Store、事务、录制状态和 OTA artifact 不迁移。
- [x] 编辑目标与已有 endpoint 重复时选择已有项，源项保持不变；删除最后一项后所有设备页面/归属浮窗释放且空状态稳定。
- [x] 删除当前项后确定性选择原索引的下一项，否则选前一项，否则 active endpoint 为空；全局 Playback 内容保持不变。

## C. 双设备并发与隔离

- [x] 一个本地 socket 可同时收取两个 loopback endpoint 的交错数据报，并严格按来源路由。
- [x] 自动化使用 loopback/注入 endpoint 验证 AFD01C protocol 8 与 ESA01 protocol 6 同时建立独立客户 Runtime；真实 `192.168.1.13/.12` 仅用于 I 节台架验收。
- [x] 两台设备的 identity、Profile、Product、Telemetry、State、Event、GNSS、Orbit Store 和统计互不混合。
- [x] 一台设备从 ONLINE 变为 RECONNECTING、重启或断开时，另一台保持 ONLINE，Store 和业务状态不清空。
- [x] 未被任一活动 admission claim 接纳的 endpoint、错误源端口和跨 endpoint 分片被丢弃并计数，不污染任何 receiver。
- [x] Directory 对已验证 UID/SN 建立跨 endpoint 索引；同一身份出现在两个 endpoint 时 Customer 明确告警并 fail closed outbound wire mutation，允许被动查看及按标准前置条件 detach/Edit/Delete 解冲，且不自动迁移控制目标。
- [x] 共享 UDP 的 Customer/Engineering/Production 普通 wire mutation/freeze 在当前 PresenceEpoch 至少一个稳定 UID/SN 新鲜验证、且本 epoch 已出现来源一致前均为 0 datagram/0 次成功；identity pending/conflict/旧缓存只允许身份/read-only 探测和被动查看，串口工程边界不受此客户合同冒充覆盖。
- [x] 回归覆盖：旧 ProfileStore/Product Store 身份不能提升权限；WAITING/ONLINE 但本 epoch 身份未到时三类 facet 均不能 mutation/freeze；同一 endpoint stale 后换 UID/SN 时旧授权立即失效，旧 Store/cache 不得成为新设备控制目标。
- [x] stale 后同一 UID/SN 用本 PresenceEpoch 新记录重新确认且来源一致时，可在不增加 Core generation 的前提下重新授权；任一来源随后变化、冲突或失效时立即撤权。活动 mutation 先进入 TERMINATING 且 0 terminal datagram，只有新 epoch 重新确认原身份后才开放缩限 terminal；确认新身份/冲突/超时直接把旧身份和操作写入 RecoveryLedger，不向新设备发送。
- [x] Production 对 ESA01 的 UNSUPPORTED 只影响 production facet：不占 slot/participant，不修改共享 Runtime/Core presence，不停止 Customer 订阅，不禁用客户 RF。

## D. 左侧设备列表和页面切换

- [x] 左侧显示设备区、添加/编辑/删除入口、最多四个两行设备项、空状态和现有页面导航。
- [x] 0 台或无 active endpoint 时 Customer session-bound 页面和工程共享 UDP 显示统一空状态且不构造伪 Core；Playback/Log 仍可用。
- [x] 当前 PresenceEpoch 身份有效时显示当前型号/SN；身份尚未在本 epoch 确认时以 endpoint 为主，旧身份只可明确标为历史信息；控制区标记“等待本次在线身份确认”并保持禁用。短状态文字准确区分 DISCONNECTED、WAITING、ONLINE、RECONNECTING，颜色不作为唯一信息。
- [x] capture-profile 恢复未确认时仅在对应设备行显示“采集模式待重新同步”，不覆盖连接阶段。
- [x] identity pending/conflict/旧缓存未复核时 Customer/共享 Engineering/Production 控制项显示“等待本次在线身份确认”或“设备身份冲突”禁用原因；不能只显示 ONLINE 或无解释 disabled。
- [x] 设备项 `accessibleName`/tooltip 包含完整 endpoint、状态、录制和事务事实；三档主题、运行时中英文切换及延迟创建项均正确更新。
- [x] 运行时主题/语言切换同步更新已存在与延迟创建的 session-bound 页面、0 台空状态、Add/Edit/端口冲突对话框及设备归属浮窗标题，不残留旧语言或旧语义色。
- [x] 设备项显示录制和设备事务 busy 状态；不可用状态不伪装为在线或已应用。
- [x] 当前页为 Overview/RF/Maintenance 或工程 Live/Device/Tracking 时，选择设备保持子页面并切到该 endpoint 的 lazy 实例；当前页为 Playback/Log 时只更新 active endpoint，不切换离线页面。
- [x] 连续切换不会调用 `begin_connection()`、`end_connection()`、rebind endpoint、增加 generation 或清空 Store。
- [x] 未选中页面的曲线、3D 和表格呈现停止；其连接、订阅、录制、OTA 和设备事务继续。
- [x] Customer Playback、Engineering Playback 和 Log 保持全局单实例；Playback 可见时设备选择只更新 active endpoint，不替换或清空离线内容。
- [x] 工程页有“共享客户 UDP”与“工程串口”明确模式；共享 UDP 只观察/控制 Customer attachment，不独立持有 TransportDemand 或 UDP Connect/Disconnect，0 台/未选/detached 时不自动连接、不 fallback、不借 Production sender。
- [x] 共享 UDP 的 Live、Device、Tracking Simulator 和 Settings ProfileStore 跟随客户 active endpoint；已开始的旧 endpoint 工程事务在其 Customer attachment 与 IdentityAuthorizationScope 有效时继续。Customer detach 先进入 TERMINATING；身份 scope 有效或重新复核为原身份后，旧工程 capability 才允许原 controller terminal 操作，否则保持 0 terminal datagram，取得终止证据或 recovery tombstone 持久化后才撤销。
- [x] 串口模式使用独立单串口会话。任一工程 recorder/OTA/事务活动时模式切换被拒绝；进入串口前释放共享 observer/capability，进入共享 UDP 前串口已明确断开且无事务/录制，不动态 rebind 或隐式断开 Customer。
- [x] 共享 UDP 工程 recorder 只记录 endpoint 当前 raw stream，不发送 capture-profile，SDB metadata 明确为 `engineering_current_stream`，不冒充客户 full-support；它可在 Production batch 中使用独立路径，并受全局 path lease、每设备 busy 及 detach/mode-switch 收尾约束。串口 recorder 只属于串口 session。
- [x] 共享 UDP 模式的 Type/IP/remote port/local port 为只读；所有 UDP endpoint 新增/修改只能通过客户目录，不存在绕过 Directory 的连接入口。
- [x] 所有设备消息信号携带冻结 scope，消费时显示型号/SN + endpoint；Live/Device/Tracking/Settings 和所有控制/确认对话框持续显示当前或冻结目标，切换竞态不能改写消息归属。
- [x] GNSS、Orbit、组件温度浮窗固定归属一个 endpoint、标题可辨认，切换不 rebind，删除设备/退出时关闭。
- [x] 1024×600 中文/英文下设备区和页面导航可操作；158 px 侧栏不产生新的横向溢出，已存在的英文 Overview 基线溢出单独记录而不伪报修复。

## E. 控制、事务与 OTA 安全

- [x] RF、TX、参数、OTA、安装姿态、Debug 和 Tracking Simulator 的 sender 固定到 controller 所属 endpoint。
- [x] 所有共享 UDP controller/确认框同时冻结 `DeviceSessionScope`、客户操作所需的 `CustomerAttachmentScope` 与 `IdentityAuthorizationScope`；endpoint、Core generation、attachment epoch、PresenceEpoch、身份事实或来源 cursor 任一失配都不能发送或推进事务。
- [x] 两台设备使用相同 request ID 时，A 的响应不能推进 B 的 pending 事务，反之亦然。
- [x] A 的 RF/TX 请求在切到 B 后仍只等待 A 的响应和 A 的更新遥测。
- [x] TX 确认对话框冻结 A 的 `DeviceSessionScope`、`CustomerAttachmentScope` 与 `IdentityAuthorizationScope`；切换设备、presence stale 或身份变化后旧确认失效，绝不向 A 的新设备或 B 发送。
- [x] 设备切换不取消已经开始的 OTA；切回后恢复同一个 controller 状态和进度。
- [x] OTA artifact 继续绑定 endpoint、Core generation、customer attachment epoch、PresenceEpoch、IdentityAuthorizationScope、Debug/Product 身份和固件事实；切换、detach、presence stale、身份变化、删除或 endpoint 编辑不能复用旧 token。
- [x] ESA01 与未知产品保持客户 OTA 不可用；AFD01C 只接受精确匹配的签名包。
- [x] detach A 按 terminal/recovery 合同收口 A 的客户事务；B 的事务/链路及 A 的 Production transport/观察继续，A 处于 recovery 时不能进入批次。
- [x] `RuntimeOperationGate` 演进取代 Core device-transaction lock；不存在第二把 controller/Core 锁，现有 transaction API 只委托同一个原子 CAS owner。
- [x] Customer/Engineering mutation 与 Production freeze 在同一个 CAS 上互斥；明确完成/终止与 batch abort/finalize 幂等释放，detach/transport/generation/销毁遇到未确认设备状态时保留 recovery fact，不能盲目变 IDLE。
- [x] OTA、Tracking、等待 RF/参数 applied 三类 detach 专项测试证明：明确终止才释放 gate；无终止证据进入 recovery，Production freeze 为 0 次成功。
- [x] OTA/Tracking/RF/参数/客户录制在 presence stale 或身份 scope 被撤销时停止普通发送；原 controller 进入 TERMINATING 但在原身份未由当前 PresenceEpoch 重新确认前保持 0 terminal datagram，确认新身份/冲突/超时则写入冻结旧身份的 RecoveryLedger，绝不以 Core generation 未变化为由继续或向新设备 abort/restore。
- [x] outbound operation 默认 MUTATING；Handshake、Product Subscription、read-only query 仍校验 facet/capability 与批次阶段，不能仅凭 operation class 绕过 gate。
- [x] 冻结/运行批次中只允许 batch owner 的 Production/system capability 按 allowlist 发送；Customer/Engineering 的 mutation、read query、Handshake/Subscription 变更全部产生 0 datagram，相关 demand pending 到批次释放后 reconcile。“被动查看”仅消费已有 Store/raw stream。
- [x] 已有客户变更事务时，Production 明确把该设备标记为“操作中/未就绪”并拒绝冻结；不能先接纳后静默记为 INCOMPLETE。
- [x] operation gate 只影响同一 Runtime，其他设备和离线 Playback 不受影响。

## F. 录制与证据

- [x] 每台设备拥有独立客户录制状态、路径、capture-profile 协商和 SDB writer。
- [x] A 录制期间切到 B 不停止 A；A 的 raw frame 不写入 B 文件，B 的 frame 不写入 A 文件。
- [x] 两台同时录制时分别保存正确 endpoint、身份、host timestamp、gap 和 summary。
- [x] 一台掉线只在自己的 SDB 写入 outage/gap；另一台记录连续。
- [x] 客户与试产 facet 同时观察同一 Runtime 时协议只解析一次，获准启动的 recorder 各自订阅同一 raw/sent event，不覆盖对方证据。
- [x] 客户录制从 full capture-profile 协商前到 writer 收尾/restore 确认全程持有同一个可重入 mutation gate owner；活动录制阻止同 Runtime 的 Production freeze，已冻结/运行批次禁止启动客户录制，不存在 passive customer recorder。
- [x] 客户默认 SDB 文件名包含时间与规范化 endpoint；同秒启动不同设备不会解析到同一路径，已有文件使用确定性序号且不覆盖。
- [x] recording path key 归一化真实父目录、`..`/symlink、NFC 文件名及 Windows/macOS 大小写别名；等价路径只能取得一个进程级 lease。
- [x] path registry 用 `O_CREAT|O_EXCL` 返回拥有 fd/path lease/file identity 的 `RecordingReservation`；默认冲突使用确定性序号，用户已有/活动路径拒绝，任何路径不以 `wb` 覆盖。
- [x] `DataRecorder.start(reservation)` 一次性接管所有权：成功后 recorder 在 stop 关闭/释放；header/schema/首写失败也关闭/释放，并只删除自己创建的 inode，删除失败改名 `.failed`。未交付 reservation 由 context manager 收尾。
- [x] 客户、共享工程、串口工程和试产 recorder 覆盖 reservation 未交付、start 成功、header/首写失败、正常 stop、异常 close、重复/别名路径及清理失败回归；不会留下空/半成品正常 `.sdb`。
- [x] 正常 detach、编辑或删除前完成 recorder 收尾和 capture-profile restore；仅切换当前设备不停止 recorder。restore 无法确认时 writer 仍有界关闭，把 UNKNOWN 写入 RecoveryLedger；只有用户明确确认才可 detach，Production freeze/客户新 mutation 持续 fail closed，直至新 PresenceEpoch 显式恢复/验证。

## G. 生命周期、性能和资源

- [x] 默认启动仍只构建必要壳；有设备时只构建首个可见客户页面，空列表不创建伪设备页面，其他设备页面首次选择时创建并复用。
- [x] 单个设备页面 bundle 首次延迟创建不超过 500 ms；主题/语言为创建时当前值。
- [x] 至少两台 attached、页面全部预热后，以 active-selection signal 到目标 `activate_view()` 返回的 `QElapsedTimer` 时长测 200 次热切换：p95 不超过 10 ms、最大不超过 50 ms；测试期间 Broker socket 恰为 1、每 endpoint 一个 Runtime/Core，无重复 signal 或 controller 增长。无 transport/discovery demand 时 socket 应为 0。
- [x] 独立 30 分钟 host soak 使用四个 loopback endpoint 各 20 Hz、四个客户 recorder 写独立临时目录且不启动 Production batch；每 100 ms 投递主线程 latency marker，p95 不超过 100 ms。跨路由/重复解析错误、非故障注入 queue/recorder drop 均为 0，socket 始终为 1，Runtime/Core 数量无增长。
- [x] 性能记录包含机器型号、CPU、OS、Python/PySide6 版本及是否 offscreen/打包；首次创建、热切换和 30 分钟 soak 分开报告，长 soak 不用缩短单测替代。
- [x] 退出应用可有界停止 Broker、完成或明确终止 recorder，并释放全部 Runtime owner。

## H. 自动化与文档验证

- [x] 通用 Broker、SessionDirectory、客户目录、Production facet 和跨设备安全均有回归测试。
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q` 全量通过，无新增 warning；计划前已存在失败必须先单独收口，不能登记为允许失败 baseline。
- [x] `python3 scripts/update_translations.py update` 与 `check` 通过，TS/QM 同步提交。
- [x] `python3 -m compileall -q satellite_debug_tool` 与 `git diff --check` 通过。
- [x] `AGENTS.md`、vNext 功能定义、配置说明及新建的客户工作区中英文指南同步为多设备事实，并删除“当前不做多设备同时连接”的旧边界。
- [x] M25R 已同步移除旧恢复持久化路径、Settings 恢复入口和对应客户指南说明。
- [x] 历史 M16 `doc/user_manual.md` / `doc/user_manual_en.md` 保持工程工作区历史定位，不被错误标记为当前客户手册。

## I. 真机验收

- [ ] AFD01C `192.168.1.13:4004` 与 ESA01 `192.168.1.12:4004` 通过同一上位机本地端口连续在线不少于 30 分钟。
- [ ] 100 次设备切换无重连、身份串台、遥测跳台或控制目标变化。
- [ ] 单独断电/重启其中一台，另一台连接、遥测和录制连续；恢复设备自动从 RECONNECTING 回到 ONLINE。
- [ ] 分别执行无 TX 风险的客户控制，匹配响应和后续遥测只更新目标设备。
- [ ] 两台独立 SDB 回放能还原各自身份和数据，不存在跨设备记录。
- [ ] AFD01C OTA、TX 和物理 RF 仍按各自专项流程验收；host 测试和 UDP send 不替代设备接受、应用或物理 RF 证据。

## J. 发布平台验收

- [ ] macOS 原生包完成双 endpoint socket、列表切换、录制路径 lease 和关闭清理冒烟。
- [ ] Windows 125%/150% DPI 完成双 endpoint socket、两行设备项、添加/编辑/删除、主题/语言切换和关闭清理冒烟。
- [ ] 缺少对应打包或平台环境时明确记为 `BLOCKED`，不得用 offscreen host 测试替代，也不阻塞“host 软件验证完成”的准确表述。

## 通过规则

Host 软件交付要求 A~H 全部通过。I 为真实混合设备台架验收，J 为独立发布平台验收；I/J 未完成时分别标记 `BLOCKED`，只能表述为“多设备客户软件实现与 host 验证完成”，不能表述为真实 AFD01C+ESA01 联机或双平台发布验收完成。
