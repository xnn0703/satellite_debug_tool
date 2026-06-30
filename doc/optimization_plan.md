# 卫星调试工具优化 Plan（v2）

> 当前状态（2026-06-30）：本文件保留为 M1-M6 阶段历史优化蓝图，用于追溯协议 v2、profile 驱动、状态/事件、分组曲线和录制回放的初始设计。当前上位机功能定义与后续路线以 `doc/upper_pc_function_definition_vnext.md` 为准。

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v1.1（plan，已吸收"多设备自适应"反馈） |
| 创建日期 | 2026-04-16 |
| 协议依据 | `DEBUG设备协议接口规范_v2.md` |
| 影响范围 | 上位机 `satellite_debug_tool`  + 下位机 debug 组件（afd01/ufd45 及未来型号） |

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-04-16 | 初稿 |
| v1.1 | 2026-04-16 | 吸收反馈：上位机 UI 完全由设备 profile 驱动，afd01/ufd45 可各自独立一套通道/状态/事件；新增 §3.3 设备自适应架构、§5.2 ProfileStore、§6.4 设备 profile 分离、§8.1 新增 C 类（兼容性）验收点 |
| v1.2 | 2026-04-16 | **放弃 v1 兼容层**：上下位机同步切换到 v2，简化代码路径。删除兼容相关任务/验收/回退策略，删除"SDB v1 回放双路径"负担（v1 .sdb 不保证可读，如需要另行提供转换脚本） |

---

## 1. 目标

让上位机在**车载动态测试**场景下做到：

- 不需要盯字面日志，一眼看清"此刻能不能用"
- 关键状态（锁星 / GPS / INS / PLL / SNR / 指向偏差）用**指示灯 + 大字数值**展示
- 事件（失锁 / 模式切换 / TLE 错误）有**时间线**可追溯
- 曲线按**量纲分组**多 Y 轴显示，可暂停、缩放、标记时刻
- 录制不堵 UI 线程，可复盘

同时**下位机侧以最低负载代价**把这些信息吐出来（协议 v2 已估算 CPU < 0.3%）。

---

## 2. 现状问题回顾（对照当前版本）

| 区域 | 问题 | 来源 |
|------|------|------|
| 协议 | 只有 3 个命令；每通道 32B name 浪费；离散量无通道 | v1 协议 |
| 数据 | 下位机只吐 8 个 float，状态/事件全丢 | `trace.c:1149-1189` |
| 曲线 | 单一 Y 轴，所有通道叠加，SNR 被姿态角压扁 | `chart_widget.py:73` |
| 曲线 | 时间窗口写死 10s，无鼠标交互 | `chart_widget.py:27` |
| 3D | 只有姿态，无卫星矢量 / 波束 / 扫描轨迹 | `attitude_widget.py` |
| Dashboard | 不存在 | — |
| 日志 | `logging` import 但未使用；无事件时间线 | `base_worker.py:1` |
| 录制 | 同步写盘，200Hz 可能堵 UI | `data_recorder.py:32` |
| 刷新 | 每帧 emit 一次，高频下队列堆积 | `serial_worker.py` |
| UI 字体 | 11~12px，#CCCCCC，车载屏可读性差 | `styles.py` |
| 控制 | 只能开关 debug 输出 | `main_window.py:414-426` |

---

## 3. 总体方案

### 3.1 分层

```
┌───────────────────────────────────────────────────────────────┐
│                    上位机 (v2)                                 │
├───────────────────────────────────────────────────────────────┤
│  UI 层  —— 完全由 ProfileStore 驱动，零设备硬编码              │
│  ┌──────────────┐ ┌───────────────┐ ┌────────────────────┐    │
│  │  Dashboard   │ │  Chart 分组   │ │  Event Timeline    │    │
│  │  (指示灯/KPI)│ │  (多 Y 轴)    │ │  (事件日志)        │    │
│  └──────────────┘ └───────────────┘ └────────────────────┘    │
│  ┌──────────────────────────────────┐ ┌─────────────────┐     │
│  │      3D 场景 (姿态+卫星+波束)     │ │  控制面板       │     │
│  └──────────────────────────────────┘ └─────────────────┘     │
├───────────────────────────────────────────────────────────────┤
│  业务层                                                        │
│  ProfileStore (按 hw_type 分桶的元数据) │ DataStore │          │
│  StateStore │ EventLog │ MarkerStore                           │
├───────────────────────────────────────────────────────────────┤
│  协议层（v2 FrameReceiver + Builder + Handshake 状态机）       │
├───────────────────────────────────────────────────────────────┤
│  通信层（QThread: Serial / UDP; 批量 emit）                    │
└───────────────────────────────────────────────────────────────┘

                            ▲
                         UDP 4004
                            ▼

┌───────────────────────────────────────────────────────────────┐
│                    下位机                                      │
│  ┌────────────────────────────────────────────────────────┐   │
│  │  debug 协议核心 (shared/MiddleWare/components/debug)   │   │
│  │  编码/发送/接收/CRC/周期任务 —— 所有设备共用           │   │
│  └────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────┐  ┌──────────────────────┐           │
│  │ afd01_debug_profile  │  │ ufd45_debug_profile  │ ...       │
│  │ (channel/state/event │  │ (各型号独立)          │           │
│  │  注册表)              │  │                      │           │
│  └──────────────────────┘  └──────────────────────┘           │
└───────────────────────────────────────────────────────────────┘
```

