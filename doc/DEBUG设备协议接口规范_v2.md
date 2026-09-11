# DEBUG设备协议接口规范 v2

## 文档信息

| 项目 | 内容 |
|------|------|
| 协议版本 | v2.0 |
| 上一版本 | v1.0（`DEBUG设备协议接口规范.md`） |
| 发布日期 | 2026-04-16 |
| 最近修订 | 2026-08-30（0x2C 安装姿态读回与 AFD01 / AFD01C / ESA01 产品注册合同） |
| 兼容设备 | 任意实现本规范的 `device_type=0x0D` 设备（当前有 afd01 / afd01c / esa01 / ufd45，后续新型号无需改协议） |
| 适用上位机 | 实现 DEBUG protocol v2 及相应产品扩展的 `satellite_debug_tool` |

---

## 1. 版本变更摘要

相对 v1 的核心变化：

| 变化 | 动机 |
|------|------|
| 数据通道改为 **ID 寻址**，通道名通过一次性 `CHANNEL_DEFINE` 定义 | 单帧带宽从 ~300B 降至 ~110B（16通道），下位机 SPI/UDP 压力降至 1/5 |
| 新增 **状态字** 帧（`STATE_REPORT`），支持 BOOL / ENUM 离散量 | 锁星、GPS_FIX、PLL_LOCK 等原本丢弃的离散量可上报 |
| 新增 **事件** 帧（`EVENT_REPORT`），带 event_id + level + 可选文本 | 状态切换、错误等一次性事件单独通道，上位机可图形化标注 |
| 新增 **心跳** 帧（`HEARTBEAT`），含 CPU 负载 / 剩余堆 | 上位机可检测链路健康和下位机健康 |
| 新增 **META_INFO** 帧，启动时交换协议版本、固件版本、硬件型号 | 上位机据此做**设备自适应**，不同型号可各自一套通道/状态/事件 |
| 扩展 `CONTROL` 子命令（用户标记、请求重发定义、设置采样率、切换模式） | 双向调试能力 |
| CRC / 帧头 / 帧尾 / 设备类型 **保持不变** | 兼容现有 FrameReceiver 状态机骨架 |

**核心设计原则（v2 新确立）**：

> **协议与具体设备完全解耦**。通道、状态字、事件的 ID、名称、单位、分组、枚举值——
> **全部**由下位机通过 DEFINE 帧自描述。协议规范只定义**编码格式**，不规定"哪个 ID 必须是什么含义"。
> 这样 afd01 / afd01c / esa01 / ufd45 / 未来新型号各自发各自的表，上位机可按自描述 Debug profile 呈现。
> 客户 Product Service、OTA 和试产准入仍必须使用显式注册的产品身份、协议版本和能力合同，不能从 Debug profile 名称推断。

**兼容性**：v2 **完全取代 v1**，上下位机同步切换（本项目决定放弃 v1 兼容层以简化代码路径）。设备启动即发 `META_INFO`，上位机校验 `protocol_ver == 0x02`，否则断连并提示升级固件。

---

## 2. 物理层与传输层

同 v1：

- **UDP**（当前已登记设备的常用实现）：端口 4004
- **串口**：115200 bps，8N1
- 字节序：**小端（Little Endian）**

建议上位机同时打开 UDP 与串口，任一路收到 `META_INFO` 即完成握手。

---

## 3. 通用帧格式（不变）

```
┌────────┬──────────┬──────────┬──────────┬────────┬────────┬────────┐
│  帧头   │ 设备类型  │ 命令类型  │ 数据长度  │  数据  │  CRC   │  帧尾  │
│ 2字节   │ 1字节     │ 1字节     │ 2字节    │ 可变   │ 2字节   │ 1字节  │
│ AA 55   │ 0D       │ 01..31*  │ 小端序   │        │ 小端序  │ EE    │
└────────┴──────────┴──────────┴──────────┴────────┴────────┴────────┘
```

`*` 命令空间分段分配：通用 Debug/GNSS/Tracking 为 `0x01..0x11`，Product Service（AFD01 / AFD01C / ESA01）为
`0x20..0x2C`，XESA01 Orbit 为 `0x30..0x31`；中间保留值不因图中范围而成为有效命令。

CRC16-CCITT（poly=0x1021, init=0xFFFF），计算范围：帧头起至数据末尾（不含 CRC 和帧尾）。

`DATA` 段最大长度：**1536 字节**。早期 v2 版本按 512 字节设计，后续曾上调到 1024 字节以兼容 esa01 27 路 `CHANNEL_DEFINE` 长帧；现统一以 1536 字节作为上位机接收与协议测试上限。afd01 / ufd45 等旧设备继续发送较短帧时仍兼容。

> 下位机调整点：若设备端支持 1536 字节 DATA 长帧，`DEBUG_MAX_FRAME_LENGTH` 应按 **至少 1548 字节** 预留。这是 `MAX_DATA_LENGTH + 12` 的保守缓冲值；实际线上帧固定开销为帧头 2 B、设备类型 1 B、命令 1 B、长度 2 B、CRC 2 B、帧尾 1 B，合计 9 B，最大实际 wire 长度为 1545 B。

---

## 4. 命令类型总览

| Cmd | 名称 | 方向 | 触发 | 典型频率 |
|-----|------|------|------|----------|
| 0x01 | `DATA_REPORT`      | D→H | 周期 | 50~100 Hz |
| 0x02 | `COMMAND_RESPONSE` | D→H | 响应上位机控制 | 按需 |
| 0x03 | `CONTROL`          | H→D | 上位机控制 | 按需 |
| 0x04 | `META_INFO`        | D→H | 启动/请求时 | 一次 |
| 0x05 | `CHANNEL_DEFINE`   | D→H | 启动 + 周期 | 0.2 Hz |
| 0x06 | `STATE_DEFINE`     | D→H | 启动 + 周期 | 0.2 Hz |
| 0x07 | `EVENT_DEFINE`     | D→H | 启动 + 周期 | 0.2 Hz |
| 0x08 | `STATE_REPORT`     | D→H | 状态变化 或 周期 | 5 Hz |
| 0x09 | `EVENT_REPORT`     | D→H | 事件触发 | 按需 |
| 0x0A | `HEARTBEAT`        | D→H | 周期 | 1 Hz |
| 0x0B | `PARA_TABLE_REPORT`| D→H | 参数表请求响应 | 按需 |
| 0x0C | `PROFILE_SEMANTICS`| D→H | 启动 + 周期 / 请求 | 0.2 Hz |
| 0x0D | `GNSS_SKY_REPORT`   | D→H | GSV 完整 talker 快照 | 约 1 Hz |
| 0x0E | `GNSS_CNR_REPORT`   | D→H | RANGECMPB 逐信号 C/N₀ 分片 | 约 1 Hz |
| 0x0F | `GNSS_SAT_REPORT`   | D→H | MG902 NAV-SAT 分片 | 约 1 Hz |
| 0x10 | `GNSS_SIGNAL_REPORT`| D→H | MG902 NAV-SIG 分片 | 约 1 Hz |
| 0x11 | `TRACKING_SIMULATION`| H→D | Debug Tracking 场景仿真 | 50 Hz / 按需 |
| 0x20 | `SERVICE_IDENTITY` | D→H | 产品身份 | 接入时 + 0.2 Hz |
| 0x21 | `SERVICE_FAST_STATE` | D→H | 客户实时状态 | 默认 10 Hz，可配 1~20 Hz |
| 0x22 | `SERVICE_SLOW_STATE` | D→H | 位置/RF 回读 | 1 Hz |
| 0x23 | `SERVICE_COMPONENT_HEALTH` | D→H | 部件健康 | 1 Hz |
| 0x24 | `SERVICE_CAPABILITIES` | D→H | 控制能力 | 接入时 + 0.2 Hz |
| 0x25 | `SERVICE_CONTROL_REQUEST` | H→D | 类型化客户控制 | 按需 |
| 0x26 | `SERVICE_CONTROL_RESPONSE` | D→H | 带 request_id 的精确响应 | 按需 |
| 0x27 | `SERVICE_LINK_DETAIL` | D→H | Modem、本振和卫星信息 | 1 Hz |
| 0x28 | `SERVICE_RF_LOCK_STATUS` | D→H | 时钟/收发本振锁定 | 与快速状态同频 |
| 0x29 | `SERVICE_HARDWARE_IDENTITY` | D→H | MCU UID 与实际 MAC | 接入时 + 0.2 Hz |
| 0x2A | `SERVICE_NAV_SOURCE_INFO` | D→H | 导航源角色与外部 INS 能力 | 接入时 + 0.2 Hz |
| 0x2B | `SERVICE_EXTERNAL_INS_DIAGNOSTICS` | D→H | 已配置外部 INS 的类型化诊断 | 1 Hz |
| 0x2C | `SERVICE_MOUNT_STATUS` | D→H | 设备安装姿态、期望/读回 RBV | 接入时 + 1 Hz |
| 0x30 | `ORBIT_REQUEST` | H→D | XESA01 TLE 目录、预测、选星和上传请求 | 按需 |
| 0x31 | `ORBIT_REPORT` | D→H | 带 request_id 的 Orbit 分页响应/确认 | 按需 |

