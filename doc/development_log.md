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
| M4 | 3D 场景 + 用户 mark 事件 | 代码完成 | §8.1.5 F-07 F-11 |
| M5 | 异步录制 (.sdb v2) + ufd45 smoke test | 待开始 | §8.1.4 §8.5 D-04 |
| M6 | 主题/字体/tooltip | 代码完成 | §8.3 U-02/U-05 |

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

## M4 — 3D 场景升级 + 用户 mark 闭环（代码完成）

### 2026-04-18 — AttitudeWidget 全面升级

#### 坐标系约定（写进 `attitude_widget.py` 顶部文档）

- 机体系：+X 机头 / +Y 左翼 / +Z 天顶
- 相控阵波束坐标：**az** 机头右为 0°、从 +Z 俯视**逆时针**增大；
  **el** 天顶为 0°、水平面为 90°
- 单位矢量公式：
  ```python
  phi   = radians(AZ_PHI_OFFSET_DEG + AZ_SIGN * az)  # 默认 270 + az
  theta = radians(el)
  x = sin(theta) * cos(phi)
  y = sin(theta) * sin(phi)
  z = cos(theta)
  ```
- 若实际硬件方向相反，只改文件头两个常数 `AZ_PHI_OFFSET_DEG` / `AZ_SIGN`
- 典型验证（已写测试）：
  - az=0,  el=0  → (0, 0, 1)   天顶
  - az=0,  el=90 → (0, -1, 0)  机头右
  - az=90, el=90 → (1, 0, 0)   机头

#### 默认机体：飞机 → 扁平长方体（相控阵卫通终端外形）

- 尺寸 3.0 × 1.8 × 0.3（长宽高），长边沿 +X
- 顶面机头端 8% 削角，直观区分朝向
- 10 顶点 / 16 三角面
- 预留 `set_body_model(mesh_data)` 接口，后续接具体设备的 STL/OBJ 模型

#### 新增 4 类 GL 场景元素

| 元素 | 类型 | 来源 | 颜色 |
|------|------|------|------|
| 卫星矢量 | GLLinePlotItem | `tgt_az / tgt_el` 通道 → unit_vec × R | 红 |
| 天线实际法向 | GLLinePlotItem | `ant_az / ant_el` 通道 → unit_vec × R | 绿 |
| 误差扇面 | GLMeshItem (translucent) | 两矢量 slerp 插值 12 段三角扇 | 半透明琥珀 |
| 扫描轨迹 | GLLinePlotItem | 最近 300 个 ant 矢量点（deque） | 淡蓝 |

任一通道未绑定/无数据时对应元素隐藏（`setData(pos=empty)`），绝不崩。

#### 通道绑定扩展：3 → 7 路

- 新增 4 个下拉：tgt_az / tgt_el / ant_az / ant_el（Attitude 第 2 行）
- `auto_bind_from_profile` 重写为规则驱动，每个 axis 有 exact + contains 两层规则
- settings 持久化：`attitude.{roll|pitch|yaw|tgt_az|tgt_el|ant_az|ant_el}_channel`
- `get_pointing_selections()` 返回 4 元组供外层按每帧取值

#### MainWindow 接入

- `_on_attitude_channel_changed` 支持全部 7 axis
- `_update_heavy` 调用 `_attitude.update_pointing(...)`，每帧用
  本地 `_latest(ch)` 帮手取值；未绑定返回 None 让 3D 场景隐藏对应元素
- 启动时从 settings 恢复 7 路下拉选择

#### 视角复位

- Attitude 第 1 行右侧加 "复位视角" 按钮 → `setCameraPosition(10, 30, 45)`
- 按钮样式跟随主题

#### 用户 mark 闭环（F-07）

- `GroupedChartWidget.add_event_marker(ts, level, name="", event_id=-1)` 扩展：
  - `event_id == 0xFFFF`（用户标记）：实线 + 2px + 琥珀色 + label "⚑ 名称"
  - 其它事件：保留原 dashed + 级别色
  - 所有竖线加 hover `setToolTip` 显示事件名
