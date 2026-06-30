# 模拟对星仿真方案

## 1. 目标

在桌面端模拟卫星通信链路，让 afd01 终端在**没有真实卫星资源**的情况下完成：
- 静态/手持/原地摇摆等非移动场景的姿态解算测试
- 波束跟踪算法（SCAN_GLOBAL → SCAN_WIDE → SCAN_LOCK）验证
- SNR 闭环逻辑验证（trace 消费 modem SNR → 调整天线指向）
- 雨衰/遮挡等异常场景的恢复能力测试

## 2. 系统架构

```
┌─────────────────────────────┐        IOT503 over UDP         ┌─────────────────────┐
│      afd01 终端（STM32H750） │ ◄────────────────────────────► │   Debug Tool（PC）   │
│                             │   端口 4004                     │                     │
│  ┌────────────────────┐    │   方向：双向 50Hz               │  ┌──────────────┐  │
│  │ simulate_modem     │    │                                 │  │  MockModem   │  │
│  │ (新增固件模块)      │    │   终端→Host: REAL_TIME_REPORT   │  │  (Python)    │  │
│  │ 替代物理 IOT503    │    │     (0xA0) GPS/姿态/波束        │  │              │  │
│  │ UART 驱动          │    │                                 │  │  卫星几何     │  │
│  └────────┬───────────┘    │   Host→终端: SNR_REPORT         │  │  天线增益     │  │
│           │                │     (0x01) SNR值                │  │  链路预算     │  │
│           ▼                │                                 │  │  SNR计算      │  │
│  ┌────────────────────┐    │   Host→终端: BEAM_CONFIG        │  └──────────────┘  │
│  │ modem 业务层       │    │     (0x02) 卫星经度/频点        │                     │
│  │ (modem.c, 不变)    │    │                                 │  ┌──────────────┐  │
│  └────────┬───────────┘    │                                 │  │ 仿真面板 GUI  │  │
│           │ IPC             │                                 │  │ 卫星/频段/    │  │
│           ▼                │                                 │  │ 雨衰/遮挡     │  │
│  ┌────────────────────┐    │                                 │  └──────────────┘  │
│  │ trace 业务层       │    │                                 │                     │
│  │ (trace.c, 不变)    │    │   Debug v2 over UDP             │  ┌──────────────┐  │
│  └────────────────────┘    │   端口 45678                    │  │ 数据监控      │  │
│                             │ ◄───────────────────────────── │  │ Chart/State/  │  │
│  ┌────────────────────┐    │   DATA_REPORT / STATE_REPORT    │  │ Event         │  │
│  │ debug_session      │    │   EVENT_REPORT / HEARTBEAT      │  └──────────────┘  │
│  │ (已有，不变)        │    │                                 │                     │
│  └────────────────────┘    │                                 └─────────────────────┘
└─────────────────────────────┘
```

### 两条独立链路

| 链路 | 协议 | 端口 | 方向 | 用途 |
|------|------|------|------|------|
| Modem 仿真 | IOT503 | 4004 | 双向 50Hz | MockModem ↔ simulate_modem |
| Debug 监控 | Debug v2 | 45678 | 终端→Host | 终端状态数据上报 |

两条链路互不干扰，可同时工作。

## 3. IOT503 协议

### 3.1 帧格式

```
0x55 | cmd(1B) | len(2B, big-endian) | payload(N bytes) | checksum(2B, big-endian)
```

- `checksum` = 从 cmd 字节开始到 payload 结尾的累加和，取低 16 位
- 最小帧长 = 6 字节（magic + cmd + len + checksum，payload=0）

### 3.2 关键命令

| cmd | 名称 | 方向 | payload | 周期 |
|-----|------|------|---------|------|
| 0x01 | SNR_REPORT | Host→终端 | snr(f32) + indicator(u8) + power(u8) + reboot(u8) = 7B | 50Hz |
| 0x02 | BEAM_CONFIG | Host→终端 | lon(i16×100) + polar(u8) + rx_freq(f32) + tx_freq(f32) = 11B | 启动时 |
| 0xA0 | REAL_TIME_REPORT | 终端→Host | GPS + 姿态 + 波束 + 模式 + 状态 = 42B | 50Hz |
| 0xA2 | HB_CHECK | 终端→Host | 空 | 50Hz |
| 0xA3 | HB_ACK | Host→终端 | 空 | 响应 |

