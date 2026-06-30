# M13 Plan: Profile 语义层与能力声明

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-30 |
| 状态 | 已确认，进入实现 |
| 关联功能定义 | `doc/upper_pc_function_definition_vnext.md` |
| 验收文档 | `doc/M13_profile_semantics_acceptance.md` |
| 开发记录 | `doc/M13_profile_semantics_dev_log.md` |

## 1. 背景

当前上位机已经做到大部分 UI 由 profile 驱动，但仍有三类“隐式语义”：

| 场景 | 当前实现 | 问题 |
|------|----------|------|
| 地图轨迹 | Playback/Log 直接找 `gps_lat` / `gps_lon` 名称 | 新设备改名后地图不可用 |
| 3D 绑定 | AttitudeWidget 按 roll/pitch/yaw/ant_az/ant_el 名称匹配 | 多套姿态源或不同命名时不可控 |
| 模式切换 | LiveView 只对 `state_id == 0` 下发 `SET_TRACE_MODE` | 新设备 state_id 规划不同会失效 |

M13 的目标是把这些隐式约定收口到一个上位机语义层。M13-A 先补上位机本地语义模型和 JSON schema v2；用户已确认下位机也可以同步修改，因此本轮追加 M13-B：新增前向兼容的 `PROFILE_SEMANTICS` 扩展帧，让新固件可直接声明角色、能力和控制绑定。

## 2. 目标

1. 新增 profile 语义模型，能表达 channel/state/event/control/capability 的角色。
2. 保持旧设备、旧 SDB v2、旧 profile cache 可读。
3. UI 消费语义时优先读显式语义，缺失时回退到现有名称约定。
4. 移除新路径对 `state_id == 0`、`gps_lat/gps_lon` 等硬约定的依赖。
5. 为后续协议 v2.x 扩展保留清晰的 JSON/schema 草案。

## 3. 设计原则

- **不破坏现有 DEFINE**：M13-A 不改 `ChannelDefEntry` / `StateDefEntry` 的二进制编码；M13-B 通过新增可忽略的 `PROFILE_SEMANTICS` 帧扩展。
- **显式优先，约定兜底**：有语义字段就用语义字段，没有就继续按名称识别，保证旧数据仍可用。
- **语义归 profile，不归 UI**：UI 不应各自维护一套角色匹配规则；统一通过 `core/profile/semantics.py` 查询。
- **SDB 自包含**：如果 SDB header 内嵌 profile 带语义，离线回放必须能保留语义。
- **语义帧不阻塞握手**：上位机 ready 仍只依赖 META + 三张 DEFINE；语义帧迟到或缺失时使用 fallback。

## 4. 语义模型草案

### 4.1 Channel Role

建议先支持这些 role：

| role | 含义 | 首批消费者 |
|------|------|------------|
| `gps_lat` | WGS-84 纬度 | Playback/Log Map |
| `gps_lon` | WGS-84 经度 | Playback/Log Map |
| `gps_alt` | 高度，可选 | Map 后续扩展 |
| `gps_num_sv` | 参与定位卫星数 | Chart / Recording |
| `gps_speed` | GNSS 地速 | Chart / Recording |
| `gps_cog` | GNSS course over ground | Chart / Recording |
| `gps_vel_n` | GNSS 北向速度 | Chart / Recording |
| `gps_vel_e` | GNSS 东向速度 | Chart / Recording |
| `gps_vel_d` | GNSS 地向速度 | Chart / Recording |
| `gps_cog_std` | GNSS COG 标准差/精度 | Chart / Recording |
| `roll` | 横滚角 | Attitude |
| `pitch` | 俯仰角 | Attitude |
| `yaw` | 航向角 | Attitude |
| `antenna_az` | 天线实际方位角 | Attitude / Simulation |
| `antenna_el` | 天线实际俯仰或阵面角 | Attitude / Simulation |
| `target_az` | 目标方位角，可选 | 后续 3D |
| `target_el` | 目标俯仰角，可选 | 后续 3D |
| `snr` | 信噪比 | Dashboard / Simulation |
| `pointing_error` | 指向误差 | Dashboard / Simulation |

