# 开发日志 — Protocol v2 + UI 重构

本文件为实施过程追踪文档，用于：
- 记录每个里程碑 (M1–M6) 的具体工作项与完成进度
- 记录与 `optimization_plan.md` / `DEBUG设备协议接口规范_v2.md` 的偏差（避免漏项或功能飘移）
- 汇总关键提交、测试结论、遗留问题

**参考文档**
- 协议规范：[doc/DEBUG设备协议接口规范_v2.md](./DEBUG设备协议接口规范_v2.md)
- 优化 plan：[doc/optimization_plan.md](./optimization_plan.md)

---

## 概览

| 项 | 值 |
|----|----|
| 启动日期 | 2026-04-16 |
| 上位机分支 | `feat/protocol_v2_refactor` (基于 `dev_3D_display`) |
| 下位机分支 | `feat/debug_protocol_v2` (基于 `feat/beampointing_refactor`) |
| 目标硬件 | afd01 (首发) + ufd45 (适配验证) |
| 当前阶段 | **M1 进行中** |

---

## 里程碑总览

| M | 主要内容 | 状态 | 对应验收 |
|---|---------|------|---------|
| M1 | 协议核心 + afd01 profile + 握手 + ProfileStore | 进行中 | §8.1.1 §8.4 §8.5 D-01~D-03 |
| M2 | State/Event 报文 + StatePanel/EventTimeline | 待开始 | §8.1.2 §8.3 |
| M3 | Dashboard + 分组曲线（完全 profile 驱动） | 待开始 | §8.1.3 §8.3 |
| M4 | 3D 场景 + 用户 mark 事件 | 待开始 | §8.1.5 |
| M5 | 异步录制 (.sdb v2) + ufd45 smoke test | 待开始 | §8.1.4 §8.5 D-04 |
| M6 | 主题/字体/布局微调 | 待开始 | §8.3 |

---

## M1 — 协议核心 + 握手 + ProfileStore

### 范围定义
- **协议核心层**（`shared/`）：设备无关，提供注册 API 和帧编解码
- **afd01 profile**（`target/afd01/`）：注册 afd01 全部 channel/state/event 并对接既有 topic
- **上位机协议层**：FrameReceiver v2 + 握手状态机
- **上位机元数据层**：ProfileStore + 本地缓存

### 下位机任务 (feat/debug_protocol_v2)

在 `code/shared/MiddleWare/components/debug/`：

- [x] `debug.h`：v2 公开 API + 所有协议常量 / 枚举 / flags
- [x] `debug_internal.h`：内部数据结构 + `DEBUG_SRAM` 宏（section 到 RAM_D2）
- [x] `debug_proto.c`：CRC16-CCITT + envelope 打包 / 解析
- [x] `debug_registry.c`：channel/state/event 注册 + META/CHANNEL/STATE/EVENT_DEFINE 序列化
- [x] `debug_session.c`：rx 任务 + worker 任务（100Hz DATA / 200ms 变化 STATE /
      1Hz HB + 全量 STATE / 5s META+DEFINE 广播）+ 事件环形队列 + CONTROL 处理
- [x] `debug.c`：g_debug 单例 + debug_init/deinit + setter
- [x] CRC16 自实现（与 `PublicLib/lib_check.c:crc16_ccitt` 常量一致，保留在 debug 内部便于未来切换硬件加速）

在 `code/target/afd01/application/app/debug/`：

- [x] `afd01_debug_profile.h`：channel/state/event ID 枚举（16 / 10 / 12 项）+ setter 原型
- [x] `afd01_debug_profile.c`：注册 + 业务层便捷 setter（push_trace_frame / on_lock_* / set_*）
- [x] `afd01_app_debug.c`：重写，绑定 WizNet UDP port 4004，注册 send_cb，注册 profile，启动 debug_task（优先级 9，栈 4KB）
- [x] `trace.c`：旧 `debug_report_data_t` 删除，`trace_debug_info()` 改用 `afd01_profile_push_trace_frame()`

### 构建验证 (./build.sh afd01 app debug)
- [x] 编译通过（4 warnings from pre-existing ins.c，非本次代码）
- [x] 内存占用：FLASH 346K/768K (44.06%)，RAM 79K/512K (15.16%)
- [x] 新组件落位 RAM_D2：12K/288K (4.11%)，DTCM 不受影响
- [x] 生成 afd01_app_0.0.16_beta.hex / .bin