### 3.3 REAL_TIME_REPORT (0xA0) 字段

| 偏移 | 类型 | 字段 | 单位 |
|------|------|------|------|
| 0 | u8 | gps_lock | 0/1 |
| 1 | i16 | lon | ×100 → ° |
| 3 | i16 | lat | ×100 → ° |
| 5 | u16 | alt | m |
| 7 | f32 | rx_freq | MHz |
| 11 | f32 | tx_freq | MHz |
| 15 | f32 | rx_lo | MHz |
| 19 | f32 | tx_lo | MHz |
| 23 | u8 | power | 0/1 |
| 24 | u8 | polar | 0/1 |
| 25 | i16 | pitch | ×100 → ° |
| 27 | i16 | roll | ×100 → ° |
| 29 | i16 | heading | ×100 → ° |
| 31 | i16 | theta | ×100 → ° (波束离轴角) |
| 33 | i16 | phi | ×100 → ° (波束航向角) |
| 35 | u8 | mode | 0=自动 1=手动 |
| 36 | u8 | tle_mode | 0=GEO 1=MEO/LEO |
| 37 | u8 | status | bit0=ready, bit5=search |
| 38 | u32 | time | ACU 运行秒数 |

### 3.4 SNR_REPORT (0x01) 字段

| 偏移 | 类型 | 字段 | 说明 |
|------|------|------|------|
| 0 | f32 | snr | 信噪比 (dB) |
| 4 | u8 | indicator | bit0=电源, bit1=锁定 |
| 5 | u8 | power | 0=正常 1=节能(关发射) |
| 6 | u8 | reboot | 0=正常 1=重启 |

### 3.5 SNR 时序特性

与真实 IOT503 modem 保持一致：

| 参数 | 值 | 说明 |
|------|-----|------|
| report_period | 20ms | SNR_REPORT 报文周期 (50Hz) |
| refresh | 30ms | SNR 真值刷新周期 |
| acq | 130ms | 从无到有时延（驱动 GLOBAL 扫描停留） |
| response | 120ms | 阶跃响应/settle（驱动 WIDE 定峰 + LOCK settle） |

## 4. 设备端：simulate_modem 模块

### 4.1 职责

替代物理 IOT503 UART 驱动，通过 UDP 收发 IOT503 帧：

- **接收** SNR_REPORT → 解析 SNR → 发布 `modem_snr` topic → trace 消费
- **接收** BEAM_CONFIG → 解析卫星参数 → 发布 `modem_sat` topic → trace 消费
- **发送** REAL_TIME_REPORT @50Hz（从 locate/trace 拉最新姿态/波束数据）
- **响应** HB_CHECK → HB_ACK

### 4.2 与 modem 业务层的关系

```
modem.c (不变)
├── modem_thread_create()
│   ├── if (sim_mode == SIM_MODE_OFF)
│   │   └── iot503_config()  →  走原有 UART 路径
│   └── if (sim_mode == SIM_MODE_UDP)
│       └── sim_modem_start()  →  走 UDP 路径
│
├── modem_rx_callback()  ←  simulate_modem 调用（与 iot503 相同回调）
│   ├── MSG_CMD_SNR_REPORT  →  mcn_publish("modem_snr")
│   ├── MSG_CMD_BEAM_CONFIG  →  mcn_publish("modem_sat")
│   └── ...
│
└── modem_pre_report_cb()  ←  simulate_modem 发送前调用
    └── 合成 report.status 等字段
```

关键：**modem 业务层代码不变**，simulate_modem 只替换底层传输。

### 4.3 配置

通过 `para_manage` 参数管理：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| SimMode | u8 | 0 | 0=关闭(用UART) 1=UDP仿真 |
| SimPort | u16 | 4004 | 本地 UDP 监听端口 |
| SimHostIP | string | "192.168.1.100" | Host IP 地址 |

Shell 命令：

```
sim_modem mode udp          # 切换到 UDP 仿真模式
sim_modem port 4004         # 设置端口
sim_modem start             # 启动
sim_modem stop              # 停止
sim_modem status            # 查看状态
```