- `EventTimeline._format_row`：0xFFFF 事件加 ⚑ 前缀
- MainWindow `_on_event_added_for_chart` 传 `name + event_id` 给 Chart

#### 测试

新增 `tests/test_attitude_pointing.py`（16 条）：

- `TestPointingUnitVec`
  - 9 组典型 az/el 值的坐标公式（天顶 / 机头右 / 机头 / 机头左 / 机尾 + 中间角）
  - 单位长度断言（扫 az × el 全空间 ≈ 80 样本）
  - 约定常数存在性（防有人偷改）
- `TestErrorFan`
  - shape / dtype / 面索引合法性
  - 零矢量退化 / 同向退化 / 不同 segments
  - 弧首尾接近两矢量方向（cos > 0.9999）

全量 pytest **176/176 通过**（160 → 176，+16）。  
MainWindow 12 组合（3 主题 × 4 字号）切换 + heavy 刷新冒烟通过。

#### 验收对照

- [x] F-11 3D 场景：飞机（→长方体）+ 卫星矢量 + 天线矢量 + 误差扇面同时渲染
- [x] F-07 用户 mark：ControlPanel 发 → 下位机回 EVENT(0xFFFF) → Chart 画加粗琥珀竖线 + ⚑ tooltip，EventTimeline ⚑ 前缀
- [ ] 通道绑定可配 + 按 hw_type 记忆到 settings.json：已实现持久化；**未做**"按 hw_type 分桶"（目前所有 hw 共享同一份 attitude.*_channel）；若切设备会串绑定，后续需要可补
- [ ] 外场实测：需真硬件 / 下位机给 tgt/ant az/el 通道

---

## 批次 A — 体验增强小项（代码完成）

### 2026-04-18 — A1/A2/A3/A4/A5/A6 一次性落地

一次性交付 plan §4.4–§4.8 里剩余的 6 项体验增强。

#### A1 + A2: EventTimeline 跳转曲线

- `GroupedChartWidget.jump_to_timestamp(ts_ms, window_sec=None)`：把 X 视窗中心定位到指定时间戳，返回 bool
- `EventTimelineWidget.jump_requested` 信号（携带 `ts_ms: int`）
- 触发源：
  - **A1** 双击任一事件行 → emit
  - **A2** 右键"在曲线上定位"菜单 → emit
- MainWindow 把信号直接连到 `chart.jump_to_timestamp`
- 每个 item 把 `rec.timestamp_ms` 存到 `Qt.UserRole`

#### A3: StatePanel 最近变化 2 秒高亮

- `StateItemRow._make_style(border)` 抽出一个模板方法，主题变化时调 normal 边、flash 时调琥珀 `#FFC107` 边
- `flash_highlight()` setStyleSheet + `QTimer.singleShot(2000, ...)` 恢复
- `_on_state_changed` 触发 `set_value()` 后调 `flash_highlight()`

#### A4: StatePanel 子系统折叠分组

- 新增 `_SubsystemSection`（可折叠 QFrame）+ `_classify_subsystem(name)` 分桶
- 规则：按 `_` 切分 token + 长度敏感匹配：
  - 2 字符及以下的关键字（如 `lo`）走精确匹配，避免 `lock` 被误归 `lo`
  - 3 字符及以上走前缀匹配（`pll_locked` 的 `pll` → rf）
  - 顺序：`modem`（snr/beacon）> `rf`（pll/lo/buc/lnb/polar）> `trace`（trace/lock）> `ins`（ins/imu/gps）> `general`
- `_rebuild` 先按规则分桶，再按 `_SUBSYSTEM_ORDER` 顺序插入各 section
- 空桶不创建，header 含实时计数 `"▼ 跟踪 (Trace)  (3)"`

#### A5: ControlPanel 通道使能 bitmask

- 协议层 `build_channel_enable_mask(mask)` 已存在（u32 lo + u32 hi）
- ControlPanel 加 "通道使能…" 按钮；点开 `_ChannelEnableDialog`（QDialog）
- 对话框：profile 驱动列出通道复选框（2 列网格），带 `全选 / 全不选 / 反选`
- 对话框 OK 返回 64bit mask，emit `channel_enable_changed(mask)`
- MainWindow `_on_channel_enable_changed` 调 `build_channel_enable_mask` 下发