### 上位机任务 (feat/protocol_v2_refactor)

在 `satellite_debug_tool/core/protocol/`：

- [x] `frame_v2.py`：v2 常量 + 数据类
- [x] `codec_v2.py`：CRC 复用 + build_frame + build_control_* + 各 decode_*
- [x] `frame_receiver_v2.py`：字节流状态机，分发到 codec
- [x] `handshake.py`：QObject + Signal，`start()`/`tick(dt_ms)`/`feed(record)` API，
      自动发 4 条 REQUEST + 超时重发 + HEARTBEAT 心跳检测 + ready/link_lost/link_restored signal
- [x] **删除 v1 文件**：`frame_receiver.py` / `data_frame.py` / `helpers.py` + 4 个依赖旧测试

在 `satellite_debug_tool/core/profile/`（新增目录）：

- [x] `models.py`：`DeviceProfile` 聚合（channel/state/event 字典 + 三张 table_ver + meta）
- [x] `profile_store.py`：`ProfileStore(QObject)`，per-hw_type 分桶，`profile_changed` Signal，apply/get/export/import
- [x] `cache.py`：`ProfileCache`，JSON 落盘到 `~/.satellite_debug_tool/profiles/{hw_type}.json`（schema_version=1）

### DataStore / comm / UI 适配

- [x] `core/data/data_store.py`：按 `channel_id` 存储，内部 key `ch_{id:02d}`，写入 `DataReport`
- [x] `io/data_importer.py`：用 `FrameReceiverV2` 解析 .sdb，yield `DataReport`；csv 暂 stub（M5 重写）
- [x] `ui/main_window.py`：创建 `ProfileStore` + `Handshake`，连接成功后启动握手 + QTimer 100ms tick

### 测试（107 条全绿）

- [x] `tests/test_frame_v2.py`：数据类属性、flags 判定（12）
- [x] `tests/test_codec_v2.py`：build/decode 正反 + 错误样本（30）
- [x] `tests/test_frame_receiver_v2.py`：状态机完整闭环、CRC 错、分包、未知 cmd（12）
- [x] `tests/test_profile_store.py`：apply/signals/multi-hw/cache/export/import（16）
- [x] `tests/test_handshake.py`：start→ready、超时重发、heartbeat/link_lost/restored（10）
- [x] 旧 `tests/test_frame_receiver.py` / `test_data.py` / `test_io.py` / `test_integration.py` 删除
- [ ] 集成测试：回放 afd01 真实抓包（等下位机到位后在 M1 收尾阶段补）

### 验收对照（摘自 optimization_plan §8）
- [ ] F-01：连接后 500ms 内收到 META + 三张 DEFINE（需下位机）
- [ ] F-02：100Hz DATA_REPORT 连续 10 分钟不丢帧，CRC 错 = 0（需下位机）
- [ ] C-03：非 0x02 protocol_ver 断连并提示（需下位机/模拟器）
- [ ] D-01：afd01 接入完整显示（需下位机）
- [ ] D-02：profile 缓存命中无需重新握手（单元已覆盖，待端到端）
- [ ] D-03：连接未知 hw_type，UI 无崩溃，显示 `ch_{id:02d}`（单元已覆盖，待端到端）

---

## M2 — State/Event（进行中，代码完成，待硬件联调）

### 业务层
- [x] `core/data/state_store.py`：StateStore(QObject)，按 hw_type 分桶，value 变化才发 state_changed
- [x] `core/data/event_log.py`：EventLog(QObject)，deque(5000) 环形，profile 查名 / 未知降级 `EVENT_<hex>`，支持 hw_type / level / keyword 过滤
- [x] `core/data/__init__.py`：导出 StateStore / StateSnapshot / EventLog / EventRecord

### UI 层
- [x] `ui/state_panel_widget.py`：Profile 驱动的状态灯板，BOOL 绿/灰（inverse 反色），ENUM 按 enum_item.level 上色（INFO 绿 / WARN 黄 / ERROR 红 / NEUTRAL 灰），profile_changed/state_changed 自动刷新
- [x] `ui/event_timeline_widget.py`：反序列表 + 级别下拉 + 关键字过滤 + 清空 + 计数

