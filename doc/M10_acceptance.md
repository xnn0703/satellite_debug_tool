# M10 — 验收锚点（Chart UX 优化）

> 关联：[M10_plan.md](M10_plan.md)
> 验收方式：每条勾选 + 截图 / 复现步骤

## F1 — 归一化 Y 轴 toggle（Live / Playback / Log 三 Tab 共享）

- [ ] **A1.1** chart 工具栏出现"归一化" toggle button，与"单图/分组/全部隐藏"同行，**Live / Playback / Log 三个 Tab 同时可见**
- [ ] **A1.2** 关闭状态：曲线按真实 Y 轴显示，Y 轴有刻度
- [ ] **A1.3** 开启状态：所有曲线被 min-max 缩放到 [0, 1]，Y 轴刻度隐藏或灰显
- [ ] **A1.4** legend 后缀显示真实范围 `[min..max]`（开启状态下）
- [ ] **A1.5** **所有模式**下归一化均用**当前可视窗口**数据计算 min/max，不被全 buffer 一次峰值压平
- [ ] **A1.6** 状态写入 `settings.chart.normalize`，下次启动自动恢复
- [ ] **A1.7** 切换不闪烁（连续点击 10 次无视觉跳变）
- [ ] **A1.8** 复现用户场景：roll/pitch/yaw 同图，yaw ±180° 大幅波动时，开归一化后 roll/pitch 的小幅波动清晰可见
- [ ] **A1.9** Live → Playback → Log 三 Tab 切换时归一化状态独立保持（不需要互相影响）

## F2 — 自定义分组

- [ ] **A2.1** `SettingsDialog` 内出现"管理图表分组..."按钮，点击弹出 `ChartGroupDialog`（modal）
- [ ] **A2.2** 对话框列出当前 hw_type 所有通道
- [ ] **A2.3** 支持新建组、删除组、重命名组标题
- [ ] **A2.4** 通道在组间移动（双击 / 拖拽 / 方向按钮均可）
- [ ] **A2.5** 通道至少属于一个组（删除组前通道自动归还"未分组"区，不能孤立）
- [ ] **A2.6** "恢复 profile 默认"按钮：清掉 settings.chart.custom_groups[hw_type] 当前条目
- [ ] **A2.7** 确定后，分组立即在 chart 上生效（不需要重启）
- [ ] **A2.8** 写入 `settings.chart.custom_groups[hw_type]`，重启后保留
- [ ] **A2.9** 切换设备 hw_type，加载对应自定义分组；未配置则用 profile 默认
- [ ] **A2.10** 同一通道不能同时属于多个组（UI 阻止 + 程序兜底）

## F3a — StatusStrip 自适应换行（FlowLayout）

- [ ] **A3.1** 启动后 StatusStrip 不再出现横向滚动条（Live / Playback / Log 三 Tab）
- [ ] **A3.2** 窗口宽度足够 → 所有 chip 单行排列
- [ ] **A3.3** 窗口缩窄 → chip 自动换行，StatusStrip 高度增加
- [ ] **A3.4** 再放大 → chip 自动回到单行，StatusStrip 高度恢复
- [ ] **A3.5** 极窄窗口（< 单个 chip 宽度） → 每行 1 个 chip，不出滚动条
- [ ] **A3.6** resize 操作不出现明显卡顿（FPS > 30），50ms 防抖生效

## F3b — 通道选择面板移到 Chart 左侧（QSplitter）

- [ ] **A3.7** Live Tab 主体改为 `QSplitter(Horizontal)`，左侧 ChannelPanel + 右侧原内容
- [ ] **A3.8** ChannelPanel 默认宽度 180~240px，用户可拖 splitter 调节
- [ ] **A3.9** Splitter 状态写入 `settings.ui.live_splitter_state`，重启恢复
- [ ] **A3.10** 通道项垂直堆叠：每行 `☐ ● name 当前值`，4 列对齐整齐
- [ ] **A3.11** 整行 hover 高亮，点击空白可切换勾选
- [ ] **A3.12** 通道太多时面板内自动出现垂直滚动条（无水平滚动）
- [ ] **A3.13** "全选 / 清空" 按钮在面板顶部工作正常
- [ ] **A3.14** Playback / Log Tab 不动（不增加 ChannelPanel）

## F4 — 分组独立 Y / X 联动

- [ ] **A4.1** stacked 模式下，鼠标在某个子图上滚轮缩放 → **只**该子图 Y 范围变
- [ ] **A4.2** 鼠标在某个子图上滚轮缩放 X → **所有**子图 X 范围同步变
- [ ] **A4.3** 鼠标在某个子图上拖拽（pan）X → 所有子图 X 同步移动
- [ ] **A4.4** 鼠标拖拽 Y → 只该子图 Y 平移
- [ ] **A4.5** 用户手动调节 Y 后，refresh 不会把 Y 拉回自动范围（**不做时间超时**）
- [ ] **A4.6** chart 工具栏出现"Y 自动"按钮，点击后所有子图 Y 恢复 display_min/max（或当前数据 auto-fit）
- [ ] **A4.7** 切换设备 / clear → Y override 标志被清掉，自动恢复 auto-fit
- [ ] **A4.8** 被手动调过的子图 Y 轴颜色微变（视觉提示"非自动"状态）

## 兼容 / 回归

- [ ] **A5.1** 现有 296 个 pytest 单测全过
- [ ] **A5.2** 录制 / 回放 / 设备 Tab / OTA 等 M1–M9 功能无视觉或行为回归
- [ ] **A5.3** 切换主题（dark / light）UI 元素颜色正确
- [ ] **A5.4** 切换字号 scale (small / medium / large) 所有新增 UI 字号跟随
- [ ] **A5.5** 启动后 settings.json 缺 `chart` section（旧版本配置文件）能正确补默认值，不崩溃

## 文档

- [ ] **A6.1** `doc/M10_dev_log.md` 记录每个 P 阶段的实施日志、踩坑、决策变更
- [ ] **A6.2** 关键截图存 `doc/screenshots/M10_*.png`
- [ ] **A6.3** 验收完成后本文档每条勾选 + 简短证据（截图编号或单测名）
- [ ] **A6.4** CLAUDE.md 更新（如有新组件 / 新约定）
