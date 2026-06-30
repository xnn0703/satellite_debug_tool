> ⚠️ 已废弃。以 `doc/simulation_plan_v2.md` + `doc/simulation_acceptance.md` 为准。

# 模拟对星仿真功能 — 实施计划

## 目标

在桌面端模拟卫星通信链路，实现闭环仿真：终端姿态 → 天线指向 → 卫星几何 → 链路预算 → SNR，用于 trace/locate 功能的离线测试与算法验证。

## 设计原则

- **先简后繁**：Phase 1 用简化几何模型（GEO 静态卫星），Phase 2 预留 SGP4 接口
- **GUI 内嵌**：仿真面板嵌入 LiveView，无需独立进程
- **零侵入**：SimulationEngine 生成与真实设备相同格式的帧，注入已有 `_on_data_received` 路径，下游（DataStore/Chart/Widget）零改动
- **数据可录制**：仿真数据可被 DataRecorder 录制为 .sdb，用于回放对比

## 分阶段计划

### Phase 1 — 简化几何闭环（本次实现）

#### S1: 卫星几何模型 `core/simulation/satellite_model.py`

- `StaticSatellite(longitude_deg, freq_ghz)` — GEO 卫星
- `look_angles(terminal_lat, terminal_lon) -> (azimuth_deg, elevation_deg)`
- 球面三角公式（余弦定理求仰角，正弦/余弦求方位角）
- 仰角 < 5° 标记为"遮挡"（建筑物/地形）

#### S2: 天线增益模型 `core/simulation/antenna_model.py`

- `AntennaModel(gain_max_dbi, beamwidth_deg)`
- `gain(off_axis_deg) -> float` — 抛物面近似 `G_max - 12*(θ/θ_3dB)^2`
- 最低限幅 -10 dBi（旁瓣地板）

#### S3: 链路预算 `core/simulation/link_budget.py`

- `LinkBudget(satellite_eirp_dbw, antenna_model, freq_ghz, noise_temp_k, bandwidth_mhz)`
- `compute_snr(off_axis_deg, rain_atten_db=0, atmos_atten_db=0) -> float`
- 公式: SNR = EIRP + G_rx - FSL - rain - atmos + 228.6 - 10*log10(BW) - 10*log10(T)
- FSL = 20*log10(d_m) + 20*log10(f_hz) + 20*log10(4π/c)
- 预设: Ku 波段 (12 GHz), Ka 波段 (20 GHz), EIRP/噪声温度按典型值

#### S4: 场景引擎 `core/simulation/scenario_engine.py`

- `ScenarioEngine(satellite_model, antenna_model, link_budget)`
- 内部状态机: `IDLE → SCANNING → LOCKING → TRACKING → (RAIN_FADE / BLOCKAGE) → RECOVERING`
- `tick(dt_s, attitude)` — 每帧调用，更新内部状态，输出 `(snr, state, events[])`
- 场景触发:
  - `start_scan()` — 开始搜星（扫描模式，SNR 波动大）
  - `inject_rain_fade(depth_db, duration_s)` — 注入雨衰
  - `inject_blockage(duration_s)` — 注入遮挡（失锁）
  - `set_manual_snr(db)` — 手动设定 SNR（调试用）

#### S5: 仿真引擎 `core/simulation/simulation_engine.py`

- `SimulationEngine(profile_spec, satellite_model, scenario_engine)`
- 定时器驱动（内部 QTimer 或外部 tick）
- 生成与 `device_simulator.py` 相同格式的帧:
  - `META_INFO` + 三张 `DEFINE`（用 afd01/ufd45 profile）
  - `DATA_REPORT` @100Hz — 姿态/指向/SNR/姿态角 从 scenario_engine 计算
  - `STATE_REPORT` @5Hz — TRACE_MODE/LOCK_FLAG/GPS_FIX 等
  - `EVENT_REPORT` — LOCK_ACQUIRED/LOCK_LOST/SNR_BELOW_THRESHOLD
  - `HEARTBEAT` @1Hz
- **姿态仿真**:
  - 基座摇摆: roll/pitch 用低频正弦 + 噪声
  - 天线跟踪: ant_az/ant_el 平滑趋近 tgt_az/tgt_el（一阶滞后）
  - 指向误差: err_az/err_el = tgt - ant（残余跟踪误差）