### 3.2 关键设计选择

| 选择 | 理由 |
|------|------|
| 协议 v2 用 ID 寻址，DEFINE 表周期重发 | 省带宽、丢包可恢复、上位机后接入无需重启 |
| **协议与设备解耦**：上位机不内置任何型号的通道/状态/事件表，全部由 DEFINE 帧动态下发 | afd01/ufd45 及未来型号可各自独立，新增型号**上位机零改动** |
| 下位机 **"协议核心 + 设备 profile"分离**：core 共用，profile 按型号隔离 | 新增型号只写新 profile，不动协议核心 |
| 上位机 UI 完全元数据驱动：Dashboard / StatePanel / Chart 读取 ProfileStore 生成控件 | 通道增减、状态字改名、枚举新增，只要 DEFINE 表变，UI 自动跟随 |
| 分组 Y 轴：按 `group_id` 每组一个 `ViewBox` 叠加 | 不同量纲清晰可读 |
| 批量 emit：Worker 每 20ms 打包一次 signal | 避免 Qt 信号队列堆积 |
| 异步录制：后台 `DataRecorder` 线程 + `queue.Queue` | 解耦 UI |
| 事件日志双存储：内存环形 + `.log` 文件 + `.sdb` 内嵌 | 实时看 + 事后查 + 回放同步 |

### 3.3 设备自适应工作流

```
上位机连接成功
     │
     ▼
┌────────────────┐   META_INFO (含 hw_type)
│  等待握手       │ ◄──────────────  下位机（afd01 / ufd45 / 新型号）
└────────┬───────┘
         │  得到 hw_type 与 table_ver
         ▼
┌────────────────────────────────────┐
│  ProfileStore 按 hw_type 取/建桶    │
│  - 本地缓存命中 → 立即渲染 UI       │
│  - 同时比对 table_ver，不一致则等   │
│    新 DEFINE 帧覆盖                 │
└────────┬───────────────────────────┘
         │
         │  CHANNEL_DEFINE / STATE_DEFINE / EVENT_DEFINE
         ▼
┌────────────────────────────────────┐
│  Profile 完整 → UI 按 profile 重建 │
│  - Dashboard 卡片列表               │
│  - Chart 分组结构                   │
│  - StatePanel 指示灯清单            │
│  - EventTimeline 事件色标映射       │
└────────┬───────────────────────────┘
         │
         ▼
    正常数据流
```

**ProfileStore 本地缓存**：

- 路径：`~/.satellite_debug_tool/profiles/{hw_type}_{table_ver}.json`
- 命中则 UI 即刻渲染（不用等 5s DEFINE 重发周期）
- `table_ver` 不一致立即失效、等下位机重发最新表
- 支持手动导出/导入（多人协作、离线分析场景）

**录制文件的设备自适应**：`.sdb v2` 文件头内嵌当前 profile JSON 快照，回放时按内嵌 profile 渲染 UI；若文件头无 profile（v1 旧文件），回退到"通用/最小"模式（通道按 `unknown_<id>` 显示）。

---

## 4. UI 详细设计

### 4.1 主窗口总体布局

