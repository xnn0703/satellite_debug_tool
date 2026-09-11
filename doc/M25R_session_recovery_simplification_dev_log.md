# M25R 客户会话恢复机制精简开发记录

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-09-03 |
| 状态 | 功能实现完成；专项验证通过 |
| 计划 | `doc/M25R_session_recovery_simplification_plan.md` |
| 验收 | `doc/M25R_session_recovery_simplification_acceptance.md` |

## 1. 实施前事实

- 工作树包含尚未提交的 M25 多设备实现，本次只在该实现上做 M25R 精简，不回退 Broker、Directory、Runtime、客户设备列表和固定 endpoint 页面 bundle。
- `CaptureProfileController` 在 restore 未确认时调用 `require_recovery()`，保留 recorder 和 operation owner。
- Runtime 将未知 operation 写入 `RecoveryTombstone` 并进入 `RECOVERY_REQUIRED`。
- `MainWindow.closeEvent()` 因设备 `operation_busy` 拒绝退出。
- Settings 唯一受限恢复 wire action 是 `SET_TX_ENABLE(false)`；它不能证明 capture-profile 已恢复。
- recovery 相关实现横跨 session、customer、production、UI、测试、翻译和文档；必须从 Runtime 状态源统一删除，不能只隐藏 Settings 页面。

## 2. 实施记录

### 2026-09-03：计划确认与引用盘点

- 用户确认 `M25R_session_recovery_simplification_plan.md` 和验收标准。
- 已定位 recovery 相关源码和测试引用。
- 已从 `SessionOperationGateway`、`EndpointSessionRuntime`、Directory、controller 和 UI 统一删除旧恢复状态源。

### 2026-09-03：实现完成

- 保留进程级 `UdpEndpointBroker`、`EndpointSessionDirectory`、每 endpoint 唯一 Runtime/Core/Store、固定页面 bundle 和左侧最多四设备切换。
- 删除恢复 ledger/controller、recovery-only scope/capability、Settings 恢复区、Customer/Engineering/Production 全局恢复警示及相关持久化路径。
- capture-profile 恢复未确认时关闭本地 writer，释放 recorder lease 与 operation owner，并在目标设备行显示“采集模式待重新同步”。下一次录制重新执行当前会话的 profile 协商。
- RF、参数、OTA、Tracking 的未知终态由各 controller 有界收口；不再共享永久恢复状态，不影响其他 endpoint。
- 应用退出仍会因 writer/worker 确实无法关闭而拒绝，但不再因设备终态未确认强制进入 Settings。
- 删除两个旧恢复专用测试文件，更新混合测试为 endpoint 本地收口合同；未创建 Git commit。

## 3. 验证记录

- 最终受影响专项（含主窗口关闭）：`86 passed in 4.43s`。
- 全量测试按用户要求中止：中止前 `676 passed in 38.64s`，该结果不登记为全量通过。
- 翻译：`update` 与 `check` 通过，目录共 1020 条，无 unfinished。
- 静态：`python3 -m compileall -q satellite_debug_tool` 通过。
- 差异格式：`git diff --check` 通过。
- 30 分钟 soak：按用户要求未执行。
- 真机双设备与物理 RF：未执行。

## 4. 未完成项

- [x] Runtime/Directory 状态模型收敛。
- [x] Capture profile 待重新同步状态。
- [x] Settings/工作区恢复 UI 删除。
- [x] 旧 recovery 测试和持久化模块删除。
- [x] 文档与翻译更新。
- [x] 专项、翻译、编译与差异检查。
- [ ] 全量回归与 30 分钟 soak（用户明确本次不执行）。
- [ ] 双真机和物理 RF 验收。
