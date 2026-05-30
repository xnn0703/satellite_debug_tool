# M10 — 开发日志

> 关联：[M10_plan.md](M10_plan.md) / [M10_acceptance.md](M10_acceptance.md)

实施过程中遇到的决策变更、踩坑记录、阶段性截图等。每完成一个 P 阶段都更新一段。

---

## P1 — FlowLayout 基类（完成 ✅）

**目标**：自写 `QLayout` 子类实现自适应换行布局。

**实现**：`ui/flow_layout.py` (~100 行)，Qt 官方 example C++ → Python 直译。
- `_do_layout(rect, test_only)` 是核心，从左到右贪心摆放、宽度溢出时换行；`test_only=True` 时只算高度（供 `heightForWidth`），`False` 时真正 `setGeometry`
- `horizontal_spacing` / `vertical_spacing` 支持配置或从父 widget style 取默认

**单测**：`tests/test_flow_layout.py` 11 个，覆盖：
- 基础 API（empty / add / takeAt / hasHeightForWidth）
- 换行计算（宽时单行 / 窄时多行 / 极窄一列 / 宽度增加行数单调不增）
- setGeometry 后实际位置（同行 y 一致、x 单调递增、换行 x 回零）
- minimumSize 覆盖最大 item

全过。

---

## P2 — StatusStrip 换 FlowLayout（完成 ✅）

**改动**：
- 删 `QHBoxLayout(outer) + QScrollArea(_scroll) + QWidget(_inner) + QHBoxLayout(row) + QWidget(_dynamic_host) + QHBoxLayout(_dynamic_layout)` 5 层嵌套
- 改成单层 `FlowLayout` 直挂在 QFrame 上
- 删 `setFixedHeight(bar_h)`，改 `SizePolicy(Expanding, Preferred) + setHeightForWidth(True)`，并 override `heightForWidth(w)` 委托给 `_flow.heightForWidth`
- `_rebuild_dynamic` 直接 `self._flow.addWidget(chip)`，`_clear_dynamic` 用 `self._flow.removeWidget`
- 主题切换 / 重建结束都调 `updateGeometry()` 触发父布局重算

**烟测结果**（offscreen，4 chip）：
- 1200px 宽 → 38px（单行）
- 300px 宽 → 72px（2 行）
- 80px 极窄 → 125px（每行 1 chip，无滚动条）

全套 307 个 pytest 全过。

---

## P3 — ChannelPanel 新组件（完成 ✅）

**新建文件**：`ui/channel_panel.py` (~220 行)

**结构**：
- `_ChannelRow(QWidget)` 内部一行：[☐] ● name 当前值；行级 `:hover` 高亮；
  点击行内空白处也切换 checkbox（`mousePressEvent` 委托给 checkbox.toggle()）；
  `set_checked()` 用 `blockSignals` 避免编程触发也 emit 信号造成循环
- `ChannelPanel(QWidget)` 主组件：标题 + 全选/清空按钮 + QScrollArea 包 QVBoxLayout
- API：`add_channel(name, color, label)` / `remove_channel` / `update_value` / `set_label`
  / `is_checked` / `channel_names` / `apply_theme`
- 信号：`selection_changed(name, checked)` / `select_all_clicked` / `clear_all_clicked`

**单测**：`tests/test_channel_panel.py` 21 个，覆盖：
- add/remove（含 idempotent、不存在的 remove）
- update_value / set_label
- 信号触发（uncheck/check 各 emit 一次）
- 全选/清空（已勾选的跳过，避免噪声 emit）
- 三种主题切换不崩

全过。

---

## P4 — LiveView QSplitter 改造（完成 ✅）

**目标**：把原来横在底部的丑陋通道选择条移到 chart 区域的左侧。

**改动**：
- `top_splitter` 从 [chart, attitude, right_panel] 改成
  **[channel_panel, chart, attitude, right_panel]**（4 个 widget 横向）
- 删掉原 311-356 行整段（QGridLayout + QScrollArea + 各种 dict 跟踪 widget）
- `_on_profile_changed_sync` 改成遍历 `self._channel_panel.channel_names()` 刷 label
- `_update_heavy` 用 `add_channel/remove_channel/update_value` API 替换原来 ~60 行手工 widget 创建/删除/刷新
- `clear()` 改成单行 `for name in panel.channel_names(): panel.remove_channel(name)`
- 主题分发把"逐个 widget setStyleSheet"换成 `self._channel_panel.apply_theme(theme, scale)`
- 全部 `_channel_checks / _channel_dots / _channel_value_labels / _channel_containers / _channel_colors` 跟踪 dict 删除

**splitter 持久化**：
- splitterMoved → 写 `settings.ui.live_top_splitter_sizes`
- `QTimer.singleShot(0, _restore_splitter_state)` 在 init 后异步恢复（确保 splitter 真实尺寸已 layout）