**定义帧（0x04/0x05/0x06/0x07/0x0C）**：下位机启动后立刻全量发送，之后每 5 秒重发一次（处理 UDP 丢包 / 上位机后接入）。上位机也可主动 `CONTROL` 请求重发。`PROFILE_SEMANTICS` 是 M13 扩展帧，不参与握手 ready 判定；旧上位机可忽略，旧下位机缺失时上位机按名称 fallback。

---

## 5. 命令详细定义

### 5.1 `DATA_REPORT` (0x01) — 数据通道上报

**用途**：连续物理量（姿态、SNR、指向角、PID 中间量等）。

**DATA 段格式**：

```
timestamp      uint32      设备启动后毫秒数
channel_count  uint8       本帧携带的通道数 (1~64)
channel[N]     { uint8 channel_id; float32 value; }   每条 5 字节
```

**帧长估算**：`9 + 4 + 1 + 5×N + 2 + 1 = 17 + 5N`。N=64 时 337 字节；100 Hz 时 33.7 KB/s。

**下位机实现建议**：
- 按 `channel_id` 自增顺序一次打包全部通道，避免分帧
- 某通道数据暂无（如 trace 未启动）时可整帧不发，不要发 nan

### 5.2 `COMMAND_RESPONSE` (0x02) — 命令响应

同 v1。

```
code     uint8      响应码（见 §6）
msg[]    utf8       可选，最长 63 字节
```

当 Profile 声明 `command_response_context` 能力时，参数响应在成功和失败时都必须
携带可归属上下文：`PARA_SET=<name>` 或 `PARA_RESET=<result>`。上位机只消费与
当前参数事务匹配的响应；Debug、OTA、其他参数或无上下文响应不能结束当前事务。

### 5.3 `CONTROL` (0x03) — 上位机控制（H→D）

**v2 扩展为子命令结构**：

```
sub_cmd   uint8
payload   bytes      长度由 sub_cmd 决定
```

**子命令表**：

| sub_cmd | 名称 | payload | 说明 |
|---------|------|---------|------|
| 0x01 | `DEBUG_ENABLE`         | `bool (1B)`                | 开/关 Debug 总输出 |
| 0x02 | `REQUEST_META_INFO`    | —                          | 请求重发 0x04 |
| 0x03 | `REQUEST_CHANNEL_DEFINE`| —                         | 请求重发 0x05 |
| 0x04 | `REQUEST_STATE_DEFINE` | —                          | 请求重发 0x06 |
| 0x05 | `REQUEST_EVENT_DEFINE` | —                          | 请求重发 0x07 |
| 0x06 | `USER_MARK`            | `u16 mark_id + u8 len + utf8 text` | 上位机打标记，下位机以 EVENT_REPORT(event_id=0xFFFF) 回传，用于曲线对齐 |
| 0x07 | `SET_SAMPLE_RATE`      | `u16 hz`                   | 设置 DATA_REPORT 频率（5~200Hz），超范围下位机拒收并 resp=PARAM_ERROR |
| 0x08 | `SET_TRACE_MODE`       | `u8 mode`                  | 切换跟踪模式，mode 定义同固件 `trace_mode_t` |
| 0x09 | `CHANNEL_ENABLE_MASK`  | `u32 bitmask_lo + u32 bitmask_hi` | 仅上报置位通道（节省带宽） |
| 0x0A | `RESET_STATS`          | —                          | 复位下位机统计（丢包、错误计数等） |
| 0x0B | `REQUEST_PARA_TABLE`   | —                          | 请求参数表，设备以 `PARA_TABLE_REPORT` 响应 |
| 0x0C | `PARA_SET`             | `name_len + name + value_len + value` | 设置参数 |
| 0x0D | `PARA_RESET`           | —                          | 恢复参数默认值 |
| 0x0E | `OTA_BEGIN`            | `file_size u32 + name_len + filename` | OTA 开始 |
| 0x0F | `OTA_DATA`             | `seq u16 + chunk[1..1021]` | OTA 数据块；chunk 上限固定为 1021 B，不随 DEBUG 长帧上限变化 |
| 0x10 | `OTA_END`              | `crc32 u32`                | OTA 结束校验 |
| 0x11 | `OTA_ABORT`            | —                          | OTA 中止 |
| 0x12 | `DEVICE_REBOOT`        | —                          | 请求设备重启 |
| 0x13 | `REQUEST_PROFILE_SEMANTICS` | —                      | 请求重发 `PROFILE_SEMANTICS` |

`OTA_DATA` 的 payload 为 2 B 序号加 1..1021 B 数据块；当前上位机通常按 512 B 分片，最后一片可更短。
1021 B 是独立于 `MAX_DATA_LENGTH=1536` 的兼容上限，不得因长帧能力自动增大。
`seq` 是 `u16`，单次传输最多 65536 片；按当前 512 B 分片时，上位机允许的镜像上限为
33,554,432 B。选包阶段必须拒绝超限镜像，编码层必须拒绝超出 `0..65535` 的序号。

Device OTA 的 `COMMAND_RESPONSE` 上下文分别为 `OTA_BEGIN=...`、`OTA_DATA=<seq>` 和
`OTA_END=...`。上位机只消费当前阶段且序号匹配的响应；迟到的参数、Debug 或其他
OTA 阶段响应不能结束当前事务。`OTA_END=VERIFIED` 只证明传输镜像已在设备端通过
完整性校验；上位机仍必须将回机身份与目标固件版本分别确认。

### 5.4 `META_INFO` (0x04) — 元信息

**DATA 段格式**：

```
protocol_ver   uint8          本协议版本号（v2 = 0x02）
fw_ver_len     uint8
fw_ver         utf8            固件版本字符串，如 "afd01-1.3.2"
hw_type_len    uint8
hw_type        utf8            硬件型号字符串，如 "afd01"
device_sn_len  uint8
device_sn      utf8            设备序列号
```

**触发**：上电/启动后发 1 次；上位机可通过 `CONTROL.REQUEST_META_INFO` 再请求。

### 5.5 `CHANNEL_DEFINE` (0x05) — 数据通道定义表

**用途**：一次性告诉上位机每个 `channel_id` 的名称、单位、分组、显示范围。之后 `DATA_REPORT` 只发 ID，大幅省带宽。

**DATA 段格式**：

```
table_ver      uint8          表版本号，固件内常量，内容变化时递增
channel_count  uint8
channel[N]:
    channel_id   uint8         0..63
    data_type    uint8         0x01=float32 (v2 唯一支持)
    group_id     uint8         Y 轴分组 ID（上位机分离绘制）
    flags        uint8         bit0=default_visible; bit1=critical（Dashboard 必显）
    name_len     uint8
    name         utf8          通道名，最大 31 字节
    unit_len     uint8
    unit         utf8          单位，如 "dB" "°" "m"，最大 15 字节
    display_min  float32       建议显示下界
    display_max  float32       建议显示上界
```

**group_id 约定**（推荐）：

| group_id | 含义 | 建议 Y 轴 |
|----------|------|-----------|
| 0 | 姿态角 | -180° ~ 180° |
| 1 | 波束指向 / 天线角 | 0 ~ 360° |
| 2 | 信号质量 | 0 ~ 60 dB |
| 3 | PID / 误差 | 自动 |
| 4 | 位置 / GPS | 自动 |
| 5 | 其它 | 自动 |

**容量边界**：注册 ID 范围为 0..63；整张定义表必须放入 1536 字节 DATA 段。
条目大小随名称和单位长度变化，设备 profile 注册后必须验证序列化结果非 0。当前 AFD01
31 个已注册通道的定义表约 774 字节。

### 5.6 `STATE_DEFINE` (0x06) — 状态字定义表

**用途**：定义离散状态量，上位机用指示灯 / 标签显示。

**DATA 段格式**：

```
table_ver    uint8
state_count  uint8
state[N]:
    state_id   uint8
    state_type uint8        0=BOOL, 1=ENUM
    flags      uint8        bit0=critical (Dashboard 必显); bit1=inverse（正常=0 时显红）
    name_len   uint8
    name       utf8
    enum_count uint8        BOOL 时为 0；ENUM 时为选项数
    enum[M]:                仅 ENUM 类型
        value          uint8
        level          uint8     0=INFO(绿), 1=WARN(黄), 2=ERROR(红), 3=NEUTRAL(灰)
        name_len       uint8
        name           utf8       如 "SCAN_GLOBAL", "LOCK"
```

**注意**：状态字 ID 和含义由**下位机自定**，不同设备型号（afd01 / ufd45 / ...）可以有完全不同的状态字集合。上位机通过 `META_INFO.hw_type` 识别设备类型，按 DEFINE 表动态渲染 UI。附录 §8 给出 afd01 参考实现（**仅作示例，非协议强制**）。

当前 AFD01 `STATE_DEFINE` 序列化后为 1170 字节（包含完整 owner wire 枚举），属于 1024 字节
历史上限无法承载、但在
1536 字节上限内的典型 DEFINE 长帧；设备 profile 变化后仍必须以实际序列化长度校验。