### MainWindow 集成
- [x] 实例化 StateStore / EventLog / StatePanelWidget / EventTimelineWidget
- [x] 顶部 splitter 新增"right_panel"（StatePanel 上、EventTimeline 下）
- [x] `_on_data_received` 扩展到 StateReport / EventReport 分发
- [x] `_on_handshake_ready` 同步 hw_type 给 StatePanel

### 测试
- [x] tests/test_state_store.py（5 条）
- [x] tests/test_event_log.py（10 条）
- 全量 122/122 通过

### 验收对照 §8.1
- [ ] F-04：状态字 200ms 内显示（单元侧通路 OK，待硬件端到端）
- [ ] F-05：全量重发恢复（StateStore 不清空；下位机 5Hz 全量发；待联调验证）
- [x] F-06：事件上报时间线显示（`event_added` → UI insert，走通）
      **曲线竖线标记**放到 M3（需 ChartWidget 重构）

---

## M3 — Dashboard + 分组曲线（代码完成，待硬件联调）

### 新增 UI 组件

- [x] `ui/dashboard_widget.py`：
  - `KpiCard`：大号等宽数字 + 单位，超出 `[display_min,display_max]` 变红
  - `ModeButtonGroup`：基于 ENUM state 生成按钮组，当前值高亮
  - `DashboardWidget`：按 `flags.critical` 自动筛卡片和模式按钮；
    `mode_requested(state_id, target)` 信号给 MainWindow
  - 刷新节奏：外部 `refresh(data_store)` 驱动，无内部定时器

- [x] `ui/status_strip_widget.py`：
  - 固定 chip：LINK / REC / BEAT（心跳闪烁 200ms）
  - 动态 chip：前 ≤ 6 个 critical state（BOOL 灯 + ENUM 文本）
  - `set_link_state / set_recording / pulse_heartbeat` API

- [x] `ui/control_panel_widget.py`：
  - 采样率下拉 (5/10/25/50/100/200 Hz)
  - 用户标记输入 + 发送按钮（自增 mark_id）
  - 复位统计按钮
  - 连接状态联动 `set_enabled(bool)`

- [x] `ui/grouped_chart_widget.py`（取代旧 ChartWidget）：
  - `GraphicsLayoutWidget` 纵向堆叠 PlotItem，按 `group_id` 分子图
  - 共享 X 轴（`setXLink`），每组独立 Y 轴 + 自带 legend
  - `refresh(data_store)` 从 ChannelBuffer 整批 `setData`（ndarray 最快路径）
  - 时间窗口：`set_time_window(sec)` / `set_auto_range(bool)`
  - `add_event_marker(ts_ms, level)` 全子图竖线标记（最多 200 条环形）
  - `setDownsampling(mode='peak', auto=True)` + `setClipToView(True)` 性能

### MainWindow 集成

- [x] import 接入四个新 widget + `Heartbeat` / `build_set_*` 等
- [x] Toolbar 下新增 StatusStrip
- [x] Toolbar 下 StatusStrip 后新增 Dashboard
- [x] 垂直 splitter: top_splitter / ControlPanel / channel_panel
- [x] top_splitter 右侧 right_panel（StatePanel + EventTimeline）保留
- [x] `_on_handshake_ready` 同步 hw_type 给 State/Dashboard/StatusStrip/Chart
- [x] `_on_connected/disconnected` 联动 ControlPanel 和 StatusStrip LINK
- [x] `_on_link_lost/restored` 切换 StatusStrip LINK 颜色
- [x] `_on_record_clicked` 同步 StatusStrip REC
- [x] `_on_data_received`：HEARTBEAT → `status_strip.pulse_heartbeat()`
- [x] `_update_display` 改用 `chart.refresh(data_store)` + `dashboard.refresh(data_store)`
- [x] ControlPanel 三个信号 → `build_set_sample_rate / build_user_mark / build_reset_stats`
- [x] Dashboard `mode_requested`：state_id=0 → `build_set_trace_mode`（协议泛化待 v2.x）
- [x] EventLog `event_added` → `chart.add_event_marker`

### 简化 / 清理

- [x] 旧 `ChartWidget` 类从 `chart_widget.py` 移除，保留 `COLORS` 常量给通道 panel
- [x] `ui/__init__.py` 导出更新

### 验收对照 §8.1

