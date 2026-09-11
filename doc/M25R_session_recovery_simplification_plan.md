# M25R 客户会话恢复机制精简计划

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-09-03 |
| 状态 | 待用户确认后实施 |
| 前置里程碑 | M25 客户多设备共享会话 |
| 验收标准 | `doc/M25R_session_recovery_simplification_acceptance.md` |
| 开发记录 | 实施时创建 `doc/M25R_session_recovery_simplification_dev_log.md` |

本计划只纠正 M25 的通用持久恢复模型。与本计划冲突的 M25 RecoveryLedger、recovery-only attach、全局恢复阻断、物理退役和退出阻断条款由本计划取代；M25 的多设备 Broker、Directory、Runtime、固定 endpoint 页面和左侧设备切换合同继续有效。

## 1. 目标

保留客户多设备同时在线和 endpoint 隔离能力，删除普通客户会话不需要的永久恢复中心，使一次采集模式恢复超时或身份授权变化不会把应用锁进设置页，也不会使用无关的 TX 关闭动作清除 `capture-profile` 状态。

目标行为：

- AFD01C `192.168.1.13:4004` 与 ESA01 `192.168.1.12:4004` 可继续同时在线，左侧切换只改变当前呈现和操作目标。
- 每个 endpoint 继续拥有唯一 Runtime/Core/Store、独立页面 bundle、操作 gate 和录制状态。
- 采集模式恢复未确认时，关闭本地 recorder，显示该 endpoint 的“采集模式待重新同步”，允许切换设备、断开和退出应用。
- 下一次有效会话使用当前 epoch 的身份和状态重新建立操作授权；开始新的客户录制前，必须重新协商目标采集模式并收到匹配响应。
- 任何状态提示只描述已确认事实，不把 TX 门控关闭描述成采集模式恢复，也不把 UDP 写入描述成设备已应用。

## 2. 当前根因

当前实现把不同领域事实合并为一个通用永久恢复状态：

1. `CaptureProfileController` 在 restore 发送失败、超时、代际变化或身份授权撤销时调用 `require_recovery()`，并保留 operation/recorder owner。
2. Runtime 把当前 operation 持久化为 `RecoveryTombstone`，gate 进入 `RECOVERY_REQUIRED` 后拒绝正常释放。
3. `MainWindow.closeEvent()` 把该状态视作仍有设备操作，阻止退出。
4. Settings 的受限恢复控制器只发送 `SET_TX_ENABLE(false)`，其证据只能证明 MCU TX gate 已关闭，不能证明 `capture-profile` 已恢复。

因此 `capture-profile` 的未知状态无法由现有恢复动作正确闭环。根因是领域状态建模错误，不是超时或提示文案不足。

## 3. 保留范围

以下 M25 能力保持不变：

- 进程级唯一 `UdpEndpointBroker` 和统一 UDP 本地端口。
- `EndpointSessionDirectory` 的 endpoint 到唯一 Runtime/Core 映射。
- Customer/Engineering/Production 对同一 endpoint 共享 Core 和 Store。
- 客户设备目录、最多 4 台显式设备、左侧设备选择和固定 endpoint 页面 bundle。
- 命令在创建时冻结 endpoint、Core generation、owner 和会话 scope，禁止因 UI 切换改变发送目标。
- 每 endpoint 的 mutation/production freeze 互斥和跨设备隔离。
- PresenceEpoch、当前会话新鲜身份授权和匹配响应/遥测判定。
- OTA、参数、RF、录制各自已有的明确完成、取消和超时状态机。

## 4. 删除范围

删除通用持久恢复子系统及其 UI/生命周期耦合：

- `SessionRecoveryLedger`、`RecoveryTombstone`、全局 recovery epoch、主备/anchor、损坏重建和审计退役流程。
- `SessionRecoveryController` 及 Settings 中“需要会话恢复”“受限 TX 安全恢复”等入口。
- Runtime/Directory 的 recovery-only attachment、recovery capability、recovery scope 和全局恢复阻断。
- Customer、Engineering、Production 中由 recovery ledger 驱动的警示和禁用状态。
- Edit/Delete/断开/重启不能清除恢复项的合同。
- 因 `RECOVERY_REQUIRED` 或遗留 operation owner 阻止应用退出的路径。
- 与上述旧模型绑定的测试、翻译、文档和持久化路径说明。