### 5.7 `EVENT_DEFINE` (0x07) — 事件定义表

```
table_ver   uint8
event_count uint8
event[N]:
    event_id  uint16        ID 编码见 §8（按子系统分段）
    level     uint8          0=DEBUG 1=INFO 2=WARN 3=ERROR
    name_len  uint8
    name      utf8            "LOCK_ACQUIRED" "TLE_PARSE_FAIL" 等短名
```

### 5.8 `STATE_REPORT` (0x08) — 状态字上报

**触发策略**（下位机侧实现要点）：
- 任一状态字变化时 **立即上报** 该条
- 周期 1~5 Hz **全量重发** 一次（处理丢包，保证上位机重连后能拿到完整状态）

**DATA 段格式**：

```
timestamp    uint32
state_count  uint8
state[N]:
    state_id  uint8
    value     uint8         BOOL: 0/1;  ENUM: 枚举值
```

每条 2 字节，16 条全量 = 32 字节。

### 5.9 `EVENT_REPORT` (0x09) — 事件上报

**DATA 段格式**：

```
timestamp   uint32
event_id    uint16
payload_len uint8           0~200
payload     bytes           可选附加信息（UTF-8 文本或结构化数据）
```

**上报时机**：事件发生时立即上报；非周期性；同一事件 1 秒内不应重复上报（下位机做去重）。

**上位机处理**：
- 写入事件时间线
- 在曲线上画垂直标记（颜色随 level）
- 若 level≥WARN，闪烁状态栏

### 5.10 `HEARTBEAT` (0x0A) — 心跳

```
uptime_ms       uint32
cpu_load        uint8         0~100 %
free_heap       uint32        剩余堆（字节）
rx_frame_rate   uint16        从上位机收到的帧率（用于双向链路诊断）
tx_frame_rate   uint16        向上位机发送的帧率
reserved        uint32
```

上位机若 **3 秒未收到心跳**，应视为下位机断线。

### 5.11 `PROFILE_SEMANTICS` (0x0C) — Profile 语义扩展

**用途**：在不修改 `CHANNEL_DEFINE` / `STATE_DEFINE` 二进制格式的前提下，补充通道角色、状态角色、控制绑定和设备能力。上位机应优先使用本帧中的显式语义；缺失时继续按历史名称约定 fallback。

**兼容性**：

- 新上位机收到本帧后写入 profile JSON schema v2 / SDB header。
- 旧上位机遇到未知 `cmd=0x0C` 应忽略为 RawFrame。

**DATA 段格式**：

```
table_ver      uint8
channel_count  uint8
channel[N]:
    channel_id   uint8
    role_count   uint8
    role[M]:     uint8 len + utf8 role

state_count    uint8
state[N]:
    state_id             uint8
    role                 uint8 len + utf8 role
    control_subcmd       uint8      0=无控制绑定；非 0 时对应 CONTROL 子命令
    control_value_from   uint8      0=enum_value

capability_count uint8
capability[N]:
    name       uint8 len + utf8
    supported  uint8      0/1
```

首批推荐 role：

| 类型 | role |
|------|------|
| channel | `gps_lat`, `gps_lon`, `gps_alt`, `gps_num_sv`, `gps_speed`, `gps_cog`, `gps_vel_n`, `gps_vel_e`, `gps_vel_d`, `gps_cog_std`, `roll`, `pitch`, `yaw`, `internal_ins_yaw`, `antenna_az`, `antenna_el`, `target_az`, `target_el`, `snr`, `pointing_error` |
| state | `trace_mode`, `lock_flag`, `gps_fix`, `ins_status`, `ins_ready`, `internal_ins_state`, `internal_ins_yaw_reference`, `pll_locked`, `modem_connected` |
| capability | `parameters`, `ota`, `sample_rate`, `user_mark`, `channel_enable_mask`, `gnss_sky_report`, `gnss_cnr_report` |

下位机只有在实际支持某控制子命令时，才应声明对应 `control_subcmd`；例如当前 debug core 尚未实现 `SET_TRACE_MODE` 时，应只声明 `trace_mode` role，不声明 control binding。

内部 INS 航向必须与业务绝对航向分开：

- `yaw`：业务主输出；新固件仅在绝对航向有效时更新。
- `internal_ins_yaw`：内部滤波器实时航向，允许在无外部航向观测时作为相对航向显示。
- `internal_ins_yaw_reference`：`0=UNAVAILABLE`、`1=RELATIVE`、`2=ABSOLUTE`。上位机只有在
  `RELATIVE` 时才用 `internal_ins_yaw` 驱动诊断 3D；`ABSOLUTE` 优先使用业务 `yaw`。
- `ABSOLUTE` 表示滤波器已经建立过北向参考，不等同于当前仍有外部航向量测。来源失效后可继续
  惯性保持绝对参考；是否仍被外部修正应结合来源/退化状态判断。
- 旧 v2 固件没有该 state 时，上位机保持历史 `yaw` 绑定，不推断相对/绝对属性。

### 5.12 `GNSS_SKY_REPORT` (0x0D) — GSV 天空快照

```text
version          u8 = 1
timestamp_ms     u32 LE
talker           char[2]
total_visible    u8
satellite_count  u8
flags            u8          bit0=truncated
satellite[N]:
    prn             u16 LE
    elevation_deg   i8
    azimuth_deg     u16 LE
    snr             u8
    valid_flags     u8       bit0=elevation, bit1=azimuth, bit2=snr
```

该帧只承载 GSV 的天空位置与单值 SNR。它不得用于生成多个频点的伪 C/N₀。

### 5.13 `GNSS_CNR_REPORT` (0x0E) — RANGECMPB 逐信号 C/N₀

```text
version             u8 = 1
timestamp_ms        u32 LE
report_id           u16 LE
chunk_index         u8
chunk_count         u8
total_observations  u16 LE
observation_count   u8
flags               u8       bit0=source_truncated
observation[N]:
    system             u8
    prn                u8
    signal_type        u8
    cn0_dbhz           u8
    tracking_state     u8
    lock_flags         u8    bit0=phase, bit1=code, bit2=prn, bit3=primary
    glo_freq_channel   u8    非 GLONASS 为 0xFF
```

- 每片最多 128 条，完整 report 最多 256 条，因此当前 `chunk_count` 最大为 2。
- 上位机必须按 `(timestamp_ms, report_id)` 隔离分片；新 report 到达时丢弃未完成旧 report，晚到的旧时间戳分片不得反向覆盖新 report。
- C/N₀ 是设备从 RANGECMPB 恢复后的整数 dB-Hz，不再缩放，也不从 GSV SNR 换算。
- 协议版本仍为 `0x02`。旧上位机把 0x0D/0x0E 当 RawFrame 忽略；它们与 CONTROL 内同值子命令互不冲突。
- 本帧不参与握手 ready 条件，丢失或旧固件不发送时不影响基本调试。

### 5.14 `GNSS_SAT_REPORT` (0x0F) — MG902 NAV-SAT

本帧与 0x10 使用相同的 14 字节分片头。`source` 固定定义为
`0=UNKNOWN, 1=MG902, 2=BYNAV, 3=MANUAL, 4=MS6222`。

```text
version        u8 = 1
source         u8
timestamp_ms   u32 LE
report_id      u16 LE
chunk_index    u8
chunk_count    u8
total_records  u16 LE
record_count   u8
flags          u8
satellite[N]:
    system         u8
    sv_id          u8
    cn0_dbhz       u8
    elevation_deg  i8
    azimuth_deg    u16 LE
    raw_sat_flags  u32 LE
```

### 5.15 `GNSS_SIGNAL_REPORT` (0x10) — MG902 NAV-SIG

```text
common_header  14B，同 0x0F
signal[N]:
    system          u8
    sv_id           u8
    raw_signal_id   u8
    freq_id         i8      GLONASS 归一化频槽 -7..+6；非 GLONASS 固定 -128
    cn0_dbhz        u8
    quality_ind     u8
    corr_source     u8
    iono_model      u8
    pr_res_0p1m     i16 LE
    raw_sig_flags   u16 LE
```

- 0x0F/0x10 每片最多 64 条、完整 report 最多 92 条，因此 `chunk_count` 最大为 2。
- `freq_id` 不保留 UBX 的原始 `freqId=slot+7`：GLONASS 上报前减 7，线上值为
  `-7..+6`；非 GLONASS 信号统一写 `-128`，不得解读为频槽。
- 上位机按 `(source, timestamp_ms, report_id)` 重组；source 变化时清除上一接收机的当前快照和未完成分片。
- Bynav 0x0D/0x0E 的 `signal_type` 属于 `UG016` namespace；MG902 的 `raw_signal_id` 属于 `UBX_M9` namespace，禁止交叉解释。
- `GNSS_SAT_REPORT.azimuth_deg=0xFFFF` 是“方位未知”哨兵。固件在 NAV-SAT 原始 elevation/azimuth
  越界时写入该值；接收端必须保留记录，但不得把它取模成有效方位。
