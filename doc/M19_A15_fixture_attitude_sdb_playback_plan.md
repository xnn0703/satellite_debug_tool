# M19-A.15 夹具姿态同步录制与回放计划

## 1. 目标

当摇摆台或 MS-6222 可用且设备正在录制 SDB v3 时，将夹具姿态同步写入该设备的录制文件。回放时把设备姿态、平台目标姿态和 MS-6222 相对 FRD 姿态放在同一“姿态”区域显示，并保留来源、质量和时间对齐证据。

## 2. 姿态事实定义

录制三类相互独立的事实：

- `device_attitude`：设备自身上报的 Roll/Pitch/Yaw，沿用现有设备协议和设备时间戳。
- `platform_target_attitude`：软件实际发送给摇摆台的相对 FRD 目标 Roll/Pitch/Yaw。它只表示已发送目标，不声明平台已经到达。
- `ms6222_relative_attitude`：MS-6222 原始姿态经已确认坐标标定转换后的相对 FRD Roll/Pitch/Yaw，表示独立姿态参考。

MS-6222 绝对航向继续单独保存为 `ms6222_true_heading`，不替代相对 Yaw。当前控制器反馈只有六缸脉冲，没有已确认的反运动学姿态回读，因此首版不创造“平台实测欧拉角”；平台实际姿态由 MS-6222 独立参考提供。

## 3. 单一数据流

1. `FixtureRuntime` 增加只读夹具遥测总线，发布目标姿态、MS 原始绝对姿态、标定后相对姿态、质量、档案 ID、标定 ID和控制会话 ID。
2. Customer、Engineering 和 Production 页面只向总线发布领域样本，不直接寻找或写入任意页面的 recorder。
3. 设备会话 recorder owner 订阅总线；只有 SDB v3 正在录制时才写入 `fixture_attitude_sample/v1` metadata record。
4. 多设备正式批次中，同一个夹具样本分别写入每台参与设备的 SDB，携带相同夹具 sample ID和原始主机时间，便于独立交付与交叉核对。
5. recorder 不可用、队列满或写入失败产生明确数据缺口，不能静默丢弃后继续给出完整姿态结论。

## 4. SDB 记录格式

继续使用 SDB v3 `SDB_RECORD_METADATA`，不改变设备原始帧：

```json
{
  "schema": "fixture_attitude_sample/v1",
  "sample_id": "...",
  "source": "platform_target_attitude | ms6222_relative_attitude | ms6222_true_heading",
  "host_monotonic_ns": 0,
  "host_wall_time_ns": 0,
  "roll_deg": 0.0,
  "pitch_deg": 0.0,
  "yaw_deg": 0.0,
  "valid": true,
  "quality": {},
  "fixture_profile_id": "...",
  "fixture_profile_sha256": "...",
  "calibration_id": "...",
  "control_session_id": "..."
}
```

- SDB record envelope 使用 `host_wall_time_ns`。
- payload 同时保存主机单调时钟，供同进程夹具证据关联。
- 数值必须为有限数；无效参考写 `valid=false` 和原因，不写伪造姿态值。
- 数据写入频率设独立上限，默认 MS 相对姿态不超过 50 Hz；平台目标按实际发送点记录。

## 5. 时间对齐合同

- SDB v3 已为每个设备接收块保存主机时间，同时设备姿态使用设备 uptime。
- 回放构建器从同一文件中生成“主机接收时间 ↔ 展开后的设备 uptime”锚点。
- 夹具样本只在相邻有效锚点内插值映射到设备时间轴；不跨设备重启、长断线或 uptime 代际插值。
- 超出锚点范围、相邻锚点间隔过大或映射残差超限的样本标记为 `unaligned`，不画在同一时间轴上，并进入完整性统计。
- 回放界面显示对齐样本数、未对齐样本数和最大对齐跨度，不把主机到达时间描述为硬件同步。

## 6. 回放显示

- Customer Playback 的“姿态”组增加九条可辨认曲线：设备 R/P/Y、平台目标 R/P/Y、MS 相对 R/P/Y。
- Engineering Playback 对设备 profile 的姿态组增加相同来源曲线；原设备通道定义不修改，夹具曲线作为只读外部 series 叠加。
- 同一轴保持同一颜色，不同来源使用不同线型；图例包含“设备 / 平台目标 / MS参考”。
- 缺少标定时仅显示平台目标和 MS 原始绝对航向信息，不把 MS 原始坐标误标为设备 FRD。
- 设备重启、夹具断线、MS 参考无效和录制缺口在曲线上断开并显示事件标记。

## 7. 生命周期与所有权

- 夹具连接不自动启动设备录制；设备录制也不自动取得夹具控制权。
- 弹窗隐藏不影响已经明确开始的控制会话或设备录制，应用关闭仍走现有受控收尾。
- 每个设备 recorder 只写自己的 SDB；共享夹具样本由 recorder owner 接收，UI 不直接写文件。
- SDB v2 和没有夹具 metadata 的历史文件继续按原逻辑回放。

## 8. 实施顺序

1. 增加类型化夹具遥测样本和 `FixtureRuntime` 只读总线。
2. 为设备/工程 recorder 增加公开的 metadata sink，删除 UI 直接访问 recorder 的需要。
3. 写入 `fixture_attitude_sample/v1` 并增加队列失败、限频和多设备归属测试。
4. 回放构建器增加 host-time/uptime 锚点与代际安全的映射器。
5. Customer/Engineering 回放把夹具 series 叠加到姿态区域。
6. 增加缺口、重启、无标定、参考失效和历史 SDB 兼容测试。
7. 全量回归后进行一段组合摇摆录制与回放真机验收。

## 9. 交付边界

- 主机接收时间对齐属于工作站时间相关，不等同于 PPS 或 PTP 硬件同步。
- 平台目标姿态不等同于平台实测姿态。
- MS-6222 相对姿态只有在标定、参考质量和对齐同时有效时才能参与误差结论。
