# M62 采集覆盖时钟修复与 v1.9.3 开发记录

## 发布门禁证据

- v1.9.2 tag Actions `41621070816` 的 Windows user 作业在源码回归失败。
- 唯一失败：`test_customer_recording_starts_before_exact_ack_and_restores_on_stop`，`support_full_coverage_ns` 实际为 0。
- 同一 SHA 的 macOS 两次全量测试均通过，说明是 Windows 墙钟分辨率暴露出的跨平台合同错误，不以重跑处理。
- v1.9.2 未晋升；macOS user 候选已在隔离目录，ops 上传在正式提交前主动停止。

## 实施与验证

- `LiveView` 保留录制开始/停止墙钟；FULL 进入、离开和停止收口统一消费 `_customer_capture_clock_ns()` 返回的 `time.monotonic_ns()`。
- 只有进入 FULL 或尚未收口的离开 FULL 边沿读取时钟；停止已经收口后转 RESTORING 不重复读取或生成第二个区间。
- SDB summary 使用 `support_full_intervals_monotonic_ns`，覆盖值继续使用 `support_full_coverage_ns`。
- 确定性回归冻结单调时钟为 1,000,000,000 → 1,000,000,123，得到唯一 123 ns 覆盖区间。
- 录制协商、M25 recorder/session 和 M49 生命周期定向回归：43 passed。
- v1.9.3 全量回归：2266 passed / 273.25 s。
- 翻译检查：1,508 条完成、0 unfinished；compileall、`git diff --check` PASS。
- user 源码 smoke：版本 1.9.3、manual、运维模块为空、Shell/总览 PASS。
- ops 源码 smoke：版本 1.9.3、online、运维模块存在、Shell/总览 PASS。