#### A6: Attitude 绑定按 hw_type 分桶

- 老 key `attitude.{axis}_channel` → 新 key `attitude.{hw}.{axis}_channel`
- `_attitude_setting_key(axis, hw)` 统一 key 生成，无 hw 时回落老格式（迁移用）
- `_restore_attitude_bindings(hw)` 按桶恢复 7 路 combo
- 触发时机（`_on_profile_changed_sync`）：
  1. 该 hw 下已有分桶保存 → 恢复这套
  2. 该 hw 下无，但存在老共享键 → **一次性迁移**老键到新 hw 桶
  3. 都没有 → 走 `auto_bind_from_profile`
- `_on_attitude_channel_changed` 按当前 hw_type 保存到对应桶

#### 测试

新增 `tests/test_ux_batch_a.py` 10 条：

- `TestSubsystemClassify`（3 条）：规则 dict 15 样本、大小写、顺序常量覆盖
- `TestChartJumpToTimestamp`（2 条）：无数据返 False / 正确设视窗中心和宽度
- `TestChannelEnableMask`（3 条）：手动勾选位掩码拼装、空勾选 / 全选 / 反选
- `TestAttitudeSettingKey`（2 条）：带 hw_type 分桶 key / 无 hw 回落老格式

全量 pytest **186/186 通过**（176 → 186，+10）。
MainWindow 冒烟：5 子系统 profile 分桶正确（trace=2 / modem=1 / rf=1 / ins=1 / general=1），
3 主题 × 4 字号切换、A5 对话框打开/关闭、A6 分桶保存读取都 OK。

#### 验收对照

- [x] plan §4.4 StatePanel 按子系统折叠分组（A4）
- [x] plan §4.4 StatePanel 最近变化项高亮（A3）
- [x] plan §4.5 U-03 EventTimeline 双击跳曲线（A1）
- [x] plan §4.5 EventTimeline 右键"在曲线上定位"（A2）
- [x] plan §4.8 ControlPanel 通道使能复选框矩阵（A5）
- [x] M4 遗留 Attitude 绑定按 hw_type 分桶（A6）

---

## 批次 B — 文档收尾（完成）

### 2026-04-19

按 plan §11 开发纪律，补齐 3 类非代码交付物：

| 文件 | 作用 |
|------|------|
| `doc/acceptance_log.md` | 按 plan §8 逐项跟踪验收状态（功能 F-01~F-15、性能 P-01~P-08、UI U-01~U-05、协议 C-03、多设备 D-01~D-10）；附"待真机/外场实测 Checklist" |
| `doc/user_manual.md` | 11 节用户手册：快速开始 / 界面总览 / 连接 / 数据查看 / 事件状态 / 3D 场景 / 控制 / 录制回放 / 主题字号 / 常见问题 / 文件位置 |
| `doc/screenshots/README.md` + `.gitkeep` | 截图命名约定与抓图清单；外场实测时按 README 给的场景抓图后在 `acceptance_log.md` 引用 |

**里程碑状态 @ 2026-04-19**（对照最新开发日志顶部总览表）：

| M | 状态 |
|---|------|
| M1–M6 主线 | 代码全部完成，自动化测试 186/186 |
| 批次 A 体验增强（A1–A6） | 代码完成 |
| 批次 B 文档收尾 | 完成 |

剩余：**全部等真下位机 / 车载外场实测关闭的 13 项**，清单见
`acceptance_log.md` 文末 Checklist。

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

### 2026-04-18 — 5 秒周期闪烁根因修复

上一轮"固定 Y + 相对时间轴 + setXRange 节流"修复了滚动帧闪烁，但**5 秒周期**的整体闪烁依旧。根因定位：

**周期对齐下位机 META + DEFINE 广播频率（每 5s 一次）。**

