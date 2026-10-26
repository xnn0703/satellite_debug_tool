# M38 石墨灰与蓝色 UI 配色开发记录

当前状态：软件实现、1591 项全量回归和 macOS 原生视觉检查完成；Windows 原生视觉待验。

配套：[计划](M38_ui_color_system_plan.md)、[验收标准](M38_ui_color_system_acceptance.md)。

## 2026-10-22：范围与验收准备

### 用户决定

- 用户认可“石墨灰与蓝 · 深色建议”的配色示意。
- 用户同意先整理计划和验收标准，再确认详细实现范围。
- 当时阶段：计划与验收待确认；后续用户已确认实施，结果见本文后半部分。

### 当前源码检查

- HEAD 为 `492bcd3`，主题中心仍使用 Mission Console 深蓝黑与青绿色板。
- 工作区已有 M37 Shell/串口/YMODEM 的源代码、测试、翻译与文档改动；本次准备阶段新增 M38 文档与入口。
- `ui/styles.py` 与 `ui/qss.py` 分别提供色板和全局 QSS，部分状态与绘图颜色仍位于组件内。
- 客户设备/导航选择、SNR 数字与状态色需要在角色层统一审查。
- `doc/UI_REDESIGN_dev_log.md` 记录过 Qt 局部裸样式向子控件传播的问题，后续按当前实际样式验证。
- 先前的 Qt 离屏渲染覆盖客户总览未连接状态；离屏环境的 OpenGL context 创建失败，3D 区域待真实窗口复核。
- 1.86:1 为源码中指定前景/背景色的计算结果；实际控件对比度需结合子控件背景确认。

### 本阶段产物

- `M38_ui_color_system_plan.md`：三主题颜色角色、页面范围、数据颜色边界与 P0–P5 顺序。
- `M38_ui_color_system_acceptance.md`：颜色角色、可读性、工作区、图表、自动化及平台视觉验收。
- 本开发记录：保留范围、实现进展和证据，供完成后逐项对照计划。
- `AGENTS.md` 增加 M38 计划入口，当前运行主题说明继续描述现状。

### 验证与当前结果

- 文档检查 PASS：三份 M38 文档内部链接存在、文件末尾换行完整，空白检查通过；目标文件 `git diff --check` 通过。
- 功能测试与三主题正式视觉验收：待实现后执行。
- 实现阶段开始条件：用户确认本次计划与验收范围。

## 实施阶段记录

| 阶段 | 修改文件与角色变化 | 自动化证据 | Qt/平台视觉证据 | 对照计划与剩余项 |
|---|---|---|---|---|
| P1 色板与角色 | `styles.py`、`qss.py`：成对角色、Qt Palette、对比度与 Qt RGBA 转换 | 配色合同 12 项通过 | 三主题进度条与焦点复核 | 完成 |
| P2 chrome 与 Customer | 导航/设备选择、正文数值、中性色边框、待确认状态呈现 | 客户工作区与既有生命周期回归通过 | 中英文两档尺寸截图 | 完成 |
| P3 Engineering 与图表 | 事件/状态即时换色、通道与实际 pen 同源、GNSS/Orbit、3D 场景、回放/日志 | 图表/状态/回放/协议回归通过 | 原生 OpenGL、GNSS/Orbit、各工程页面 | 完成 |
| P4 Production 与弹窗 | SNR 正文数值、夹具/MS 曲线主题化、共享 Shell/摇摆台主题分发 | 试产/夹具/Shell 回归通过 | 三主题共享弹窗、设置与温度窗口 | 完成 |
| P5 回归与交付 | 全量测试、翻译、差异检查与文档核对 | 1591 passed，翻译 1413 条通过 | macOS 原生检查完成 | Windows 原生视觉待验 |

## 2026-10-22：用户确认后的实现与验收

### 根因与单一来源

1. `styles.py` 统一拥有主题、选择、焦点、按钮、进度条、设备状态和类别色映射。
   历史常量由同一色板派生，现有 `palette()` 与三主题设置合同继续有效。
2. 删除状态带、状态板、KPI 枚举、事件列表与图表中的重复状态色表；
   ENUM 正常与事件 INFO 分别使用正常角色和信息角色。
3. 状态行缓存已呈现值，状态点保存呈现角色，事件项保留 level；切主题立即重绘已有内容。
   这些缓存只服务于呈现，设备事实仍由 Core/Store 提供。