删除必须完整，不保留兼容开关、空壳类、旁路状态源或旧配置读取 fallback。

## 5. 替代状态模型

### 5.1 Runtime operation gate

每个 endpoint 只保留当前进程内的肯定状态：

- `IDLE`：没有活动 mutation/freeze。
- `MUTATING`：一个明确 owner 正在执行设备写操作。
- `PRODUCTION_FROZEN`：试产批次拥有该 endpoint。
- `TERMINATING`：原 owner 正在完成或取消操作，等待有界结果。

终止超时后，controller 必须结束本地资源并释放 gate，同时把具体领域状态更新为“待重新同步”；不再转入跨重启的通用 recovery 状态。

### 5.2 采集模式和客户录制

- 客户录制开始前，发送 full capture-profile 并等待匹配 response/applied mask；确认后才启动 writer。
- 停止录制时先有界关闭 writer，再发送默认 profile restore。
- restore 确认成功：显示“默认采集模式已恢复”。
- restore 发送失败、超时、断线、代际变化或身份授权撤销：显示“采集模式待重新同步”，释放 recorder 和 operation owner，允许正常退出。
- “待重新同步”只属于该 endpoint 的内存态；重新建立有效会话后，在下一次需要录制时重新协商，不用 TX 控制替代。
- 设备可能仍保持 full profile 是未确认事实，界面不能显示为已恢复。

### 5.3 其他设备写操作

- 每个 controller 继续依据自身协议合同判定完成、拒绝、超时和取消，不再把所有未知结果投影为同一种永久 tombstone。
- 会话失效后立即撤销旧 capability；新会话只有在当前 PresenceEpoch 的身份重新确认后才能获得 mutation capability。
- 下一次操作必须以新会话 Store/响应为依据，不能沿用上一代请求、遥测或“已应用”状态。
- OTA、TX/RF、参数等风险边界仍 fail closed：未确认结果显示“结果未确认”，但应用退出不被用作设备状态证明，也不保留不可清除的本地主锁。

## 6. 退出与设备管理

- 仍在写本地 SDB 时，退出流程先请求有界收尾；writer 无法关闭属于真实本地资源错误，可阻止退出并明确报告文件风险。
- 已发出的设备请求尚在正常等待窗口内，可提示用户等待或终止；明确终止或等待超时后必须释放本地 owner。
- capture-profile、RF、参数或 OTA 的设备终态未确认时，退出提示只说明“设备结果未确认”，不能强制进入 Settings 恢复中心。
- 客户设备 Edit/Delete 先关闭该 endpoint 的窗口和本地 recorder，并撤销其 attachment/capability；不操作其他 endpoint。
- 正常退出依次收尾工程串口、本地 recorders、客户 bundles、Directory 和 Broker，不能因历史设备状态永久拒绝退出。

## 7. 实施步骤

1. 用现有测试稳定复现截图链路：capture-profile pending → identity authorization revoked → operation busy → closeEvent 拒绝退出。
2. 收敛 Runtime operation gate，删除 recovery 状态、持久化 recorder 和 recovery attachment/capability。
3. 修改 CaptureProfileController：未知 restore 进入 endpoint 局部“待重新同步”，有界释放 recorder/gate。
4. 逐个调整 RF、参数、OTA、Tracking 和 Production 的终止路径，确保各 controller 自己表达结果，且 generation/identity 失效后不继续发送。
5. 删除 recovery ledger/controller、Settings 恢复 UI、工作区警示、持久化路径和翻译词条。
6. 调整退出、Edit/Delete、detach 和 shutdown 顺序，确保资源有界释放且不串设备。
7. 更新 M25、AGENTS、客户指南和开发记录，标明 M25R 对旧恢复条款的取代关系。
8. 运行专项测试、全量 pytest、翻译 update/check、compileall 和多设备 soak；对照验收文档 review。

## 8. 非目标与验收边界

- 不改变 DEBUG v2/Product Service 线协议和设备固件。
- 不改变客户最多 4 台设备和左侧列表交互。
- 不改变试产 CIDR 发现、配方、批次报告和夹具业务。
- 不把“允许应用退出”表述为设备状态安全或物理 RF 安全。
- Host 测试只能证明软件隔离、状态机和资源收尾；AFD01C/ESA01 双真机在线、断网恢复和 RF 物理状态仍需独立验收。

