# DEBUG设备协议接口规范 v2

## 文档信息

| 项目 | 内容 |
|------|------|
| 协议版本 | v2.0 |
| 上一版本 | v1.0（`DEBUG设备协议接口规范.md`） |
| 发布日期 | 2026-04-16 |
| 兼容设备 | 任意实现本规范的 `device_type=0x0D` 设备（当前有 afd01 / ufd45，后续新型号无需改协议） |
| 适用上位机 | satellite_debug_tool ≥ v2.0 |

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
> 这样 afd01 / ufd45 / 未来新型号各自发各自的表，上位机零改动即可适配。

**兼容性**：v2 **完全取代 v1**，上下位机同步切换（本项目决定放弃 v1 兼容层以简化代码路径）。设备启动即发 `META_INFO`，上位机校验 `protocol_ver == 0x02`，否则断连并提示升级固件。

---

## 2. 物理层与传输层

同 v1：

- **UDP**（推荐，afd01/ufd45 当前实现）：端口 4004
- **串口**：115200 bps，8N1
- 字节序：**小端（Little Endian）**

建议上位机同时打开 UDP 与串口，任一路收到 `META_INFO` 即完成握手。

---

## 3. 通用帧格式（不变）

```
┌────────┬──────────┬──────────┬──────────┬────────┬────────┬────────┐
│  帧头   │ 设备类型  │ 命令类型  │ 数据长度  │  数据  │  CRC   │  帧尾  │
│ 2字节   │ 1字节     │ 1字节     │ 2字节    │ 可变   │ 2字节   │ 1字节  │
│ AA 55   │ 0D       │ 01..0A   │ 小端序   │        │ 小端序  │ EE    │
└────────┴──────────┴──────────┴──────────┴────────┴────────┴────────┘
```

CRC16-CCITT（poly=0x1021, init=0xFFFF），计算范围：帧头起至数据末尾（不含 CRC 和帧尾）。

`DATA` 段最大长度：**512 字节**（v2 从 v1 的 ~300B 提升，用于承载 `CHANNEL_DEFINE` 等长帧）。

> 下位机调整点：`DEBUG_MAX_FRAME_LENGTH` 从 320 调至 **540**（512 + 帧头 9 + CRC 2 + 帧尾 1，16 字节对齐取 540）。

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

**定义帧（0x04/0x05/0x06/0x07）**：下位机启动后立刻全量发送，之后每 5 秒重发一次（处理 UDP 丢包 / 上位机后接入）。上位机也可主动 `CONTROL` 请求重发。

---

## 5. 命令详细定义

### 5.1 `DATA_REPORT` (0x01) — 数据通道上报

**用途**：连续物理量（姿态、SNR、指向角、PID 中间量等）。

**DATA 段格式**：

```
timestamp      uint32      设备启动后毫秒数
channel_count  uint8       本帧携带的通道数 (1~16)
channel[N]     { uint8 channel_id; float32 value; }   每条 5 字节
```

**帧长估算**：`9 + 4 + 1 + 5×N + 2 + 1 = 17 + 5N`。N=16 时 97 字节；100 Hz 时 9.7 KB/s。

**下位机实现建议**：
- 按 `channel_id` 自增顺序一次打包全部通道，避免分帧
- 某通道数据暂无（如 trace 未启动）时可整帧不发，不要发 nan

### 5.2 `COMMAND_RESPONSE` (0x02) — 命令响应

同 v1。

```
code     uint8      响应码（见 §6）
msg[]    utf8       可选，最长 63 字节
```

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
    channel_id   uint8         0..15
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

**估算**：16 通道约 400 字节。5 秒重发 = 80 B/s。

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
| `channel_id` 取值 | 0 ~ 15（一帧 DATA_REPORT 最多带 16 个）|
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
| DATA_REPORT (16ch)    | 100 Hz | 97 B  | 9.7 KB/s |
| STATE_REPORT (12)     | 5 Hz   | 37 B  | 0.2 KB/s |
| EVENT_REPORT          | ~5 Hz  | ~30 B | 0.15 KB/s |
| HEARTBEAT             | 1 Hz   | 21 B  | 0.02 KB/s |
| CHANNEL/STATE/EVENT_DEFINE | 0.2 Hz | ~500 B | 0.1 KB/s |
| **合计** | | | **~10.2 KB/s** |

W5500 UDP 实测吞吐 ≥ 1 MB/s，**余量充足**（利用率 ~1%）。

### 9.2 CPU

- `DATA_REPORT` 打包：O(16)，约 2~5 μs/帧 @400MHz，100Hz → 0.05% CPU
- CRC16 计算：硬件加速可更快；软件实现约 8 μs/100B → 100Hz 时 0.08% CPU
- 状态字/事件：事件驱动，几乎零开销
- **总增量 CPU ≤ 0.3%**，相对 v1 同频率只增不超过 0.1%（主要因省去 `strncpy(32)`）

### 9.3 ROM/RAM

- ROM：定义表字符串常量，估算 ~1 KB
- RAM：channel/state 表结构 ~300 B；event 去重环形缓冲 ~200 B；总增量 < 1 KB
- 帧缓冲 `DEBUG_MAX_FRAME_LENGTH` 从 320 → 540，增 220 B

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
| 2 | GPS_FIX          | ENUM | ✓ | NO_FIX/2D/3D/RTK |
| 3 | INS_READY        | BOOL | ✓ | INS 对准完成 |
| 4 | PLL_LOCKED       | BOOL | ✓ | PLL 锁定（adf4002/lmx2594） |
| 5 | PA_ENABLED       | BOOL |   | 功放使能 |
| 6 | ANT_ENABLED      | BOOL |   | 天线使能 |
| 7 | WIZNET_LINK      | BOOL |   | 物理链路 |
| 8 | MODEM_CONNECTED  | BOOL | ✓ | 调制解调器就绪 |
| 9 | TLE_LOADED       | BOOL |   | TLE 已配置 |
| 10 | OTA_ACTIVE      | BOOL |   | OTA 升级中 |

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

文档结束。