- NAV-SAT 只驱动天空图。上位机仅绘制 `0<=elevation_deg<=90` 且
  `0<=azimuth_deg<=360` 的记录；越界值表示当前没有可用天空位置，仍可保留为接收机原始记录，
  但不得投影或计入可绘星数。
- 只有 NAV-SIG 才能生成逐信号/逐频段柱图。MG902 的 `quality_ind=4..7` 单独表示信号已锁定；
  在已锁定基础上 `cn0_dbhz>0` 才表示 C/N0 可进入柱图和有效值统计。C/N0 为 0 不得清除
  `LOCK`，但也不得生成零高柱。
- `raw_sig_flags` bit3/bit4/bit5 分别为 `prUsed/crUsed/doUsed`，仅说明对应观测是否参与当前解算；
  `LOCK` 与 `USED` 是两个独立维度，未参与解算的锁定信号仍应显示 C/N0。
- 旧上位机把 0x0F/0x10 当 RawFrame 忽略；新上位机继续兼容 0x0D/0x0E 和旧 SDB。

### 5.16 产品服务扩展 (0x20~0x2C)

**用途**：为客户工作台提供稳定的产品语义。该扩展复用 v2 帧包络，但不依赖动态
`CHANNEL_DEFINE/STATE_DEFINE` 名称；工程 Debug 与产品服务可以同时存在。AFD01、AFD01C 与 ESA01 是
当前正式登记的客户产品：AFD01 兼容 service protocol 2~8，AFD01C 只使用 protocol 8，ESA01 使用完整的 protocol 6。
其他 `hw_type` 不支持客户 Product Service，收到 0x25 必须明确返回不支持，不能靠同名 Debug
字段猜测产品能力。能力差异必须由 `valid_mask`、极化掩码和 feature flags 明确声明。

所有多字节整数和 `float32` 均为小端。每个产品服务 payload 首字节为 `schema`，当前固定为
`1`；接收端必须拒绝未知 schema，不能按 schema 1 强行解析。遥测帧的 `timestamp_ms` 是设备
启动后毫秒数。`valid_mask` 中未置位的字段必须显示为不支持/不可用，数值 0 仍是合法值。

#### 5.16.1 `SERVICE_IDENTITY` (0x20)

```text
schema              u8 = 1
timestamp_ms        u32
valid_mask          u32       bit0=model, bit1=serial, bit2=main firmware,
                              bit3=boot firmware, bit4=service protocol
model               u8 len + utf8
serial_number       u8 len + utf8
main_firmware       u8 len + utf8
boot_firmware       u8 len + utf8
service_protocol    u8
```

AFD01 序列号由参数 `DeviceType` 与 10 位生产后缀 `dev_sn` 组合，例如
`AFD01-202607N001`。未写入合法后缀时 bit1=0 且字符串为空，不得用 MCU UID 冒充生产 SN。
当前未取得 boot 版本时 bit3=0，空字符串不得当成有效版本。AFD01 与 AFD01C 当前使用
`service_protocol=8`，ESA01 使用 `service_protocol=6`；ESA01 无权威生产 SN 时必须清除 bit1
并发送空字符串。版本 8 表示支持 operation 5 和可选的 0x2C；版本 7 明确控制响应是
accepted 证据、完成必须等待后续读回；版本 6 表示支持可选的 0x2A/0x2B，版本 5 表示支持可选的 0x29，
版本 4 表示支持可选的 0x28，版本 3 表示
支持可选的 0x27，版本 2 仅包含 0x20~0x26。该字段不改变外层 Debug v2 的
`META_INFO.protocol_ver`。

#### 5.16.2 `SERVICE_FAST_STATE` (0x21)

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
control_mode, tracking_phase, locked   u8, u8, u8
navigation_state, gnss_fix, tx_enabled u8, u8, u8
roll, pitch, yaw                       float32 x3, deg
beam_az, beam_el, snr                  float32 x3, deg/deg/dB
```

`valid_mask` bit0..11 依次对应上述 12 个业务字段。枚举约定：

`tx_enabled` 表示最终 TX gate GPIO 的 MCU 引脚读回；只有 gate 写入并读回一致时才置 bit5。该字段不表示
阵面或 PA 已应用，也不构成物理 RF 输出证据。

- `control_mode`: `0=AUTO, 1=MANUAL`，其他值为 UNKNOWN。
- `tracking_phase`: `0=STANDBY, 1=ACQUIRING, 2=FINE_TRACKING, 3=LOCKED, 4=REACQUIRING, 5=FAULT`。
- `navigation_state`: `0=UNAVAILABLE, 1=INITIALIZING, 2=ALIGNING, 3=READY, 4=DEGRADED, 5=FAULT`。
- `gnss_fix`: 与 §5.11 `gps_fix` role 一致：`0=NO_FIX, 1=2D, 2=3D, 3=RTK_FIXED,
  4=DGNSS, 5=RTK_FLOAT, 6=STALE`。

#### 5.16.3 `SERVICE_SLOW_STATE` (0x22)

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
latitude, longitude, altitude          float32 x3, deg/deg/m
rx_frequency, tx_frequency             float32 x2, MHz
rx_polarization, tx_polarization       u8, u8
tx_enabled                             u8
```

`valid_mask` bit0..7 依次对应纬度、经度、高度、接收频点、发射频点、接收极化、发射极化和
发射使能。位置位只有在 `GPS_FIX=2D..RTK_FLOAT` 且最近位置更新时间不超过 3000 ms 时才可置 1；
`NO_FIX/STALE` 时允许保留 payload 数值，但不得置有效位。
其中 bit7 与 FAST bit5 使用同一 TX gate GPIO 读回事实，不表示物理 RF 输出。

产品服务极化枚举固定为 `0=VERTICAL, 1=HORIZONTAL, 2=LEFT_CIRCULAR,
3=RIGHT_CIRCULAR`。AFD01、AFD01C 与 ESA01/503 阵面当前只声明并接受 2/3；设备端负责与内部阵面枚举转换，禁止把产品
服务值 2/3 直接写入阵面驱动。

#### 5.16.4 `SERVICE_COMPONENT_HEALTH` (0x23)

```text
schema, timestamp_ms                 u8, u32
component[3]                         converter, tx_array, rx_array
    valid_mask                       u8    bit0=online, bit1=temperature,
                                           bit2=voltage, bit3=version
    online                           u8
    temperature_c, voltage_v         float32, float32
    version                          u32
```

各部件独立使用有效位。当前 AFD01 变频板只保证 online/temperature；无来源的电压和版本必须
保持未置位。阵列板字段由阵面健康快照提供。

#### 5.16.5 `SERVICE_CAPABILITIES` (0x24)

```text
schema, timestamp_ms, valid_mask                      u8, u32, u32
rx_min, rx_max, tx_min, tx_max                        float32 x4, MHz
polarization_mask, feature_flags, capture_profile_mask u8, u8, u8
```

`valid_mask` bit0..7 对应四个频率边界、极化掩码、独立极化能力、发射控制能力、全量录制能力。
`polarization_mask` 的 bit 位置等于极化枚举值；AFD01、AFD01C 与 ESA01/503 当前均为 `0x0C`。
ESA01/503 当前声明的 RX 范围为 17700~21200 MHz，TX 范围为 27500~31000 MHz。
`feature_flags.bit0` 表示
收发极化可独立设置，bit1 表示支持发射控制，bit2 表示支持设备安装姿态原子配置与0x2C读回。
`capture_profile_mask.bit0=customer_live`，
bit1=`support_full`。
客户 RF 控制只有在 bit0..5 均有效且 `feature_flags.bit0=1` 时才可启用；bit5 缺失或 bit0 为
0 时，上位机必须保持 RF 控制禁用并说明该设备未声明可独立设置收发极化。

#### 5.16.6 `SERVICE_CONTROL_REQUEST/RESPONSE` (0x25/0x26)

请求公共头：

```text
schema        u8 = 1
request_id    u32
operation     u8
payload       bytes
```

| operation | 名称 | payload |
|-----------|------|---------|
| 0 | `SUBSCRIBE` | `fast_rate_hz u8`，范围 1~20 |
| 1 | `SET_CONTROL_MODE` | `mode u8`，0=AUTO / 1=MANUAL |
| 2 | `APPLY_RF` | `rx_freq f32 + tx_freq f32 + rx_polar u8 + tx_polar u8`，原子应用 |
| 3 | `SET_TX_ENABLE` | `enabled u8`，0/1 |
| 4 | `SET_CAPTURE_PROFILE` | `profile u8`，0=customer_live / 1=support_full |
| 5 | `SET_DEVICE_MOUNT` | `mount_yaw f32 + mount_pitch f32 + mount_roll f32`，单位度；总 DATA 长度固定18 B |

`SUBSCRIBE` 是上位机的 Product Service 发现与保活请求：连接后立即发送一次，确认后每 1000 ms
使用新的 `request_id` 幂等重发，断开后停止。未收到保活时是否执行发射 fail-close 属于具体产品和
固件版本的安全合同，不是本通用包络能够证明的事实；只有产品合同明确声明且设备回读确认后，
上位机才能显示对应状态。停止发送 `SUBSCRIBE` 本身不能作为发射已关闭的证据。发现阶段的快速/
慢速重试仅用于尚未确认的设备，不替代已确认会话的 1 s 保活。

