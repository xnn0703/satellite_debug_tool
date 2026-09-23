# M19-A.3 PSW 电源独立调试页开发记录

## 2026-09-18：实施启动

- 用户已确认 `M19_A3_power_supply_debug_plan.md` 与验收标准。
- 实施前定向基线：生产批次、夹具、摇摆台、MS-6222 与 PSW 共 `290 passed in 5.41s`。
- 实施前 `GwInstekPswAdapter` 只有核心与单元测试，应用界面没有真实调用入口。
- 本轮只接入独立工程调试页，不接入 M19 批次自动流程，不修改设备固件。

## 实施记录

- 新增 `PowerSupplyDebugWorkspace`，在 Production Workspace 中作为第三个懒加载页面显示。
- 电源页不依赖批次、配方、DUT 或摇摆台档案；操作者只填写本次需求电压、电流，没有 14 V/2 A 默认值。设定值读回容差、开启窗口和关断门限由 PSW80-27 验证策略自动计算并只读显示。
- 复用 `FixtureControlLease` 作为批次、夹具调试和电源调试的唯一控制所有者；活动会话期间禁止切换到另一控制页面。
- 新增单一 `PowerSupplyDebugWorker`，串行执行连接识别、状态读取、OFF 预检、OUTPUT ON、OUTPUT OFF、返回本地和断开连接。停止 worker 时丢弃排队中的普通操作，优先关闭 TCP，不重放 OUTPUT ON。
- 连接只执行 `*IDN?`。状态读取与写操作继续复用 `GwInstekPswAdapter` 的闭环状态模型，没有新增旁路 SCPI 路径。
- 输出未确认关闭时，断开连接和关闭应用会明确提示 TCP 断开不会关闭实体输出；断开后界面只显示“已断开”，不推断输出已关闭。
- 调试证据写入 `~/.satellite_debug_tool/power_supply_sessions/<session_id>/`，包含配置、身份、逐条 SCPI、操作结果、事件、最终状态和 manifest，统一标记 `ENGINEERING_ONLY`。
- 配置区只持久化非动作型的电源 IPv4；不持久化自动开启意图或上次输出状态。
- 更新 Production Workspace 架构说明、持久化路径以及中英文 TS/QM 资源。

## 计划对照

- 计划中的独立入口、显式配置、闭环动作、共享租约、后台串行执行、退出语义和工程证据均已实现。
- 页面没有接入批次自动流程，没有生成 DUT PASS 或正式测试报告，也没有把电源总电流解释为单台 DUT 电流。
- 真机试用后修正了 `GwInstekPswAdapter` 的输出转换模型：OUTPUT ON/OFF 均在稳定期限内观察电压，不以转换过程中的单次读数作最终结论。
- 真机身份、空载设定、带载开关、面板对照和断线验收仍保留为现场测试项。

## 验证记录

- 定向回归：电源核心、独立电源页、夹具页、Production Workspace、页面生命周期和 i18n 共 `69 passed in 13.78s`。
- 电源专项在补充连接代次与结构化操作结果后再次通过，共 `12 passed`。
- 翻译：`python3 scripts/update_translations.py update` 与 `check` 通过，`1102 messages`、`0 unfinished`。
- 全量回归：`PYTHONPATH=. pytest satellite_debug_tool/tests -q`，`1311 passed in 57.81s`。
- 离屏布局检查：1024×600 和 1280×800 下配置、操作和状态区可见；命令记录与事件区可通过页面滚动访问。
- `python3 -m py_compile` 与 `git diff --check` 通过。

## 2026-09-18：真机反馈后的简化与稳定化

- 会话 `20260918T110227Z-bce6821e` 证明 OUTPUT ON 后的首次低电压属于升压过程，约 1.52 s 后达到 `12.005 V`；开启闭环改为在稳定期限内观察，不再立即失败。
- “开启输出”自动执行必要的 OFF 准备和参数应用；手工准备入口更名为“应用参数并确认关闭”。
- 四个高级验证输入已移除，改为按需求电压、电流自动计算并只读显示。
- 关断确认也等待电压降到自动门限，避免旧的 `9 V` 手工门限误报已关闭。
- 更新后定向回归 `76 passed in 14.06s`，全量回归 `1318 passed in 59.71s`，翻译检查 `1097 messages`、`0 unfinished`。

## 尚待真机验证

- 真实 PSW 的 `*IDN?` 返回是否匹配当前厂家、型号和可选 SN 合同。
- 现场批准参数下的 OFF 预检、设定值读回、错误队列和保护状态。
- 安全负载与实体安全措施就绪后的 OUTPUT ON、实测电压窗口和 OUTPUT OFF 电压下降闭环。
- TCP 断线、应用退出和重启后，电源面板状态及会话证据是否与人工观察一致。
