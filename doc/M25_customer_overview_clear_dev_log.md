# M25 客户总览清除显示数据开发记录

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-09-03 |
| 状态 | 功能实现完成，软件专项验证通过 |
| 计划 | `doc/M25_customer_overview_clear_plan.md` |
| 验收 | `doc/M25_customer_overview_clear_acceptance.md` |

## 实施前事实

- 客户总览顶部当前有连接、GNSS、Orbit/TLE 和录制按钮，没有清除按钮。
- `LiveView` 已存在垃圾桶图标和本地显示清理实现，但入口为私有槽。
- 本功能应复用现有清理动作，只增加客户总览入口，不建立第二套状态源或清理流程。

## 实施记录

- 用户确认计划与验收标准后开始实现。
- `LiveView` 原清理槽收敛为公开 `clear_display_data()`，原 Live 垃圾桶继续调用同一入口。
- `CustomerOverviewView` 在“录制”按钮后增加 `30×29` ghost 垃圾桶按钮，按主题刷新图标，按语言刷新 tooltip/accessible name；Playback 模式隐藏。
- 客户总览按钮只委托其固定 endpoint 的 `LiveView.clear_display_data()`，随后立即刷新页面，不复制 Store 清理代码。
- `ProductServiceStore` 增加 `clear_history()`，只清除 SNR 与组件温度历史并保留当前 Product 事实；避免为清图破坏身份、能力及当前状态。
- 未修改 Broker、Directory、Runtime、录制、设备事务和 wire command 路径；未创建 Git commit。

### 2026-09-03：无设备点击与退出崩溃修复

- 用户现场稳定复现：未连接设备时点击客户总览垃圾桶，延迟构建的 `LiveView` 尚无 `_chart`，原清理方法无条件访问呈现控件并抛出 `AttributeError`。
- 根因修复为单一两层清理：Store、历史与计数始终清除；只有 `presentation_ready` 时才清理工程 Live 的 chart、attitude、dashboard、timeline、channel panel 和标签。清理不会为了访问控件而构建隐藏呈现树。
- 日志中的 `QThread: Destroyed while thread is still running` 另由静默更新检查线程缺少窗口退出收口导致，与是否连接设备无关。`MainWindow` 现在于不可逆页面拆除前请求停止并有界等待该线程；失败则保留窗口。
- macOS `IMKCFRunLoopWakeUpReliable` 为输入法框架日志，与本次 Python `_chart` 异常不是同一根因。

## 验证记录

- 客户总览/清理/多设备/主窗口/i18n 专项：`103 passed in 11.95s`。
- 更新线程相邻专项：`41 passed in 0.65s`。
- 翻译 `update/check`：通过，共 1022 条，无 unfinished。
- `python3 -m compileall -q satellite_debug_tool`：通过。
- `git diff --check`：通过。
- 全量回归：按用户当前要求未执行，不登记为通过。
- 双真机与物理设备行为：未验证。
