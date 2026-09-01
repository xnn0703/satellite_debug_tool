# M23 部件温度曲线悬浮窗开发记录

对应计划：`doc/M23_component_temperature_windows_plan.md`

验收标准：`doc/M23_component_temperature_windows_acceptance.md`

## 2026-09-01：基线与授权

- 用户确认在 Customer Overview 底部三部件状态项增加温度历史曲线小窗，要求靠近对应模块悬浮、三窗可同时打开，并允许最大化、最小化和关闭。
- 已核对当前上位机源码：姿态、波束、SNR 由 `0x21` 进入 Customer Overview；变频器、TX/RX 阵列状态由 `0x23` 进入 `ProductServiceStore` 后呈现。
- 本轮复用该权威 Store，只记录已经收到的温度；打开窗口不触发固件或设备额外查询。
- 用户明确不要求全量回归。本轮只做相关 Store/UI/协议测试、翻译检查、固件增量构建和差异检查。
- 当前上位机工作区已有未提交改动，本轮保留并只增加本功能所需的最小改动；不 commit、不 push、不制作发布包。
- 当前实现基于本地 `v1.1.1-13-g7498b90-dirty` 源码；已发布 `v1.1.1` 不含当前 Customer Workspace，
  因此“源码已支持”不等于现有安装包已包含本功能。

## 实现与验证记录

- `ProductServiceStore` 为 converter、TX array、RX array 各维护一路当前连接内存历史，时间窗为 30 分钟。
- `0x23` 规范频率为 1 Hz，历史 deque 按 2 Hz 上限配置 3602 条/部件，兼顾突发重发余量和内存边界。
- 三路共用一条独立于 SNR 的 `U32UptimeUnwrapper`；乱序旧包不推进历史，u32 回绕保持时间单调。
- temperature valid 未置位或数值非 finite 时记录 `NaN` 断点，曲线使用 `connect="finite"`，不会伪造 `0 °C`或跨缺失区间连线。
- `clear()` 同步清空三路历史并重置时间展开器，历史不落盘、不跨连接。
- 新增 `component_temperature_window.py`：可键盘操作的部件按钮卡片和独立 `QMainWindow` 温度窗口。窗口具有原生最小化/最大化/关闭 flags，首次靠近对应卡片并按屏幕可用区域限位。
- Customer Overview 仅管理三个窗口实例的创建、复用、主题和翻译切换；弹窗只持有 `ProductServiceStore` 引用，无发送或设备查询入口。
- 保留了开发前已存在的 Customer Overview TX 无读回文案改动，本轮未回退或覆盖。

### 定向软件验证

- `PYTHONPATH=. pytest -q satellite_debug_tool/tests/test_component_temperature_window.py satellite_debug_tool/tests/test_customer_overview_view.py satellite_debug_tool/tests/test_product_service_protocol.py satellite_debug_tool/tests/test_customer_product_policy.py satellite_debug_tool/tests/test_device_session_core.py`
  - 结果：`131 passed in 0.76s`。
- `python3 scripts/update_translations.py check`
  - 结果：`Translation catalog OK: 929 messages`。
- `python3 -m py_compile ...`
  - 结果：PASS。
- `git diff --check`
  - 结果：PASS。
- ESA01 `./build.sh esa01 app release`
  - 结果：PASS；生成 `esa01_application_v0.1.254.bin`，FLASH 618768 B（78.68%）。
- ESA01 Product Service 协议 Host 定向用例
  - 结果：PASS。
- 按用户要求未执行全量 pytest，未制作发布包；系统窗口管理器、DPI 与实机连续温升曲线保留待验证。
