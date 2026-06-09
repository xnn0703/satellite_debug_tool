# M12 Plan: 归一化卡顿根因优化 + 按子图独立归一化

## 背景

开启「归一化」后图表明显卡顿。经定位**不是点数问题**（开/关归一化喂给 `setData`
的点数一致，都已降采样到 ~3000）。真正开销在 `refresh()`（5Hz）里每帧、每条曲线：

```python
self._update_legend_label(plot, curve, entry.name, ymin, ymax)  # 每帧改 legend 文字
```

- 改 legend 文字 → pyqtgraph 整个图例**重新布局 + 重绘**
- `for sample, label in plot.legend.items` 查找是 **O(N²)/plot**
- 几条曲线 × 5Hz = 每秒上百次图例重排

## 决策（已与用户确认）

**根因优化 + 按子图独立归一化** 一起做。

## 改动清单（仅上位机 `ui/grouped_chart_widget.py`）

### A. legend 文字节流（根因）
1. `refresh()` 里用 `time.monotonic()` 控制：`[min..max]` 文字 **≤1Hz** 更新，
   而非每帧。归一化的数学映射（曲线缩放）仍每帧做，只节流文字。
2. `_normalize_ys` 改为返回 `(ys_draw, vmin, vmax)`，避免 refresh 与其内部
   **重复算两次 min/max**。
3. 新增 `self._legend_label_by_curve: dict[id(curve)] -> label` 缓存，
   `_update_legend_label` O(1) 查表，干掉 O(N²) 遍历。rebuild 时填充。

### B. 按子图独立归一化
4. 状态从单个 `self._normalize: bool` 扩展为 `self._plot_normalize: dict[plot_key,bool]`。
   `_is_plot_normalized(plot_key)` 返回该子图有效状态。
5. `refresh()` 归一化分支按 **每条曲线所属 plot_key** 判定，而非全局。
6. 每个子图右键菜单加 checkable QAction「归一化此图」→ `_on_plot_normalize_toggled`。
   存 `self._plot_norm_actions[plot_key]` 以便全局开关时同步勾选态。
7. 子图标题归一化时追加「 · 归一」标记（状态可见，替代无法内嵌的 checkbox）。
8. 工具栏「归一化」按钮语义变为 **全局开/关所有子图**（一键全选/全不选），
   仍持久化到 `settings.chart.normalize` 作为 rebuild 默认值。
9. 每个子图 Y 范围切换（`[-0.05,1.05]` ↔ 原始 display 范围）按 plot_key 独立处理，
   抽出 `_apply_plot_normalize_view(plot_key, on)`。

## 验收标准

- [ ] 全局「归一化」开启后，实时刷新不再卡（FPS 不应明显低于关闭时）
- [ ] legend 的 `[min..max]` 文字仍会更新（约每秒一次），数值正确
- [ ] 分组模式下，右键某个子图 →「归一化此图」只归一化该子图，其余不变
- [ ] 被归一化的子图标题显示「· 归一」标记
- [ ] 工具栏「归一化」一键开启/关闭所有子图，右键菜单勾选态同步
- [ ] 单图(combined)模式下右键菜单与工具栏开关行为一致（只有一个 plot）
- [ ] 归一化的数学结果不变（曲线仍按各自 min-max 映射到 [0,1]）
- [ ] 442 existing tests 不受影响（必要时补 normalize 单测）

---

## 实施结果（已完成）

仅改 `ui/grouped_chart_widget.py` + 测试，未动协议/设备端。

**根因优化**
- `refresh()` 用 `time.monotonic()` 把 legend `[min..max]` 文字节流到 ≤1Hz；
  归一化的曲线缩放仍每帧做。状态切换时 `_last_legend_ts=0` 强制下一帧立即更新。
- `_normalize_ys` 改返回 `(ys_draw, vmin, vmax)`，refresh 不再重复算 min/max。
- 新增 `_legend_label_by_curve`（`id(curve)→LabelItem`）缓存，
  `_update_legend_label` 由 O(N²) 遍历变 O(1) 查表，rebuild 时 `_cache_legend_labels` 填充。

**按子图独立归一化**
- `self._normalize`（全局默认）+ `self._plot_normalize[plot_key]`（每子图实际状态）。
- 每个子图右键菜单加 checkable「归一化此图」(`_register_plot`)，标题归一化时追加「· 归一」。
- 工具栏「归一化」=全局一键开/关所有子图，与右键勾选态双向同步
  （`_on_normalize_toggled` / `_on_plot_normalize_toggled`，blockSignals 防回环）。
- `refresh()` 按每条曲线所属子图判定是否归一化。
- `_on_y_auto_clicked` 复位按每子图状态分别恢复 [0,1] / display 范围。

**验收**：446 passed（原 442 + 4 个新 per-plot 测试；6 个 normalize 单测随新返回值更新）。
App 正常启动。FPS 改善需真机确认（离屏无法测）。

> 说明：pyqtgraph 的 PlotItem 是 GraphicsItem，无法内嵌 QCheckBox，故用
> 「右键菜单勾选 + 标题标记」实现每表开关，效果等同。

---

## 追加修复：曲线关抗锯齿（归一化卡顿真正主因）

第一轮(legend 节流)后用户反馈仍卡。截图定位到「信号」子图：`snr` 这类**低方差噪声
信号**归一化后被拉伸**铺满整个 [0,1] 高度**，配合峰值降采样的 min/max 竖条，变成
一片密集锯齿。pyqtgraph 全局开了抗锯齿(`antialias=True`)，描这种**铺满全高的长路径**
代价按像素长度飙升 —— 点数没变(≤3000)，但 AA 光栅化像素翻了好几倍。不归一化时同一
信号被压在窄带里，AA 像素少，所以不卡。

**修复**：两个 rebuild 里 `plot.plot(..., antialias=False)` —— 数据曲线关 AA，
坐标轴/文字/网格仍走全局 AA(per-curve 覆盖全局)。对实时密集曲线渲染提速显著，
工程示波器场景线条略微硬朗无妨。

> 若仍偏慢的后备旋钮：`_live_setdata_max_points` 3000→1500、刷新率 200ms→250ms。
