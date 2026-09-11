# M25R 客户会话恢复机制精简验收标准

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-09-03 |
| 状态 | 软件专项验收通过；全量、soak 与真机验收未执行 |
| 实施计划 | `doc/M25R_session_recovery_simplification_plan.md` |

## A. 多设备能力保持

- [x] AFD01C 与 ESA01 两个不同 endpoint 可同时 attached/ONLINE，复用一个 Broker，分别使用唯一 Runtime/Core/Store。
- [x] 左侧切换 200 次不触发重连、generation 变化、Store 清空、controller rebind 或命令改投另一 endpoint。
- [x] 未选中设备继续接收遥测、执行已开始事务和写入自己的 SDB。
- [x] Customer/Engineering/Production 对同 endpoint 仍共享 Runtime；不同 endpoint 的 operation gate、recorder 和错误状态互不影响。

## B. 旧恢复模型完整移除

- [x] 源码不再定义或引用 `SessionRecoveryLedger`、`RecoveryTombstone`、`SessionRecoveryController`、recovery-only attachment/capability/scope 或全局 recovery epoch。
- [x] Runtime operation gate 不再包含 `RECOVERY_REQUIRED`；终止完成或有界超时后可回到 `IDLE`。
- [x] Settings 不再显示会话恢复记录、受限 TX 恢复、ledger 导出/重建或物理退役入口。
- [x] Customer、Engineering、Production 不再显示由 recovery ledger 驱动的全局阻断或恢复警示。
- [x] 配置文档和代码不再声明或创建 `session_recovery.json`、`.bak`、`.anchor`。
- [x] 不保留旧模型兼容开关、空实现、隐藏入口、旧配置 fallback 或失效测试。

## C. 截图问题回归

- [x] 稳定复现旧链路：capture-profile restore pending 时撤销 identity authorization；旧实现产生永久恢复记录并使 `closeEvent` 拒绝退出。
- [x] 新实现对同一链路关闭本地 writer、释放 recorder lease 和 operation owner，并把该 endpoint 标记为“采集模式待重新同步”。
- [x] 上述状态不会生成恢复文件，不会打开 Settings 恢复入口，也不会要求发送 `SET_TX_ENABLE(false)`。
- [x] 用户可正常切换到另一设备、断开故障设备并退出应用。
- [x] 另一 endpoint 的连接、遥测、录制和操作不受影响。

## D. Capture profile 事实语义

- [x] 客户录制只有在 full capture-profile 的匹配响应和 applied mask 确认后才启动 writer。
- [x] restore 确认成功时才显示“默认采集模式已恢复”。
- [x] restore 发送失败、超时、断线、代际变化或身份撤销时显示“采集模式待重新同步”，不显示为已恢复。
- [x] 下一次录制使用新 PresenceEpoch、当前身份和新 request 重新协商 full profile；旧响应和旧 Store 值不能满足确认。
- [x] TX gate 状态不作为 capture-profile 的恢复证据。

## E. 操作与安全边界

- [x] endpoint 在 WAITING/STALE、身份未确认或身份冲突时，普通 mutation 继续产生 0 个 datagram。
- [x] generation、attachment epoch 或身份 scope 变化后，旧 capability 不能发送新 mutation。
- [x] RF、参数、OTA、Tracking 和 Production 各自通过本领域状态机完成或终止，不共享通用永久恢复状态。
- [x] 未确认的设备终态显示“结果未确认”；UDP 写入、设备接受、遥测应用和物理 RF 证据保持不同语义。
- [x] 一个 endpoint 的终止/超时只释放该 endpoint 的 owner，不释放或阻断其他 endpoint。

## F. 退出、断开和删除

- [x] 本地 writer 正常收尾后，即使 capture-profile restore 未确认也可有界退出。
- [x] writer 无法关闭时可因明确的本地文件风险阻止退出，错误信息包含具体 endpoint/path，不使用设备恢复文案。
- [x] 正常等待中的操作允许用户终止；完成终止或达到有界超时后不会永久保持 `operation_busy`。
- [x] Edit/Delete/detach 只收尾目标 endpoint 的窗口、recorder、attachment 和 capability。
- [x] 正常退出停止工程串口、全部客户 recorder、Directory 和 Broker，不遗留活动 QThread、socket 或文件句柄。

## G. 自动化验证

- [x] 新增截图链路的回归测试，并证明旧行为可复现、新行为稳定通过。
- [x] 更新或删除全部 recovery ledger/controller/UI 测试；测试集合中不再把旧模型作为有效合同。
- [x] 客户目录、固定 bundle、Runtime、录制、主窗口退出、试产共享链路专项测试通过。
- [ ] `PYTHONPATH=. pytest satellite_debug_tool/tests` 全量通过，无新增允许失败项。
- [x] `python3 scripts/update_translations.py update` 后更新 TS/QM，`python3 scripts/update_translations.py check` 通过。
- [x] `python3 -m compileall satellite_debug_tool` 通过，`git diff --check` 通过。
- [ ] 四 endpoint × 20 Hz、多 recorder soak 至少运行 30 分钟：零串路、零重复解析、零异常 drop，并可干净退出。

## H. 真机验收边界

- [ ] AFD01C `192.168.1.13:4004` 与 ESA01 `192.168.1.12:4004` 双真机同时在线和左侧切换通过。
- [ ] 单独断网/重启其中一台，另一台连接、遥测和录制连续。
- [ ] 录制停止期间制造 restore 超时，应用仍可退出；设备重新连接后下一次录制重新协商成功。
- [ ] 真机记录分别标注：指令已发送、设备接受、遥测已应用；物理 RF 状态只由独立仪表/硬件证据确认。

Host 自动化通过不能代替 H 项真机验收。