产品射频和发射控制只允许在已确认的 MANUAL 模式执行；安装姿态配置不要求 MANUAL，但必须与
OTA、参数写入等设备事务互斥。三个安装角一次性校验和持久化，不允许逐轴部分成功：yaw/roll范围
`[-180,180]`，pitch范围`[-90,90]`，所有值必须有限。切换到
`support_full` 会打开动态 Debug 数据用于全量 SDB；恢复 `customer_live` 会关闭动态 Debug。
上位机必须记住录制前 Debug 状态，并在 customer_live 响应成功后通过严格 Debug ACK 流程恢复。

响应固定为：

```text
schema, request_id, operation, result_code, applied_mask u8, u32, u8, u8, u32
control_mode                                           u8
rx_frequency, tx_frequency                            float32, float32
rx_polarization, tx_polarization, tx_enabled           u8, u8, u8
```

`applied_mask` bit0..7 依次表示 mode、RX 频点、TX 频点、RX 极化、TX 极化、TX 使能、录制配置、
安装姿态请求已原子持久化。安装姿态 bit7 的语义是 `PERSISTED`，只证明 FRAM 事务完成，不证明
外部 INS RBV 已应用或设备姿态已切换。
`result_code=0` 只证明设备已接受该请求，不能表示控制完成。响应携带发送时的设备读回快照；
上位机必须同时匹配 `request_id + operation`，并等待后续 Product telemetry 与目标值一致后才显示
成功，不能接受无上下文 `OK` 或只依据请求值/响应快照显示完成。

产品服务结果码独立于 §6 的通用 Debug 响应码：`0=ACCEPTED, 1=INVALID_REQUEST,
2=OUT_OF_RANGE, 3=STATE_NOT_ALLOWED, 4=NOT_SUPPORTED, 5=BUSY, 6=INTERNAL_ERROR`。

#### 5.16.7 `SERVICE_LINK_DETAIL` (0x27)

该帧是可选扩展，不改变 0x20~0x26 的定长布局。旧上位机按未知 `RawFrame` 忽略；新上位机
在帧缺失或有效位未置位时显示不可用。

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
modem_online                           u8
rx_lo, tx_lo                           float32 x2, MHz
satellite_mode                         u8
satellite_longitude                    float32, deg
satellite_id                           u32
satellite_name                         u8 len + utf8
```

`valid_mask` bit0..6 依次对应 Modem 在线、RX LO、TX LO、卫星模式、卫星经度、卫星编号和
卫星名称。`satellite_mode` 为 `0=UNKNOWN, 1=GEO, 2=LEO_TLE`。GEO 模式置 bit3/bit4，
`satellite_longitude` 东经为正、西经为负；TLE 模式置 bit3，并在可解析时置 bit5 或 bit6。
当前 AFD01 从 TLE 第一行的 NORAD catalog number 提供 bit5，设备没有权威名称时 bit6 必须为 0。

#### 5.16.8 `SERVICE_RF_LOCK_STATUS` (0x28)

该帧是可选扩展，不改变 0x20~0x27 的既有布局。旧上位机按未知 `RawFrame` 忽略；新上位机在
帧缺失或有效位未置位时显示不可用，不得把缺失值当成锁定或失锁。

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
lock_mask                              u8
```

`valid_mask` 和 `lock_mask` 的 bit0..2 分别对应 `clock PLL`、`TX PLL`、`RX PLL`。
有效位已置且对应 `lock_mask` 位为 1 表示锁定，为 0 表示失锁。三路均锁定时等价于工程 Debug
的聚合 `PLL_LOCKED`，但客户服务必须保留三路明细，便于定位具体射频链路。

#### 5.16.9 `SERVICE_HARDWARE_IDENTITY` (0x29)

该帧是可选扩展，不改变 0x20 的字符串布局。旧上位机按未知 `RawFrame` 忽略；生产测试上位机
优先使用完整 MCU UID 绑定跨工位测试工程，旧固件缺失 0x29 时才回退到生产 SN。

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
uid_word0, uid_word1, uid_word2        u32 x3
mac_address                            u8[6]，网络显示顺序
mac_source                             u8
```

`valid_mask.bit0=UID`、`bit1=MAC`、`bit2=MAC 派生算法版本`。UID 文本展示固定按
`uid_word0/1/2` 各 8 位大写十六进制拼接为 24 字符，不改变 payload 的小端整数编码。
`mac_source=1` 表示 `SOFTHZ/AFD01/MAC/V1`：以命名空间和 UID 三个 word 的固定小端字节序
执行 FNV-1a 64，取低 48 bit 后强制 `I/G=0, U/L=1`。bootloader 与 application 必须使用
同一实现；MAC 不允许通过参数系统覆盖。由于 96-bit UID 压缩到 46 个可用地址位不可能形成
数学上的一一映射，产线必须同时保存完整 UID 并拒绝重复 MAC。

#### 5.16.10 `SERVICE_NAV_SOURCE_INFO` (0x2A)

该帧声明设备实际配置的导航来源角色，是试产流程判断外部 INS 是否适用的唯一产品服务依据。
字段名保持通用外部 INS 语义；`BYNAV` 只是当前已实现的来源枚举值，不得把通用配置事实改名为供应商专用字段。
不得根据动态通道名称或最终整机姿态反推外部模块是否安装。

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
gnss_source                            u8
imu_source                             u8
attitude_source                        u8
external_ins_source                    u8
external_role_mask                     u8
capability_flags                       u8
imu_mount_rotation                     u8
```

`valid_mask.bit0..6` 依次对应 GNSS 源、IMU 源、姿态源、外部 INS 源、外部角色掩码、能力标志和
内部原始 IMU 安装旋转。`imu_mount_rotation` 只描述当前选用的板载原始 IMU 到设备 body 的诊断事实，
不得用于推算外部 INS 的 RBV，也不得替代设备相对载具的三个安装角。来源枚举固定为 `0=NONE, 1=ICM42688, 2=MG902, 3=BYNAV, 4=TRACE,
5=IAM20680, 6=MS6222, 7=DEBUG_ORACLE`；未知值必须保留为 UNKNOWN，不得映射成 NONE。

`external_role_mask.bit0=GNSS, bit1=IMU, bit2=ATTITUDE`。`capability_flags` 定义为：

- bit0：固件支持外部 INS 类型化诊断；不代表硬件已安装。
- bit1：当前至少一个导航角色配置为外部 INS。
- bit2：本次启动已观察到外部 INS 数据。
- bit3：当前外部 INS 在线。

未配置外部 INS 时，bit0 仍可为 1，bit1..3 为 0，`external_ins_source=NONE` 且
`external_role_mask=0`。试产上位机应将外部 INS 项记为 `N/A/SKIPPED`，不能将“未安装”判为
故障；配置存在但离线时仍是应测对象。

