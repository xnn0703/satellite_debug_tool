# M10 — Chart UX 优化（归一化 / 自定义分组 / 自适应布局 / 独立 Y）

> 状态：草案，待用户确认
> 关联：[M10_acceptance.md](M10_acceptance.md)
> 前置：M9（设备 Tab + OTA）已合并

## 1. 背景与目标

M1–M9 把功能补齐了，本期解决 4 个堆积的 UX 问题：

| # | 问题 | 用户原话 |
|---|------|----------|
| F1 | 同组多通道量纲差异大，小范围曲线被压平 | "航向变化很大，俯仰横滚就看不出来了" |
| F2 | 分组完全由 profile 写死，无法用户调整 | "可以自定义分组吗？" |
| F3 | StatusStrip / 通道选择区固定一行 + 横向滚动，丑且容易超屏 | "把滑块去了，直接屏幕内显示，放不下换行" |
| F4 | 分组模式下缩放 Y 轴所有子图一起动 | "X 一起调，Y 各分组自行调节" |

## 2. 关键决策

| 项目 | 决策 |
|------|------|
| 适用页面 | **Live / Playback / Log 三个 Tab 同时支持**（共享 GroupedChartWidget） |
| 归一化触发 | chart 工具栏小巧 toggle button（默认显示页面上，非二级菜单，常用功能），写 `settings.chart.normalize` 持久化 |
| 归一化算法 | min-max 到 [0, 1]，legend 后缀加 `[min..max]` 显示真实范围 |
| 归一化数据范围 | **当前可视窗口**（不是全 buffer，避免一次峰值压扁） |
| 自定义分组存储 | `settings.chart.custom_groups[hw_type] = {group_id: [ch_name, ...]}`，profile 默认 group_id 仅作初始值 |
| 自定义分组入口 | `SettingsDialog` 二级按钮 "管理图表分组..." → 弹出 `ChartGroupDialog` |
| 通道选择区位置 | **从横向条移到 chart 区域左侧**（垂直布局），可压缩 chart 横向空间 |
| StatusStrip 改造 | `QHBoxLayout + QScrollArea` → `FlowLayout`，去掉滚动条 |
| FlowLayout 实现 | 自写 `QLayout` 子类（Qt 官方 example 移植，~80 行），不引第三方 |
| 独立 Y 验证 | 现有 stacked 模式 Y 标称独立，先 reproduce 用户场景；用户手动 Y 后**只在点"自动"按钮时复位**，不做时间超时 |

## 3. 各 Feature 实施细节

### F1. 归一化 Y 轴 toggle

**位置**：`grouped_chart_widget.py` 工具栏（与"单图/分组/全部隐藏"按钮同行），三个 Tab（Live / Playback / Log）共用同一组件，自动一处改全部生效。

**UI**：
- 小巧 toggle button "归一化"（icon + 文字，按下视觉凹陷），与"全部隐藏"等并列
- 工具提示："切换 Y 轴显示模式：绝对值 / 各曲线归一化到 [0,1]"
- 启用时所有子图 Y 范围固定 `(-0.05, 1.05)`，Y 轴刻度隐藏（避免误读）
- legend 显示后缀：`yaw [-180.0 .. 180.0]`，便于回查真实量级

**归一化算法**（refresh 内 `setData` 前介入）：
```python
def _normalize(ys: np.ndarray) -> np.ndarray:
    if ys.size == 0:
        return ys
    vmin, vmax = float(ys.min()), float(ys.max())
    if vmax - vmin < 1e-9:
        return np.full_like(ys, 0.5, dtype=np.float32)  # 平直线显示在中间
    return ((ys - vmin) / (vmax - vmin)).astype(np.float32)
```

**数据范围**：**所有模式（Live / Playback / Log）一律按当前可视窗口 (xs_min, xs_max) 切片后计算 min/max**，保证三种场景行为一致。Live 模式视窗 = `time_window` 秒；Playback / Log 视窗 = 用户在 TimeRangeControl 选的区间。

**legend 后缀刷新频率**：与 `refresh()` 同频（5Hz），避免每帧重算字符串。

### F2. 自定义分组对话框

**入口**：`SettingsDialog` 内加二级按钮"管理图表分组..."，点击弹出 `ChartGroupDialog`（modal）。三个 Tab 共享配置，hw_type 切换时自动加载对应方案。

