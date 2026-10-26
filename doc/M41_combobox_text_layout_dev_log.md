# M41 时间范围下拉框文字布局开发记录

配套：[计划](M41_combobox_text_layout_plan.md)、[验收标准](M41_combobox_text_layout_acceptance.md)。

## 2026-10-22：原生复现

- macOS Qt cocoa + 全局浅色 QSS，TimeRangeControl 弹出列表复现用户截图中的右侧裁切。
- 中文框体固定 110 px；框体字体 Menlo 12 px，弹出视图字体系统 13 px；视口宽 91 px，原菜单委托项建议宽 126 px，实际行宽 137 px。
- 英文框体仍为 110 px，建议尺寸为 144 px，视口 122 px，实际行宽 168 px。
- 修复前截图保存在临时诊断目录，交付阶段保留对应证据。

## 修复与验收

### 根因修复

- `qss.py` 将 QComboBox 从数值输入的等宽字体角色中分离，框体与弹出视图明确使用相同 UI 字体和字号；QLineEdit/QSpinBox/QDoubleSpinBox 保留等宽字体。
- `TimeRangeControl` 使用 Qt 标准 `QStyledItemDelegate` 负责列表项测量与绘制，替换 macOS 默认菜单委托；删除固定 110 px，改用 `AdjustToContents` 和内容尺寸策略。
- 语言切换更新现有模型文本，Qt 重新计算尺寸；当前预设 ID、时间范围和信号语义保持原合同。
- 页面复核发现 Playback 的文件信息、总时长、夹具提示与范围同排，仍会压缩英文框体。Playback 与 Log 将文件信息和范围分为两行，由各自布局容纳完整内容，移除父布局挤压来源。

### 回归证据

- 原实现的新增 6 组语言/主题测试全部失败：中文编辑区宽 74 px、所需 77 px；英文宽 74 px、所需 108 px。
- 控件修复后 15 项范围测试通过。首次全量为 1601 passed；原生页面复核随后检出父工具栏挤压，范围测试扩展为 standalone/Playback/Log 三个宿主。
- 父工具栏修复前，新增 Playback 宿主检查可检出语言切换后的裁切（内容宽 66 px、所需 92 px）。
- 最终范围测试 **27 passed in 0.88s**，含 18 组宿主 × 语言 × 主题几何检查，并在每个实例上执行中英文往返切换；当前选择、范围与信号次数均保持预期。
- 最终全量回归 **1613 passed in 186.91s (0:03:06)**；范围与宿主修复后的全部软件检查通过。

### 原生视觉与交互

- macOS Qt cocoa：独立控件 6 组主题/语言弹出列表检查通过。
- Playback/Log：两页面 × 两语言 × 三主题共 12 组原生页面检查通过；中文框体宽 107 px，文字内容区 71 px、所需 70 px；英文框体宽 129 px，内容区 93 px、所需 92 px。
- 所有行位于弹出列表视口内，长选项完整显示；标准列表通过背景高亮指示选择。
- 原生鼠标点击交互另行确认：选择“最近 5 分钟”后范围为 3300..3600 s，`range_changed` 发出一次。
- 原始裁切：`doc/screenshots/M41_before_popup_{zh_CN,en_US}_light.png`。
- 父工具栏挤压：`doc/screenshots/M41_before_toolbar_reflow_en_US.png`。
- 修复后独立弹出列表与控件：`M41_popup_{zh_CN,en_US}_{dark,dark_hc,light}.png`、`M41_control_{zh_CN,en_US}_{dark,dark_hc,light}.png`。
- 真实页面：`M41_{playback,log}_{zh_CN,en_US}.png` 与对应 `M41_{playback,log}_popup_{zh_CN,en_US}.png`。

### 其余验收与边界

- 翻译资源 check 通过，1413 条消息；本次沿用原有文案。
- compileall 与差异空白检查通过。
- 用户确认的配色、图标、中性命名和既有 M37 改动保持本地工作区现状。
- Windows 原生字体和弹出列表显示在对应平台发行包复核；本次未执行提交或发布。