#### 5.16.11 `SERVICE_EXTERNAL_INS_DIAGNOSTICS` (0x2B)

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
source, role_mask, online              u8 x3
state, aligned, raw_ins_status         u8 x3
raw_position_type, gnss_position_type  u8 x2
num_svs                                u8
inspvax_count, rawimuxa_count,
bestpvt_count                          u32 x3
inspvax_hz, rawimuxa_hz, bestpvt_hz    float32 x3, Hz
ascii_crc_errors, binary_crc_errors,
binary_format_errors, rx_overflow_bytes u32 x4
yaw, pitch, roll                       float32 x3, deg
yaw_std, pitch_std, roll_std           float32 x3, deg
latitude_std, longitude_std, height_std float32 x3, m
velocity_north_std, velocity_east_std,
velocity_up_std                        float32 x3, m/s
solution_age, differential_age         float32 x2, s
```

`valid_mask` 以字段组定义：bit0=来源与角色，bit1=在线，bit2=归一化状态/对准/原始 INS 状态，
bit3=两类 position type，bit4=卫星数，bit5=帧累计与频率，bit6=错误计数，bit7=原始姿态，
bit8=姿态标准差，bit9=位置标准差，bit10=速度标准差，bit11=解算/差分龄期。

`state` 固定为 `0=NONE, 1=STALE, 2=UNALIGNED, 3=ROLL_PITCH_READY,
4=YAW_ALIGNED`。AFD01 的 Bynav 适配直接读取驱动统计和最新 `INSPVAXA/BESTPVTA`；
未配置外部 INS 时仅 bit0 可有效，其余字段不得以 0 冒充测量值。当前设备端没有权威的外部模块
型号、模块固件版本和配置哈希来源，因此本 schema 不上报这些字段；后续取得稳定来源后必须通过
新 schema 或独立可选记录扩展，禁止伪造占位值。

#### 5.16.12 `SERVICE_MOUNT_STATUS` (0x2C)

该帧只由声明`service_protocol>=8`且`feature_flags.bit2=1`的设备发送。旧上位机按未知帧忽略。

```text
schema, timestamp_ms, valid_mask       u8, u32, u32
mount_contract_id                      u32
mount_yaw, mount_pitch, mount_roll     float32 x3, deg
expected_rbv_x/y/z                     float32 x3, deg
readback_rbv_x/y/z                     float32 x3, deg
imu_mount_rotation                     u8
rbv_verified                           u8
restart_required                       u8
```

固定格式为`<BIII9fBBB>`，DATA长度52 B。`valid_mask`定义：bit0=contract、bit1=三个配置角、
bit2=期望RBV、bit3=读回RBV、bit4=`imu_mount_rotation`、bit5=`rbv_verified`、
bit6=`restart_required`。接收端必须拒绝未定义的`valid_mask`位、9个float中任一非有限值，
以及不为0/1的`rbv_verified`/`restart_required`。

`mount_contract_id=0x31445246`（小端字节文本`FRD1`）表示设备与载具均使用FRD：X前、Y右、Z下；
三个安装角表示设备相对载具的yaw-pitch-roll，旋转顺序为ZYX。数值0是合法配置，不表示未配置。
设备端以已配置的外部 INS 相对设备旋转`ext_ins_rot`和这三个安装角计算`expected_rbv`，并以模块读回填充
`readback_rbv`；上位机不得自行复算或用`imu_mount_rotation`替代其中任一输入。后者仅为内部原始 IMU
诊断字段，保留在本帧是为了与导航源信息交叉核对。上位机必须同时验证bit0、合同ID和bit1，不能把未知合同按FRD1显示。

operation 5成功后，设备发布带新角度且`restart_required=1`的新0x2C；上位机只有收到该新读回后才
发送通用`DEVICE_REBOOT`。设备重启后再次发布同一角度且`restart_required=0`，上位机才能显示完成。
整个事务必须绑定发起时可用的`SERVICE_IDENTITY.serial_number`和
`SERVICE_HARDWARE_IDENTITY.device_uid`；相同网络端点在重启窗口内换成另一台设备时，旧事务必须失败关闭。
重启后的完成判断必须使用同一设备新收到的0x2A导航配置事实；未收到或字段无效时继续等待，不得沿用重启前判断。
当`SERVICE_NAV_SOURCE_INFO`同时证明`capability_flags.bit1=1`、`external_ins_source=BYNAV`且
`external_role_mask.bit2=1`时，还必须等待bit5有效且`rbv_verified=1`。不得使用当前动态
`attitude_source`代替这三个稳定配置事实；其他配置不要求RBV验证。请求、ACK或重启指令发送成功均不能单独
作为安装姿态已应用证据。

### 5.17 XESA01 Orbit/TLE 扩展 (0x30~0x31)

Orbit Service 复用 Debug v2 envelope，但不依赖 `DEBUG_ENABLE`，也不会因 capability/查询请求隐式打开连续
工程遥测。所有多字节整数和 float32 均为 little-endian。

请求 DATA 头固定为：

```text
schema_version  u8      当前 1
operation       u8
request_id      u32     非零，由上位机分配
payload         bytes   由 operation 决定
```

响应 DATA 头固定为：

```text
schema_version  u8      当前 1
operation       u8      回显请求操作
status          u8      0=OK, 1=INVALID_REQUEST, 2=UNAVAILABLE, 3=BUSY,
                        4=INTERNAL_ERROR, 5=CRC_ERROR
request_id      u32
payload         bytes   status 非 OK 时为空
```

operation 和请求 payload：

| 值 | 名称 | 请求 payload | 成功响应摘要 |
|---:|---|---|---|
| 1 | `CAPABILITIES` | — | feature flags、目录/文件/窗口/步长/点数上限、stale 天数 |
| 2 | `SCAN` | — | 无 payload；请求已入队或与当前扫描合并 |
| 3 | `CATALOG` | `page:u16` | generation、total/page/count、扫描/错误统计、变长名称和来源 |
| 4 | `CURRENT` | `norad:u32, page:u16`；0=all | generation、total/page/count、卫星 LLA、az/el/range、TLE age/flags |
| 5 | `PREDICT_SUBMIT` | `norad:u32, horizon:u32, step:u16, min_el:f32` | job/generation/参数、起点 UTC、固定站位 LLA、assumption flags |
| 6 | `PREDICT_PAGE` | `job:u32, page:u16` | 单星每页 16 点；all 每页 8 颗首过境摘要 |
| 7 | `SELECT` | `norad:u32` | 无 payload；只替换 tracking target，不改变 TX |
| 8 | `UPLOAD_BEGIN` | `size:u32, crc32:u32` | 接受的文件总长度 |
| 9 | `UPLOAD_CHUNK` | `offset:u32, len:u16, bytes[1..1012]` | 已确认的累计 offset；bytes 上限固定为 1012 B |
| 10 | `UPLOAD_END` | — | 长度/CRC/parser/原子替换和 rescan 结果 |
| 11 | `UPLOAD_ABORT` | — | 释放匹配 request ID 的上传会话 |
| 12 | `SKY_SNAPSHOT` | `snapshot_id:u32, page:u16`；0/0=新快照 | 同一 UTC/导航/姿态/阵面 profile 下的地理与规范阵面位置 |

`CAPABILITIES.feature_flags` 当前定义为bit0=catalog、bit1=prediction、bit2=TLE upload、
bit3=array sky snapshot；未声明 bit3 的旧固件继续使用 `CURRENT`，不得将地理 az/el 直接画入阵面极坐标图。

`CATALOG` 每页最多 4 项。每项为
`norad:u32, epoch_unix_s:u32, name_len:u8, source_len:u8, name:utf8, source:utf8`。`CURRENT` 每项为
`norad:u32, utc_unix_ms:u64, sat_lat/lon/alt:f32×3, az/el/range:f32×3, tle_age_days:f32, flags:u8`，
flags bit0=stale、bit1=地平线以上。

`PREDICT_PAGE` 首字节 `mode=0` 表示单星采样，`mode=1` 表示 all-satellite 首过境摘要；之后固定包含
`job_id:u32, page:u16, generation:u32, total:u16, count:u8`。预测提交的 `assumption_flags.bit0` 表示整个
窗口固定使用请求时地面站位置。上位机不得将该结果标成未来真实平台姿态或可直接执行的 TX 波束命令。
`PREDICT_PAGE` 在 Debug RX 上只完成校验与排队，页内 SGP4 计算和回包由低优先级 Orbit owner 异步执行；
队列已满时返回 `BUSY`，上位机应按 request ID 等待对应报告，不应假定请求调用内同步完成。

`SKY_SNAPSHOT` 的首页请求固定为 `snapshot_id=0,page=0`。设备在 Orbit owner 中原子捕获
catalog generation、GNSS/UTC、body FRD 姿态、RX 阵面安装/profile、扫描包络和当前 tracking target，
分配非零 snapshot ID；后续页必须回传该 ID。每页最多 16 颗，固定元数据为：

```text
snapshot_id, catalog_generation        u32 x2
utc_unix_ms                            u64
total, page                            u16 x2
count                                  u8
hard_offaxis, recommended_offaxis      float32 x2, deg
mount_yaw, mount_pitch, mount_roll     float32 x3, deg（R_array_to_body）
azimuth_zero_offset                    float32, deg
azimuth_direction                      int8，+1=同向，-1=反向
second_angle_type                      u8，0=offaxis，1=elevation
active_target_id                       u32，0=无
profile_flags                          u8，bit0=characterized
```

每颗卫星的 41 字节记录为：

```text
norad_id                               u32
sat_lat, sat_lon, sat_alt              float32 x3, deg/deg/m
geographic_az, geographic_el, range    float32 x3, deg/deg/m
array_az, array_offaxis                float32 x2, deg（规范阵面系）
tle_age_days                           float32
flags                                  u8
```

flags bit0=stale、bit1=地平线可见、bit2=阵面正半球、bit3=硬离轴包络内、bit4=当前跟踪目标。
页内 SGP4 传播与坐标换算同样由低优先级 Orbit owner 异步执行。上位机只有完整收齐同一
snapshot ID、generation 和元数据的连续分页后才能原子替换显示；迟到页、缺页和跨 generation 页必须丢弃。
本操作只读，不执行阵面指向、自动换星或 TX 授权。

上传会话使用同一个 request ID，严格等待每片 ACK 后再发送下一 offset；总文件最大 64 KiB。
`UPLOAD_CHUNK.bytes` 为 1..1012 B，1012 B 上限独立于 `MAX_DATA_LENGTH=1536`，不得因长帧能力自动增大。
上位机可显式 `UPLOAD_ABORT`；设备会回收 10 秒无合法分片的会话。乱序、重复、越界、错误 CRC、坏 TLE
或未知版本均失败关闭，并保留旧 active catalog。

---

## 6. 响应码（扩展）

| 响应码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 无效命令 |
| 2 | 参数错误 |
| 3 | 设备忙 |
| 4 | 内部错误 |
| 5 | 不支持的操作 |
| 6 | 状态不允许（如跟踪中拒绝切换模式） |
| 7 | 超出范围 |

---

## 7. 通信流程

### 7.1 启动握手

```
Device                                  Host
  | --- META_INFO (0x04) ------------->|
  | --- CHANNEL_DEFINE (0x05) -------->|   上位机建立通道表
  | --- STATE_DEFINE (0x06) ---------->|   上位机建立状态字表
  | --- EVENT_DEFINE (0x07) ---------->|   上位机建立事件表
  | --- PROFILE_SEMANTICS (0x0C) ----->|   可选：上位机建立语义角色
  | --- STATE_REPORT (0x08) 全量 ----->|   上位机点亮所有指示灯
  | --- HEARTBEAT (0x0A) ------------->|
  |                                    |
  | --- DATA_REPORT × N -------------->|   高频（如 50Hz）
  | --- STATE_REPORT (变化) ---------->|   仅变化时
  | --- EVENT_REPORT (事件) ---------->|   按需
  | --- HEARTBEAT (1Hz) -------------->|
