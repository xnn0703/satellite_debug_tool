# M7 开发日志

本文件追踪 M7 plan ([doc/M7_plan.md](M7_plan.md)) 实施过程，按 S1-S7 阶段
记录关键决策、偏离 plan 之处、commit hash 与测试结果。

| 项 | 值 |
|----|----|
| 启动日期 | 2026-05-15 |
| 上位机分支 | `claude/v0.2-tabs-perf-log`（基线 master `c36b4b2`） |
| 当前状态 | S1-S6 完成，S7 收尾 |

---

## 上下文校准（开工前重要发现）

最初 plan 是基于错误的 baseline（master `47cbd7f`，约 v0.1 协议）写的。
切换到正确 baseline（master `c36b4b2`，含 M1-M6 全部 commit）后发现：

- ❌ 原"P1 卡顿源于 list↔numpy 反复转换" → M6 GroupedChartWidget 已重写
- ❌ 原"录制 bug 重复写 N 次" → M6 已修
- ❌ 原"ChannelBuffer 容量 2000" → 已升 30000
- ❌ 原"导入 API 错配会崩" → M6 已重写
- ✅ "录制只显示 5min" → 仍存在（30000/100Hz = 5min 上限）
- ✅ "导入污染实时" → 仍存在（复用主 DataStore）
- ✅ "字号 combo 想去掉" → 截图属实，M6 commit f90ef38 引入
- ✅ "WindTerm log 解析" → 无

→ 重写 plan 为本版 M7_plan.md。

---

## S1 — 字号固化 + 布局压缩

**commit**: `b5cfe2a refactor(ui/M7-S1): 移除字号档位 UI + 布局压缩`

**做了什么**：
- `MainWindow._font_scale_combo` + `_on_font_scale_changed` + `_label_to_scale` 删除
- `_apply_theme(theme, scale)` → `_apply_theme(theme)`，内部固定 `scale="small"`
- 启动时 `self._settings.remove("ui.font_scale")` 清掉旧 key（沿用 attitude 的清理模式）
- 通道卡片行高 32→28、间距 6→4、圆点 10px 固定
- toolbar 高度 42→36
- ControlPanel `setMaximumHeight(80)` 防止 splitter 拖动吞曲线区

**关键决策**：
- `styles.py` 不动（test_styles.py 还依赖 `FONT_SCALES` API），只是 UI 不再暴露
- LiveView 内部所有 `set_theme(theme, scale)` 调用仍传 "small"，保持子 widget 接口不变

**测试**：183 passed（baseline 不变） + 1 baseline failed（test_codec_v2 oversize，已 spawn_task）

---

## S2 — ChannelBuffer 无界模式

**commit**: `77c10f5 feat(data/M7-S2): ChannelBuffer / DataStore 支持无界模式`

**做了什么**：
- `ChannelBuffer.__init__(capacity: int | None = 30000)`
  - `capacity = None`：内部 list 累加，`get_times/get_values` 一次性 `np.asarray()` 并缓存（append 时失效）
  - 加 `__len__` 和 `capacity` 属性
- `DataStore.__init__(buffer_capacity: int | None = 30000)` 透传
- 新增 `tests/test_channel_buffer.py`：11 条覆盖环形 + 无界（含 10 万样本不环回、缓存失效、dtype）

**性能验证**：append 10 万次 + `get_values` < 50ms（包含 list→ndarray 转换）

**测试**：194 passed（+11 新）

---

## S3 — TimeRangeControl + chart API

**commit**: `0221220 feat(ui/M7-S3): TimeRangeControl + GroupedChart.set_x_range_sec`

**做了什么**：
- 新建 `ui/time_range_control.py`：QHBoxLayout 装 [QComboBox 预设 + QDoubleSpinBox 起止 + 应用按钮]
  - 预设：全部 / 最近 30s / 1min / 5min / 30min / 自定义
  - 总时长 < 预设窗口时回退为 (0, total)
  - 自定义模式启用 SpinBox + "应用"按钮
  - `range_changed(start, end)` 信号；`set_range()` 程序化设置不 emit
- `GroupedChartWidget.set_x_range_sec(start, end)`：直接 setXRange + 更新 `_x_view_max`
- `tests/test_time_range_control.py`：9 条覆盖初始 / 预设 / 自定义 / 程序化

**偏离 plan**：QComboBox 没有 `__len__`，单测原来写 `len(combo)` 报 TypeError，改 `combo.count()`

**测试**：203 passed（+9 新）

---

## S4 — MainWindow QTabWidget 容器 + LiveView 抽出

**commit**: `0c7efca refactor(ui/M7-S4): MainWindow → QTabWidget 容器 + 抽出 LiveView`

**做了什么**：
- 新建 `ui/live_view.py`（含 `LiveView(QWidget)`，~900 行）：把原 MainWindow 的全部业务搬过来
  - worker / receiver / handshake / data_store / state_store / event_log / profile_store
  - toolbar / chart / dashboard / state_panel / event_timeline / attitude / control_panel / channel panel
  - `status_message(str, int)` 信号上报 statusbar 短消息
- 新建 `ui/playback_view.py` + `ui/log_view.py`（S4 阶段占位 QLabel）
- 重写 `ui/main_window.py`（~150 行）：QTabWidget + 顶部主题切换 toolbar + QStatusBar
- `ui/__init__.py` 导出新 view

**关键决策**：
- LiveView **不复用** QMainWindow 的 toolbar/statusbar 机制 — 直接在 QVBoxLayout 内嵌
  - 工具栏放在 `root` 第一行（QToolBar 仍能放在普通 QWidget layout 里）
  - 统计 4 个 label（FPS/Channels/Frames/Errors）改用底部 22px 高的 QWidget 行