### 4.4 文件清单

```
code/target/afd01/application/app/modem/simulate_modem.h
code/target/afd01/application/app/modem/simulate_modem.c
code/target/afd01/application/app/modem/modem.c  (修改：集成 sim_mode 分支)
```

## 5. Debug Tool：MockModem 模块

### 5.1 职责

通过 UDP 与设备端 simulate_modem 通信，模拟卫星/modem 侧的 SNR 响应：

- **接收** REAL_TIME_REPORT (0xA0) → 解析设备姿态/波束
- **计算** SNR（卫星几何 + 天线增益 + 链路预算 + 雨衰）
- **发送** SNR_REPORT (0x01) @50Hz
- **发送** BEAM_CONFIG (0x02) 配置卫星参数
- **响应** HB_CHECK → HB_ACK

### 5.2 SNR 计算流程

```
REAL_TIME_REPORT (0xA0)
  │
  ├─ lat/lon/alt  ──→  StaticSatellite.look_angles()
  │                      │
  │                      ▼
  │                   azimuth_deg, elevation_deg, range_km
  │
  ├─ theta (离轴角) ──→  off_axis_deg
  │
  └─ rain_fade_db  ──→  LinkBudget.compute_snr()
                          │
                          ▼
                       SNR (dB)  ──→  SNR_REPORT (0x01)
```

### 5.3 链路预算公式

```
SNR = EIRP + G_rx - FSL - rain - atmos + 228.6 - 10*log10(BW) - 10*log10(T)

其中:
  EIRP     = 卫星等效全向辐射功率 (dBW)
  G_rx     = 天线增益 (dBi)，按偏轴角查 G_max - 12*(θ/θ_3dB)^2
  FSL      = 20*log10(4πdf/c) 自由空间损耗
  rain     = ITU-R P.838 简化雨衰模型
  atmos    = 大气衰减（仰角相关）
  BW       = 信号带宽 (Hz)
  T        = 系统噪声温度 (K)
```

### 5.4 GUI 集成

仿真面板嵌入 LiveView 第 4 列（默认隐藏，点"仿真"显示）：

```
┌─ 通道 ──┬─ Chart ──────────┬─ 状态/事件 ──┬─ 仿真面板 ──┐
│         │                  │              │ 卫星: 亚太6号 │
│ ch_00   │   ~~~~~~~~~~~~   │  姿态 3D     │ 经度: 134°E  │
│ ch_01   │   ~~~~~~~~~~~~   │  状态面板    │ 频段: Ka     │
│ ...     │                  │  事件时间线  │             │
│         │                  │              │ [遮挡 5s]    │
│         │                  │              │ 雨衰: [====] │
│         │                  │              │             │
│         │                  │              │ SNR: 12.3dB  │
│         │                  │              │ 偏轴: 0.5°   │
│         │                  │              │ Pitch: 2.1°  │
└─────────┴──────────────────┴──────────────┴─────────────┘
```

### 5.5 文件清单

```
satellite_debug_tool/core/simulation/mock_modem.py        (新增)
satellite_debug_tool/core/simulation/simulation_engine.py  (重写：纯计算)
satellite_debug_tool/core/simulation/scenario_engine.py    (修复：完整状态机)
satellite_debug_tool/core/simulation/__init__.py           (更新)
satellite_debug_tool/ui/live_view.py                       (修改：MockModem集成+布局)
satellite_debug_tool/ui/simulation_panel_widget.py         (重写：简化)
satellite_debug_tool/tests/test_mock_modem.py              (新增)
satellite_debug_tool/tests/test_simulation_engine.py       (重写)
satellite_debug_tool/tests/test_scenario_engine.py         (修复)
```

## 6. 数据流全景

### 6.1 正常仿真流程