```
┌────────────────────────────────────────────────────────────────────────┐
│ 菜单: 文件  视图  工具  帮助                                            │
├────────────────────────────────────────────────────────────────────────┤
│ 工具栏: [连接▾] [断开] [● 录制] [▶ 回放] │ [⚑ 标记] [⏸ 暂停曲线]        │
│        │ 采样率: [100▾]Hz │ 主题: [深色▾] │ 字号: [中▾]                │
├────────────────────────────────────────────────────────────────────────┤
│ ── 顶部指示灯带 (StatusStrip) ────────────────────────────────────────  │
│  🟢 LINK 50ms  🟢 LOCK  🟢 GPS 3D(9★)  🟢 INS  🟢 PLL  🟡 SNR 18.2  │
├────────────────────────────────────────────────────────────────────────┤
│ ┌─ Dashboard (高度可折叠, 默认展开) ─────────────────────────────────┐ │
│ │ ┌──SNR──┐ ┌──AZ──────┐ ┌──EL──────┐ ┌──姿态──────┐ ┌──误差─────┐ │ │
│ │ │ 18.2  │ │ 目 125.3 │ │ 目 42.1  │ │ R   0.5°   │ │ dAz 0.3°  │ │ │
│ │ │  dB   │ │ 实 124.9 │ │ 实 41.8  │ │ P  -1.2°   │ │ dEl 0.3°  │ │ │
│ │ │ ∿∿∿   │ │ Δ   0.4° │ │ Δ   0.3° │ │ Y 182.4°   │ │           │ │ │
│ │ └───────┘ └──────────┘ └──────────┘ └────────────┘ └───────────┘ │ │
│ │   TRACE_MODE: [LOCK]    模式切换按钮: [AUTO] [MANUAL] [STANDBY]   │ │
│ └────────────────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────┬──────────────────────────────┐│
│ │                                      │  ┌── 状态指示灯板 ──────┐   ││
│ │   ┌ 分组曲线 (可单独缩放/暂停) ──┐   │  │ 🟢 TRACE_MODE=LOCK   │   ││
│ │   │ Y1: 姿态  R/P/Y              │   │  │ 🟢 LOCK_FLAG         │   ││
│ │   │ Y2: 指向  tgt/ant AZ/EL      │   │  │ 🟢 INS_READY         │   ││
│ │   │ Y3: SNR                      │   │  │ 🟢 PLL_LOCKED        │   ││
│ │   │ Y4: 误差  err_az/el          │   │  │ 🟢 MODEM_CONNECTED   │   ││
│ │   └──────────────────────────────┘   │  │ ⚫ PA_ENABLED         │   ││
│ │                                      │  │ ⚫ ANT_ENABLED        │   ││
│ │                                      │  │ 🟢 WIZNET_LINK       │   ││
│ │   ┌ 3D 场景 ───────────────────┐     │  └──────────────────────┘   ││
│ │   │  [飞机+波束锥+卫星矢量]     │     │  ┌── 事件时间线 ────────┐   ││
│ │   │                            │     │  │ 10:23:45 🟢 LOCK_ACQ │   ││
│ │   └────────────────────────────┘     │  │ 10:23:12 🟡 SCAN_WIDE│   ││
│ │                                      │  │ 10:22:58 ℹ️ TLE_LOAD  │   ││
│ └──────────────────────────────────────┴──│ 10:22:01 🔴 PLL_UNLK │───┘│
│                                           │ ...                  │    │
├───────────────────────────────────────────└──────────────────────┘────┤
│ 通道选择 (可折叠): ☑roll ☑pitch ☑yaw ☑snr ☑tgt_az ☑ant_az ...        │
├────────────────────────────────────────────────────────────────────────┤
│ 状态栏: 链路 UDP 4004 │ 帧率 100Hz │ 丢帧 0 │ CRC错 0 │ 录制 00:15:32 │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Dashboard Widget（新增）

**职责**：显示 `critical` 标志的通道和状态字，确保司机/测试员一眼看到。

**完全元数据驱动**：Dashboard 不认识 "SNR" 或 "tgt_az" 这种具体名字——它只读 ProfileStore 里 `flags.critical == 1` 的通道/状态字，按顺序生成卡片。afd01 上显示 SNR/AZ/EL 卡片，ufd45 上可能显示完全不同的卡片，代码无需改动。

组件：
- **KPI 卡片**（由 `flags.critical` 数据通道驱动）：
  - 大号数字（36~48pt 等宽字体）+ 单位（来自 `channel_def.unit`）
  - 右下角 sparkline（最近 30s 迷你曲线）
  - 背景色随阈值变：超出 `[display_min, display_max]` 时告警色
  - **卡片合并规则**：通道名遵循 `{prefix}_{axis}` 模式且成对出现（如 `tgt_az/ant_az`、`err_az/err_el`），自动合并为"目标 / 实际 / Δ"三行卡片。不匹配的就单卡显示。
- **模式切换按钮区**：检测到任一 ENUM 状态字带 `flags.critical` 时，自动生成该枚举的按钮组；点击按钮时通过 `CONTROL.SET_TRACE_MODE`（或扩展子命令）下发。按钮组标题 = 状态字 name，按钮文本 = 枚举项 name。

**实现位置**：新增 `ui/dashboard_widget.py`，顶层嵌在主窗口上方。

### 4.3 StatusStrip（顶部状态灯带）

横向一行，高度 32px，放最关键的 ≤8 个条目。**条目列表完全由 profile 决定**：
- 固定项：LINK（HEARTBEAT 最近延迟，协议级通用）、RECORDING（本机状态）
- 动态项：从 ProfileStore 筛出 `flags.critical == 1` 的状态字和通道，最多取 6 条

颜色规则由 state_type 决定：
- **BOOL** 状态字：按 `flags.inverse` 判断（默认 1=绿、0=灰；inverse 时 0=红、1=绿）
- **ENUM** 状态字：颜色取自 DEFINE 帧里每个枚举项的 `level` 字段（INFO=绿、WARN=黄、ERROR=红、NEUTRAL=灰）
- **critical 通道**：超出 `[display_min, display_max]` 为红，否则绿

> 所有颜色决定权在下位机 profile，上位机不做"SNR<15 就报黄"这种硬编码判断。

**实现位置**：新增 `ui/status_strip_widget.py`。

### 4.4 StatePanelWidget（状态灯板）

右侧栏上半部，**显示所有 state_id 的当前值**：
- BOOL：圆点灯 + 名称
- ENUM：名称 + 当前枚举值文本（文本色随 enum.level）
- 最近变化的项短暂高亮 2 秒

支持按子系统（trace / modem / ins / rf）折叠分组。

**实现位置**：新增 `ui/state_panel_widget.py`。

### 4.5 EventTimelineWidget（事件时间线）

右侧栏下半部，反序列表展示最近事件：

```
HH:MM:SS.mmm  [LVL]  EVENT_NAME    payload
```

- 级别用色标（info 蓝、warn 黄、error 红）
- 双击事件：曲线跳到该时刻（回放模式下）
- 支持级别过滤、关键字搜索
- 右键"在曲线上定位"

**实现位置**：新增 `ui/event_timeline_widget.py`。  
**后端**：`core/data/event_log.py`（内存环形 + 可选落盘）。

### 4.6 ChartWidget 重构（分组 Y 轴）

从单 PlotWidget 改为 **多 PlotItem 纵向堆叠**（一个 group 一个 PlotItem）：

- 按 `group_id` 自动创建子图，共享 X 轴
- 每组独立 Y 轴、独立鼠标缩放
- 顶部每组一个标题行（组名 + 暂停 ⏸ 按钮）
- 全局"暂停曲线"按钮冻结所有组
- 使能 `setDownsampling(auto=True)` + `setClipToView(True)`
- 鼠标滚轮缩放、左键拖动、右键菜单（重置视图 / 保存图片）

**新增控件**：
- 时间窗口滑块（5s / 30s / 1min / 5min / 全部）
- 标记竖线：接到 `EVENT_REPORT` 时在所有子图画一条半透明竖线，悬停显示事件名

**实现位置**：重写 `ui/chart_widget.py`（保留 API 向后兼容）。

### 4.7 AttitudeWidget → Scene3DWidget

升级为"飞机 + 卫星 + 波束 + 轨迹"组合场景。**通道绑定可配置**：
- 用户通过下拉框把三路欧拉角通道（roll/pitch/yaw）和两对指向角通道（tgt_az/el、ant_az/el）映射到 3D 场景的对应输入
- 映射方案按 `hw_type` 记忆到 `settings.json`，下次自动恢复
- 若 profile 里存在带 `semantic_hint` 的通道（协议 v2.1 可扩展），自动预填；否则用户手选

场景元素：

- 载体坐标系飞机（保留现状）
- **卫星矢量**：从绑定的 `tgt_az/el` 计算单位矢量，红色长线
- **天线实际法向**：从绑定的 `ant_az/el` 画绿色线
- **误差夹角区域**：两矢量间填充半透明扇面
- **扫描轨迹**：最近 N 秒 `ant_az/el` 的历史轨迹用淡蓝点迹
- 视角控制：鼠标拖动旋转、滚轮缩放
- 无可用指向通道时（比如新型号 profile 没有相关通道），3D 场景自动只显示飞机，不崩溃

**实现位置**：`ui/attitude_widget.py` → 重命名 `ui/scene3d_widget.py`（或保留文件，类改名）。

### 4.8 ControlPanelWidget（控制面板）

独立面板，放在 Dashboard 右下或工具栏弹出：
- 跟踪模式按钮组
- 采样率下拉（5 / 10 / 50 / 100 / 200 Hz）→ `CONTROL.SET_SAMPLE_RATE`
- 通道使能复选框矩阵 → `CONTROL.CHANNEL_ENABLE_MASK`
- TLE 导入（文件选择 → 通过 `CONTROL` 子命令发送，v2 预留扩展点）
- 标记按钮（发 `CONTROL.USER_MARK`）+ 标记文本输入

**实现位置**：新增 `ui/control_panel_widget.py`。

### 4.9 主题 / 字体改进

扩展 `ui/styles.py`：

| 主题 | 背景 | 文字 | 适用 |
|------|------|------|------|
| dark | #1E1E1E | #CCCCCC | 桌面开发 |
| **dark_hc** | #000000 | #F0F0F0 | 车载（高对比度） |
| light | #FAFAFA | #222222 | 日间外场 |

字号档位：小（默认 12）/ 中（14）/ 大（18）/ 超大（22）。  
Dashboard 数字字体锁定 **JetBrains Mono / Consolas / SF Mono** 等宽，不受档位影响以外的换字。

---

## 5. 上位机改造任务分解

### 5.1 协议层（`core/protocol/`）

决策（v1.2）：**v2 全量替换，不保留 v1 实现**。旧文件 `frame_receiver.py` / `data_frame.py` / `helpers.py` 删除并重写。

| 任务 | 文件 | 说明 |
|------|------|------|
| v2 常量与数据类 | `frame_v2.py`（新增） | 10 类命令枚举、SubCmd 子命令、ChannelDef/StateDef/EventDef/... 数据类 |
| v2 编解码 | `codec_v2.py`（新增） | `build_frame` + 各 DECODE；CRC16 复用 `crc16.py`；所有 CONTROL 子命令构造函数 |
| v2 状态机 | `frame_receiver_v2.py`（新增） | 字节流 → `FrameV2Record`，按 cmd_type 分发至 codec，含 CRC/framing/decode 计数 |
| 握手状态机 | `core/protocol/handshake.py`（新增） | 管理"等待 META → 等待 DEFINE → Ready"，超时重发 REQUEST_* |
| 删除 v1 | `frame_receiver.py` `data_frame.py` `helpers.py` | 直接删除 |

### 5.2 业务层 / 数据层（`core/data/`）

| 任务 | 文件 | 说明 |
|------|------|------|
| **ProfileStore** | `core/profile_store.py`（新增） | **核心组件**。按 `hw_type` 分桶存储 channel/state/event 元数据；支持本地 JSON 缓存 + 导入导出；DEFINE 帧到达时比对 `table_ver` 增量更新；profile 变更时发 Qt signal `profile_changed(hw_type)` 让 UI 重建 |
| DataStore | `core/data/data_store.py` | 改造：不再按"通道名"而按 `(hw_type, channel_id)` 寻址 |
| StateStore | `core/data/state_store.py`（新增） | 存储所有 state_id 当前值 + 最近变化时间，Qt signal 通知 UI |
| EventLog | `core/data/event_log.py`（新增） | 环形缓冲（5000 条），按时间/级别索引；事件文本从 ProfileStore 查 |
| ChannelBuffer 加锁 | `channel_buffer.py` | 加 `threading.Lock`（读写竞态） |
| MarkerStore | `core/data/marker_store.py`（新增） | 存储用户标记，用于曲线竖线绘制 |

**ProfileStore 对外接口（草案）**：

```python
class ProfileStore:
    profile_changed = Signal(str)   # hw_type

    def load_cached(self, hw_type: str, table_ver: int) -> bool: ...
    def apply_channel_define(self, hw_type, table_ver, channels: list[ChannelDef]): ...
    def apply_state_define(self, hw_type, table_ver, states: list[StateDef]): ...
    def apply_event_define(self, hw_type, table_ver, events: list[EventDef]): ...

    def get_channels(self, hw_type) -> list[ChannelDef]: ...
    def get_states(self, hw_type) -> list[StateDef]: ...
    def get_event(self, hw_type, event_id: int) -> EventDef | None: ...

    def export(self, hw_type, path: Path): ...    # 导出 JSON
    def import_(self, path: Path): ...            # 导入 JSON（离线分析 .sdb 用）

    def current_hw_type(self) -> str | None: ...  # 当前连接的设备型号
