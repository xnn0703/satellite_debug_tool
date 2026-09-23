# M19-A.3 PSW 开启流程与升压确认修复开发记录

## 2026-09-18：实施启动

- 用户已确认 `M19_A3_power_turn_on_stabilization_fix_plan.md` 与验收标准。
- 现场会话 `20260918T110227Z-bce6821e` 证明 OUTPUT ON 后约 70 ms 的低电压是升压中间态，约 1.52 s 后实测电压达到 `12.005 V`。
- 当前按钮要求手工进入 `READY_OFF`，当前核心则把一次立即测量当作最终升压结论。

## 实施记录

- `PowerSupplyConfig` 新增 3 s 默认输出稳定期限；`GwInstekPswAdapter` 在一次 `OUTP 1` 后按 100 ms 上限间隔观察输出、测量值、状态字和保护状态，达到自动窗口后才进入 `ON_CONFIRMED`。
- worker 的一次“开启输出”意图在需要时先执行现有 OFF 准备，再执行 OUTPUT ON；两段共享一个 action ID。连接识别完成后即可单击开启，无需手工先点准备按钮。
- 原“准备输出关闭”改为“应用参数并确认关闭”，开启确认框明确显示 OFF、应用设定值、ON 和等待稳定的完整步骤。
- 操作者输入收敛为需求电压和电流。`psw80_27_validation_policy()` 根据官方规格自动生成设定值读回容差、开启窗口和关断门限，并在界面只读显示。
- 12 V / 1 A 自动限制为：电压设定值读回 `±0.002 V`、电流设定值读回 `±0.002 A`、开启窗口 `11.952..12.048 V`、关断门限 `≤0.050 V`。
- OUTPUT OFF 同样在稳定期限内等待电压下降。现场日志中的 `8.077 V` 不再能因宽松的手工 `9 V` 门限被确认关闭。
- PSW80-27 需求值统一限制为 `0 < V ≤ 80 V`、`0 < I ≤ 27 A`、`V × I ≤ 720 W`。

## 验证记录

- 现场时序回归覆盖首次测量低电压、约 1.5 s 后进入窗口，最终确认开启；同一动作只发送一次 `OUTP 1`。
- 新增 OUTPUT ON 稳定超时、保护触发和 OUTPUT OFF 放电等待回归。
- 电源核心、独立电源页、夹具互斥、Production Workspace、页面生命周期和 i18n 定向回归：`76 passed in 14.06s`。
- 翻译 `update` 与 `check` 通过：`1097 messages`、`0 unfinished`。
- 1024×600 与 1280×800 离屏布局检查通过，自动验证结果和主要操作可见。
- `python3 -m py_compile` 与 `git diff --check` 通过。
- 全量回归：`PYTHONPATH=. pytest satellite_debug_tool/tests -q`，`1318 passed in 59.71s`。
