# 模拟对星仿真 — 修订实施方案 v2（证据锁定版）

> **本文取代** `simulation_plan.md`（mimo Phase 1 纯软件草案）与 `simulation_design.md`
> （mimo 硬件在环草案）。二者描述了两套互相矛盾的架构，且 SNR 建模与固件实际不符。
> 本文经 afd01 固件源码核对后定稿，作为唯一实施依据。
>
> 固件参考路径（只读，勿改）：
> `…/3-project_dev/3-satlite_comm_terminal/code/target/afd01`
> `…/code/shared/MiddleWare/DALib/device_driver/modem/iot503`
> `…/code/shared/MiddleWare/DALib/device_driver/antenna/ka256`

## 0. 已确认的架构决策

| 决策 | 选择 | 理由 |
|------|------|------|
| trace 状态机跑哪 | **设备固件**（硬件在环），PC 仅 mock modem/卫星 | 验证真实 trace.c 算法 |
| 桌面可测性 | 补 **PC fake-device** 复用 ScenarioEngine，单机自测后再上真机 | 不依赖固件即可回归 |
| 失指角来源 | **PC 独立解算**（姿态⊕波束指令 vs 真卫星方向） | 设备不能给自己批改作业 |
| SNR 标定 | **可调常数基准**（默认 16dB）− 各项真实损失 | 量级可控；损失项保真；丢弃虚假精度的链路预算 |
| 几何/扫描损失模型 | **移植固件**（bp 适配器 + ka256 多项式表），不另造 | 与固件坐标系/标定逐位一致 |

## 1. 固件核对结论（关键事实）

### 1.1 IOT503 协议 — MockModem 现状基本正确

核对 `iot503.c` / `iot503.h` 后：

- **线上字节序 = 大端**（parse `data[4]<<24|…`；TX 每字段 `mem_swap_bytes` 转大端）。
  MockModem 用 `>h/>f` **正确**。
- **cmd = 1 字节**（ARM EABI `-fshort-enums`，`iot503_cmd_t` 编成 1B）。MockModem 一致。
- **checksum** = `check_sum(data+1, len-3)`，覆盖 cmd→payload（不含 magic 与 2B 校验），
  累加和取低 16 位。MockModem `iot503_checksum(data[1:])` **一致**。
- **REAL_TIME_REPORT 实际类型 = `real_time_report_payload_tle_t`**（含 tle_mode），
  字段偏移 theta@31 / phi@33 / mode@35 / tle_mode@36 / status@37 / time@38，
  与 `decode_real_time_report` **逐字节吻合**。
- ⚠️ 唯一协议级 bug：`encode_beam_config` 的 `int(lon*100) & 0xFFFF` 对负经度令
  `struct.pack(">h")` 抛错 → 去掉 mask。

### 1.2 theta/phi 语义 — 当前 SNR 建模根本错误

- `iot503.h:108` `int16_t theta; // 波束离轴角 [0°~90°]*100`
- `modem.c:239` `report.theta = beam_ctrl.rx_angle[1]`（阵面 **EL**），`phi = rx_angle[2]`（阵面 **AZ**）
- `trace_antenna_adapter`：`el_is_zenith=true` → **EL = 天顶距**（离阵面法线的扫描角，0~90°）
- `antenna.c:161/167` 直接拿 `rx_angle[1]` 喂 `ka256_scan_loss_dB` 做**扫描损失**

→ **theta 是扫描角（离阵面法线），不是相对卫星的失指误差。**
当前 `mock_modem._compute_snr` 把它当失指灌进抛物面，且 HPBW 填 1.8°
（真实 `KA256_BEAM_WIDTH = 6.3°`），SNR 恒为地板值。

### 1.3 可移植的固件正向模型

- **扫描损失** `ka256_loss_table.c`：9 频段(17.70~21.20GHz) × 4 阶多项式 `a[5]`，
  Horner 求值，70° 饱和，`result<0→0` clamp。纯数据，头注释明确"不依赖 beampointing"。
- **几何** `trace_antenna_adapter.c`：`bp_antenna_phased_solve`（body→array，
  mount_yaw=90°，ZYX Tait-Bryan）+ Layer1/2。初等三角，可移植；
  实测锚点："下发 AZ=0 EL=30，主瓣目视机头右侧"。
- **antenna 层归一化**：`antenna_snr = modem_snr + scan_loss`（把扫描损失加回）→
  **MockModem 产生的原始 SNR 必须已扣掉扫描损失**，否则 antenna 层过补偿。

## 2. SNR 模型（定稿）

```
raw_snr = SNR_clear_boresight                       # 可调常数，默认 16 dB（晴空/对准/扫描0 上限）
          − ka256_scan_loss_dB(freq_ghz, 扫描角)     # 扫描角 = 设备上报 theta(=EL 天顶距)
          − 12 · (失指误差 / 6.3)²                    # HPBW = KA256_BEAM_WIDTH = 6.3°
          − rain_db − atmos_db − blockage_db
```

