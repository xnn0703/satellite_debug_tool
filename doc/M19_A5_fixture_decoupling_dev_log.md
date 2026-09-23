# M19-A.5 摇摆台与 MS-6222 独立调试开发记录

## 2026-09-18：计划阶段

- 用户提出将夹具测试中的摇摆台与 MS-6222 解耦，并增加可单独验证 MS-6222 的页面。
- 当前底层 UDP 平台控制与 RS-422 参考采集已经独立，但 `FixtureDebugWorkspace` 同时拥有两条链路、联合记录、标定和比较 UI。
- 计划将 Production Workspace 调整为四个入口，并为 MS-6222 建立独立串口 owner、会话目录和证据合同。
- 等待计划与验收标准确认后开始实现。

## 2026-09-18：软件实现

- 新增 `Ms6222ControlLease`，同一进程只允许一个 MS-6222 串口 owner；冲突时直接报告当前 owner。
- 新增 `Ms6222SessionRecorder`，独立保存到 `~/.satellite_debug_tool/ms6222_sessions/`。会话包含 `configuration.json`、`raw_frames.jsonl`、`parsed_frames.csv`、`statistics.jsonl`、`events.jsonl`、`summary.json` 和带 SHA-256 的 `manifest.json`。
- 新增 `Ms6222DebugWorkspace`：串口刷新/连接、合法帧确认、三类报文最新值、频率/有效率/解析错误、最近五分钟姿态曲线和独立采集会话。
- Production Workspace 调整为“批次测试 / 摇摆台测试 / MS-6222 测试 / 电源测试”四个入口，均保持首次访问构建。
- 生产入口中的摇摆台页面启用 `motion_only`，不再创建或展示 MS 串口 owner、参考状态、联合标定和误差图。历史联合夹具类与会话文件保持兼容。
- 1024×600 使用可滚动 MS 页面，顶部连接和数据确认始终可见；摇摆台页面保留档案、安全门禁、单步、回中、复位和组合轨迹。
- 联合标定和正式批次参考时间线尚未接入新的 MS 独占租约，按验收文档保持未完成状态。

## 验证记录

- MS 协议、worker、证据、夹具控制/会话/分析、Production Workspace 与电源页相关测试：`222 passed in 3.76s`。
- 翻译目录：`Translation catalog OK: 1195 messages`。
- 1024×600 中文 MS-6222 页面完成离屏渲染检查，窗口保持 1024×600，页面可滚动。
- 全量测试：`1336 passed in 59.83s`，无新增 warning。
- `py_compile`、`git diff --check` 与翻译 `check` 通过。
