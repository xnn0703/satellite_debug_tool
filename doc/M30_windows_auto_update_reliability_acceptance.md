# M30 Windows 自动更新可靠性验收标准

## A. 故障复现

- [x] 使用真实 v1.4.0 发布包确认主 EXE 路径为 `release/satellite_debug_tool-win-v1.4.0/SatelliteDebugTool.exe`。
- [x] 最小复现证明旧 `Applier` 更新后安装目录根部缺少 `SatelliteDebugTool.exe`。
- [x] 确认旧 `UpdateDialog` 直接调用 `QApplication.quit()`，绕过 `MainWindow.closeEvent()`。
- [x] 确认实现日志路径与发布文档不一致。
- [x] 确认旧测试 78 passed，但未覆盖真实 CI 归档层级和更新受控关闭。

## B. 更新交接

- [x] `UpdateDialog` 只产生不可变安装请求，不直接启动 updater 或退出 QApplication。
- [x] `MainWindow` 是更新受控关闭和 updater 启动的唯一 owner。
- [x] 正常关闭完成后只启动一次 updater。
- [x] 电源关闭取消、活动操作拒绝、线程收尾失败和资源租约未释放时不启动 updater。
- [x] 普通退出路径保持现有行为。

## C. 载荷与替换

- [x] 真实 CI 双层 wrapper 能解析到唯一载荷根目录。
- [x] 单层安装目录和历史兼容 wrapper 能解析。
- [x] 缺少主 EXE、updater 或 `_internal` 时在旧安装目录变更前失败。
- [x] 多个有效载荷候选明确失败。
- [x] 替换后主 EXE 位于安装目录根部。
- [x] 替换失败后旧版本完成回滚并可启动。

## D. 可观察性

- [x] 日志固定保存在 `~/.satellite_debug_tool/updates/<tag>/updater.log`。
- [x] `update-status.json` 原子记录每个状态和最终结果。
- [x] 解压期间至少每 5 秒记录一次运行心跳和已用时间。
- [x] 失败消息明确显示阶段、原因和日志路径。
- [x] 成功后日志与状态保留，临时解压目录被清理。

## E. 自动化与发布

- [x] 旧实现稳定失败、修复后稳定通过的归档层级回归已加入。
- [x] MainWindow 更新交接的成功、取消和收尾失败有测试。
- [x] updater main、applier、checker、downloader、dialog 相邻测试通过。
- [x] Windows CI 在上传前验证最终 7z 的载荷结构。
- [x] 翻译检查、`compileall`、`git diff --check` 和全量 pytest 通过。

## F. Windows 真机

- [ ] 从旧版本发起更新，下载后主程序完成受控退出。
- [ ] updater 终端持续显示阶段或解压心跳，没有无说明停顿。
- [ ] 新版本覆盖到原安装目录根部并自动启动。
- [ ] 新程序版本号与目标 Release 一致。
- [ ] `updater.log` 和 `update-status.json` 显示 `completed`。
- [ ] 模拟无写权限或占用失败时旧版本仍可启动，状态为 `failed` 并显示日志路径。
