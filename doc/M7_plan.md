# M7 — 多 Tab 化 + 字号固化 + WindTerm Log 解析

> 状态：草案，待用户确认
> 关联：[M7_acceptance.md](M7_acceptance.md) · [optimization_plan.md](optimization_plan.md) v1.2 之后的迭代
> 工作分支：`claude/v0.2-tabs-perf-log`（基线 master `c36b4b2`）

## 0. 上下文与基线状态校准

M6 已经完成了大量优化，**用户反馈的三个问题在新基线下严重程度变了**，先校准认知：

| 用户反馈 | 在 M6 基线下的现状 | 仍需做的 |
|---------|------------------|---------|
| ① UI 卡顿（连接久了拖拽点击有延时） | 双定时器解耦（100ms 轻 + 200ms 重）+ GroupedChartWidget ndarray 整批 setData + setDownsampling + setClipToView + 滚屏阈值 1s 已实施 | **先实测验证是否仍卡**；若仍卡再做针对性优化（可能是事件竖线/StatePanel 累积） |
| ② 录制只能看最近 5 分钟 | `ChannelBuffer` 容量 30000，100Hz × 5min ≈ 30000 — **完全对应**。`.sdb` 文件本身完整 | **回放需要独立的无界 buffer**；当前 `_on_import_clicked` 复用主 `_data_store`，再次受 30000 上限 |
| ② 导入污染实时 | `_on_import_clicked` 调 `self._data_store.clear()` 后灌入 — 实时数据被清 | **导入必须走独立 DataStore + 独立 ProfileStore** |
| ③ WindTerm log 解析 | 无 | **从零做**，复用 GroupedChartWidget |
| ④ 字号 combo 没用（截图显示"字号: 小"） | M6 提供四档 (small/medium/large/xlarge)，对应 12/14/18/22 px；用户实际只用 small | **删 combo + 持久化，所有调用点固定 small** |

**M6 已修复但用户尚未感知的**：录制 bug（旧版 `for frame in frames: write_frame(data)` 重复写）已在 [main_window.py:727-730](../satellite_debug_tool/ui/main_window.py) 修好。

## 1. 目标

1. 三个 Tab：Live / Playback / Log，**互不污染**
2. Playback 完整保留全部录制数据，不再受 30000 帧上限
3. WindTerm log 文件可导入解析、自动按表头分列、绘曲线
4. UI 全局固定 small 档（基准 12px），删除字号调节入口
5. 实时模式继续无明显卡顿（如有则做针对性优化）

## 2. 整体架构

```
MainWindow (薄壳)
├── QToolBar (全局：主题切换 + spacer + hw 标签 + 状态)
├── QTabWidget
│   ├── Tab "实时"  → LiveView      (现有 main_window 主体抽出)
│   ├── Tab "回放"  → PlaybackView   (独立 DataStore + ProfileStore)
│   └── Tab "Log"  → LogView        (独立 DataStore + 虚拟 ProfileStore)
└── QStatusBar (FPS/Channels/Frames/Errors, 显示当前 active tab 的统计)
```

**复用 vs 新建**：

| 组件 | Live | Playback | Log | 备注 |
|------|------|----------|-----|------|
| `GroupedChartWidget` | ✓ | ✓ | ✓ | profile 驱动；每个 view 持有独立实例 + 独立 ProfileStore |
| `DashboardWidget` | ✓ | ✓ | ✗ | log 无 state 概念，不展示 KPI |
| `StatePanelWidget` | ✓ | ✗ | ✗ | 仅实时设备状态字 |
| `EventTimelineWidget` | ✓ | ✓ | ✗ | 录制含 EventReport |
| `AttitudeWidget` | ✓ | ✗ | ✗ | 实时姿态 3D |
| `ControlPanelWidget` | ✓ | ✗ | ✗ | 命令下发，回放/log 无意义 |
| Channel 勾选面板 | ✓ | ✓ | ✓ | 各 view 独立 |

