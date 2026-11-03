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

## v1.9.3 正式发行

- 发布提交/不可变标签：`57f6b1f08a1bd03741b21fcc8439cae905b2912e` / `v1.9.3`。
- GitHub Actions `41625049569`：Windows user/ops 全量源码回归、冻结构建、冒烟及候选上传全部通过。
- macOS user/ops 使用 Python 3.11 正式环境构建；两版 Cocoa 冒烟均通过，ops 目录声明 `installation_mode=automatic`。
- macOS ops publication bundle `3320f0b8...8934d7`、Windows ops bundle `20a6e0fd...a4253` 已通过 ECS 本机完整 SHA-256、HTTPS Range 抽样并原子发布。
- fresh `ReleaseChecker` 对 macOS arm64 和 Windows x86_64 均返回 v1.9.3、同一 build ID 和 `automatic`；本版两个目录 `delta_count=0`，均使用完整包。
- `/downloads/v1.9.3/` 已发布四份完整包、`release.json` 与 `SHA256SUMS.txt`；服务器本机四包 SHA-256、ZIP/7z CRC 和公网 Range 均通过。
- 首次 macOS commit 的公网 Range 探针因本机 Python 3.13 未显式加载 CA bundle 而拒绝，timestamp 未切换；同一已签名 bundle 显式指定 CA 后成功，候选与签名字节未重建。
- 本轮再次确认旧上传/晋升路径会经 3 Mbps 公网重复传输或回读大包。完整摘要应继续由 ECS 本机计算；后续流程优化目标是 incoming 到 public 的服务器内原子复制，离线签名端只处理 receipt、目录和签名元数据，不降低完整 SHA-256 门禁。
- 机器可读结果见 `M62_release_validation.json`。Windows 实机自动更新/回滚、Apple notarization、真实设备、RF、供电与摇摆台现场验收仍是独立边界。