**烟测结果**：
top_splitter widgets: 4
  [0] ChannelPanel size=200x400
  [1] GroupedChartWidget size=568x400
  [2] AttitudeWidget size=350x400
  [3] QSplitter size=260x400

全套 328 个 pytest 全过（296 + 11 FlowLayout + 21 ChannelPanel = 328 ✓）。

---

## P5 — 归一化 toggle（完成 ✅）

**改动**：
- 工具栏新增 `_btn_normalize`（"归一化" toggle 按钮），与"单图/分组/全部隐藏"同行
- `_on_normalize_toggled(on)`：切 `_normalize` flag；同步把所有 plot 的 Y 范围
  设为 `(-0.05, 1.05)` 并隐藏 Y 刻度（`getAxis("left").setStyle(showValues=False)`），
  关闭时按 `_plot_y_ranges[plot_key]` 恢复
- `_plot_y_ranges: Dict[plot_key, (ymin, ymax)]` 在 `_rebuild_combined` /
  `_rebuild_stacked` 中记录原始 Y 范围；`_clear_plots` 时清掉
- `refresh()` 内每条曲线：归一化模式下，按当前 plot.viewRange()[0] 切片得到
  "可视窗口内" `ys_for_range`，调 `_normalize_ys(ys, ys_for_range)` 缩放到
  [0, 1] 再 setData；同时调 `_update_legend_label(plot, curve, name, ymin, ymax)`
  把 legend 文字改成 `roll [-5.2..3.1]` 形式
- `_normalize_ys` 是 staticmethod，便于纯函数单测；平直线退回 0.5
- `_apply_button_theme` 加上 `_btn_normalize.setStyleSheet(btn_style)`

**单测**：`tests/test_grouped_chart_normalize.py` 7 个：
- min-max 简单映射 / 含负值范围 / 平直线 / 空数组
- ys_for_range 子集场景（端点超 [0,1] 是预期行为）
- toggle button 切换 `_normalize` flag

**全套 335 个 pytest 全过。**

**待 P10 真机验证**：legend `[min..max]` 在数据快速变化时是否会闪烁（如有，
加 dirty 比对，只在 min/max 变化 > 5% 时更新文本）。

---

## P6 — 独立 Y + "Y 自动"按钮（完成 ✅）

**调研发现**：stacked 模式 Y 轴本来就独立（rebuild 调 `enableAutoRange(x=False, y=False)`，
只 `setXLink(first_plot)` 联动 X，没碰 Y）；refresh() 也只 setData 不动 Y。
所以"所有曲线跟着动"主要发生在 combined 模式（1 plot 所有曲线共享 Y，wheel
zoom 必然全跟着）。stacked 模式已符合用户期望（X 联动 / Y 独立）。

**改动**：
- 工具栏新增 `_btn_y_auto`（"Y 自动"按钮，非 toggle 一次性触发）
- `_y_overridden: Dict[plot_key, bool]` 跟踪每个子图 Y 是否被用户手动调过
- `_connect_user_y_override(plot_key, plot)` 在每个 plot 创建后接入
  `vb.sigRangeChangedManually` —— 只在用户鼠标交互时触发，程序化 setYRange 不触发，
  完美区分用户/程序
- 用户手动改 Y 后 → `_on_user_y_override` 把该子图 Y 轴文字色改为橙色 `#FFA500`
- 点击"Y 自动" → 所有子图按归一化模式/`_plot_y_ranges` 复位 + 清 override 标记 +
  axis 文字色恢复
- `_clear_plots` 清空 `_y_overridden`

**烟测**：5 个按钮都正常显示（单图/分组/全部隐藏/归一化/Y 自动），点击 _on_y_auto_clicked
不崩。全套 335 个 pytest 全过。

**待 P10 真机验证**：sigRangeChangedManually 在不同 pyqtgraph 版本是否一致触发。

---

## P7 — chart settings schema（完成 ✅）

**core/config.py DEFAULT_CONFIG 新增**：
- `ui.live_top_splitter_sizes: []` — F3b LiveView 主体 splitter 列宽
- `chart.normalize: False` — F1 归一化 toggle 状态
- `chart.custom_groups: {}` — F2 自定义分组 `{hw_type: {gid: {"title", "channels"}}}`

**GroupedChartWidget**：
- 新增 `self._settings = None`（构造默认）+ `set_settings(settings)` API
- `set_settings` 立即读 `settings.chart.normalize` 恢复按钮状态
- `_on_normalize_toggled` 切换时写 `settings.chart.normalize`，重启保留
- LiveView / PlaybackView / LogView 三处都加 `self._chart.set_settings(self._settings)`

**全套 335 个 pytest 全过。**

---

## P8 — ChartGroupDialog（完成 ✅）

**新建文件**：`ui/chart_group_dialog.py` (~280 行)