**关键设计**：
- 每个 view 持有自己的 `DataStore`、`ProfileStore`（独立 `ProfileCache=None` 或共享磁盘 cache）
- Toolbar 上的 Connect/Disconnect/Debug/Record 按钮**只在 Live tab 可见**；切到 Playback 显示 "Open .sdb"；切到 Log 显示 "Open .log"
- 工具栏右侧主题切换、设备 hw 标签等全局元素始终显示

## 3. 详细设计

### 3.1 字号固化（先做，最小风险）

[main_window.py](../satellite_debug_tool/ui/main_window.py) 改动：

- 删 `self._font_scale` 实例属性
- 删 `self._font_scale_combo` + 它的 QLabel "字号:"
- 删 `_on_font_scale_changed` 方法
- 删 `_load_settings` 里读 `ui.font_scale` 的代码
- `_apply_theme(theme, scale)` 签名保留（避免改下游 widget），但**调用点全部固定 `scale="small"`**
- `__init__` 末尾增加 `if self._settings.remove("ui.font_scale"): self._settings.save()`（一次性清掉旧 key，跟 `attitude` 同款）

[styles.py](../satellite_debug_tool/ui/styles.py) **不动**（test_styles.py 仍依赖 FONT_SCALES API；这些定义保留只是不再被 UI 暴露）。

### 3.2 ChannelBuffer 无界模式（支撑 Playback / Log）

[channel_buffer.py](../satellite_debug_tool/core/data/channel_buffer.py)：

- `__init__(name, capacity: int | None = 30000)`：`capacity=None` 进入无界模式
- 无界模式内部用两个 `list[float]` 追加，`get_times/get_values` 时一次性 `np.asarray()` 转出（缓存结果，dirty flag 重置）
- `get_latest` 直接读 list[-1]

[data_store.py](../satellite_debug_tool/core/data/data_store.py)：
- `DataStore(max_channels=16, buffer_capacity: int | None = 30000)` 透传

### 3.3 Tab 化 — MainWindow 拆分

把现有 `MainWindow` 大部分内容搬到新文件 `ui/live_view.py` 的 `LiveView(QWidget)` 类。`MainWindow` 改为薄壳：

```python
class MainWindow(QMainWindow):
    def __init__(self):
        ...
        self._tabs = QTabWidget()
        self._live = LiveView(settings=self._settings)
        self._playback = PlaybackView()
        self._log = LogView()
        self._tabs.addTab(self._live, "实时")
        self._tabs.addTab(self._playback, "回放")
        self._tabs.addTab(self._log, "Log")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        # toolbar/状态栏由 MainWindow 持有；切 tab 时切换按钮可见性
```

**LiveView 抽出范围**：
- worker / receiver / handshake / profile_store / state_store / event_log / data_store
- toolbar 里 Type/Port/UDP/Connect/Disconnect/Debug/Record + ControlPanel
- 主区：StatusStrip + Dashboard + GroupedChartWidget + AttitudeWidget + StatePanel + EventTimeline + Channel panel

**MainWindow 保留**：
- toolbar 框架 + 全局元素（主题切换、hw label、连接状态 label）
- statusbar
- tab 切换调度

### 3.4 PlaybackView

```python
class PlaybackView(QWidget):
    def __init__(self):
        self._data_store = DataStore(buffer_capacity=None)   # 无界
        self._profile_store = ProfileStore(cache=None)        # 独立，不写盘
        self._state_store = StateStore()
        self._event_log = EventLog()
        self._chart = GroupedChartWidget()                    # 自带"单图/分组"按钮（已具备）
        self._chart.set_profile_store(self._profile_store)
        self._dashboard = DashboardWidget(self._profile_store, self._state_store)
        self._event_timeline = EventTimelineWidget(self._event_log)
        self._range_ctl = TimeRangeControl()                  # §3.7 共用控件
        # 顶部：Open 按钮 + 文件名 label + 总时长 label + 范围控件
        ...
```

