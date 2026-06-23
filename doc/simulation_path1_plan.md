# Path 1 — 真机在环桌面对星测试（方案总览）

> 目标：真机跑**真实 trace.c** 跟踪算法，PC 用 MockModem 喂 SNR、用 debug-v2 看遥测。
> 这才是"桌面测试跟踪流程"的真正达成。承接 `simulation_plan_v2.md`（PC 仿真已完成）。

## 0. 两个必须先纠正的事实（侦察固件得到）

1. **端口冲突（会让真机直接报废）**：设备 debug-v2 链路本身就在 **WizNet UDP 4004**
   （`afd01_app_debug.c: AFD01_DEBUG_UDP_PORT 4004`）。mimo 设计/现 MockModem 把 IOT503 也设成
   4004 → 真机上两条链路撞端口。**IOT503-over-UDP 必须换独立端口**（本方案定 **5004**）。
2. **真实 modem 是 UART**（`modem_cfg.com = Modem1_COM = "uart8"`），传输被 `hd_device_write/read`
   + `cfg->com` 抽象。iot503.c 的成帧/解析/checksum/report 组装**全部可复用**，只需换传输层。

## 1. 链路拓扑（定稿）

| 链路 | 协议 | 设备侧 | PC 侧 | 方向 | 用途 |
|------|------|--------|-------|------|------|
| Debug 监控 | debug-v2 | WizNet UDP **4004** | bind **45678** | 设备→PC | chart/姿态/状态/事件遥测 |
| Modem 仿真 | IOT503 | WizNet UDP **5004**(新) | bind **45679** | 双向 50Hz | MockModem ↔ simulate_modem |

两条链路独立并存。**关键 UI 修正**：仿真模式必须允许 debug-v2 Connect 同时在线
（现 `_start_simulation` 禁用了 Connect → 这就是 chart 没数据的直接原因）。

## 2. 数据流闭环（真机在环）

```
真机 locate(IMU/GPS) ─┐
真机 trace.c ──────────┼─→ simulate_modem 组 REAL_TIME_REPORT(0xA0) ──UDP5004──→ MockModem(PC)
  (SCAN→LOCK 算法)     │                                                            │
                       │                                              MockModem 用 B0 几何算 SNR
                       │   ┌────────────── SNR_REPORT(0x01) ◀──UDP5004── (基准−扫描损失−失指−雨衰)
                       ▼   ▼
            modem_snr topic → antenna 扫描损失归一化 → trace 消费 → 调波束 → 回到顶部
                       │
真机 debug_session ──UDP4004──→ PC debug-v2 → chart/姿态/状态/事件（实时显示）
```

## 3. 两条工作流

### 工作流 A — PC 侧收尾（任务 `sim_P1_pc.md`，交 MiMo）
- A1 补删死代码（C 阶段 mimo 跳过的）
- A2 UI：仿真模式与 debug-v2 Connect 并存（删禁用逻辑）
- A3 MockModem IOT503 远端端口 4004→**5004**（避开 debug），并做成可配置

### 工作流 B — 固件 simulate_modem（任务 `sim_P1_fw.md`，框架交 MiMo 填，Claude 逐 diff 把关）
- 技术路线：simulate_modem.c 注册自己的 WizNet UDP 端口(5004)，**镜像 `afd01_app_debug.c`**；
  RX 字节 →（复用）`iot503_frame_parse` → 既有 `modem_rx_callback` → `mcn_publish(modem_snr/modem_sat)`；
  TX → 复用 iot503 的 report 组装（提取 `iot503_build_report_frame()` 公共helper）→ `wiznet_port_send`
- modem.c 加薄 `sim_mode` 分支：sim 时 `modem_cfg.com` 走 UDP 路径而非 `iot503_config(uart8)`
- **不动** trace.c / locate.c / antenna.c / iot503 既有 UART 路径

## 4. 硬约束（必须周知）

- **固件无法在此环境构建/烧录/联调**（需 arm-none-eabi + 真机）。固件 build + flash + 上机联测
  **在你的工作台做**；Claude 只能逐 diff review + 给框架。
- 固件是高危底层（FreeRTOS 线程 / WizNet rx 回调在 wiz mutex 临界区 / IPC topic）→ MiMo 只填
  机械部分（report 组装、frame 解析接线、shell 命令、para），**危险点按骨架严格照做、存疑即停报告**；
  Claude review 每个 diff 后才允许上机。
- WizNet(W5500) 硬件 socket 数有限（debug 占 1，modem-sim 再占 1，确认不超额）。

## 5. 验收（真机）

| # | 项 | 方法 |
|---|---|---|
| P1-A1 | 死代码清零，全量 pytest 绿 | pytest |
| P1-A2 | 仿真时仍能 Connect debug-v2，chart 实时滚动 | 真机联测截图 |
| P1-A3 | IOT503 走 5004，与 debug 4004 不冲突 | 抓包/日志 |
| P1-B1 | `sim_modem start` 后 MockModem 收到 RTR、设备收到 SNR | 双侧日志 |
| P1-B2 | 真 trace 在仿真 SNR 下完成 SCAN→LOCK | 观察 TRACE_MODE/LOCK_FLAG |
| P1-B3 | 雨衰/遮挡注入 → 真 trace 状态变化/失锁恢复 | 面板操作 + 事件时间线 |
