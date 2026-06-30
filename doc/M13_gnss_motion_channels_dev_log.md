# M13-GNSS 开发记录

## 2026-06-30

### 输入

用户确认 COG 等 GNSS 相关数据属于关键数据，要求补入上报和显示链路。

### 调研证据

- 上位机 M13 语义层当前只定义 `gps_lat` / `gps_lon` / `gps_alt`，没有 COG、速度、N/E/D 或 cAcc role。
- AFD01 `locate_info.gps` 已有 `speed`、`heading`、`velN`、`velE`、`velD`、`cAcc`，trace verbose 已打印这些字段。
- AFD01 DEBUG profile 只注册 `gps_lat` / `gps_lon` / `gps_alt` / `gps_num_sv`，且现有 `afd01_profile_push_gps_position()` 尚未在周期 debug 喂数点调用。
- UFD45 `locate_info.gps` 已有 `latitude`、`longitude`、`altitude`、`numSV`、`speed`、`heading`，但 DEBUG profile 未注册 GPS channel。

### 当前决策

- 新增 GNSS 运动量走普通 channel，默认可见，不设 critical。
- no-fix 时不推送位置/运动 channel，只持续上报 `GPS_FIX` state。
- 数据喂入集中放在各型号 `trace_debug_info()`，避免定位模块直接依赖 debug profile。

### 待实现

- 暂无。

### 已实现

- 上位机已新增 GNSS motion role 常量、导出、名称 fallback 和单元测试用例。
- M13 profile 语义计划与 DEBUG v2 协议规范已补充推荐 role 列表。
- AFD01 DEBUG profile 已新增 26..31 GNSS motion 通道、role 声明和 motion setter。
- AFD01 `trace_debug_info()` 已同步上报 GPS_FIX，并在 fixed 时推送位置、星数、速度、COG、N/E/D、cAcc。
- UFD45 DEBUG profile 已新增 16..21 GPS 位置/运动通道、role 声明和 GPS setter。
- UFD45 `trace_debug_info()` 已同步上报 GPS_FIX，并在 fixed 时推送位置、星数、速度、COG。

### 已验证

- `PYTHONPATH=. pytest satellite_debug_tool/tests/test_profile_semantics.py -q`
  - 结果：5 passed。
- `PYTHONPATH=. pytest satellite_debug_tool/tests`
  - 结果：498 passed, 4 warnings。
- `cd /Users/xmac/Documents/project_manager/1-softHz/3-project_dev/3-satlite_comm_terminal/code && ./build.sh afd01 app debug`
  - 结果：构建成功；保留既有 warning。
- `cd /Users/xmac/Documents/project_manager/1-softHz/3-project_dev/3-satlite_comm_terminal/code && ./build.sh ufd45 app debug`
  - 结果：构建成功；保留既有 warning。
- `git diff --check`（上位机仓库）
  - 结果：通过。
- `git diff --check`（下位机仓库）
  - 结果：通过。