### 4.2 State Role

| role | 含义 | 首批消费者 |
|------|------|------------|
| `trace_mode` | 跟踪模式 ENUM | Dashboard mode buttons |
| `lock_flag` | 锁定状态 | Status/Dashboard |
| `gps_fix` | GPS 定位状态 | Status |
| `ins_ready` | INS 可用状态 | Status |
| `pll_locked` | PLL 锁定 | Status |
| `modem_connected` | 调制解调器在线 | Status |

### 4.3 Control Binding

对可点击 ENUM 状态字增加控制绑定：

```json
{
  "states": {
    "0": {
      "role": "trace_mode",
      "control": {
        "subcmd": "SET_TRACE_MODE",
        "value_from": "enum_value"
      }
    }
  }
}
```

首批只实现 `SET_TRACE_MODE`，但数据结构允许后续扩展到其它 CONTROL 子命令。

### 4.4 Device Capabilities

```json
{
  "capabilities": {
    "parameters": true,
    "ota": true,
    "sample_rate": true,
    "channel_enable_mask": true,
    "user_mark": true,
    "simulation": false
  }
}
```

首批可只做数据结构和查询函数，不一定立刻隐藏 UI 控件。

## 5. 存储格式

M13-A 使用 profile JSON 扩展字段，不改二进制 DEFINE：

```json
{
  "schema_version": 2,
  "hw_type": "afd01",
  "channel_table_ver": 1,
  "state_table_ver": 1,
  "event_table_ver": 1,
  "channels": [...],
  "states": [...],
  "events": [...],
  "semantics": {
    "channels": {
      "0": {"roles": ["roll"]},
      "1": {"roles": ["pitch"]},
      "8": {"roles": ["gps_lat"]},
      "9": {"roles": ["gps_lon"]}
    },
    "states": {
      "0": {
        "role": "trace_mode",
        "control": {"subcmd": "SET_TRACE_MODE", "value_from": "enum_value"}
      }
    },
    "capabilities": {
      "parameters": true,
      "ota": true
    }
  }
}
```

兼容策略：

- `schema_version == 1`：按旧格式读取，语义由 resolver 自动推断。
- `schema_version == 2`：读取 `semantics` 字段；字段缺失仍推断。
- 导出和录制 SDB 时写 v2 schema；必要时提供 `export_legacy` 不在首批范围。

## 6. 实施范围

### M13-A1: 数据模型与解析

- 新增 `core/profile/semantics.py`
  - `ChannelRole` / `StateRole` 常量或枚举
  - `ControlBinding`
  - `ProfileSemantics`
  - `SemanticResolver`
- 扩展 `DeviceProfile`，增加 `semantics` 字段。
- 扩展 `cache.py`：
  - 能读 schema v1 和 v2。
  - 能写 schema v2。
  - 语义字段 round-trip。

### M13-A2: 统一查询接口

在 `ProfileStore` 增加只读 helper：

- `find_channel_by_role(hw_type, role) -> ChannelDefEntry | None`
- `find_channels_by_role(hw_type, role) -> list[ChannelDefEntry]`
- `find_state_by_role(hw_type, role) -> StateDefEntry | None`
- `get_state_control_binding(hw_type, state_id) -> ControlBinding | None`
- `has_capability(hw_type, name, default=False) -> bool`

这些 helper 内部统一执行“显式语义 -> 名称 fallback”。

### M13-A3: UI 接入

- PlaybackView 地图检测：
  - 优先找 `gps_lat` / `gps_lon` role。
  - 找不到再按旧名称。
- LogView 地图检测：
  - 虚拟 profile 构造时可按列名自动生成 role。
  - 检测时仍通过 resolver。
- AttitudeWidget / LiveView：
  - auto_bind 优先用 roll/pitch/yaw/antenna_az/antenna_el role。
  - 找不到再按旧名称。