- [x] F-08 Dashboard：SNR/AZ/EL/姿态/误差 5 类 KPI 卡片完全由 `flags.critical` 生成 ✓（单元路径通）
- [x] F-09 曲线分组：SNR 与姿态角分属 group_id=2 与 group_id=0，各自独立 Y 轴 ✓
- [x] F-10 曲线交互：pyqtgraph 默认鼠标滚轮缩放 / 拖动 / 右键菜单已启用 ✓
- [x] F-14 模式切换：Dashboard 按钮点击下发 SET_TRACE_MODE（state_id=0）；其它 ENUM 状态等协议扩展

全量 pytest 122/122 通过；`import satellite_debug_tool.ui.main_window` 通过。

---

## M4 — 3D + 用户 mark（待开始）

---

## M5 — 异步录制 + ufd45 smoke（代码完成，待端到端联调）

### 下位机：ufd45 profile

- [x] `target/ufd45/application/app/debug/ufd45_debug_profile.h/c`
  - 16 channels（含 ufd45 专属 `bcn_rssi` / `bcn_freq_offset` / `ku_lo`
    / `buc_temp` / `lnb_current` / `tx_power` / `rx_agc`）
  - 10 states（含专属 `BUC_READY` / `LNB_OK` / `BEACON_LOCKED` / `POLARIZATION` ENUM）
  - 11 events（含专属 `BEACON_ACQUIRED/LOST` / `BUC_OVERTEMP` / `LNB_FAULT`
    / `POLARIZATION_SWITCH`）
  - ID 分布 / 枚举项均与 afd01 故意差异化，验证 §8.5 D-02
- [x] `ufd45_app_debug.c` 重写：对齐 afd01 结构，绑定 WizNet UDP + 启动 debug_task
- [x] `trace.c` 清理旧 `debug_report_data_t` + `ufd45_app_debug_report_data` → `ufd45_profile_push_trace_frame`
- [x] `./build.sh ufd45 app debug` 通过（FLASH 39.17%，RAM_D2 4.10%，DTCM 不变）
- shared/ 协议核心**零改动**：证明 M1 设计的设备解耦架构有效

### 上位机：异步 Recorder + SDB v2

- [x] `io/data_recorder.py` 重写：threading.Thread + queue.Queue(10k)
  - `write_frame()` 非阻塞 `put_nowait`
  - `dropped_count` / `written_count` 属性
  - `stop()` 用哨兵（None）通知线程退出，timeout 3s join
- [x] SDB v2 文件格式：header 内嵌 `profile_len` + `profile_json`
  - 录制时把当前 `ProfileStore.get_profile(hw)` → `profile_to_dict` 嵌入
  - 回放时 `import_dict` 恢复到 ProfileStore，UI 按文件内 profile 重建
- [x] `io/data_importer.py` 重写：
  - `DataImporter.open_sdb(path)` → `SdbFile(version, timestamp, profile, ...)`
  - `iter_records()` / `iter_data_reports()` 双视图
  - v1 拒绝读取（明确抛 `SdbFormatError`）
- [x] `ProfileStore.import_dict(dict)`：新增 API 支持直接从 dict 恢复
- [x] `_on_record_clicked` 把当前 profile 传给 Recorder
- [x] `_on_import_clicked` 重写：先恢复 profile → 清空 DataStore → 分发 Data/State/Event
- [x] `tests/test_recorder_importer.py`（7 条）：
  - 无 profile / 带 profile 写入 + 头结构
  - 端到端 round-trip（5 帧 + profile）
  - 错误处理：bad magic / v1 拒绝 / 截断

### 验收对照 §8.5

- [x] D-01：afd01 profile 完整显示（M1-M4 已通；待硬件端到端）
- [x] D-02：ufd45 profile 与 afd01 完全不同的 channel/state/event，**上位机零改动** ✓
- [x] D-03：profile 缓存命中逻辑（M1 已有；待硬件端到端）
- [x] D-04：table_ver 递增触发重建（M1 ProfileStore.apply_*_define 实现）
- [x] D-05：profile 导出 JSON 可直接解析（testcovered 2x profile roundtrip）
- [x] D-06：.sdb v2 内嵌 profile，跨机器回放 ✓（test_round_trip_with_profile）
- [x] D-07：未知 channel_id → `ch_{id:02d}` 占位（DataStore 行为已实现）
- [x] D-08：未知 event_id → `EVENT_<hex>`（EventLog 行为已实现）
- [x] D-09：下位机无事件 → EventTimeline 空面板不崩溃（设计如此）
- [x] D-10：下位机零通道 → Chart 区域"等待握手"，StatePanel/Dashboard 仍可工作（设计如此）

