# 模拟对星仿真 — 验收标准（对应 simulation_plan_v2.md）

## B0 — 移植固件正向模型

| # | 验收项 | 验证方法 | 通过判据 |
|---|--------|----------|----------|
| B0-1 | ka256 扫描损失 0° 无损失 | `ka256_scan_loss_dB(20.2, 0)` | ≈ 0 dB（clamp 后） |
| B0-2 | ka256 扫描损失单调递增 | 0→65° 采样 | 整体随扫描角增大（拟合内单调） |
| B0-3 | ka256 70° 饱和 | `ka256_scan_loss_dB(f, 80)` | = 70° 边界值，不返回哨兵 |
| B0-4 | ka256 频段就近选择 | freq 19.5 → 选 19.45 band | 命中最近 band |
| B0-5 | bp 几何锚点 | AZ0/EL30 输入 → 主瓣方向 | 与固件实测语义一致（机头右侧） |
| B0-6 | 失指误差对称/零点 | 同一指向 vs 自身 | sep ≈ 0；偏 N° → sep ≈ N° |

## B1 — MockModem SNR 重写

| # | 验收项 | 验证方法 | 通过判据 |
|---|--------|----------|----------|
| B1-1 | 失指 0 + 扫描角 θ → SNR = 基准 − ka256(θ) | 构造 report | 误差 < 0.1 dB |
| B1-2 | 失指增大 → SNR 单调下降 | 扫失指 0→6° | 严格递减 |
| B1-3 | 负经度卫星不崩 | `encode_beam_config(-100, …)` | 正常编码，往返一致 |
| B1-4 | IOT503 编解码往返 | build→parse(SNR/BEAM/RTR) | 字段逐位还原 |
| B1-5 | 线程安全 | GUI 改参数 + worker 收发并发 | 无崩溃/无脏读（命令队列） |

## B2 — PC fake-device 单机闭环

| # | 验收项 | 验证方法 | 通过判据 |
|---|--------|----------|----------|
| B2-1 | fake_device 发 RTR、收 SNR | loopback | MockModem 收到合法 RTR |
| B2-2 | 闭环收敛：搜星→失指收小→SNR 上升 | 跑 N 秒 | 稳态失指 < HPBW/2，SNR 接近 基准−scan_loss |
| B2-3 | BEAM_CONFIG 设星生效 | 改卫星经度 | fake_device 目标随之变 |
| B2-4 | HB_CHECK/ACK | fake 发 HB | MockModem 回 ACK |

## C — Bug 修复 + 集成

| # | 验收项 | 验证方法 | 通过判据 |
|---|--------|----------|----------|
| C1 | 遮挡注入 → 失锁 → 恢复重锁 | 点"遮挡5s" | SNR 跌地板 5s 后恢复，fake 重搜锁定 |
| C2 | 基准可调 | 面板改 SNR_clear_boresight | 稳态 SNR 同步平移 |
| C3 | GUI 不挤压 chart/rpanel | 截图 | 第4列正常，主区不变形 |
| C4 | 雨衰滑块 → SNR 下降 | 调 0→20dB | SNR 线性下降，trace 状态变化 |
| — | 全量 pytest 绿 | `pytest satellite_debug_tool/tests` | 0 failed（baseline 已知失败除外） |

## 自评（实施时填）

> 实施完成后逐项对照填写 PASS/FAIL + 证据（测试输出/截图）。