```
1. 用户在设备 shell 执行:
   > sim_modem mode udp
   > sim_modem start

2. 用户在 PC 启动 Debug Tool，点击"仿真"按钮

3. MockModem 启动 (UDP 45679)：
   → 发送 BEAM_CONFIG (0x02) 到设备 (UDP 4004)
     卫星经度=134°E, 频段=Ka

4. 设备 simulate_modem 收到 BEAM_CONFIG：
   → 解析卫星参数
   → 发布 modem_sat topic
   → trace 消费 → 初始化指向

5. 设备每 20ms：
   → 构造 REAL_TIME_REPORT (0xA0)
     (从 locate 拉 GPS/姿态，从 trace 拉波束角)
   → 发送到 MockModem (UDP 45679)

6. MockModem 收到 REAL_TIME_REPORT：
   → 解析姿态/波束/GPS
   → 计算 SNR (卫星几何 + 链路预算)
   → 发送 SNR_REPORT (0x01) 到设备 (UDP 4004)

7. 设备 simulate_modem 收到 SNR_REPORT：
   → 解析 SNR 值
   → 发布 modem_snr topic
   → trace 消费 → 更新波束指向

8. 循环 5-7，形成闭环：
   姿态 → 波束指向 → 偏轴角 → SNR → trace 调整 → 波束指向 → ...
```

### 6.2 雨衰注入

```
用户调雨衰滑块 (GUI)
  → MockModem.set_rain_fade(10.0 dB)
  → SNR_REPORT 中 SNR 值下降 10dB
  → 设备 trace 检测到 SNR 下降
  → trace 状态变化: TRACKING → RAIN_FADE
  → 如果 SNR < 阈值: 触发 SNR_BELOW_THRESHOLD 事件
  → Debug Tool 监控到状态变化（通过 debug v2）
```

### 6.3 遮挡注入

```
用户点"遮挡 5s" (GUI)
  → MockModem 暂停发送 SNR_REPORT（或发 SNR=-10dB）
  → 设备 trace 检测到 SNR 丢失
  → trace 状态变化: TRACKING → BLOCKAGE → LOCK_LOST 事件
  → 5s 后 MockModem 恢复发送
  → trace 重新搜星: LOCKING → TRACKING → LOCK_ACQUIRED 事件
```

## 7. 使用流程

### 7.1 设备端准备

```bash
# 1. 确保终端固件已集成 simulate_modem 模块
# 2. 终端上电，通过 shell 配置仿真模式
> sim_modem mode udp
> sim_modem port 4004
> sim_modem start

# 3. 确认 simulate_modem 已启动
> sim_modem status
simulate_modem: running, port=4004, remote=192.168.1.100:45679
```

### 7.2 Debug Tool 操作

```bash
# 1. 启动 Debug Tool
$ python3 -m satellite_debug_tool.main

# 2. 连接终端（debug v2 链路）
#    UDP 远端: 192.168.1.12:45678（设备 debug 端口）

# 3. 点击工具栏"仿真"按钮
#    → MockModem 自动启动（UDP 45679）
#    → 仿真面板显示在右侧第 4 列

# 4. 配置仿真参数
#    - 选择卫星（亚太6号 / 中星10号 / ...）
#    - 选择频段（Ku / Ka）

# 5. 观察
#    - Chart: snr 曲线实时滚动
#    - 状态面板: TRACE_MODE / LOCK_FLAG 变化
#    - 事件时间线: LOCK_ACQUIRED / LOCK_LOST 事件
#    - 仿真面板: SNR / 偏轴角 / 姿态

# 6. 测试异常场景
#    - 调雨衰滑块 → 观察 SNR 下降 → trace 状态变化
#    - 点"遮挡 5s" → 观察失锁 → 恢复
```

## 8. 验收标准

| # | 验收项 | 验证方法 |
|---|--------|----------|
| A1 | 设备端 simulate_modem 可通过 UDP 收发 IOT503 帧 | `sim_modem start` + MockModem 连接 |
| A2 | MockModem 发送 SNR → 设备 trace 消费 → snr 通道显示 | Debug Tool 观察 snr 曲线 |
| A3 | 雨衰注入 → SNR 下降 → trace 状态变化 | 调雨衰滑块 → 观察 TRACE_MODE |
| A4 | 遮挡注入 → 失锁 → 恢复 → 重新锁定 | 点遮挡 → 观察 LOCK_LOST/LOCK_ACQUIRED |
| A5 | 仿真面板布局正常，不挤压 chart/rpanel | GUI 截图验证 |
| A6 | 设备 trace 在仿真模式下正常搜星→锁定 | 观察 state 变化 |
| A7 | 全量测试通过 | pytest (490 passed) |
