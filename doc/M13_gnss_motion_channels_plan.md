# M13-GNSS Plan: GNSS 运动关键数据上报

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-30 |
| 状态 | 已确认，进入实现 |
| 关联里程碑 | M13 Profile 语义层 |
| 验收文档 | `doc/M13_gnss_motion_channels_acceptance.md` |
| 开发记录 | `doc/M13_gnss_motion_channels_dev_log.md` |

## 1. 背景

M13 已经让上位机通过 profile role 识别 `gps_lat` / `gps_lon` / `gps_alt`，但设备端现有 DEBUG profile 只覆盖 GNSS 位置和星数，未把 COG、速度、N/E/D 速度、COG 精度等运动量纳入正式通道。对 AFD01 而言，COG 是 yaw 可观测来源之一，速度和 cAcc 又是判断 COG 可信度的关键证据；这些数据应能被实时曲线、录制和回放保留。

## 2. 范围

- 上位机新增 GNSS 运动相关 channel role：
  - `gps_num_sv`
  - `gps_speed`
  - `gps_cog`
  - `gps_vel_n`
  - `gps_vel_e`
  - `gps_vel_d`
  - `gps_cog_std`
- AFD01 DEBUG profile 新增并上报：
  - `gps_speed`
  - `gps_cog`
  - `gps_vel_n`
  - `gps_vel_e`
  - `gps_vel_d`
  - `gps_cog_std`
- UFD45 DEBUG profile 新增并上报：
  - `gps_lat`
  - `gps_lon`
  - `gps_alt`
  - `gps_num_sv`
  - `gps_speed`
  - `gps_cog`
- 两个型号都在周期 debug 喂数点同步 `GPS_FIX` state。

## 3. 设计约束

- 新通道放入 POSITION/GNSS 分组，默认可见，但不设为 critical，避免挤占 Dashboard 顶部关键状态区。
- 地图仍只依赖 `gps_lat` / `gps_lon`；本轮不做地图航向箭头。
- no-fix 时不推送位置和运动通道，避免把轨迹画到无效坐标；`GPS_FIX` state 仍持续上报。
- 不改 `DEBUG_MAX_CHANNELS=32`，AFD01 使用 26..31 补齐 6 个剩余 ID，UFD45 使用 16..21。

## 4. 验收标准

以 `doc/M13_gnss_motion_channels_acceptance.md` 为准。