**对话框 UI**：
```
┌─ 图表分组管理 (hw_type=afd01) ─────────────────────┐
│  当前分组：[组 0: 姿态 ▼] [+ 新建组] [× 删除组]   │
│                                                    │
│  ┌─ 未分组通道 ─┐    ┌─ 当前组通道 ─────────────┐ │
│  │ tgt_az       │ →  │ roll                     │ │
│  │ tgt_el       │ ←  │ pitch                    │ │
│  │ ant_az       │    │ yaw                      │ │
│  │ ...          │    └──────────────────────────┘ │
│  └──────────────┘                                  │
│                                                    │
│  [恢复 profile 默认]  [取消]  [确定]              │
└────────────────────────────────────────────────────┘
```

**存储格式**：
```json
{
  "chart": {
    "custom_groups": {
      "afd01": {
        "0": {"title": "姿态", "channels": ["roll", "pitch", "yaw"]},
        "1": {"title": "指向", "channels": ["ant_az", "ant_el"]},
        ...
      }
    }
  }
}
```

**加载逻辑**：`GroupedChartWidget._rebuild_stacked()` 优先查 settings.chart.custom_groups[hw_type]，未配置时回退到 profile 的 group_id。

**新建/删除组限制**：通道至少属于一个组（不能孤立）；可空组（仅用作标题占位）。

### F3. 布局重构（StatusStrip 自适应 + 通道选择区移到左侧）

分成两块独立改动：

#### F3a — StatusStrip 自适应换行（FlowLayout）

**新建文件**：`satellite_debug_tool/ui/flow_layout.py`（Qt 官方 example C++→Python 移植）

**API**：
```python
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, h_spacing=6, v_spacing=4)
    def addItem(self, item: QLayoutItem)
    def setGeometry(self, rect: QRect)   # 按当前 rect.width() 重排
    def heightForWidth(self, width: int) -> int
    def sizeHint(self) -> QSize
    def minimumSize(self) -> QSize
```

**StatusStrip 改造**（`status_strip_widget.py`）：
- 删 `QScrollArea` + `_inner` + 内层 `QHBoxLayout`
- chip 直接 addItem 到 FlowLayout
- 整个 widget `setSizePolicy(Expanding, Preferred)`，高度随行数自动增长
- 极窄窗口 → 每行 1 个 chip，不出滚动条

#### F3b — 通道选择区移到 chart 左侧

**当前**：`live_view.py` 把 `channel_panel`（标题 + QScrollArea + QGridLayout）放在 chart **上方/下方横向条**，长得难看且容易超屏。

**改造**：用 `QSplitter(Qt.Horizontal)` 重新组织 Live Tab 主体：

```
LiveView (QWidget)
└─ QVBoxLayout
   ├─ ConnectionBar (顶部连接控件，保持)
   ├─ StatusStripWidget (FlowLayout 后高度自适应)
   └─ QSplitter(Horizontal)  ← 主体新增
      ├─ ChannelPanel (左侧，宽度 180~240px 可拖)
      │   ├─ QLabel "通道"
      │   ├─ "全选 / 清空" 按钮一行
      │   └─ QScrollArea
      │       └─ QVBoxLayout: 通道 item 一列垂直堆
      │           (每个 item: ☐ dot name value)
      └─ QTabWidget / 右侧主区域 (chart + dashboard + map ...)
```

**好处**：
- chart 横向少 ~200px，但通道列表换行/紧凑布局后视觉清爽
- 用户可拖 splitter 自由调节左右宽度（存 `settings.ui.live_splitter_state`）
- 通道项垂直堆叠，每条一行，dot+名+值整齐对齐，再多通道也不丑

**Playback / Log Tab 是否同步改造？**
- Playback Tab 已没有"通道选择"概念（曲线全画），不动
- Log Tab 也是全画，不动
- **只 Live Tab 做 splitter 改造**

**ChannelPanel 内通道项 UI**：
```
[☑] ● gps_lat        31.234
[☑] ● gps_lon       121.567
[☐] ● roll           -0.05
[☑] ● pitch           1.23
```
- checkbox：是否绘制
- 彩色圆点：当前曲线颜色
- 名称：左对齐固定列宽
- 当前值：右对齐，等宽字体
- 整行 hover 时高亮，点击空白处切换勾选

### F4. 独立 Y 验证 + 修复

