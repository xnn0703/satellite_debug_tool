# UI 重设计实施日志 — "Mission Console"

> 来源：Claude Design 交付包 `doc/卫星通信终端测试工具/handoff/`（设计语言 + HTML 原型 + token）
> 目标栈：PySide6 (Qt 6) + QSS，翻译设计 token 为 Qt 调色板
> 风格：Mission Console —— 工程师向深蓝仪表台，青色信号强调，等宽数字，发丝级分隔线

---

## 总览

8 个阶段（D1–D8），全程 438 测试零退化（新增 11 个：icons 7 + channel visibility 4）。

| 阶段 | 内容 | 状态 |
|------|------|------|
| D1 | 设计 token 重写（styles.py palette） | ✅ |
| D2 | 字体系统（IBM Plex Sans/Mono + 加载器） | ✅ |
| D3 | 图标系统（SVG IconRegistry，62 图标） | ✅ |
| D4 | 全局 QSS 样式表（chrome 控件统一） | ✅ |
| D5 | 曲线信号色板（16 色高区分度） | ✅ |
| D6 | P0 修复：通道勾选控制曲线显隐 | ✅ |
| D7 | 组件视觉细化（按钮/chip/KPI/通道行/统计行） | ✅ |
| D8 | 全量回归 + 三主题截图 + dev_log | ✅ |

---

## D1 — 设计 token 重写

`ui/styles.py` `palette()` 完全重写为 Mission Console 三主题（dark / dark_hc / light），同源。

- **兼容键**保留（test_styles 契约 + 老代码）：bg/panel/card/card_alt/border/text/
  text_muted/text_faint/input_bg/input_border/button_bg/value_number/success/warning/error/primary
  —— 全部映射到新色值（primary 从深蓝 #0E639C → 青 #2DD4BF）
- **新语义键**：accent/accent_2/accent_ink/accent_soft、ok/warn/err/info/violet、
  text_2/text_3/text_inv、card_2/panel_2/card_hover/border_2/border_glow、*_soft、grid_line
- 新增 `SIGNAL_PALETTE`（16 色曲线板）
- 老常量（BG_DARK 等）值同步迁移到 Mission Console

dark 主背景 `#090D12`（深蓝黑），accent `#2DD4BF`（青）；dark_hc 纯黑 + `#00F5D4`；
light `#EEF1F4` + `#0EA5A4`。

## D2 — 字体系统

- `monospace_family()` 偏好序列改为 IBM Plex Mono → JetBrains Mono → SF Mono → Menlo → Consolas
- 新增 `sans_family()`（IBM Plex Sans → PingFang SC / Microsoft YaHei 回退）
- 新增 `load_bundled_fonts()`：加载 `ui/assets/fonts/` 下 .ttf/.otf（留空则系统回退）
- `apply_global_font()` 同时设界面字体；main.py 启动时调用
- `assets/fonts/README.md` 说明如何加 IBM Plex（OFL 协议）

## D3 — 图标系统

`ui/icons.py`：62 个线性 SVG 图标（path 内嵌，来自 handoff/icons.js）。
- `icon(name, color, size)` → QSvgRenderer 渲染为按主题着色的 QIcon，HiDPI 清晰，带缓存
- `clear_cache()` 主题切换时调用重新着色
- 替换工具栏 emoji：⚙→settings、🔄→refresh、连接条加 plug/unplug/record/import/trash 等
- 单测 `test_icons.py` 7 个

## D4 — 全局 QSS

`ui/qss.py` `build(theme, scale)`：按 palette 生成覆盖标准 Qt chrome 的全局样式表
（按钮/输入/下拉/复选框/Tab/滚动条/菜单/工具提示/表格/对话框/进度条/列表）。
- MainWindow `_apply_theme` 注入 `app.setStyleSheet()`，各 widget per-widget 样式覆盖局部
- 变体按钮：`setProperty("variant", "primary"/"danger"/"ghost")` + repolish
- Qt QSS 不支持 box-shadow/transition/color-mix → 用 1px accent 边框等价替代；
  半透明用 `rgba()` 转换（`_rgba` 辅助）

