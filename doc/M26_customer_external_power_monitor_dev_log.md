# M26 客户页外接电源只读监测开发记录

## 1. 实施结论

- 用户已确认 `M26_customer_external_power_monitor_plan.md` 与验收标准。
- 新增 `core/external_power_monitor.py`，建立进程级 `ExternalPowerMonitor / ExternalPowerWorker / ExternalPowerStore` 单一所有权。
- 客户监测只允许 `*IDN? / OUTP? / MEAS:ALL? / STAT:OPER:COND? / STAT:QUES:COND? / OUTP:PROT:TRIP?` 六项查询；实现没有设定值、OUTPUT ON/OFF 或任意 SCPI 写接口。
- IP 留空时不创建 worker；配置只接受 IPv4，目标型号固定为 `GW-INSTEK / PSW 80-27`，端口固定 `2268`。
- 网络/解析失败立即撤销在线与当前读数，关闭 transport 后按固定节拍重新身份识别；同一配置历史保留并以单调连接代次区分。

## 2. 客户界面

- Customer Overview 底部扩为“变频板 / 发射阵列 / 接收阵列 / 外接电源”四个等宽状态项。
- 外接电源状态项呈现未配置、未启用、连接中、读取失败或在线事实；在线时显示 V/A/W。
- 新增共享 `ExternalPowerHistoryWindow`：一个进程只创建一个窗口，多 endpoint 点击复用；窗口显示身份、输出、保护、运行/告警状态和最近 30 分钟 V/A 双轴曲线。
- 完成深色主题下的离屏视觉检查：第四状态项、双轴曲线、状态摘要和窗口布局均正常；正式系统窗口/DPI 仍由现场包验收。

## 3. 设置与生命周期

- `SettingsDialog` 增加 Customer / Engineering / Production scope。
- Customer scope 新增外接电源 IPv4 和固定只读端口说明，不显示试产配置和试产报告。
- Engineering scope 不显示试产配置和试产报告；Production scope 保留原有完整试产设置。
- `MainWindow` 根据当前顶层工作区传入 scope。外接电源监测只在 Customer Workspace 活动；切入 Engineering/Production 前停止，返回客户页再恢复。
- 应用关闭等待电源 worker；若后续可逆关闭门禁失败，客户页监测按当前工作区恢复。

## 4. 记录合同

- `LiveView.record_external_power_sample()` 只接受活动 Customer SDB v3 录制。
- 每个有效样本写入 `external_power_sample/v1` metadata，包含主机纳秒时间、电源身份、连接代次、V/A/W、输出、运行、告警和保护状态。
- `CustomerEndpointSessionBundleFactory` 把同一共享样本广播给所有已存在 bundle；各 `LiveView` 自己判断当前是否为活动客户录制，因此未录制 endpoint 不写入。
- 使用 `DataImporter.open_sdb()` 回读实际生成文件，已验证 metadata 类型、时间、代次和值一致。

## 5. 自动化与视觉验证

- 实施前定向基线：`51 passed in 1.47s`。
- M26 与相邻定向回归：`64 passed in 5.35s`；后续新增超时、非有限值和清空 IP 用例后，相关测试 `23 passed in 1.10s`。
- 翻译：`1246` 条消息全部完成，`update` 与 `check` 通过。
- 全量回归最终结果：`1360 passed in 53.97s`。
- `git diff --check` 通过。

## 6. 保留的外部验收

- 真实 PSW 80-27 的 `*IDN?`、TCP/2268 分片行为和长时自动重连。
- 软件 V/A/W 与电源面板、独立仪表和实际负载功耗的一致性。
- Customer 监测切换到 Production 电源调试/批次控制时，真实设备侧无双连接或命令交错。
- Windows/macOS 正式发布包中的窗口管理器、DPI、休眠/唤醒和长时记录表现。

> 软件验证证明只读查询、状态流、曲线、设置隔离和 SDB 记录合同；不替代真实电源、负载和测量精度验收。