- Dashboard mode buttons：
  - `ModeButtonGroup` 保持 emit `(state_id, target_value)`。
  - LiveView 下发时查 `control_binding`；显式绑定为 `SET_TRACE_MODE` 时下发。
  - 旧 profile fallback：如果 `state_id == 0` 且无 binding，继续下发 `SET_TRACE_MODE`，保证旧设备不退化。

### M13-A4: 测试

新增/更新测试：

- `test_profile_semantics.py`
  - schema v1 fallback
  - schema v2 explicit roles
  - control binding round-trip
  - capabilities query
- `test_playback_view.py`
  - 非 `gps_lat/gps_lon` 命名但带 role 时地图可用。
- `test_log_view.py` 或现有 parser 测试
  - 日志列名生成虚拟 role。
- `test_attitude_pointing.py` 或新增轻量测试
  - role 优先绑定。
- `test_dashboard_control_binding.py`
  - trace_mode 不在 state_id 0 时仍能下发正确 frame。

### M13-B1: DEBUG v2 前向兼容扩展帧

新增设备到上位机方向帧：

| 项目 | 值 |
|------|----|
| Cmd | `0x0C PROFILE_SEMANTICS` |
| 方向 | D→H |
| 触发 | 启动、周期 DEFINE 广播、可选请求 |
| 握手影响 | 不参与 ready 判定 |

DATA 段：

```
table_ver u8
channel_count u8
channel[N]:
    channel_id u8
    role_count u8
    role[M]: len_u8 + utf8
state_count u8
state[N]:
    state_id u8
    role: len_u8 + utf8
    control_subcmd u8       # 0 表示无控制绑定
    control_value_from u8   # 0=enum_value
capability_count u8
capability[N]:
    name: len_u8 + utf8
    supported u8            # 0/1
```

对应可选请求子命令：

| sub_cmd | 名称 | payload |
|---------|------|---------|
| `0x13` | `REQUEST_PROFILE_SEMANTICS` | 无 |

兼容策略：

- 旧上位机遇到 `0x0C` 会按 RawFrame 忽略。
- 旧下位机不支持 `0x13` 时，上位机仍靠本地 fallback。
- 语义帧只声明能力，不替代原三张 DEFINE 表。

### M13-B2: 下位机注册 API

在 debug 组件增加设备无关 API：

- `debug_channel_add_role(channel_id, role)`
- `debug_state_set_role(state_id, role)`
- `debug_state_bind_control(state_id, subcmd, value_from)`
- `debug_capability_set(name, supported)`

本轮 afd01 / ufd45 profile 先声明 channel/state role 和 capability。由于当前 debug core 对 `SET_TRACE_MODE` 仍返回 `NOT_SUPPORTED`，下位机暂不声明 trace mode 的 control binding；上位机保留旧 profile 的 `state_id == 0` fallback，但对显式“有 role、无 control”的新语义按不可控处理。

## 7. 不在 M13 范围

- 不做 UI 里的语义编辑器。
- 不做 profile 手工导入导出的 GUI。
- 不把 `SET_TRACE_MODE` 在下位机核心里实现为真实控制；本轮只避免新增语义误报可控。

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| schema v2 导致旧 profile cache 不能读 | 新代码读 v1/v2；旧代码不保证读 v2，属于升级后行为 |
| 名称 fallback 与显式语义冲突 | 显式语义优先；测试覆盖冲突场景 |
| role 命名过早定死 | 使用字符串常量，先覆盖首批消费者，保留未知 role |
| UI 行为被 capability 隐藏误伤 | M13-A 只提供查询，不默认隐藏控件 |
| Dashboard 控制绑定误发命令 | 首批只允许白名单 `SET_TRACE_MODE`；未知 subcmd 不发送 |
| 新语义帧影响旧链路 | 新 cmd 不参与 ready，旧上位机忽略 RawFrame，旧下位机没有该帧也可 fallback |

## 9. 验收标准

详见 `doc/M13_profile_semantics_acceptance.md`。

## 10. 用户确认

用户已确认按计划开发，并明确下位机部分也可以直接修改。本轮按 M13-A + M13-B 同步推进。