4. Shell 字符格式保留 ANSI 源色/时间戳角色；主题切换只修改格式，保留正文、SGR、CR 与游标解析状态。
5. 通道面板消费实际曲线 pen 的颜色；姿态轴、源线型、GNSS 类别与 ANSI 色号保持原含义。
6. Qt 原生 Palette 与 QSS 使用同一来源，主按钮、选中项、焦点与进度条分别配对。
   原生截图发现百分比跨越填充区时对比不足，随后补齐专用 progress 角色并增加验证。

### 状态呈现边界

- 待确认与不支持的跟踪/锁定/导航数据采用中性文字，SNR 与试产数值使用正文色。
- 有效异常、遥测过期，以及已支持 TX 控制但缺少回读继续使用原有告警含义。
- 常规面板和正常状态使用中性色边框；设备告警、危险操作和活动录制保留独立含义。
- 页面布局、设备 owner、连接代际、事务租约与固件传输逻辑沿用原合同。
- 既有 M37 Shell/串口/YMODEM 工作区改动保留；本次相关增量集中在主题呈现。

### 针对性回归证据

新增 `satellite_debug_tool/tests/test_ui_color_contract.py`。最初两项在旧实现稳定失败：

- `test_existing_event_text_is_readable_after_switch_to_light`：原事件 INFO 文本 `#4EC9B0`
  在白色背景上对比度约 2.04:1，切换主题后旧条目保留旧色。
- `test_unknown_state_recolors_without_new_device_report`：切到浅色后，未知状态文字仍为深色色值 `#586976`。

新实现两项通过，并补充三主题文字/按钮/焦点/输入边界/进度条配对、数据类别、Qt 颜色合同、
录制事实保持、既有 ANSI 与时间戳重着色、曲线与通道色块一致性，共 12 项通过。
客户总览新增待确认遥测使用中性状态的回归；现有 TX 缺回读和过期数据测试继续通过。

首轮全量曾出现 10 项短色值 `#123` 解析失败，已按既有 Qt 颜色输入合同修复，
并验证短 HEX、完整 HEX 与命名颜色。最终全量完成后全部通过。

### 最终软件验证

```bash
PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q --tb=short
# 1591 passed in 82.06s

.venv/bin/python scripts/update_translations.py check
# Translation catalog OK: 1413 messages, 0 unfinished

.venv/bin/python -m compileall -q satellite_debug_tool/ui satellite_debug_tool/tests/test_ui_color_contract.py
git diff --check
```

本次呈现增量使用既有文案；现有 M37 TS/QM 改动保持原样，通过当前源文案检查。
未执行打包或发版；未提交 Git。

### macOS 原生视觉证据

- Qt 平台为 `cocoa`，实际创建窗口并完成原生渲染；与早期 offscreen 的 OpenGL 限制分别记录。
- 177 组矩阵截图：Customer 5 页、Engineering 5 页、Production 4 页 × 两个语言场景 ×
  两档请求尺寸 × 三主题，共 168 组；GNSS、Orbit 与进度条另有 9 组。
- 实际尺寸记录于 `matrix.json`。1440×900 请求受当前可用桌面限制为 1440×891；1280×800 正常。
- 另复核三主题共享 Shell/YMODEM、摇摆台弹窗，以及三个 scope 的设置弹窗和温度窗口。
- 3D 工程视图在深浅背景下实际渲染模型；状态、来源、曲线、网格、刻度、焦点和选择按新角色呈现。
- 完整矩阵为自动采集，人工视觉复核采用代表页面；运行中的真实设备状态由自动化模拟证据覆盖，
  本轮没有将离线截图解释为真机连接、升级或 RF 验收。
- 完整会话证据目录：
  `/Users/xmac/.codex/visualizations/2026/10/22/01a1628b-5c77-74b0-80a3-d225f0c8c452/m38-check/`，
  包含 `matrix.json`、采集脚本与 `pytest-delivery.log`。
- 代表截图已复制到 `doc/screenshots/M38_*.png`，
  [M38_manifest.json](screenshots/M38_manifest.json) 记录其 SHA-256。

### 对照计划与待验项

- P1–P4 的软件实现完成；P5 自动回归与 macOS 原生检查完成。
- 浅色正常/告警色为保证最深 hover 表面上的可读性而调深，计划色值表已同步。
- Qt 原生 Palette、进度条的成对角色以及通道面板消费真实曲线颜色，均属于原计划的可读性/单源收敛。
- Windows 原生窗口、字体和显示缩放仍为待验，本轮 macOS 结果仅证明本平台的视觉呈现。