每次 simulator / 下位机重发 META_INFO，`ProfileStore.apply_meta`
**无条件** emit `profile_changed` 信号，触发 5 个订阅者整体重建：
- Chart._rebuild（清空所有 plot）
- Dashboard._rebuild
- StatePanel._rebuild
- StatusStrip._rebuild_dynamic
- MainWindow._on_profile_changed_sync（channel panel）

而 `apply_channel/state/event_define` 本来就有 table_ver 幂等，唯独 `apply_meta` 漏了。

**修复**：
- `apply_meta` 比较 protocol_ver / fw_ver / hw_type / device_sn 四字段，
  **全部相同时不 emit**；仅首次 / hw 切换 / 字段变化时触发
- 加回归测试 `test_apply_meta_idempotent`：同一 meta 连发 3 次只 emit 1 次
- 额外给三个 UI 组件加签名级幂等（双保险，未来其他上游错误重发也兜得住）：
  - `GroupedChartWidget._on_profile_changed`: channels 签名（id/name/unit/group/range）
  - `StatePanelWidget._on_profile_changed`: states 签名（含 enum 列表）
  - `DashboardWidget._on_profile_changed`: critical channels + critical ENUM states 签名
- `MainWindow._on_profile_changed_sync`: checkbox text 仅变化时 setText，
  attitude auto_bind 仅 name→key 映射变化时调用

测试 143/143 全部通过（新增 1 条）。

---

### 2026-04-18 — 布局 + 防闪烁

用户进一步反馈：
- 分组模式曲线显示不全（没有滚动条）
- 曲线过一会儿"整体闪烁一下"像全图刷新
- 请评估整体布局

**1. 分组曲线滚动**
- `GroupedChartWidget` 的 `GraphicsLayoutWidget` 外包了 `QScrollArea`
- stacked 模式下 GL 的 `setMinimumHeight(n_groups × 220)`，超出可视区自动出纵向滚动条
- combined 模式 GL minHeight=0，撑满滚动区

**2. 消闪烁（业界做法参考）**
pyqtgraph 闪烁根因：每帧 `setXRange` → `sigRangeChanged` → 刻度/网格 relayout 整图重绘。
做法：
- **相对时间轴**：引入 `_x_origin_ms`（首帧时间戳），X 显示 = `(ts_ms - origin)/1000` 秒。
  原来设备已运行几千秒时 pyqtgraph 会把单位从 `Time(s)` 切到 `Time(ks)` 刻度跳变；
  现在稳定显示秒数
- **固定 Y 范围**：combined 取所有通道 `display_min/max` 包络，stacked 取组内包络，
  `enableAutoRange(x=False, y=False)`，不让 Y 自适应触发回弹
- **X 滚窗节流**：新数据超出当前右边界 **≥ 1s** 才 `setXRange` 扩窗（多扩 1s 缓冲）
  意味着连续 5 次 refresh 才触发 1 次坐标重排，肉眼不可察觉
- `clear()` / `_clear_plots()` 重置 X 原点与窗口

**3. 布局 polish**
- Dashboard 改为单行布局：KPI 卡片区（stretch=1）+ 模式按钮区（fixed）并排；
  原来两行容易对不齐，现在同一行高度一致
- `KpiCard` 固定高度 72，`ModeButtonGroup` 固定高度 72，按钮 fixed 26
- 主窗口 VBoxLayout spacing=3，Dashboard outer margin 2px 避免"松垮"感

全量 pytest 142/142 通过。

---

### 2026-04-18 — 性能/布局补丁

用户反馈 "点击/拖拽一顿一顿" + "曲线太分散看不全"，两项针对性修复：

**1. 拆双定时器**
- 原 `_update_timer` 100ms 做所有事情（含 chart.refresh 整份 ndarray setData）
- 现拆成：
  - `_update_timer` 100ms：FPS/计数/3D 姿态/通道数值 label —— 只是轻量标签更新
  - `_heavy_timer` 200ms：chart.refresh + dashboard.refresh —— 重绘制 5Hz 即可
