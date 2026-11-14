# M72 开发与复核记录

## 2026-11-14 现场失败证据

- 录屏显示 v1.9.10 下载 v1.9.11 后退出，随后仍启动 v1.9.10，并再次提示 v1.9.11。
- 事务 `7db0151ddf4e448a9b4752cd68b30d40`：09:22:01 目录切换完成；09:22:05
  在 `waiting_for_health` 报错 `new application exited before health confirmation`；journal 状态为
  `rolled_back`；09:22:06 旧版重新初始化应用日志。
- 事务目录没有 `health-process.json`，证明候选在发布启动进程身份回执之前退出。
- 正式安装 `Info.plist` 声明 `CFBundleExecutable=SatelliteTerminalManager`；旧代码却要求
  `/Applications/SatelliteTerminalManagerOps.app/SatelliteTerminalManager.exe`。
- 正常试运行由 updater 直接创建子进程；LaunchAgent 仅承担中断恢复。因此 macOS 后台项目开关
  不是本次失败的状态源。

## 实现与验证

- `updater/health.py` 删除 `WINDOWS_EXECUTABLE` 依赖，直接消费
  `install_platform.main_executable(request.install_dir)`；Windows 与 macOS 由已有安装平台合同选择入口。
- 健康门禁新增 Windows 和 macOS 两组正反入口测试；macOS 测试从 `Info.plist` 读取
  `SatelliteTerminalManager`，并拒绝目录名派生的 `SatelliteTerminalManagerOps`。
- macOS 自动更新 fixture 改为消费 `APPLICATION_EXECUTABLE`，与正式 PyInstaller app 一致。
- 更新/健康/edition 相邻组合回归：82 passed。
- 首次组合测试使用相对 `PYTHONPATH=.`，前序 updater 测试改变工作目录后，3 个 edition 子进程无法导入包；改用项目绝对
  `PYTHONPATH` 后同一组合稳定通过。该现象没有被当作产品回归，也没有通过修改业务代码掩盖。
- 修复实现快照全量回归：2361 passed in 272.26s；v1.9.12 版本与发布文档冻结后的最终提交快照：
  2361 passed in 275.07s。
- 翻译检查：1558 finished / 0 unfinished；`compileall` 与 `git diff --check` 通过。
- 正式 app 自动更新仍待下一版本现场验证，验收项保持未完成。