```

### 7.2 上位机后接入 / 重连

上位机成功绑定 UDP 后立即发：
```
CONTROL(REQUEST_META_INFO)
CONTROL(REQUEST_CHANNEL_DEFINE)
CONTROL(REQUEST_STATE_DEFINE)
CONTROL(REQUEST_EVENT_DEFINE)
CONTROL(REQUEST_PROFILE_SEMANTICS)   # 可选；旧固件不支持时不影响 ready
```

下位机收到应立即（500ms 内）回应所有定义帧。

### 7.3 用户标记

```
Host  -> CONTROL(USER_MARK, mark_id=1, text="test point A")
Device -> EVENT_REPORT(event_id=0xFFFF, payload="test point A")
Device -> COMMAND_RESPONSE(code=0, msg="OK")
```

下位机把 mark 作为 EVENT 回灌到数据流中，保证上位机录制文件里能看到标记的准确时间位置（考虑链路延迟）。

---

## 8. ID 规划原则与保留范围

### 8.1 协议层规定（强制）

协议层**只规定**以下内容，其它任何 ID 含义均由下位机自描述：

| 范围 | 规则 |
|------|------|
| `channel_id` 取值 | 0 ~ 63（一帧 DATA_REPORT 最多带 64 个）|
| `state_id` 取值 | 0 ~ 63 |
| `event_id` 取值 | 0x0001 ~ 0xFFFE |
| `event_id = 0xFFFF` | **保留给 USER_MARK**（上位机通过 CONTROL.USER_MARK 触发，下位机以 EVENT_REPORT 回灌到数据流），下位机**不得**用于其它事件 |
| `event_id = 0x0000` | 保留，禁止使用 |
| `state_type` 取值 | 0=BOOL, 1=ENUM（其它值保留） |
| `data_type` 取值 | 0x01=float32（其它值保留） |
| `level` 取值 | 0=DEBUG, 1=INFO, 2=WARN, 3=ERROR |

### 8.2 设备侧自由规划（建议）

下位机为各自型号规划 ID 时，建议遵循以下软约定（上位机不依赖，便于工程师阅读协议包）：

- `event_id` 按子系统分段，每段 256 个 ID，例如：`0x01xx` = trace 子系统、`0x02xx` = modem、`0x03xx` = ins
- 各型号的 ID 规划独立管理，互不冲突；在下位机 `debug_profile_xxx.c` 中集中声明
- `flags.critical` 标志只标记"车载场景必显"的项，**宁少勿多**（建议 ≤ 8 个）

### 8.3 上位机对未知 ID 的处理

- 未知 `channel_id`：按 `unknown_<id>` 显示，仍绘曲线（不丢弃）
- 未知 `state_id`：忽略本条，不阻塞其它状态
- 未知 `event_id`：显示为 `EVENT_<hex>`，level=INFO

### 8.4 示例参考

**afd01 / ufd45 的具体通道/状态/事件清单由各自固件仓库的 `debug_profile_xxx.c` 定义**，不在本协议规范内。开发者可参考附录 C 给出的 afd01 示例骨架。

---

## 9. 下位机负载估算

### 9.1 带宽

| 帧类型 | 频率 | 单帧 | 速率 |
|--------|------|------|------|
| DATA_REPORT（典型 16ch） | 100 Hz | 97 B  | 9.7 KB/s |
| STATE_REPORT (12)     | 5 Hz   | 37 B  | 0.2 KB/s |
| EVENT_REPORT          | ~5 Hz  | ~30 B | 0.15 KB/s |
| HEARTBEAT             | 1 Hz   | 21 B  | 0.02 KB/s |
| GNSS_SKY_REPORT       | 约 1 Hz | 最大 274 B | 约 0.27 KB/s |
| GNSS_CNR_REPORT       | 约 1 Hz | 最大 921 B/片，最多 2 片 | 最大约 1.8 KB/s |
| GNSS_SAT_REPORT       | 约 1 Hz | 最大 663 B/片，最多 2 片 | 最大约 1.3 KB/s |
| GNSS_SIGNAL_REPORT    | 约 1 Hz | 最大 791 B/片，最多 2 片 | 最大约 1.6 KB/s |
| CHANNEL_DEFINE（AFD01 31ch） | 0.2 Hz | 约 774 B | 约 0.15 KB/s |
| STATE_DEFINE（AFD01） | 0.2 Hz | 1170 B | 约 0.23 KB/s |
| EVENT_DEFINE | 0.2 Hz | 约 500 B | 约 0.10 KB/s |
| **合计（同时上报两类接收机数据的典型上界；不含按需 OTA/Orbit 上传）** | | | **~13.5 KB/s** |

W5500 UDP 实测吞吐 ≥ 1 MB/s，**余量充足**（利用率 ~1%）。

### 9.2 CPU

- `DATA_REPORT` 打包：扫描固定 64 个注册槽，只序列化本周期已更新的通道；实际耗时以目标板测量为准
- CRC16 计算：硬件加速可更快；软件实现约 8 μs/100B → 100Hz 时 0.08% CPU
- 状态字/事件：事件驱动，几乎零开销
- **总增量 CPU ≤ 0.3%**，相对 v1 同频率只增不超过 0.1%（主要因省去 `strncpy(32)`）

### 9.3 ROM/RAM

- ROM：定义表字符串常量，估算 ~1 KB
- RAM：channel/state/event/semantic 表均为静态容量，具体占用以目标固件 map 为准；不得用协议条目数
  直接推算，也不得把 Host/模拟器结果当作目标板 RAM 证据
- 帧缓冲：早期 afd01 / ufd45 的 512 字节 DATA 帧可继续使用约 540 B 缓冲；支持 1536 字节 DATA 长帧时，`DEBUG_MAX_FRAME_LENGTH` 必须预留至少 1548 B。1548 B 是保守缓冲值，实际最大 wire 长度为 1536 B DATA + 9 B 包络 = 1545 B。

### 9.4 实时性要求

- `EVENT_REPORT` 触发到发出 **< 20 ms**（FreeRTOS 下 debug 任务优先级至少 10）
- `STATE_REPORT` 变化到发出 **< 200 ms**
- `HEARTBEAT` 误差 **< 10%**（1Hz ± 100ms）

---

## 10. 错误处理（新增）

| 场景 | 下位机 | 上位机 |
|------|--------|--------|
| 上位机未收到 DEFINE 就收到 REPORT | — | 丢弃 REPORT，发 `REQUEST_*_DEFINE` |
| DEFINE 表版本变更（table_ver 变） | 强制下次 REPORT 前先发 DEFINE | 收到新 table_ver 后清空本地表 |
| 3 秒无 HEARTBEAT | — | 标记链路断开，保留数据 |
| CRC 错误 | 丢帧，计数+1 | 丢帧，计数+1，事件日志写一条 WARN |
| 不支持的 sub_cmd | resp=NOT_SUPPORTED | 弹提示 |

---

## 11. 版本切换策略

v2 与 v1 **不兼容**。项目采取一次性切换方式：

1. 上位机和下位机固件必须在同一次发布中同时升级到 v2
2. 混版场景（旧固件 + 新工具 / 新固件 + 旧工具）直接断连并提示版本错位
3. v1 .sdb 录制文件不再由主工具打开；如需转换旧录制，提供独立 `sdb_v1_convert.py`
4. 协议后续演进（v2.x、v3.x）沿用相同原则：小版本在 v2 内兼容扩展（如新增子命令），大版本升级由双端同步切换

---

## 附录 A：帧示例

### A.1 DATA_REPORT（3 通道）

```
AA 55 0D 01 15 00                          # 帧头+type+cmd+len=21
64 00 00 00                                 # timestamp=100ms
03                                          # channel_count=3
00 00 00 00 80 41                           # ch0=16.0
03 DA 0A D1 42                              # ch3=104.421...
0A 9A 99 0A 42                              # ch10=34.65
XX XX                                       # CRC16
EE
```

### A.2 STATE_REPORT（3 状态）

```
AA 55 0D 08 0B 00
64 00 00 00                                 # ts
03                                          # count=3
00 03                                       # TRACE_MODE=LOCK(3)
01 01                                       # LOCK_FLAG=true
02 02                                       # GPS_FIX=3D
XX XX
EE
```

### A.3 EVENT_REPORT（LOCK_ACQUIRED, 无 payload）

```
AA 55 0D 09 07 00
64 00 00 00                                 # ts
03 00                                       # event_id=0x0003
00                                          # payload_len=0
XX XX
EE
```

---

## 附录 B：固件侧 API 草案（对下位机开发者）

协议实现和设备 profile **分离**：
- `components/debug/` 放**协议核心**（编码、发送、接收、CRC），所有设备共用
- `target/<hw>/application/app/debug/<hw>_debug_profile.c` 放**具体型号的通道/状态/事件注册表**

```c
/* ---- 协议核心 API (components/debug/debug.h) ---- */

