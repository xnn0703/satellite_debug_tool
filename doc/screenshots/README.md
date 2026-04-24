# 截图归档

按 plan §11 纪律，每轮 UI 改动保留关键界面截图，便于回溯和验收对照。

## 命名约定

```
{YYYY-MM-DD}_{milestone_or_feature}_{scene}.png
```

- `milestone_or_feature`：`m3` / `m4` / `m6` / `a1` / `ux-fix` 等
- `scene`：用下划线串联，如 `dark_xlarge_main`、`dashboard_kpi_overflow`

示例：

```
2026-04-18_m6_theme_dark.png
2026-04-18_m6_theme_dark_hc.png
2026-04-18_m6_theme_light.png
2026-04-18_m6_fontsize_xlarge.png
2026-04-18_m4_3d_pointing_vectors.png
2026-04-18_m4_3d_error_cone.png
2026-04-18_a1_event_jump_to_chart.png
2026-04-18_a4_state_panel_grouped.png
2026-04-18_a5_channel_enable_dialog.png
```

## 推荐抓取的场景（外场实测时）

1. **主题 × 字号矩阵**（3 × 4 = 12 张）
   - 各组合下主窗口全景
   - 重点关注 `dark_hc + xlarge`：Dashboard/StatePanel/Chart
2. **Dashboard 卡片告警态**：阈值超限红背景（F-08）
3. **3D 场景**
   - 长方体机体 + 参考轴
   - 卫星矢量 + 天线矢量 + 误差扇面
   - 扫描轨迹
4. **事件时间线 + 曲线跳转（A1）**：双击前后 Chart X 范围变化
5. **StatePanel 分组折叠（A4）**：展开 / 折叠两态
6. **通道使能对话框（A5）**
7. **1366×768 / 1920×1080 布局**（U-04）
8. **录制/回放联动**：REC 红灯闪、导入 .sdb 后 UI 按内嵌 profile 重建
9. **外场车载屏**：500cd/m² 阳光下 dark_hc 可读性证据（F-15 / U-01）

## 大小与格式

- 首选 PNG（无损）
- 分辨率 ≥ 1280×800，主题切换场景建议 1600×1000 方便观察细节
- 单张 < 2MB；超过时用 `pngquant` 或 `optipng` 压一下再入库
- 超大视频（> 10MB）建议放外部盘或 git-lfs，本目录只留代表性静态截图

## 如何抓图

- **macOS**：`Cmd+Shift+4` 区域截图，`Cmd+Shift+5` 窗口截图
- **Linux**：`gnome-screenshot -w`，或 `scrot -s`
- **程序内**：工具目前没内置截图按钮；如果外场来不及，可以用系统录屏后切帧

抓完后在对应 plan 验收条目（`doc/acceptance_log.md`）的"证据"列
链接到文件名即可。