失指误差 = 设备上报波束 (theta,phi)=(EL,AZ) 与 PC 用移植 bp 管线算出的"真·应指 (EL,AZ)"
的球面夹角（el 为天顶距/极角）：

```
cos(sep) = cos(el₁)·cos(el₂) + sin(el₁)·sin(el₂)·cos(az₁−az₂)
```

- 跟踪稳定（失指 0）：`snr ≈ 16 − scan_loss(扫描角)`
- trace 偏 3°：再 `− 12·(3/6.3)² ≈ −2.7 dB`，trace 看到下降并纠偏 → 真闭环
- 遮挡：拉到地板（≈ −10 dB）→ 失锁

## 3. 落地步骤

### B0 — 移植固件正向模型（纯函数 + 单测，零 UI 风险，先做）

- `core/simulation/ka256_loss.py` — 移植 9×5 多项式表 + Horner + 饱和 + clamp；
  常量 `KA256_BEAM_WIDTH=6.3`、`KA256_HARD_LIMIT_DEG=70`、`KA256_MAX_SCAN_DEG=65`
- `core/simulation/beampointing.py` — 移植 `bp_geo_sat_view` / `bp_sat_view_to_body` /
  `bp_antenna_phased_solve`（afd01 mount 默认）+ `pointing_error()`
- 单测 `test_ka256_loss.py` / `test_beampointing.py`：固件锚点（AZ0/EL30 主瓣方向、
  0°扫描损失≈0、单调性、70°饱和）

### B1 — 重写 MockModem SNR（用 B0）

- `mock_modem._compute_snr` 改用 §2 模型；删 `StaticSatellite` / `AntennaModel` /
  `LinkBudget` / `SimulationEngine`（全被 B0 取代）
- 修 `encode_beam_config` 负经度 mask
- 线程安全：worker 加命令队列，GUI 线程只 push 参数；socket 仅 worker 线程碰

### B2 — PC fake-device（桌面自测替身）

- `tools/fake_device.py`（独立 CLI，`python -m tools.fake_device`）—— 默认放 tools/，
  与生产代码隔离（如需 UI"无设备演示"再迁 core/）
- **自带新的精简 FSM**（SEARCH→LOCK→TRACK→re-search）+ B0 几何：发 REAL_TIME_REPORT，
  消费 SNR 把波束朝卫星收敛，收 BEAM_CONFIG 设目标星，HB_CHECK/ACK
  > 更正：**不复用旧 ScenarioEngine** —— 它的契约是"自己产 SNR"，与 fake-device"消费 SNR"
  > 相反，强行改会废掉其 44 个测试。旧 ScenarioEngine 在 C 阶段作为死代码删除。
- `MockModem + fake_device` 单机闭环

### C — Bug 修复 + 收尾

- C1 遮挡按钮接通：`MockModem.inject_blockage(s)` → worker 窗口内停发/发地板值；
  `live_view._on_sim_blockage` 真正调用
- C2 SNR 量级：面板加 `SNR_clear_boresight` 输入框（默认 16dB）
- C3 补测试：`test_mock_modem.py`（协议往返、负经度、SNR 随失指/扫描单调）、loopback 集成
- C4 文档收口：本文 + 验收文档为准；mimo 两稿标注废弃

## 3.5 执行：委派 MiMo（任务文件已就绪）

每个步骤一个自包含任务文件（含精确契约/代码/golden 值），放 `.mimocode/tasks/`。
**串行派单**，每个产出 Claude review + 跑测后再派下一个：

| 步骤 | 任务文件 | 说明 |
|------|----------|------|
| B0 | `sim_B0_forward_model.md` | 移植几何+ka256（含验证过的完整 Python + golden 值，逐字转录） |
| B1 | `sim_B1_mockmodem.md` | 重写 SNR + 负经度修复 + 命令队列线程安全 |
| B2 | `sim_B2_fake_device.md` | fake-device 自带 FSM + loopback 集成测试 |
| C  | `sim_C_integration.md` | 遮挡接通 + SNR基准UI + 删死代码 + 文档收口 |

派单（示例）：
```bash
~/.claude/workflow/scripts/delegate.sh \
  .mimocode/tasks/sim_B0_forward_model.md xiaomi/mimo-v2.5-pro .
# B0 绿了再派 B1，依次类推
```
铁律：产出以 `git diff` 为准；git commit 只在 Claude；高危/坐标符号已由 Claude 在任务文件中
固化为"逐字转录"，MiMo 不得自行推导。

## 4. 不做（边界）

- 不引入 SGP4 / TLE 轨道（GEO 静态足够，Phase 2 再说）
- 不做精确 ITU-R P.618 雨衰（线性 dB 注入够测 trace）
- 不动固件（simulate_modem.c 由固件侧另行实现；本文只定 PC 侧契约）