```

### 5.3 通信层（`core/comm/`）

| 任务 | 文件 | 说明 |
|------|------|------|
| 批量 emit | `base_worker.py` `serial_worker.py` `udp_worker.py` | 改为每 20ms 把累积的帧列表一次 emit |
| 心跳监测 | `base_worker.py` | 3s 无 HEARTBEAT → emit link_lost 信号 |
| 重连握手 | `main_window.py` | 连接成功后自动发 4 条 REQUEST 帧 |

### 5.4 UI 层

| 任务 | 文件 | 新增/修改 |
|------|------|-----------|
| Dashboard | `ui/dashboard_widget.py` | 新增 |
| StatusStrip | `ui/status_strip_widget.py` | 新增 |
| StatePanel | `ui/state_panel_widget.py` | 新增 |
| EventTimeline | `ui/event_timeline_widget.py` | 新增 |
| Chart 分组 | `ui/chart_widget.py` | 重构 |
| Scene3D | `ui/attitude_widget.py` | 重构 |
| ControlPanel | `ui/control_panel_widget.py` | 新增 |
| 主窗口整合 | `ui/main_window.py` | 重构布局 + 握手/批量更新 |
| 主题扩展 | `ui/styles.py` | 加 dark_hc + 字号档位 |

### 5.5 IO 层

| 任务 | 文件 | 说明 |
|------|------|------|
| 异步录制 | `io/data_recorder.py` | 后台线程 + queue，UI 只 put |
| SDB 格式扩展 v2 | `io/data_recorder.py` | 文件头版本字段；record 支持状态/事件/标记帧 |
| SDB 回放兼容 | `io/data_importer.py` | 读 v1/v2，v2 能同步回放事件时间线 |
| 文件日志 | `io/log_writer.py`（新增） | 按会话自动生成 `.log`，带时间戳分级 |

### 5.6 测试

| 任务 | 文件 |
|------|------|
| FrameV2 / codec_v2 / receiver_v2 | `tests/test_frame_v2.py` `tests/test_codec_v2.py` `tests/test_frame_receiver_v2.py`（新增） |
| ProfileStore | `tests/test_profile_store.py`（新增） |
| StateStore | `tests/test_state_store.py`（新增） |
| EventLog | `tests/test_event_log.py`（新增） |
| Handshake | `tests/test_handshake.py`（新增） |
| AsyncRecorder | `tests/test_data_recorder.py` 重写 |
| v1 旧测试 | `tests/test_frame_receiver.py` `tests/test_data.py` `tests/test_io.py` 重写或删除 |

---

## 6. 下位机改造任务分解

**设计原则**：**协议核心 与 设备 profile 物理分离**。协议核心放 `shared/` 下由所有型号共享，profile 放 `target/<hw>/` 下各自独立。新增型号**不改动 shared 任何代码**，只在新 target 下写一份 profile 即可。

### 6.1 协议核心（所有设备共用）

位置：`shared/MiddleWare/components/debug/`

| 任务 | 文件 | 说明 |
|------|------|------|
| 扩展 `debug.h` | `debug.h` | 新增 cmd 枚举、数据结构、注册 API 原型 |
| 实现 v2 打包 | `debug.c` | 各 `build_*_frame` 函数 |
| 注册表实现 | `debug_registry.c`（新增） | 通道/状态字/事件动态表，支持 register/lookup/serialize_define |
| 周期任务 | `debug_task.c`（新增） | 独立 FreeRTOS 任务：100Hz DATA、5Hz STATE 全量、1Hz HEARTBEAT、0.2Hz DEFINE 重发 |
| 事件队列 | `debug.c` | 环形队列，事件触发入队；1s 去重窗口 |
| 开关 | `debug.h` / CMake | 使用既有 `USE_DEBUG` 开关整体启停；**不保留 v1 协议实现**（决策 v1.2） |

### 6.2 设备 profile 层（按型号独立）

每个型号一个 profile 文件，集中声明该型号所有通道/状态字/事件。协议核心启动时调用 profile 的 register 入口。

位置：`target/<hw>/application/app/debug/`

| 文件 | 归属 | 职责 |
|------|------|------|
| `afd01_debug_profile.c` | afd01 | afd01 的所有通道/状态字/事件注册 + `hw_type="afd01"` |
| `afd01_debug_profile.h` | afd01 | 导出 `afd01_debug_profile_register()` 入口 |
| `ufd45_debug_profile.c` | ufd45 | ufd45 专属 profile（通道/状态字/事件可与 afd01 **完全不同**）|
| `ufd45_debug_profile.h` | ufd45 | 同上 |

**profile 内部结构**（afd01 示例骨架）：

```c
void afd01_debug_profile_register(void)
{
    debug_v2_set_meta(AFD01_FW_VERSION, "afd01", get_device_sn());

    /* channels —— 见协议文档附录 C.1 */
    debug_v2_channel_register(0, "roll",  "°", GRP_ATTITUDE, FLAG_CRITICAL, -180, 180);
    /* ... */

    /* states —— 见协议文档附录 C.2 */
    debug_v2_state_register_enum(0, "TRACE_MODE", FLAG_CRITICAL,
                                 trace_mode_items, ARRAY_SIZE(trace_mode_items));
    /* ... */

    /* events —— 见协议文档附录 C.3 */
    debug_v2_event_register(0x0003, LVL_INFO, "LOCK_ACQUIRED");
    /* ... */
}
```

ufd45 profile 的 ID / 名称 / 枚举 完全独立规划，与 afd01 零耦合。

### 6.3 业务模块接入点

各业务模块只负责**喂数据**，不负责定义通道（定义全在 profile 里）。

| 模块 | 接入方式 |
|------|----------|
| trace | 周期回调里 `debug_v2_data_set(ID_ROLL, ...)`；模式变化时 `debug_v2_event_trigger(...)` |
| locate/ins | 同上 |
| modem / transceiver / beampointing / system | 同上 |

> 具体哪些 ID 归哪个模块由该型号的 profile 决定，代码里用宏或 enum 符号引用（如 `AFD01_CH_ROLL`），避免魔数散落。

### 6.4 参数 / 配置

- 协议级参数（所有型号共享）：
  - `debug_sample_rate`（默认 100 Hz，范围 5~200）
  - `debug_enabled_channels_mask`（默认 0xFFFF）
- 参数写入 FRAM，上位机 `CONTROL.SET_SAMPLE_RATE` 立即生效且持久化

### 6.5 构建系统

- `shared/MiddleWare/components/debug/CMakeLists.txt` 编译协议核心（独立库）
- 各 target 的 `application/CMakeLists.txt` 仅把自己的 profile.c 加入编译
- 通过 `USE_DEBUG` 编译特性开关控制是否启用整个 debug 子系统
- 新增型号时：复制一份 profile.c 改 ID/名字即可，**不动 shared**

---

## 7. 里程碑

| 阶段 | 交付物 | 预计工作量（相对） |
|------|--------|-------------------|
| **M1 协议落地 + Profile 骨架** | 下位机：debug 协议核心 + afd01 profile。上位机：FrameReceiver v2 + 握手状态机 + **ProfileStore（关键）** + profile 本地缓存。两端能完成 META/CHANNEL/STATE/EVENT DEFINE 握手并跑 DATA_REPORT | 1.2 |
| **M2 状态/事件** | STATE/EVENT 帧 + StateStore/EventLog + StatePanel（profile 驱动）+ EventTimeline | 1 |
| **M3 Dashboard + 分组曲线** | Dashboard/StatusStrip/ControlPanel（全部 profile 驱动）+ Chart 分组 Y 轴 | 1.2 |
| **M4 3D 场景 + 标记** | Scene3D 通道绑定可配 + USER_MARK 闭环 + 曲线标记 | 0.8 |
| **M5 异步录制 + ufd45 profile 冒烟** | 异步 Recorder + SDB v2（含内嵌 profile）+ **ufd45_debug_profile 骨架 + 跨设备切换验收** | 1 |
| **M6 主题/字号/文档** | dark_hc + 字号档位 + 用户手册更新 | 0.3 |

每个 M 独立可验收；M1 完成即可在桌面看到原有信息量（不丢功能），M2 起开始有新价值，M3 起具备车载可用性，M5 完成多设备自适应闭环。

---

## 8. 验收标准

### 8.1 功能验收

| 编号 | 功能 | 验收条件 | 阶段 |
|------|------|----------|------|
| F-01 | 协议握手 | 上位机连接后 500ms 内收到 META_INFO + 三张 DEFINE 表 | M1 |
| F-02 | 数据上报 | 100Hz 下连续 10 分钟不丢帧、CRC 错 = 0 | M1 |
| F-04 | 状态字 | 下位机 LOCK_FLAG 切换，上位机 200ms 内显示 | M2 |
| F-05 | 全量重发 | 上位机中途重启，重新握手后状态板所有灯恢复正确 | M2 |
| F-06 | 事件上报 | LOCK_ACQUIRED 事件触发，时间线立即显示，曲线画竖线 | M2 |
| F-07 | 用户标记 | 上位机点标记按钮，下位机 300ms 内回 EVENT(0xFFFF) | M4 |
| F-08 | Dashboard | SNR/AZ/EL/姿态/误差 5 类卡片根据 CHANNEL_DEFINE.critical 自动生成 | M3 |
| F-09 | 曲线分组 | SNR 和姿态角显示在不同 Y 轴，都清晰可读 | M3 |
| F-10 | 曲线交互 | 鼠标滚轮缩放、拖动、右键复位、暂停按钮冻结 | M3 |
| F-11 | 3D 场景 | 飞机 + 卫星矢量 + 天线矢量 + 误差扇面同时渲染正确 | M4 |
| F-12 | 录制 | 100Hz 数据连续录制 1 小时，UI 帧率不低于 30fps | M5 |
| F-13 | 回放 | .sdb v2 回放时事件时间线与曲线同步 | M5 |
| F-14 | 模式切换 | 点击 AUTO/MANUAL 按钮，下位机 trace_mode 切换并回 resp | M3 |
| F-15 | 高对比主题 | 切到 dark_hc 后所有文字在 500cd/m² 阳光屏仍可读 | M6 |

### 8.2 性能验收

| 编号 | 指标 | 验收条件 |
|------|------|----------|
| P-01 | 下位机 CPU 增量 | 启用 v2 后相比 v1 CPU 增加 ≤ 1%（100Hz 上报）|
| P-02 | 下位机带宽 | 总上报带宽 ≤ 15 KB/s |
| P-03 | 下位机 RAM 增量 | 相比 v1 增加 ≤ 2 KB |
| P-04 | 上位机 CPU | 100Hz × 16 通道，CPU ≤ 25%（Intel i5 2020+）|
| P-05 | 上位机内存 | 运行 2 小时，内存 ≤ 600 MB |
| P-06 | UI 帧率 | 曲线 + Dashboard + 3D 同时工作下 ≥ 30 fps |
| P-07 | 事件延迟 | 下位机触发到上位机显示 < 300 ms |
| P-08 | 录制延迟 | 录制写盘不阻塞 UI，最大卡顿 < 50 ms |

### 8.3 UI 验收

| 编号 | 指标 | 验收条件 |
|------|------|----------|
| U-01 | 一眼可读 | 外场测试者 3 米外能读到 SNR、锁星灯、模式 |
| U-02 | 指示灯含义 | 每个灯悬停显示含义 tooltip |
| U-03 | 事件追溯 | 任何一个事件可双击跳转到曲线时刻 |
| U-04 | 车载屏适配 | 1920×1080 和 1366×768 下布局不溢出、不遮挡 |
| U-05 | 字号切换 | 超大字号下 Dashboard 数字宽度不截断 |

### 8.4 协议版本验收

| 编号 | 条件 | 验收条件 |
|------|------|----------|
| C-03 | 协议版本字段 | META_INFO.protocol_ver == 0x02；上位机收到非 0x02 时提示并断连 |

> 决策 v1.2：不再维护 v1 兼容层。原 C-01/C-02 验收条目移除。

### 8.5 多设备自适应验收

| 编号 | 场景 | 验收条件 |
|------|------|----------|
| D-01 | afd01 → ufd45 切换（物理切换设备） | 上位机无需重启，UI 在 2 秒内按 ufd45 profile 重建 Dashboard / StatePanel / Chart 分组 |
| D-02 | 未知型号（新增的 hw_type） | 上位机正常工作，UI 按下位机下发的 DEFINE 表渲染，无需上位机代码改动 |
| D-03 | Profile 缓存命中 | 上位机二次连接同型号时，未收到新 DEFINE 前即按本地缓存渲染 UI |
| D-04 | Profile 版本升级 | 下位机 `table_ver` 递增后，上位机旧缓存自动失效，以新 DEFINE 为准 |
| D-05 | Profile 导出导入 | 导出的 JSON 可被离线工具直接解析 .sdb 文件 |
| D-06 | .sdb 回放跨设备 | afd01 录的 .sdb 在只连过 ufd45 的机器上能正确回放（文件头内嵌 profile 生效）|
| D-07 | 未知 channel_id | 上位机按 `unknown_<id>` 显示曲线，不丢弃数据 |
| D-08 | 未知 event_id | 上位机显示 `EVENT_<hex>`，不崩溃 |
| D-09 | 无事件型号 | 下位机 profile 未注册任何事件时，EventTimeline 显示为空面板但不崩溃 |
| D-10 | 零通道型号 | 下位机 profile 未注册任何数据通道时，Chart 区域显示"无数据"但 Dashboard/StatePanel 仍可工作 |

---

## 9. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 下位机 v2 改动影响现有 trace 逻辑 | 既有 `trace.c` 中的 `debug_report_data` 调用点重写为 v2 API；单独 M1 验证 trace 数据路径 |
| UDP 丢包导致 DEFINE 表错位 | DEFINE 每 5 秒重发 + table_ver 校验 + 上位机 REQUEST 重传 |
| 上位机重构工作量大，中途不可用 | 按 M1~M6 分阶段，每个阶段都可独立发布 |
| 离散量上报频率错误估算 | 压力测试阶段（M5）用模拟器回放 6 小时数据验证 |
| v1 格式旧 `.sdb` 文件无法读取 | 不再维护 v1 兼容；若确有需要，提供独立 `sdb_v1_convert.py` 一次性转换脚本（非主线任务） |
| 字体/主题在不同系统表现不一致 | 内置 JetBrains Mono 字体，不依赖系统字体 |
| afd01 / ufd45 profile ID 空间冲突 | 无需担心——每个 hw_type 在上位机 ProfileStore 里独立分桶，ID 空间互不可见 |
| 新型号发布时临时要求上位机改动 | 按设计不应发生；若确实需要，只能是"协议层能力不足"，走协议 v2.x 升级流程而非改 UI |
| 下位机 profile 注册遗漏（如忘记注册 LOCK_FLAG）| 上位机 StatePanel 该灯不存在，行为正常（不崩溃）；测试验收表 F-04 会暴露遗漏 |

---

## 10. 不做的事

为避免范围蔓延，本 plan 明确**不包含**：

- TLE 星历编辑器（仅预留 CONTROL 扩展点，具体指令在 v2.1 再议）
- OTA 固件升级 UI（沿用现有命令行流程）
- 多设备**同时**连接（UI 一次只连一个设备；hw_type 切换时整体重建界面）
- 在上位机里**硬编码**任何 afd01/ufd45 特定逻辑（所有设备差异必须通过 profile 表达；若 profile 无法表达，走协议扩展而非 UI 分支）
- 协议核心里**硬编码**任何型号逻辑（所有型号差异只能出现在 `target/<hw>/debug_profile.c` 中）
- 网页版 / 移动端
- 自动告警短信 / 钉钉推送

---

## 11. 开发纪律

- 每个 M 阶段完成时，对照 §8 验收表跑一遍，结果记录到 `doc/acceptance_log.md`
- UI 改动保留截图（放 `doc/screenshots/`）
- 协议任何补充修改都要同步更新 `DEBUG设备协议接口规范_v2.md` 并递增 `protocol_ver`
- 上位机改动遵循原有目录约定（UI/core/io 分层）

---

文档结束。等待用户确认方案后，再进入实现阶段。