/* 初始化 */
bool debug_v2_init(void);
bool debug_v2_set_meta(const char *fw_ver, const char *hw_type, const char *sn);

/* 通道注册 */
bool debug_v2_channel_register(uint8_t id, const char *name, const char *unit,
                               uint8_t group, uint8_t flags,
                               float min, float max);

/* 状态字注册 */
bool debug_v2_state_register_bool(uint8_t id, const char *name, uint8_t flags);
bool debug_v2_state_register_enum(uint8_t id, const char *name, uint8_t flags,
                                  const debug_enum_item_t *items, uint8_t count);

/* 事件注册 */
bool debug_v2_event_register(uint16_t id, uint8_t level, const char *name);

/* 周期上报（100Hz 调用，内部只打包已 set 的通道） */
bool debug_v2_data_set(uint8_t id, float value);
bool debug_v2_data_flush(void);   /* 实际打包并 send */

/* 状态字（值变化时才会触发上报） */
bool debug_v2_state_set(uint8_t id, uint8_t value);

/* 事件（立即上报） */
bool debug_v2_event_trigger(uint16_t id, const void *payload, uint8_t payload_len);


/* ---- 设备 profile 层 (afd01_debug_profile.c 示例) ---- */

void afd01_debug_profile_register(void)
{
    debug_v2_set_meta("afd01-1.3.2", "afd01", get_device_sn());

    /* channels */
    debug_v2_channel_register(0, "roll",   "°", 0, FLAG_CRITICAL, -180, 180);
    debug_v2_channel_register(1, "pitch",  "°", 0, FLAG_CRITICAL,  -90,  90);
    /* ... 其它通道 ... */

    /* states */
    debug_v2_state_register_bool(1, "LOCK_FLAG", FLAG_CRITICAL);
    static const debug_enum_item_t trace_mode_items[] = {
        {0, LVL_NEUTRAL, "STANDBY"},
        {1, LVL_WARN,    "SCAN_GLOBAL"},
        {2, LVL_WARN,    "SCAN_WIDE"},
        {3, LVL_INFO,    "LOCK"},
        {4, LVL_NEUTRAL, "MANUAL"},
    };
    debug_v2_state_register_enum(0, "TRACE_MODE", FLAG_CRITICAL,
                                 trace_mode_items, 5);
    /* ... */

    /* events */
    debug_v2_event_register(0x0003, LVL_INFO,  "LOCK_ACQUIRED");
    debug_v2_event_register(0x0004, LVL_WARN,  "LOCK_LOST");
    /* ... */
}
```

ufd45 同理实现 `ufd45_debug_profile_register()`，两套互不影响。

---

## 附录 C：afd01 参考 Profile（仅示例，非协议强制）

以下为 afd01 当前计划上报的通道/状态/事件清单，供下位机开发者起步参考。**ufd45 及未来型号应各自规划独立的 profile**。

### C.1 数据通道（afd01 示例）

| ID | 名称 | 单位 | group | critical | 来源 |
|----|------|------|-------|----------|------|
| 0  | roll           | °    | 0 | ✓ | locate_info |
| 1  | pitch          | °    | 0 | ✓ | locate_info |
| 2  | yaw            | °    | 0 | ✓ | locate_info |
| 3  | ant_az         | °    | 1 | ✓ | trace_beam.rx_angle[2] |
| 4  | ant_el         | °    | 1 | ✓ | trace_beam.rx_angle[1] |
| 5  | ant_skew       | °    | 1 |   | trace_beam.rx_angle[0] |
| 6  | tgt_az         | °    | 1 | ✓ | bp_body_view 目标方位 |
| 7  | tgt_el         | °    | 1 | ✓ | bp_body_view 目标仰角 |
| 8  | err_az         | °    | 3 |   | tgt_az - ant_az |
| 9  | err_el         | °    | 3 |   | tgt_el - ant_el |
| 10 | snr            | dB   | 2 | ✓ | trace_snr.snr |
| 11 | snr_avg        | dB   | 2 |   | 滑动平均 |
| 12 | yaw_compen     | °    | 3 |   | scan.cur_yaw_compen |
| 13 | pid_az_out     | —    | 3 |   | PID azimuth 输出 |
| 14 | pid_el_out     | —    | 3 |   | PID elevation 输出 |
| 15 | cpu_load       | %    | 5 |   | HEARTBEAT 同源 |

### C.2 状态字（afd01 示例）

| ID | 名称 | 类型 | critical | 说明 |
|----|------|------|----------|------|
| 0 | TRACE_MODE       | ENUM | ✓ | STANDBY/SCAN_GLOBAL/SCAN_WIDE/LOCK/MANUAL |
| 1 | LOCK_FLAG        | BOOL | ✓ | 锁星标志 |
| 2 | GPS_FIX          | ENUM | ✓ | NO_FIX/2D/3D/RTK_FIXED/DGNSS/RTK_FLOAT/STALE |
| 3 | INS_READY        | BOOL | ✓ | INS 对准完成 |
| 4 | PLL_LOCKED       | BOOL | ✓ | PLL 锁定（adf4002/lmx2594） |
| 5 | PA_ENABLED       | BOOL |   | 功放使能 |
| 6 | ANT_ENABLED      | BOOL |   | 天线使能 |
| 7 | WIZNET_LINK      | BOOL |   | 物理链路 |
| 8 | MODEM_CONNECTED  | BOOL | ✓ | 调制解调器就绪 |
| 9 | TLE_LOADED       | BOOL |   | TLE 已配置 |
| 10 | OTA_ACTIVE      | BOOL |   | OTA 升级中 |
| 11 | GNSS_SOURCE     | ENUM |   | UNKNOWN/MG902/BYNAV/MANUAL/MS6222 |

### C.3 事件（afd01 示例）

事件 ID 段分配建议（afd01）：

| 段 | 子系统 |
|----|--------|
| 0x0001 ~ 0x00FF | trace（跟踪）|
| 0x0100 ~ 0x01FF | modem |
| 0x0200 ~ 0x02FF | ins / locate |
| 0x0300 ~ 0x03FF | beampointing |
| 0x0400 ~ 0x04FF | transceiver / PLL |
| 0x0500 ~ 0x05FF | 系统 / 存储 / 网络 |
| 0xFFFF          | USER_MARK（协议保留，见 §8.1）|

常用事件：

| event_id | level | name | 说明 |
|----------|-------|------|------|
| 0x0001 | INFO  | SCAN_GLOBAL_START   | 进入全局扫描 |
| 0x0002 | INFO  | SCAN_WIDE_START     | 进入宽域扫描 |
| 0x0003 | INFO  | LOCK_ACQUIRED       | 锁星成功 |
| 0x0004 | WARN  | LOCK_LOST           | 失锁 |
| 0x0005 | INFO  | TRACE_MODE_CHANGED  | 模式切换（payload=new_mode）|
| 0x0006 | WARN  | SNR_BELOW_THRESHOLD | SNR 低于阈值 |
| 0x0101 | ERROR | MODEM_TIMEOUT       | 调制解调器超时 |
| 0x0201 | INFO  | INS_ALIGN_DONE      | INS 对准完成 |
| 0x0202 | WARN  | GPS_FIX_LOST        | GPS 失锁 |
| 0x0301 | ERROR | TLE_PARSE_FAIL      | TLE 解析失败 |
| 0x0401 | ERROR | PLL_UNLOCKED        | PLL 失锁 |
| 0x0501 | WARN  | FRAM_WRITE_FAIL     | 参数持久化失败 |

---

## Debug Tracking 场景仿真（0x11）

该命令只由 AFD01/AFD01C Debug 固件处理，Release 固件不提供。DATA 使用小端编码：

- 公共头：`schema:u8=1 | op:u8 | session_id:u32`。
- `op=0 START`、`op=1 SAMPLE` 的 DATA 固定 87 字节；公共头后依次为 `flags:u8`、`latitude/longitude:f64`、11 个姿态/运动/GEO `f32`、`sat_num:u32` 和 4 个 SNR 诊断 `f32`。
- `flags.bit0/1/2` 分别表示 `snr_valid/norm_valid/rx_online`。
- `op=2 STOP` 的 DATA 固定 6 字节。
- `session_id` 必须非零；活动会话只接受相同 ID，停止后的迟到包会被拒绝。

START/SAMPLE 连续 2000 ms 未到达时设备自动退出仿真。仿真期间设备强制关闭 TX；显式 STOP 或超时后恢复原 TX 策略，原请求仍成立时 TX 可能重新开启。

---

文档结束。