**UI 结构**：
- 顶部：组下拉 + 新建 / 删除 / 重命名 按钮一行
- 中部：左"未分组通道" QListWidget + 中"→ / ←"移动按钮 + 右"当前组通道" QListWidget
- 底部：恢复 profile 默认 + 取消 / 确定 (QDialogButtonBox)
- hw_type=None（未连接设备）时整个编辑区禁用

**业务逻辑**：
- `_groups: Dict[int, {"title": str, "channels": [name, ...]}]` 工作副本
- 加载：优先 settings.chart.custom_groups[hw_type]，否则从 profile group_id 聚合
- 通道在组间唯一（move 到当前组前先确保没有重复；从当前组移出 = 进未分组）
- 至少保留 1 个组（删最后一个时 QMessageBox.warning 阻止）
- 确定：写 `settings.chart.custom_groups`（key 转 str 便于 JSON）+ `settings.save()`
- 取消：不写
- 恢复默认：丢副本重新从 profile 构建

**单测**：`tests/test_chart_group_dialog.py` 12 个，覆盖：
- 初始加载（profile 默认 / 已有 settings / 未连接禁用）
- 未分组列表行为
- CRUD（新建 + 删除 + 不能删最后一个 + 重命名）
- 持久化（accept 写 / reject 不写）
- 恢复 profile 默认

Mock 通过 monkeypatch QInputDialog.getText / QMessageBox.question/warning，避免弹真窗。

全过。

---

## P9 — SettingsDialog 入口 + GroupedChart 接入（完成 ✅）

**SettingsDialog**：
- 构造增加可选 `profile_store: ProfileStore = None`
- UI 新增"图表分组: [管理图表分组...]" 一行；profile_store=None 时按钮禁用
- 点击 → 弹出 ChartGroupDialog(profile_store, settings, current_hw_type)
- Accept 后手动 `profile_store.profile_changed.emit(hw_type)`，让所有 chart 立即重建子图（无需重启）
- `main_window._on_open_settings` 改成 `SettingsDialog(self._settings, self, profile_store=self._live._profile_store)`

**GroupedChartWidget._rebuild_stacked**：
- 新增 `_get_custom_groups_for_current_hw()` / `_custom_group_title(gid)` 两个辅助方法
- 重写分组聚合：优先从 `settings.chart.custom_groups[hw_type]` 读，回退到 profile group_id
- 自定义模式下未被分组的通道不画（用户自己删除的）
- 空组（用户新建未填充）跳过，不画子图
- subplot title 改用 `_custom_group_title(gid)`，支持用户自定义标题

**烟测**：
- 不带 profile_store: 按钮禁用 ✓
- 带 profile_store: 按钮启用 ✓
- 全套 347 个 pytest 全过（296 baseline + 11 FlowLayout + 21 ChannelPanel + 7 normalize + 12 chart_group_dialog = 347 ✓）

---

## P10 — 回归 + 验收（完成 ✅）

**自动化回归**：347 个 pytest 全过
- baseline 296（M1–M9）
- M10 新增 51：FlowLayout 11、ChannelPanel 21、normalize 7、chart_group_dialog 12

**端到端烟测**（headless main_window）：
- Live 主体 splitter 4 个 widget：[ChannelPanel, GroupedChartWidget, AttitudeWidget, QSplitter(right)]
- chart toolbar 5 按钮：单图 / 分组 / 全部隐藏 / 归一化 / Y 自动
- SettingsDialog(profile_store=...) "管理图表分组..." 按钮启用
- StatusStrip 使用 FlowLayout

**遗留 / 待真机验证**：
- F1 legend `[min..max]` 在数据快速变化时是否闪烁（如有，加 dirty 阈值，只在 min/max 变化 > 5% 时更新）
- F4 sigRangeChangedManually 在当前 pyqtgraph 版本是否稳定触发（不同版本签名略有差异）
- F2 ChartGroupDialog 用户实际拖动顺序是否友好（QListWidget 支持但未启用拖拽，仅按钮）

**M10 整体交付**：见 doc/M10_acceptance.md，按真机操作勾选每条 A 验收点。

---

# M10 汇总

10 个 P 阶段，无返工，全程 347 测试零退化。新增模块：

| 文件 | 行数 | 用途 |
|------|------|------|
| ui/flow_layout.py | ~100 | 自适应换行布局基类 |
| ui/channel_panel.py | ~220 | 左侧垂直通道选择面板 |
| ui/chart_group_dialog.py | ~280 | 图表分组管理弹窗 |

测试：

| 文件 | 用例数 |
|------|--------|
| tests/test_flow_layout.py | 11 |
| tests/test_channel_panel.py | 21 |
| tests/test_grouped_chart_normalize.py | 7 |
| tests/test_chart_group_dialog.py | 12 |

修改：6 个现有文件（status_strip / live_view / grouped_chart / settings_dialog / config / main_window）。