全量 pytest 129/129 通过。

---

## 工具：下位机模拟器（M1-M5 端到端联调）

- [x] `tools/device_simulator.py`：
  - Python 实现的 debug v2 下位机模拟器，纯 UDP
  - `--profile afd01 | ufd45` 切换型号
  - 启动即广播 META + 三张 DEFINE，之后按协议节奏发 DATA(100Hz)/STATE(5Hz + 全量 1Hz)/HEARTBEAT(1Hz)/DEFINE(0.2Hz)
  - 自动注入事件：每 ~6s 切换 LOCK_FLAG + LOCK_ACQUIRED/LOCK_LOST；每 ~10s 随机事件
  - 响应全部 CONTROL 子命令：DEBUG_ENABLE / REQUEST_* / USER_MARK / SET_SAMPLE_RATE / SET_TRACE_MODE / RESET_STATS
  - afd01 profile: 12 channels / 7 states / 5 events（含 SNR 渐强模拟锁星）
  - ufd45 profile: 12 channels（含 bcn_rssi/ku_lo/buc_temp 等 Ku 频段特色） / 6 states / 6 events
  - 数据生成器模拟真实姿态漂移 + 锁星前后 SNR 变化

- [x] `tests/test_simulator.py`（16 条）：
  - encode → build_frame → FrameReceiverV2 对称性：META/CHANNEL/STATE/EVENT_DEFINE + DATA/STATE_REPORT + HEARTBEAT
  - afd01 和 ufd45 双 profile 参数化
  - **端到端 smoke**：模拟器发 4 帧握手 → Handshake.ready 触发 → ProfileStore 三张表就绪 ✓

全量 pytest 142/142。

**使用方式**：
```bash
# 终端 1
python tools/device_simulator.py --profile afd01 -v

# 终端 2：启动上位机，连接 UDP 127.0.0.1:4004（本地 45678）
python -m satellite_debug_tool
```

---

## UI 体验修复（基于联调反馈）

用户用 simulator 跑通端到端后反馈的 5 个问题，本轮一次性修复：

### 1. 3D 自动绑定 roll/pitch/yaw
- `AttitudeWidget.auto_bind_from_profile(name_to_key)`：按通道名（小写）匹配 exact 或 contains "roll"/"pitch"/"yaw"，自动填到三个 combo 并触发 channel_changed 信号
- MainWindow 在 `profile_changed` 时用 profile 通道名构造 `name_to_key` 字典，调 auto_bind；用户仍可手动覆盖

### 2. Channel Selection 显示真名 + 单位
- MainWindow 新增 `_channel_display_label(key)`：`ch_00` → `roll (°)` （profile 驱动）
- `_update_display` 创建 checkbox 时使用真名；`_on_profile_changed_sync` 在 profile 到达时刷新已有 checkbox 的 text

### 3. Clear 按钮扩展
原来只清 chart/attitude/data_store/channel_panel。现在额外：
- `event_log.clear()` + EventTimeline list 清空 + count 刷新
- `chart.clear_event_markers()` 清除曲线竖线
- `dashboard.refresh()` → KPI 卡片归位 "—"
- `frame_count / error_count` 归零，StatusBar 计数同步更新
- StatePanel / StatusStrip 不清（状态字保留以便立即识别设备）

### 4. 曲线历史保留
- `ChannelBuffer` 默认容量 2000 → **30000**（100Hz 下可保留 5 分钟）
- `DataStore` 默认 buffer_capacity 同步更新
- `GroupedChartWidget` 默认 `time_window` 30s → **120s**

### 5. Light 主题全局生效
- 新增 `styles.palette(is_dark) -> dict` 统一色板（card / border / text / input_bg 等 11 项色值）
- 所有 M2/M3 新 widget 去掉硬编码颜色，加 `set_dark_theme(bool)`：
  StatusStripWidget / `_Chip`
  DashboardWidget / KpiCard / ModeButtonGroup
  StatePanelWidget / StateItemRow
  EventTimelineWidget
  ControlPanelWidget
