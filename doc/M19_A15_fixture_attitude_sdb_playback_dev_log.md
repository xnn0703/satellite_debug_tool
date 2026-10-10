# M19-A.15 夹具姿态同步录制与回放开发记录

## 当前状态

- 状态：软件实现完成，待真机验收
- 日期：2026-10-09
- 前置能力：SDB v3 主机时间 record、FixtureRuntime、客户/工程回放、平台目标轨迹、MS-6222 标定后相对 FRD 姿态

## 架构审查结论

- SDB v3 已有 metadata record，可扩展夹具样本而不改变设备原始帧。
- 设备曲线使用设备 uptime，夹具使用主机时钟；必须先建立 SDB 内锚点映射，不能直接混画。
- 当前控制器反馈提供六缸脉冲和停止/保护结论，未提供已确认的欧拉角反解；首版只记录平台目标与 MS 独立实测。
- Customer Playback 已有固定姿态组；Engineering Playback 需要外部 series 叠加接口。

## 开发记录

| 日期 | 工作项 | 状态 | 证据 |
|---|---|---|---|
| 2026-10-09 | 建立计划与验收标准 | 完成 | A15 plan / acceptance |
| 2026-10-09 | 建立 `FixtureTelemetryBus` 与不可变姿态样本 | 完成 | 平台目标、MS 相对 FRD、MS 真实航向保持独立来源；MS 样本限频并保留有效性跳变 |
| 2026-10-09 | 接入 Customer / Engineering / Production recorder owner | 完成 | 活动 SDB v3 写入 `fixture_attitude_sample/v1`；多设备使用相同 sample ID 分别落入各自 SDB |
| 2026-10-09 | 建立主机接收时间到设备 uptime 的分段映射 | 完成 | 拒绝范围外、重启代际、长断线和异常时钟比例；未对齐样本不进入曲线 |
| 2026-10-09 | Customer / Engineering 回放叠加夹具姿态 | 完成 | 设备实线、平台目标虚线、MS 参考点线；相同轴固定同色并位于同一姿态区域 |
| 2026-10-09 | 建立缺口呈现 | 完成 | 无效参考与 MS 采样中断写入 NaN 曲线断点和事件竖线；回放显示完整性统计 |
| 2026-10-09 | A15 专项与相邻模块回归 | 完成 | A15 专项 10 passed；相关回归 97 passed；翻译目录 1363 messages 通过 |
| 2026-10-09 | 全量回归 | 完成 | `PYTHONPATH=. pytest -q satellite_debug_tool/tests`：1447 passed |

## 软件验证边界

- 主机接收时间映射用于相关回放，不代表 PPS/PTP 硬件同步。
- 平台曲线是已发送目标；当前平台实测姿态仍由标定有效的 MS-6222 提供。
- 真机组合摇摆、MS 中断、DUT 重启和双 DUT 同录仍按验收标准 E 执行。