**打开流程**：
1. 文件对话框选 `.sdb`（QApplication.processEvents 期间状态栏显示 "Loading..."）
2. `DataImporter.open_sdb(path)` → `SdbFile`
3. 恢复 profile：`hw = self._profile_store.import_dict(sdb.profile)` → 各 widget `set_hw_type(hw)`
4. 清空：`self._data_store.clear(); self._state_store.clear(hw); self._event_log.clear()`
5. 流式灌入（每 5000 条 `processEvents()` 一次给 UI 喘息）：`for rec in sdb.iter_records(): ...`
6. 计算总时长 `(last_ts - first_ts)/1000.0` 设置到 `TimeRangeControl`
7. 默认范围 = "全部"，`self._chart.set_auto_range(True)` + `self._chart.refresh()`

**单图 / 分组切换**：`GroupedChartWidget` 已内置 `set_mode("combined" / "stacked")` + toolbar 按钮（[grouped_chart_widget.py:121-133](../satellite_debug_tool/ui/grouped_chart_widget.py)），PlaybackView 直接复用，无需额外代码。

**性能**：30000 → 无界后，30min @ 100Hz × 8 channels ≈ 17 MB 内存；2h @ 100Hz × 16ch ≈ 138 MB（在状态栏明示"~M MB loaded"，**不设硬上限**）。`setDownsampling("peak", auto=True)` 自动抽稀显示，配合 §3.7 范围选择，不会卡。

### 3.5 LogView + WindTermLogParser

#### Parser（`core/log_parser/windterm_log.py`）

```python
@dataclass
class WindTermLogResult:
    columns: list[str]               # 已剔除非数字列
    data: np.ndarray                 # shape (N, M)，dtype=float32
    timestamps_ms: np.ndarray        # shape (N,)，按行号 × 100ms 占位

class WindTermLogParser:
    HEADER_RE = re.compile(r"track_debug_print_table_header:\s*(.+?)\s*$")
    ROW_RE    = re.compile(r"track_table_row_bynav:\s*(.+?)\s*$")
    TS_RE     = re.compile(r"^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]")

    def parse(self, filepath: str | Path,
              progress_cb: Callable[[int], None] | None = None) -> WindTermLogResult:
        ...
```

**规则**：
- 表头：以最近一次出现的 `track_debug_print_table_header:` 为准
- 数据行：空格分隔，列数应与表头一致；不一致计 warning 跳过
- **非数字列剔除**：扫描首条有效数据行，每列尝试 `float()`，失败列整列 + 列名同步丢弃
- **时间戳**：按行号 × 100ms 占位，X 轴标题注明 "（行号 × 100ms 占位）"
- progress_cb：每 5000 行回调一次，给 UI 显示进度

#### LogView

```python
class LogView(QWidget):
    def __init__(self):
        self._data_store = DataStore(buffer_capacity=None)
        self._profile_store = ProfileStore(cache=None)
        self._chart = GroupedChartWidget()
        self._chart.set_profile_store(self._profile_store)
        # 顶部：Open .log 按钮 + 文件名 + 行数/通道数 label

    def _on_open_clicked(self):
        path = QFileDialog.getOpenFileName(...)
        result = WindTermLogParser().parse(path, progress_cb=self._update_progress)
        # 构造虚拟 profile_dict，每列一个 ChannelDefEntry
        virtual_profile = self._build_virtual_profile(result.columns)
        self._profile_store.import_dict(virtual_profile)
        hw = "windterm_log"
        self._chart.set_hw_type(hw)
        # 构造 DataReport 流灌入 DataStore
        self._inject(result)
        self._chart.set_auto_range(True)
        self._chart.refresh(self._data_store)
```