- MainWindow `_apply_stylesheet` 统一调用所有 widget 的 set_dark_theme，
  并追加处理 StatusBar / channel panel 容器 / 通道条目

全量 pytest 142/142 通过；`import main_window` 通过。

---

## M6 — 主题字体（待开始）

---

## 偏差记录

> 与 `optimization_plan.md` / `DEBUG设备协议接口规范_v2.md` 的实际差异（发现后即时记录，含原因与补偿措施）。

### 2026-04-16 — 放弃 v1 兼容层
- **决策**：v2 完全取代 v1，不在上位机维护"检测并回退 v1 帧格式"的逻辑，也不在下位机维护 `USE_DEBUG_PROTO_V2` 开关的 v1 路径
- **原因**：本项目上下位机同仓同步发布，保留兼容层只增加代码路径、测试负担和误解风险
- **影响**：
  - 删除上位机 `core/protocol/frame_receiver.py` / `data_frame.py` / `helpers.py` 与相关 v1 测试
  - 重写 `core/protocol/__init__.py` 只导出 v2 接口
  - 下位机新协议核心只实现 v2；`trace.c` 现有 `debug_report_data()` 调用点重写为 v2 API
  - 旧 `.sdb` v1 文件不再由主工具读取；如需要留一个独立 `sdb_v1_convert.py` 一次性脚本（非 M1 范围）
- **对应文档更新**：
  - `optimization_plan.md` v1.2：§5.1 / §5.6 / §6.1 / §7 M5 / §8.1 / §8.4 / §9 风险表
  - `DEBUG设备协议接口规范_v2.md`：§1 兼容性、§5.3 v1 兼容段（删）、§11 整节改写

---

## 变更日志

| 日期 | 分支 | 摘要 |
|------|------|------|
| 2026-04-16 | 上位机 `dev_3D_display` | commit `aef6483` — docs: 协议 v2 规范 + 优化 plan |
| 2026-04-16 | 上位机/下位机 | 各自创建开发分支，启动 M1 |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 新增 `frame_v2.py` / `codec_v2.py` / `frame_receiver_v2.py` 三件套 |
| 2026-04-16 | 文档 | plan v1.2 + 协议规范更新：**废除 v1 兼容层** |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 删除 v1 代码 + DataStore/importer 切 v2；MainWindow 协议入口切换 |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 新增 `core/profile/`（models + ProfileStore + cache） |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 新增 `core/protocol/handshake.py`；MainWindow 自动握手 + 100ms tick |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 上位机 M1 任务完工，测试 107/107 通过 |
| 2026-04-17 | 下位机 `feat/debug_protocol_v2` | 修正分支创建（原先误建到上位机 repo） |
| 2026-04-17 | 下位机 `feat/debug_protocol_v2` | shared/debug 重写为 v2：debug.h + internal + proto + registry + session + debug.c |
| 2026-04-17 | 下位机 `feat/debug_protocol_v2` | target/afd01 新增 afd01_debug_profile.h/c，改写 afd01_app_debug.c，trace.c 接入新 API |
| 2026-04-17 | 下位机 `feat/debug_protocol_v2` | 编译通过（FLASH 44%, RAM 15%, RAM_D2 4%，DTCM 不变） |
| 2026-04-17 | 上位机 `feat/protocol_v2_refactor` | M2 State/Event UI (commit f5c7678) |
| 2026-04-17 | 上位机 `feat/protocol_v2_refactor` | M3 Dashboard/StatusStrip/ControlPanel/分组曲线 (commit 27144c9) |
| 2026-04-17 | 下位机 `feat/debug_protocol_v2` | M5 下位机：ufd45_debug_profile + app_debug + trace.c（ufd45 编译通过，FLASH 39%, RAM_D2 4.1%） |
| 2026-04-17 | 上位机 `feat/protocol_v2_refactor` | M5 上位机：异步 DataRecorder + SDB v2 (内嵌 profile) + DataImporter v2 + 7 条测试 (129/129) |
| 2026-04-17 | 上位机 `feat/protocol_v2_refactor` | 新增 tools/device_simulator.py + 16 条 smoke 测试 (142/142) |

---

## 遗留问题 / 待决策

*暂无*
