# M30 Windows 自动更新可靠性开发记录

## 当前状态

- 状态：软件修复与自动化回归完成，待 Windows 真机从旧版升级验收
- 日期：2026-10-10
- 现场现象：Windows 下载完成后 updater 终端长时间无后续反应，用户终止后手动安装

## 根因证据

| 项目 | 结果 | 证据 |
|---|---|---|
| v1.4.0 归档布局 | 确认不匹配 | 主 EXE 位于 `release/satellite_debug_tool-win-v1.4.0/`，并非归档根或 `SatelliteDebugTool/` |
| 旧替换结果 | 稳定复现失败 | 替换后根部 EXE 不存在，嵌套路径中存在 EXE，旧版备份已产生 |
| 应用退出 | 所有权错误 | `UpdateDialog._exit_app()` 调用 `QApplication.quit()`；权威清理由 `MainWindow.closeEvent()` 持有 |
| 日志路径 | 实现与文档冲突 | 实现为 `%TEMP%/satdbg_update_*/updater.log`，文档为用户 updates 目录 |
| 解压反馈 | 信息不足 | `extractall()` 期间只有 0% 和完成后的 100% |
| 既有测试 | 未覆盖现场合同 | updater 相关 78 passed；测试包根目录与安装目录同名 |

## 实施记录

| 日期 | 工作项 | 状态 | 证据 |
|---|---|---|---|
| 2026-10-10 | 更新交接所有权 | 完成 | `UpdateDialog` 只生成不可变请求；`MainWindow.closeEvent()` 完成受控关闭后启动 updater |
| 2026-10-10 | 固定事务目录 | 完成 | `<tag>/updater.log`、`update-status.json` 与独立 `work-<id>/` |
| 2026-10-10 | 载荷根解析 | 完成 | 主 EXE、updater、`_internal` 三项合同；缺失或多候选在移动旧目录前失败 |
| 2026-10-10 | 历史归档回归 | 完成 | 真实 CI 双层 wrapper 可解析，替换后主 EXE 位于安装目录根部 |
| 2026-10-10 | 旧 updater 兼容 | 完成 | 新 CI Release 归档改为应用内容平铺根目录 |
| 2026-10-10 | 进度与失败诊断 | 完成 | 解压每 5 秒心跳；失败状态原子保存；Windows 消息框显示日志路径 |
| 2026-10-10 | 发布前结构校验 | 完成 | `scripts/verify_update_archive.py` 已接入 Windows Release workflow |
| 2026-10-10 | 更新器专项回归 | 完成 | 91 passed |
| 2026-10-10 | 翻译 | 完成 | 1364 messages，0 unfinished |
| 2026-10-10 | 全量回归 | 完成 | 1460 passed；`compileall`、翻译 check、`git diff --check` 通过 |

## 待验收

- 在 Windows 上使用旧版本通过界面更新到包含本修复的新版本。
- 验证控制台心跳、根目录替换、自动重启和固定状态文件。
- 使用占用或无写权限场景验证失败呈现和旧版本恢复。