- 12 条曲线 × 30000 点 setData 频率从 10Hz 降到 5Hz，UI 事件循环不再被挤压

**2. Chart 单图 / 分组 模式**
- GroupedChartWidget 加 `_mode` 字段（默认 `"combined"`）
- `_rebuild_combined(channels)`：所有通道叠一张大图，共用 Y 轴 + enableAutoRange；legend 每条 `name (unit)`
- `_rebuild_stacked(channels)`：保留原来的按 group_id 分子图
- 顶部 toolbar 加 "单图 / 分组" toggle 按钮，`set_mode()` 切换即重建
- 按钮色板跟随主题

全量 pytest 142/142 通过。

---

## M6 — 主题/字体/tooltip（代码完成）

### 2026-04-18 — 三档主题 + 四档字号 + 全局 tooltip

#### styles.py 扩展

- `palette(theme)` 三档：
  - `dark`（沿用）/ `dark_hc`（#000000 黑底 + #F5F5F5 纯白 + 高饱和语义色，500cd/m² 车载强光屏可读）/ `light`
  - 新增键：`primary / success / warning / error`（各主题各自调），统一替代散落硬编码
  - 老签名 `palette(bool)` 向下兼容（True→dark, False→light）
- `FONT_SCALES` 四档：small(1.0) / medium(1.17) / large(1.5) / xlarge(1.83)
  - 基准 12px → 12 / 14 / 18 / 22
- `font_px(base, scale)`：按档位像素缩放，最低 6px 钳位
- `monospace_family()`：运行时探测 JetBrains Mono → Consolas → SF Mono → Menlo → Courier New；
  无 QGuiApplication 时回退 "monospace"（测试环境不崩）
- `apply_global_font(app, scale)`：QApplication 级字号缩放入口

#### MainWindow toolbar

- 主题下拉：`Dark/Light` → `深色 / 深色·高对比 / 浅色`（标签驱动，持久化英文 key `dark/dark_hc/light`）
- 新增字号下拉：`小 / 中 / 大 / 超大`
- 老配置 `ui.theme = "Dark"/"Light"` 自动迁移到新值；新增 `ui.font_scale`
- `_on_theme_changed` / `_on_font_scale_changed` 各持久化
- `_apply_stylesheet` 重命名 `_apply_theme(theme, scale)`；
  内部改用 `palette()` 取色，不再 `S.PRIMARY if is_dark else S.PRIMARY_LIGHT` 这类散落分支
- 子 widget 分发：优先调 `set_theme(theme, scale)` 新接口，向下兼容 `set_dark_theme(bool)`

#### 子 widget 统一改造

每个 widget 都新增 `set_theme(theme, scale)`，保留 `set_dark_theme(bool)` 作兼容薄皮：

| Widget | 关键改动 |
|--------|---------|
| StatusStripWidget / `_Chip` | `apply_theme(theme, scale)`，chip 文字 `font_px(11, scale)`；critical-state chip tooltip 展示 state_id/类型/枚举项 |
| DashboardWidget / KpiCard / ModeButtonGroup | KPI 数字字体 = `QFont(monospace_family(), font_px(22, scale), Bold)`（字名锁定，字号随档缩放）；卡片在 xlarge 档自适应加高；卡片 tooltip=通道全信息；按钮 tooltip=目标枚举 |
| StatePanelWidget / StateItemRow | 行 tooltip 展示 state_id/flags/枚举；三档主题适配；字号缩放 |
| EventTimelineWidget | 级别下拉/关键字输入/清空 三个控件 tooltip；每行事件 tooltip 显示 event_id、完整 payload（utf-8/hex）、时间戳 |
| ControlPanelWidget | 采样率/标记/复位 tooltip；三档主题 + 字号 |
| AttitudeWidget | 去掉所有硬编码 `#CCCCCC / #888888 / #0E639C`，改由 `palette()` 取；`_combo_style(active)` 统一按钮状态；3D 背景按主题（#000 / #FFF / #1E1E1E）；label/combo tooltip |
| GroupedChartWidget | `_apply_theme` 改用 palette；axis 颜色 = `text`；按钮 tooltip（单图/分组模式说明）；字号缩放 |