### 关键踩坑：bare 声明 cascade

`widget.setStyleSheet("background-color: X")`（无选择器）会 **cascade 到所有子控件**，
盖掉全局 QSS 的 `QPushButton[variant="primary"]` 主按钮色 —— Connect 按钮变灰。
**修复**：所有容器 / 工具栏 / view 的 bare 声明改成带类型选择器
（`LiveView {...}` / `QToolBar {...}` / `#statsRow {...}`），只命中自身不 cascade。
验证：Connect 按钮渲染 RGB = (45,212,191) = accent ✓

## D5 — 曲线信号色板

`grouped_chart_widget._DISTINCT_PALETTE` / `chart_widget.COLORS` 改为 `SIGNAL_PALETTE`
（16 色高区分度，相邻通道色相拉开，三主题一致）。事件竖线色改 Mission Console 语义色。
ChannelPanel 色块与曲线同源。

## D6 — P0 修复：通道勾选控制曲线显隐

原痛点：ChannelPanel checkbox 只控制值标签刷新，不控曲线 —— 用户预期落差大。
- `GroupedChartWidget.set_channel_visible(name, visible)`：按 DataStore key → curve.setVisible
- live_view 连 `ChannelPanel.selection_changed` → chart；mode 切换后 `_resync_channel_visibility`
- 未勾选行整行变暗（色块去饱和 + 文字弱化），所见即所得
- 单测 `test_chart_channel_visibility.py` 4 个

## D7 — 组件视觉细化

- **连接条按钮**：Connect = variant primary（accent 实底 + plug 图标）；Disconnect = danger
  （err 软底 + unplug）；Debug/Record/Import/Clear = 次按钮 + 图标。按钮加宽容下图标+文字。
- **录制中**：Record 变红边框 + 红录制图标；Debug ON = checkable checked 态（accent-soft）
- **StatusStrip chip**：8px 圆点（含辉光）+ card_2 软底 + pill 圆角，语义色更新
- **Dashboard KPI 卡**：左 2px accent 竖条（告警变 err），微标签弱色 + 等宽数字
- **ChannelPanel 行**：hover card_hover 高亮；未勾整行变暗
- **底部统计行**：等宽数字 + 弱色 + 按主题刷新（修 light 主题残留深底）

## D8 — 回归 + 验证

- **438 测试全过**（原 427 + icons 7 + visibility 4）
- E2E smoke：3 主题 × 4 Tab × 3 弹窗 全切不崩
- 截图：`doc/screenshots/UI_redesign_{dark,light,hc,device}.png`

---

## 新增 / 修改文件

| 文件 | 操作 |
|------|------|
| `ui/styles.py` | 改 — palette 重写 + SIGNAL_PALETTE + sans/load_bundled_fonts |
| `ui/icons.py` | **新建** — 62 SVG 图标 + 渲染缓存 |
| `ui/qss.py` | **新建** — 全局 QSS 生成器 |
| `ui/chart_widget.py` | 改 — COLORS 对齐 SIGNAL_PALETTE |
| `ui/grouped_chart_widget.py` | 改 — 信号色板 + set_channel_visible |
| `ui/channel_panel.py` | 改 — 未勾变暗 + mono 字体 |
| `ui/status_strip_widget.py` | 改 — chip 点阵化 + 语义色 |
| `ui/dashboard_widget.py` | 改 — KPI 左 accent 竖条 |
| `ui/main_window.py` | 改 — 注入全局 QSS + 图标按钮 + 状态栏版本 |
| `ui/live_view.py` | 改 — variant 按钮 + 通道显隐 + 统计行主题 + 防 cascade |
| `ui/playback_view.py` / `log_view.py` | 改 — 防 cascade 选择器 |
| `main.py` | 改 — 启动加载字体 |
| `ui/assets/fonts/README.md` | **新建** |
| `tests/test_icons.py` | **新建** — 7 |
| `tests/test_chart_channel_visibility.py` | **新建** — 4 |

