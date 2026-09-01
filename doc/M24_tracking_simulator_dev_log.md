# M24 Tracking Simulator 开发记录

## 2026-09-01

- 用户确认完整方案并要求直接实施。
- 上位机基线 `7498b90`；已有 M22、M23 温度窗口及 Product Store/UI 用户改动，全部保留。
- 协议继续使用 Debug v2 envelope；`0x11` 只用于 AFD01/AFD01C Debug Tracking 仿真，不进入 Product Service。
## 2026-09-01 实施记录

- 新增纯场景模型、最短 yaw 插值、派生速度/body rate、KA256 同表扫描损失、闭环 SNR 和固定长度 wire encoder。
- 工程工作区新增延迟创建页面；复用共享 DeviceSessionCore、事务租约、Product Service RF 控制和 SDB 数据源。
- 运行发送只使用一个 20 ms PreciseTimer；未增加线程、socket 或 Mock Modem。
- 场景纯逻辑 3 项、视图生命周期与 i18n 23 项专项测试通过；翻译目录 check 通过，共 948 条完整翻译。
- `py_compile` 与双仓库 `git diff --check` 通过。未增加 UI controller mock 测试、线程、socket、Mock Modem 或第二套会话。
- M22/M23 及温度窗口相关未提交文件保持在原工作树中；M24 只新增独立模型、页面、协议枚举、文档与必要的主窗口/i18n 接线。