**虚拟 profile 构造**：
- `hw_type = "windterm_log"`，meta 占位
- 每个列名生成 `ChannelDefEntry(channel_id=i, name=col, unit="", display_min=auto, display_max=auto, group_id=auto)`
- `display_min/max`：从该列数据 min/max ± 5% padding
- `group_id`：暂时全部 0（combined 模式下所有线一张图），未来可按列名前缀自动分组（`ins_*` → group 0，`gps_*` → group 4 等，沿用 GroupedChartWidget 的 _GROUP_PALETTES）

**注入 DataStore**：构造一系列 `DataReport(timestamp, samples=[ChannelSample(ch_id, val), ...])` 喂给 `data_store.update(report)`。每帧一个 DataReport。

**单图 / 分组**：同 Playback，复用 GroupedChartWidget 内置切换。Log 默认进 combined 模式（一张大图扫全貌），分组按列名前缀（`ins_*`/`gps_*`/`imu_*`）映射到 `group_id` 0/4/0。

### 3.7 TimeRangeControl — Playback / Log 共用横坐标范围控件

新建 `ui/time_range_control.py`，独立 widget：

```
┌─────────────────────────────────────────────────────────────┐
│ 范围: [▼ 全部           ] 起 [0.0   ] s 终 [1800.0 ] s [应用]│
└─────────────────────────────────────────────────────────────┘
```

- **预设 ComboBox**：`全部 / 最近 30 秒 / 最近 1 分钟 / 最近 5 分钟 / 最近 30 分钟 / 自定义`
- 选预设 → 自动计算起止并 emit `range_changed(start_sec, end_sec)`，**SpinBox 跟随更新**
- 选"自定义" → 启用 SpinBox，用户改值 + 点"应用" emit
- 接入：`PlaybackView` / `LogView` 监听 `range_changed`，调 `self._chart.jump_to_timestamp` 或新增 `set_x_range(start, end)`
- Chart 端：`GroupedChartWidget` 已有 `jump_to_timestamp(ts_ms, window_sec)` ([grouped_chart_widget.py:200](../satellite_debug_tool/ui/grouped_chart_widget.py))，扩展一个 `set_x_range_sec(start_sec, end_sec)` 直接 setXRange 到任意区间

**总时长接口**：`PlaybackView.load_total_duration_sec(sec)` → `self._range_ctl.set_total(sec)`，让 SpinBox 最大值跟随。LogView 同。

### 3.8 布局优化（字号固化后顺手做）

字号统一 small 后，固定尺寸可让布局更紧凑：

- `_setup_ui` 里 `self._channel_layout.setSpacing(6)` → `4`
- Channel 卡片行高目前 `S.font_px(12, scale) + 20 = 32px`，固定后直接写 `28px`，圆点 `10px`
- `Dashboard` KPI 卡片间距压缩（看实际效果再调）
- `ControlPanel` 高度 `setMaximumHeight(80)` 限定，避免拉伸吃掉曲线区
- `StatusStrip` 高度固定 `28px`
- 工具栏 fixed height 从 42 → 36
- 各 Splitter 默认 stretch factor 重算（chart 区域占比更大）

不改 `styles.py` API；所有尺寸直接在 `_apply_theme` 或 `_setup_ui` 里硬编码（既然字号都固定了，尺寸常量也没必要再走 `font_px` 计算）。

### 3.9 性能再验证（仅在用户实测仍卡时做）

如果阶段 1-4 完成后实测仍有卡顿，候选优化：

- 把 `_heavy_timer` 从 200ms 进一步降到 500ms（曲线 5Hz 仍流畅）
- 事件竖线累积上限 200 → 100，超出 FIFO 出栈
- StatePanel 的 `refresh_channel_values()` 也下移到 200ms timer（当前已经是 _update_heavy 里调）
- Channel 勾选面板的 `_update_display` 里 widget 增删用 dirty flag，profile 变化才执行（profile 不变时跳过 set 操作）

