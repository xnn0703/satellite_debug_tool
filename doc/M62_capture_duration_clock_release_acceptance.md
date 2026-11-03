# M62 采集覆盖时钟修复与 v1.9.3 验收标准

日期：2026-11-03。状态：按用户已授权发版目标执行。

| ID | 验收要求 |
| --- | --- |
| A01 | FULL 覆盖持续时间只由单调高分辨率时钟计算；录制开始/停止墙钟保持独立。 |
| A02 | 固定墙钟下，FULL 确认到停止的确定性测试得到正的精确覆盖时长。 |
| A03 | summary 使用 `support_full_intervals_monotonic_ns` 和 `support_full_coverage_ns`，不再输出含糊的墙钟区间键。 |
| A04 | 录制协商、生命周期、导入及相邻 customer/session 回归通过。 |
| A05 | v1.9.2 tag 不改写且不晋升；修复使用唯一 v1.9.3 commit/tag/候选。 |
| A06 | v1.9.3 全量测试、翻译、compileall、源码 smoke 和四包发行门禁重新通过。 |