#### Toolbar 按钮 tooltip（U-02）

- Connect / Disconnect / Debug / Record / Import / Clear / Theme / FontScale：每个都带中文说明
- Dashboard KPI / StatusStrip chip / StatePanel row / EventTimeline item：均 setToolTip

#### 测试

- 新增 `tests/test_styles.py`（17 条）：
  - palette 三档键完整性 + bool/None/未知回落
  - dark_hc 对比度断言（bg=#000000，text 比 dark 更亮）
  - font_px 四档准确性（12/14/18/22）+ 未知档回落 medium + 数值输入 + 6px 最小钳位
  - monospace_family 非空 + 无 QApplication 不崩
- MainWindow 12 组合（3 主题 × 4 字号）切换冒烟通过

**全量 pytest 160/160 通过（143 → 160，+17）。**

#### 补丁：超大字号下的水平溢出

用户反馈在 **超大字号 + dark_hc** 实测时 StatusStrip 末端 chip（POLARIZATION / BEACON）被
截断、Dashboard KPI 数字（`2.09°` 显示成 `2.0|`）也被挤压。

讨论后选择 **分条独立横向滚动**（非整窗全局滚动，避免与图表自身的水平拖动冲突）：

- `StatusStripWidget`：chip 行移进 `QScrollArea`（横滚 AsNeeded / 纵向关闭），
  条带固定高度随字号放大（chip 字号 + 18 padding + 12 滚动条）
- `DashboardWidget`：`main_row` 移进 `QScrollArea`；`setMinimumHeight(card_h + 20)`
  给横滚条留位
- `KpiCard`：`SizePolicy` 由 `Expanding` 改为 `Preferred`（装得下按自然宽度，
  装不下交给外层横滚）；`minWidth` 按 value 字号放大
  `max(110, int(value_px * 4.0) + 40)`
    - small 22 → 120, medium 26 → 144, large 33 → 172, xlarge 40 → 200
  保证 "−000.00°" 这种 6 字符读数在各档都不截断

全量 pytest 仍 160/160 通过，12 组合切换冒烟仍通过。

#### 补丁 2：顶部工具栏也需要横向滚动

用户进一步反馈：超大字号下 **工具栏末端的"字号"下拉** 被 QToolBar 的 `>>` 扩展菜单
吞掉（QComboBox 放进扩展菜单交互很差），**实际失去了再修改字号的能力**。

修法：不再用 `QMainWindow.addToolBar()` 把工具栏放进 QMainWindow 的工具栏区，而是
**把 QToolBar 包进一个横向 QScrollArea 后作为普通 widget 放在中央布局顶部**：

```python
self._toolbar_scroll = QScrollArea()
self._toolbar_scroll.setWidgetResizable(True)
self._toolbar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
self._toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
self._toolbar_scroll.setWidget(self._toolbar)
layout.addWidget(self._toolbar_scroll)   # 取代 self.addToolBar(...)
```

`_apply_theme` 里按内容 sizeHint 自适应高度 `max(42, tb_h + 12)` 给横滚条留位。
实测 xlarge 下 toolbar 自然宽度 ~1798px > 窗口 1240px，横滚条自动出现，
能滚到末尾取到字号下拉。

pytest 仍 160/160 通过。

#### 补丁 3：工具栏彻底**不跟随**全局字号

横滚能让字号下拉可达，但用户反馈实测仍找不到（Mac trackpad 横滚发现不了）。
最终方案：**工具栏字号固定 12px，不跟随全局字号档位变化**。

```python
# _apply_theme 里，apply_global_font 之后：
fixed = QFont(self._toolbar.font())
fixed.setPixelSize(12)
self._toolbar.setFont(fixed)
for child in self._toolbar.findChildren(QWidget):
    child.setFont(fixed)
self._toolbar_scroll.setFixedHeight(42)   # 不再按 scale 变
```