## 4. 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `core/data/channel_buffer.py` | 修改 | `capacity=None` 无界模式 |
| `core/data/data_store.py` | 修改 | 透传 `buffer_capacity` |
| **新** `core/log_parser/__init__.py` | 新增 | 导出 parser |
| **新** `core/log_parser/windterm_log.py` | 新增 | parser 实现 |
| **新** `ui/live_view.py` | 新增 | 从 main_window 抽出实时部分 |
| **新** `ui/playback_view.py` | 新增 | 回放 Tab |
| **新** `ui/log_view.py` | 新增 | Log Tab |
| **新** `ui/time_range_control.py` | 新增 | Playback / Log 共用范围控件 |
| `ui/grouped_chart_widget.py` | 修改 | 新增 `set_x_range_sec(start, end)` |
| `ui/main_window.py` | 大改 | 改为 Tab 容器；删字号 combo；按 tab 切按钮可见性；布局压缩 |
| `ui/__init__.py` | 修改 | 导出新 view + TimeRangeControl |
| `core/config.py` | 修改 | DEFAULT_CONFIG 增 `ui.active_tab` / `playback.last_dir` / `log.last_dir`，删 `ui.font_scale` |
| `tests/test_channel_buffer.py` *(新)* | 新增 | 无界模式单测 |
| `tests/test_windterm_log_parser.py` *(新)* | 新增 | 表头/列剔除/异常行 |
| `tests/test_playback_view.py` *(新)* | 新增 | Playback 与 Live DataStore 隔离 |
| `tests/test_time_range_control.py` *(新)* | 新增 | 预设计算 / 自定义模式 emit |

## 5. 实施阶段

每阶段独立可测，提交单独的 commit。

| 阶段 | 内容 | 验收锚点 |
|------|------|---------|
| **S1 字号固化 + 布局压缩** | 删 combo + 调用点固定 small + 清旧 settings key + §3.8 布局调整 | A4, A6 |
| **S2 后端无界 buffer** | ChannelBuffer / DataStore 支持 `capacity=None` + 单测 | F-12 |
| **S3 TimeRangeControl + chart API** | 新 widget + `set_x_range_sec` + 单测 | D1–D3 |
| **S4 Tab 容器** | MainWindow → QTabWidget；抽出 LiveView；其余 Tab 占位 | A1, A2, A3 |
| **S5 PlaybackView** | 独立 store + 完整导入流程 + 接入 TimeRangeControl + 单图/分组复用 | B1–B5, D4 |
| **S6 LogView + Parser** | parser 单测 + LogView 接入 + 接入 TimeRangeControl + 真实 log 验证 | C1–C7, D5 |
| **S7 性能复测 / 收尾** | 60min 实测；如卡顿做 §3.9；更新 CLAUDE.md + dev log + acceptance 自评 | A1 终验，G1–G4 |

## 6. 不做的事

- 不改协议、不动设备端
- 不做 .sdb v1 兼容（M5 已明确）
- 不做 log 的实时追加监听（一次性加载即可）
- 不做时间轴 scrub（回放只能整段查看；状态字按 EventReport 顺序更新而非按时间滚回）
- 不再保留任何字号档位 UI；styles.py 的 FONT_SCALES 仅为 test_styles 兼容保留，不被使用
- 不引入新依赖

## 7. 风险与权衡

| 风险 | 缓解 |
|------|------|
| MainWindow 1189 行抽出工作量大 | 分阶段：先 S2/S3 改最少代码做出 Tab 骨架，S4/S5 再逐步搬迁；每阶段跑 pytest |
| ProfileStore 三份实例 + 缓存竞争 | Playback/Log 用 `cache=None`；Live 仍用 `ProfileCache()` 写盘 |
| 无界 ChannelBuffer 内存爆 | 30min @ 100Hz × 16ch ≈ 35 MB，可接受；超大 log 文件 (>10 万行) 加 warning 提示 |
| Log 表头变化（如未来设备改 schema） | parser 仅依赖正则关键字，列名完全数据驱动 |
| Tab 切换时 toolbar 按钮闪烁 | 按钮容器用 setVisible，避免 QHBoxLayout addWidget/removeWidget 的视觉断层 |