---

---

# 第二轮：结构性重构（R1–R6）

第一轮（D1–D8）只做了 token/QSS 配色层「换肤」，但用 Microsoft Edge headless 渲染
设计原型 `Prototype.html` 后发现：设计稿是一套**完全不同的布局结构**，不是重新着色。
第二轮按原型逐组件重建布局。参考截图存 `doc/screenshots/PROTO_*.png`。

## R1 — 全局顶栏重构

MainWindow 顶栏从 [主题 combo | spacer | 更新 | 设置] + QTabWidget 原生 tab bar
改为单条 gbar：
- **左**：品牌 = 青色渐变圆角方块(satellite 图标) + "Satellite Debug Tool"
- **中**：seg--accent 药丸 Tab（实时/回放/Log/设备，带图标，选中 card_2 底 + accent_2 字）
- **右**：主题切换图标(moon/contrast/sun 循环) + 检查更新 ghost + 设置图标
- QTabWidget `tabBar().hide()`，由药丸驱动 setCurrentIndex；currentChanged 反向同步药丸

## R2 — 连接条重构

live_view cbar：
- **左设备状态卡** connCard：satellite 图标框(ok/err 软底着色) + 设备名(mono) / 连接状态文案
- Type/IP/Port 字段
- Connect = primary(plug) / Disconnect = danger icon-only / Debug / REC / Import / Clear ghost icon-only
- **右 statline**：FPS / CH / FRM / ERR（等宽，从底部移到连接条右侧）
- 删除底部 stats_row；toolbar 高度 36→48 容下状态卡

## R3 — 通道面板重构

ChannelPanel：
- 标题行 "通道" + 计数徽标 "checked / total"
- 全选 / 清空 / 反选(layers 图标)
- 搜索框（search 图标 + "筛选通道…" + 实时过滤）
- **按 profile group 分组**：姿态/指向/信号/位置 分组标题分隔线（修了 header 插入 off-by-one）
- 未勾选行整行变暗（色块去饱和 + 文字弱化）
- live_view 传 `group=_channel_group_title(name)`

## R4 — 右栏重构（3 列布局）

主区从 4 列 [通道|chart|attitude|state/event] 改为 **3 列** [通道|中心|右栏]：
- 姿态 3D 从独立列移入右栏顶部
- 右栏 rpanel = 垂直 [attitude, state_panel, event_timeline]

## R5 — 中心区 KPI 卡行

Dashboard（KPI 卡行）从全宽移入**中心列顶部**（center_col = [dashboard, chart]），
匹配设计 kpi-row 在 chart 之上的布局。

## R6 — 回归 + 截图核对

- **438 pytest 全过**（零退化）
- 连接态截图 `UI_redesign_connected.png`：设备卡 afd01/LINK OK、通道面板分组+实时值、
  分组堆叠图(姿态/指向/信号)、多色信号曲线 —— 与 PROTO_live.png 高度一致
- 三主题截图 `UI_redesign_{dark,light,hc}.png`

## 已知差异（与原型）

- KPI 卡：设计是 roll/pitch/yaw/snr/lock 派生值卡；本项目 Dashboard 是 profile STATE 驱动
  （需 STATE_DEFINE），语义不同，未强行对齐
- 右栏「状态」未改成设计的 2×2 简单网格，沿用 StatePanelWidget（profile 驱动更完整）
- 图表未做成每图独立 card 边框（沿用 pyqtgraph stacked + 组标题）

## 后续可选

- 打包真实 IBM Plex 字体到 assets/fonts/
- 右栏区段标题（姿态 3D / 状态 / 事件）+ 图标
- EventTimeline 未读数字徽标
- StatusStrip 超出收 `+N 更多`
