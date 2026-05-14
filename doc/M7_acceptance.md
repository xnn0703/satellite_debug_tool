# M7 验收标准

> 关联 [M7_plan.md](M7_plan.md)
> 命名沿用 `optimization_plan.md §8` 的 F-/A-/B-/C-/D- 编号体系，避开已用编号

## A. UI / 架构

- [ ] **A1 实时长时间运行无卡顿**：设备或 simulator 100Hz / 8+ 通道，连续 60min，点击/拖拽/切 tab 响应 < 200ms
- [ ] **A2 三 Tab 切换**：实时 / 回放 / Log Tab 切换 < 100ms，无视觉闪烁
- [ ] **A3 工具栏按 Tab 切换**：Live 显示 Connect/Disconnect/Debug/Record；Playback 显示 "Open .sdb"；Log 显示 "Open .log"
- [ ] **A4 字号统一为 small**：toolbar 上**无字号 combo**；UI 默认 12px；settings.json 中无 `ui.font_scale` 字段（启动时一次性清掉）
- [ ] **A5 主题切换全 Tab 生效**：Dark / Dark-HC / Light 切换后三个 Tab 同时响应
- [ ] **A6 布局更紧凑**：相对 M6，相同窗口尺寸下曲线区可视高度增加 ≥ 10%（截图对比）；通道勾选卡片行高 28px、间距 4px；ControlPanel 不再因字号变化而拉伸吃掉曲线区

## B. 回放（Playback Tab）

- [ ] **B1 完整数据展示**：录制 30min `.sdb` 文件在 Playback 打开，曲线时间范围覆盖完整 30min，曲线尾端时间戳 ≈ 录制最后一帧
- [ ] **B2 通道与 profile 同步恢复**：曲线显示通道名 + 单位（来自 .sdb 内嵌 profile），dashboard KPI 渲染正确
- [ ] **B3 回放不污染实时**：先 Live 连接积累 10s 数据 → 切 Playback 打开文件 → 切回 Live → 实时曲线连续无中断，channel panel 不变
- [ ] **B4 实时不污染回放**：B3 之后 Live 继续接新数据 5s → 切 Playback → 数据不变
- [ ] **B5 异常容错**：导入非 `.sdb`、损坏文件、v1 文件 → 状态栏明确错误提示，应用不崩溃
- [ ] **B6 单图 / 分组切换**：Playback 内复用 GroupedChartWidget 的"单图 / 分组"按钮，切换不丢数据
- [ ] **B7 状态栏显示加载信息**：导入完成后状态栏显示 "Loaded N samples (~M MB) over T seconds"

## C. WindTerm Log（Log Tab）

测试输入：用户提供的 `192.168.1.12_2026-05-14_19-29-47.log`

- [ ] **C1 自动表头识别**：从 `track_debug_print_table_header:` 行提取列名；多次出现取最近一次
- [ ] **C2 数据行解析**：所有 `track_table_row_bynav:` 行被解析；行数 ≈ grep 计数 ± 跳过的 warning 行
- [ ] **C3 非数字列剔除**：`state`/`ins_st`/`eskf`/`stat`/`base`/`lazy` 等枚举或字符串列从绘图与勾选列表中消失
- [ ] **C4 列错位容错**：人为破坏一行（删一个值）→ 该行 skip + 状态栏 warning 计数累加；其他行正常
- [ ] **C5 X 轴标注**：X 轴 label 或标题包含 "（行号 × 100ms 占位）" 或等价说明
- [ ] **C6 与实时/回放隔离**：在 Log Tab 加载文件不影响 Live / Playback Tab
- [ ] **C7 大文件不冻 UI**：1 万行 log 解析期间 UI 仍可响应（progress 至少 5000 行刷新一次）
- [ ] **C8 单图 / 分组**：Log Tab 默认 combined 模式；切到 stacked 模式按列名前缀（ins/gps/imu/其他）分子图

## D. TimeRangeControl（Playback + Log 共用）

- [ ] **D1 预设范围**：ComboBox 含 "全部 / 最近 30 秒 / 最近 1 分钟 / 最近 5 分钟 / 最近 30 分钟 / 自定义"
- [ ] **D2 预设联动 SpinBox**：选预设时起止 SpinBox 自动填值并禁用编辑；选"自定义"时启用
- [ ] **D3 应用范围**：自定义模式下改 SpinBox + 点"应用" → chart 立刻 setXRange 到该段，曲线/事件竖线只显示该段
- [ ] **D4 Playback 接入**：2 小时录制文件，选"最近 5 分钟" → 曲线只显示最后 5 分钟；切回"全部"→ 完整显示
- [ ] **D5 Log 接入**：1 万行 log，选"最近 1 分钟"（log 时间轴是行号 × 100ms 占位，1min = 600 行） → 只显示尾段 600 行对应数据

## F-12. 无界 ChannelBuffer

- [ ] **F-12a 单元测试**：`capacity=None` 模式 append 10 万次后 `get_times/get_values` 返回 100k 长度 ndarray，dtype 正确
- [ ] **F-12b 性能基准**：append 1 万次 < 50ms；get_values 转 ndarray < 20ms
- [ ] **F-12c 兼容性**：`capacity=30000` 默认值行为不变，原有 `test_data*` 全部通过

## G. 文档与测试覆盖

- [ ] **G1 CLAUDE.md 更新**：新增 Tab 架构说明 + Log Tab + 字号固化注记
- [ ] **G2 dev log**：写 `doc/M7_dev_log.md` 记录每阶段实施细节、偏离 plan 处与原因
- [ ] **G3 acceptance 自评**：本文件每项自评 ✅/❌ 并记录证据（测试文件 / commit hash / 截图）
- [ ] **G4 pytest 全套通过**：`pytest satellite_debug_tool/tests` 退出码 0，新测试覆盖 channel_buffer 无界模式 / windterm_log parser / playback view 隔离 / time_range_control 预设

## H. 回归基线（不退化）

- [ ] **H1 协议层**：`test_codec_v2.py` / `test_frame_v2.py` / `test_frame_receiver_v2.py` / `test_handshake.py` 全绿
- [ ] **H2 数据层**：`test_state_store.py` / `test_event_log.py` / `test_profile_store.py` 全绿
- [ ] **H3 录制 / 导入**：`test_recorder_importer.py` 全绿（异步录制 + SDB v2）
- [ ] **H4 主题**：`test_styles.py` 全绿（FONT_SCALES 保留所以测试不破）
- [ ] **H5 UX 批 A**：`test_ux_batch_a.py` 全绿
- [ ] **H6 工具 / simulator**：`test_simulator.py` 全绿
- [ ] **H7 实时主流程手测**：Live Tab Connect → 握手 → DataReport 滚动 → Record → Stop → Disconnect 全链路无异常

---

## 自评模板

每阶段完成后在此填写：

```markdown
### Sx 完成 — YYYY-MM-DD

| 锚点 | 状态 | 证据 |
|------|------|------|
| Ax   | ✅   | commit abc1234, screenshot doc/screenshots/sx_a.png |
| Bx   | 🟡   | 自动化测试过，真机待用户验证 |
```