- `set_terminal_gps(lat, lon, alt)` — 设置终端位置
- `set_rain_fade(db)` — 设置雨衰深度
- 输出: `frame_ready: Signal(bytes)` — 原始帧字节，LiveView 接收

#### S6: GUI 仿真面板 `ui/simulation_panel_widget.py`

- 卫星选择: 经度输入 + 频段下拉（Ku/Ka）
- 终端 GPS: lat/lon/alt 输入框（默认南京新街口）
- 场景控制:
  - "搜星" 按钮 → scenario_engine.start_scan()
  - "雨衰" 滑块 (0-20 dB) → scenario_engine.inject_rain_fade()
  - "遮挡" 按钮 → scenario_engine.inject_blockage(5s)
  - "手动SNR" 输入框 → scenario_engine.set_manual_snr()
- 实时显示（只读）:
  - 当前状态: SCAN/LOCK/RAIN/BLOCK
  - 方位角 / 仰角 / 偏轴角
  - 天线增益 / SNR
- 信号: `config_changed(dict)` → SimulationEngine 更新参数

#### S7: LiveView 集成

- 工具栏新增 "仿真" toggle 按钮
- 切换逻辑:
  - **开启**: 禁用 Connect/Disconnect，创建 SimulationEngine，注入 profile，启动仿真
  - **关闭**: 停止仿真，恢复 Connect/Disconnect
- SimulationEngine.frame_ready → LiveView._on_data_received（走已有路径）
- 仿真面板放在 ControlPanelWidget 上方（或作为 QDockWidget 浮窗）

#### S8: 测试

- `tests/test_satellite_model.py` — 几何计算精度验证
- `tests/test_antenna_model.py` — 增益方向图边界条件
- `tests/test_link_budget.py` — 链路预算 SNR 计算
- `tests/test_scenario_engine.py` — 状态机转换 + 事件触发
- `tests/test_simulation_engine.py` — 帧生成 + 数据流完整性

### Phase 2 — 预留（后续升级）

- SGP4 轨道传播（多星、多普勒）
- 多星波束切换逻辑
- 移动终端轨迹回放（GPS log → 连续更新终端位置）
- ITU-R P.618 精确雨衰模型
- 天线方向图实测数据导入（.csv 查表）
- 仿真场景录制/回放（场景脚本）

## 验收标准

### Phase 1 验收

| # | 验收项 | 验证方法 |
|---|--------|----------|
| A1 | 卫星几何：南京→亚太6号(134°E) 方位角≈176°, 仰角≈52° | pytest 精度 ±1° |
| A2 | 天线增益：θ=0 时 = G_max, θ=θ_3dB 时 = G_max-3dB | pytest |
| A3 | 链路预算：Ku 波段典型场景 SNR 在 8-15 dB 范围 | pytest |
| A4 | 场景引擎：搜星→锁定 状态机转换正确，事件触发时序正确 | pytest |
| A5 | 仿真引擎：生成的帧可被 FrameReceiverV2 正确解码 | pytest 往返测试 |
| A6 | GUI：仿真面板可切换卫星/GPS/雨衰，实时显示 SNR 曲线 | 手动验证 |
| A7 | 集成：仿真数据可在 Chart 中实时滚动，姿态 3D 模型正确旋转 | 手动验证 |
| A8 | 录制：仿真数据可录制为 .sdb 并在 Playback Tab 回放 | 手动验证 |

## 文件清单

### 新增

```
satellite_debug_tool/core/simulation/__init__.py
satellite_debug_tool/core/simulation/satellite_model.py
satellite_debug_tool/core/simulation/antenna_model.py
satellite_debug_tool/core/simulation/link_budget.py
satellite_debug_tool/core/simulation/scenario_engine.py
satellite_debug_tool/core/simulation/simulation_engine.py
satellite_debug_tool/ui/simulation_panel_widget.py
satellite_debug_tool/tests/test_satellite_model.py
satellite_debug_tool/tests/test_antenna_model.py
satellite_debug_tool/tests/test_link_budget.py
satellite_debug_tool/tests/test_scenario_engine.py
satellite_debug_tool/tests/test_simulation_engine.py
```

### 修改

```
satellite_debug_tool/ui/live_view.py        — 新增仿真 toggle + 面板集成
```