- 主题切换在 MainWindow 顶部，`_on_theme_changed` 广播到三个 view（duck typing：`hasattr(set_theme)`）
- `ui.active_tab` 持久化，启动恢复

**偏离 plan**：
- LiveView 也保留了 `_on_import_clicked`（plan 想移到 PlaybackView）— 决策：保留 Live 的"快速导入"入口
  以兼容用户习惯，但 toolbar tooltip 标注"M7：建议改用回放 Tab"
- baseline bug 发现：`attitude_widget.py:572` `setTransform(np.eye(3))` 应传 4x4，clear 路径会崩；
  跟 S4 无关，未修（待 spawn_task 跟进）

**测试**：203 passed（与 S3 持平，无新增；smoke 单独验证 3 Tab 创建/切换/主题切换/status 路由都正常）

---

## S5 — PlaybackView 完整实现

**commit**: `1349afe feat(ui/M7-S5): PlaybackView 完整实现`

**做了什么**：
- `PlaybackView` 独立持有 DataStore(`buffer_capacity=None`) + ProfileStore(`cache=None`) + StateStore + EventLog
- 顶部一行：Open .sdb + 文件名 label + 总时长/帧数 label + TimeRangeControl
- 主区：GroupedChartWidget | (Dashboard + EventTimeline) 水平 splitter
- 加载流程：`DataImporter.open_sdb` → profile 自动恢复 → 清空旧数据 → 流式灌入 → 计算总时长 → `set_total` → auto_range
- TimeRangeControl `range_changed` 接到 `chart.set_x_range_sec`，同步关掉 auto_range
- `tests/test_playback_view.py`：4 条覆盖（无界 + 三 store 独立 + range 接线 + 取消文件对话框不崩）

**测试**：207 passed（+4 新）

---

## S6 — WindTermLogParser + LogView

**commit**: `c37ef4c feat(log/M7-S6): WindTermLogParser + LogView 完整实现`

**做了什么**：
- 新建 `core/log_parser/windterm_log.py`：
  - 正则匹配 `track_debug_print_table_header:` / `track_table_row_bynav:`
  - 多次表头取最近一次；列数错位行跳过 + skipped_rows 计数
  - **非数字列整列剔除**（扫描首条数据行，每列 try float()）
  - 行号 × 100ms 占位时间戳
  - `progress_cb` 每 N 行回调
- 新建 `ui/log_view.py`：
  - 独立 DataStore（`max_channels=128, buffer_capacity=None`）
  - 虚拟 ProfileStore（`hw_type="windterm_log"`，每列一个 `ChannelDefEntry`，group_id 按列名前缀启发式）
  - 复用 GroupedChartWidget + TimeRangeControl
  - X 轴下方 hint label "行号 × 100ms 占位"
- `tests/test_windterm_log_parser.py`：11 条覆盖（无表头 / 多次表头 / 非数字列剔除 / 列错位 / 时间戳 / 空输入 / 全字符串 / 真实样例 68 列→65 列 / progress_cb）

**端到端验证**（用户提供的真实文件）：
- 文件: `192.168.1.12_2026-05-14_19-29-47.log` (194 MB / 483984 行)
- Parser 耗时: **5.79s**
- 识别: **65 数字列** (剔除 state/ins_st/eskf 三个字符串列)
- 跳过: 8368 行（约 1.7%，多为协议握手/版本信息等表格外行）

**关键决策**：
- `DataStore(max_channels=128)`：实测 65 列，留余量；协议 v2 上限 32 不适用 log 路径
- 虚拟 profile 的 `display_min/max` 用该列 min/max ± 5% padding，防止全 0 列退化为 0~0 视窗
- `group_id` 启发式（ins/imu→0, gps→4, snr→2, bias→3, 其他→2）复用 GroupedChartWidget 已有配色

**偏离 plan**：
- 测试初版数错了表头列数（写 62 实际 65），运行报错后修正

**测试**：218 passed（+11 新）

---

## S7 — 收尾（进行中）

**做了什么（截至当前）**：
- 更新 `CLAUDE.md`：完整重写反映 M1-M7 架构（旧版还在 v1 协议）
- 写本文件
- 在 `M7_acceptance.md` 末尾追加自评（见下）

**待用户验证项**：
- A1：60min 长时间连接实测（需真实设备 + 持续点击拖拽）
- B1-B5：录制 30min `.sdb` 后回放完整性 + 隔离（需真机录制）
- D4/D5：用 2h 录制和真实 log 验证范围选择

---

## 6 个 commit 一览

```
c37ef4c feat(log/M7-S6): WindTermLogParser + LogView 完整实现
1349afe feat(ui/M7-S5): PlaybackView 完整实现
0c7efca refactor(ui/M7-S4): MainWindow → QTabWidget 容器 + 抽出 LiveView
0221220 feat(ui/M7-S3): TimeRangeControl + GroupedChart.set_x_range_sec
77c10f5 feat(data/M7-S2): ChannelBuffer / DataStore 支持无界模式
b5cfe2a refactor(ui/M7-S1): 移除字号档位 UI + 布局压缩
```

测试演进：183 (baseline) → 194 (S2) → 203 (S3) → 207 (S5) → 218 (S6)

---

## 已知遗留（spawn_task 跟进）

- `test_codec_v2::test_oversize_data_rejected` 在 master baseline 上 fail
  （commit `ae878cf` 把 MAX_DATA_LENGTH 512→1024 但测试没同步）
- `attitude_widget.py:572` clear 路径 `setTransform(np.eye(3))` 应传 4x4（pyqtgraph Transform3D 要求 16 元素）

两者均与 M7 改动无关，是 baseline 已存在的问题。
