# M15 GNSS 状态真值与 MG902 信号可视化开发记录

> 日期：2026-07-22
> 状态：上位机软件验证完成；真机验收待完成

## 基线

- Bynav 天空图与逐信号 C/N0 基线提交：`5224e58`。
- 基线专项测试：29 passed。
- 基线全量测试：549 passed、4 warnings。
- 真机 Bynav 与厂商工具、Live 录制到 Playback 对照仍待完成。

## 实施记录

- 2026-07-22：确定保留顶部状态条和 Dashboard，两处共用同一 StateStore。
- 2026-07-22：确定新增 `0x0F/0x10`，旧 `0x0D/0x0E` 保持兼容。
- 2026-07-22：确定区分设备侧 NO_FIX/STALE 与上位机链路侧 UNKNOWN。
- 2026-07-22：`StateSnapshot` 分离 `last_received_wallclock` 与 `last_changed_wallclock`；相同值周期帧只刷新
  received。Live 3.5 秒收不到 STATE_REPORT 时显示 UNKNOWN，Debug OFF、断线和切换设备立即清状态；
  Playback 打开、关闭、换文件时清状态，暂停不使用墙钟判 stale。
- 2026-07-22：`ProfileStore` 仅在 `table_ver` 和规范化完整内容均相同时跳过更新；保留旧 SDB 的内嵌旧
  profile，不重解释历史值 3 的 `RTK`。State/Channel/Event 三类 DEFINE 均使用相同判等原则。
- 2026-07-22：新增 `0x0F GNSS_SAT_REPORT`、`0x10 GNSS_SIGNAL_REPORT` 解码和 source-aware
  `GnssStore`。MG902 使用 `UBX_M9` namespace，Bynav legacy 使用 `UG016`；Sky/Signal 独立维护
  age、stale、pending，完整报告切源时清除上一源快照和历史。decoder 严格校验 64 条/片、92 条总上限
  和 canonical 分片形状；重复片的 records 或 flags 冲突都会丢弃整轮。
- 2026-07-22：GNSS Live/Playback 共用同一 Store/widget；只有 NAV-SAT 时只显示天空图，NAV-SIG 明细
  保留 receiver-native signal ID、freq、quality、correction 和 raw flags；UNKNOWN source 使用 RAW fallback。

## 软件验证结果

- 完整测试：`PYTHONPATH=. pytest satellite_debug_tool/tests`，596 passed、4 条既有
  `datetime.utcnow()` 弃用告警。
- 定向回归覆盖同版本 profile 刷新、双处 GPS_FIX 一致、Live/Playback 生命周期、跨源 namespace、92 条
  分片、旧 SDB 内嵌 profile，以及旧 receiver 将 `0x0F/0x10` 作为 RawFrame 且 decode error 不增长。
- 固件 C 与 Python codec golden bytes、CRC 和分片边界一致；`git diff --check` 通过。

## 待真机验证

- MG902 与 u-center 逐星/逐信号对照，并录制真实 SDB 后验证 Playback。
- Bynav 与厂商工具逐星/逐信号对照，并录制真实 SDB 后验证 Playback。
- 真机 Debug OFF、断线、GNSS 停帧、恢复和切源时，核对顶部状态条与 Dashboard 同步进入
  NO_FIX/STALE/UNKNOWN 及恢复。