效果：切到任意字号档，工具栏宽度/高度/控件字号都不变（sizeHint 稳定 1525×41），
主题 / 字号下拉始终在屏幕同一位置可见；其它区域（StatusStrip / Dashboard /
Chart / Channel Panel）继续跟随档位缩放。

设计理由：工具栏是"操作 UI 的 UI"，它本身跟随字号放大反而让切换字号变困难。
类似 VSCode 的命令面板、IntelliJ 的 Settings 对话框，都不跟随编辑器字号。

pytest 仍 160/160 通过，12 组合切换验证：toolbar 尺寸稳定不变。

#### 补丁 4：补齐几处漏掉的"硬编码行高"

用户进一步反馈实测发现"行高看上去不是动态的"。排查后确认以下几处仍是硬编码像素：

| 位置 | 之前 | 现在 |
|------|------|------|
| `main_window._update_display` 通道条目 `container.setFixedHeight(32)` | 固定 32 | `font_px(12, scale) + 20` (32/34/38/42) |
| `main_window._update_display` 通道圆点 `dot.setFixedSize(10,10)` + `border-radius: 50%` | 固定 10px，border-radius 部分 Qt 版本不生效 | `max(10, font_px(10, scale) - 2)` + `border-radius: {dot//2}px` |
| `dashboard_widget.ModeButtonGroup._buttons[*].setFixedHeight(26)` | 固定 26 | `btn_px + 14` (25/27/32/36) |
| `_apply_theme` 的通道 container 遍历循环 | 只改 stylesheet | 再 `setFixedHeight(row_h)` + 重刷圆点 |

验证脚本（模拟握手 + 数据）测量结果：

| scale | toolbar | statusstrip | channel row |
|-------|---------|-------------|-------------|
| small | 42 (锁定) | 41 | 32 |
| medium | 42 | 43 | 34 |
| large | 42 | 46 | 38 |
| xlarge | 42 | 50 | 42 |

所有行均随字号放大，仅 toolbar 按上一轮决定保持锁定 42。

pytest 仍 160/160。

#### 验收对照 §8.3

- [x] U-02 指示灯悬停显示含义：StatusStrip/Dashboard/StatePanel/EventTimeline 全部加 tooltip ✓
- [x] U-05 超大字号 KPI 数字不截断：KpiCard 按数字像素自适应卡片高度 ✓（需外场实测确认）
- [ ] U-01 外场 3 米可读：需 dark_hc 在 500cd/m² 屏实测
- [ ] U-04 1366×768 布局不溢出：需实测
- [ ] F-15 高对比主题阳光屏可读：需外场实测

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

### 2026-07-17 — Bynav 天空图与逐频点 C/N₀

- debug v2 增加顶层 `0x0D GNSS_SKY_REPORT` 与 `0x0E GNSS_CNR_REPORT`，协议版本和 SDB 文件版本均不变。
- 新增 `GnssStore`：GSV 与 RANGECMPB 独立保存，CNR 按 `(timestamp, report_id)` 完整重组；重复片幂等，冲突片和未完成旧轮丢弃。
- 新增 Live/Playback 共用 `GnssWidget`：左侧北向天空图，右侧按系统+PRN 分组的真实频段 C/N₀ 柱图，下方保留全部 signal type 明细。
- 柱图默认只使用 phase/code lock 观测，同物理频段柱高取锁定观测最大值；不从 GSV SNR 推算多频信号。
- 频段映射按 UG016 固化为 GPS L1/L2/L5、GLONASS G1/G2、Galileo E1/E5a/E5b/E6/E5 AltBOC、BDS B1/B2/B3、QZSS L1/L2/L5、SBAS L1/L5、NavIC L5。
- SDB v2 继续录制原始 debug 帧；Playback 增加 GNSS 快照滑块与前后帧，旧文件无 GNSS 时入口禁用。
- 自动化结果：GNSS 专项 16 项、完整测试 `549 passed, 4 warnings`；固件/上位机共用的 SKY/CNR
  golden payload 已完成字节级测试。Qt 离屏渲染复核了天空图/柱状图/明细布局；真机逐项一致性仍需串口与厂商工具验收。