**调研验证**：先 reproduce 用户场景：
1. 打开 Live Tab，切到"分组"模式
2. 进多个组（如姿态 + 指向）
3. 鼠标在姿态子图上滚轮缩放 Y
4. 观察指向子图 Y 范围是否跟着变

**预期**：现有 `_rebuild_stacked()` 第 671 行只调了 `setXLink`，Y 不应联动。若用户复现的"全跟着动"是事实，可能源头：
- `_apply_display_range()` 第 681-685 行强制 Y 范围 → 用户滚轮缩放后立即被覆盖回去
- 解决：滚轮拖拽时记录"用户手动 override"标志，自动 Y 范围只在初始化和"复位"时生效

**改动点**：
- 给 ViewBox 监听 `sigYRangeChanged`：用户操作（mouse=True）时设 per-subplot 标志
- `refresh()` 前检查标志，跳过该子图的 `setRange` 调用
- 工具栏加"Y 自动"按钮 — 点击后**清掉所有 override 标志**，恢复 auto-fit；**不做时间超时自动复位**（用户明确手动控制）
- 视觉提示：被手动调过的子图 Y 轴颜色微变（如细一点的浅色），暗示"非自动"状态

## 4. 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `ui/grouped_chart_widget.py` | 改 | F1 toggle + 归一化 transform；F2 读 custom_groups；F4 独立 Y 修复 + "Y 自动"按钮 |
| `ui/chart_group_dialog.py` | **新建** | F2 分组管理对话框 |
| `ui/settings_dialog.py` | 改 | 增加"管理图表分组..."按钮 → 打开 ChartGroupDialog |
| `ui/flow_layout.py` | **新建** | F3a 自适应换行布局 |
| `ui/status_strip_widget.py` | 改 | F3a 去 QScrollArea，换 FlowLayout |
| `ui/channel_panel.py` | **新建** | F3b 通道选择面板（垂直布局，splitter 左侧用） |
| `ui/live_view.py` | 改 | F3b 主体改 QSplitter；老 channel_panel 内联代码迁出去；保存/恢复 splitter state |
| `core/config.py` | 改 | DEFAULT_CONFIG 增加 `chart` section + `ui.live_splitter_state` |
| `tests/test_grouped_chart.py` | 改 / 新建 | F1 归一化算法单测、F4 Y 范围隔离单测 |
| `tests/test_chart_group_dialog.py` | **新建** | F2 对话框逻辑单测 |
| `tests/test_flow_layout.py` | **新建** | F3a 布局换行计算单测 |
| `tests/test_channel_panel.py` | **新建** | F3b 通道项渲染、勾选信号单测 |

## 5. 实施顺序

| 阶段 | 内容 | 依赖 |
|------|------|------|
| P1 | F3a FlowLayout 基类 + 单测 | 无 |
| P2 | F3a StatusStrip 换 FlowLayout（Live / Playback / Log 共用） | P1 |
| P3 | F3b ChannelPanel 新组件 + 单测 | 无 |
| P4 | F3b LiveView 主体 QSplitter 改造 + splitter 状态持久化 | P3 |
| P5 | F1 归一化 toggle + transform + legend 后缀（三 Tab 共用） | 无 |
| P6 | F4 独立 Y 复现 → 修复 → "Y 自动"按钮 | 无 |
| P7 | F2 settings.chart.custom_groups schema | 无 |
| P8 | F2 ChartGroupDialog 实现 + 单测 | P7 |
| P9 | F2 SettingsDialog 入口 + GroupedChart 读 custom_groups | P7+P8 |
| P10 | 整体回归测试 + 截图 / dev_log 更新 | 所有 |

## 6. 风险

| 风险 | 缓解 |
|------|------|
| 归一化模式下 legend 字符串过长，工具栏溢出 | legend 后缀只显示 2 位有效数字；超过 6 条曲线时折叠 |
| FlowLayout 频繁 resize 触发性能问题 | 加 50ms 防抖；只在 widget width 变化超过 10px 时 reflow |
| 自定义分组保存后切换设备 hw_type，分组失效 | 按 hw_type 隔离存储，切设备自动加载对应配置；未配置则用 profile 默认 |
| 独立 Y 修复破坏 Live 模式自动跟随新数据 | 子图 Y 颜色微变作视觉提示；"Y 自动"按钮一键复位 |
| Splitter 改造影响 Live Tab 原有 dashboard/state/event 子区域布局 | 改造前截图存档；右侧主区维持现有 QTabWidget / Splitter 嵌套不变 |
