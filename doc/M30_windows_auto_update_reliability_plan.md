# M30 Windows 自动更新可靠性修复计划

## 1. 背景与已确认根因

2026-10-09 Windows 现场出现“下载完成后弹出 updater 终端，长时间没有后续反应”。当前源码和 v1.4.0 实际发布包已经确认以下问题：

1. **发布包根目录与更新器假设不一致**
   - v1.4.0 发布包中的主程序位于 `release/satellite_debug_tool-win-v1.4.0/SatelliteDebugTool.exe`。
   - `Applier.swap_install_dir()` 只查找与当前安装目录同名的顶层目录；找不到时把整个 staging 当作新安装目录。
   - 最小复现证明更新后 `安装目录/SatelliteDebugTool.exe` 不存在，程序被放入两层子目录，重启必然找不到主程序。
2. **更新退出绕过应用的权威关闭流程**
   - `UpdateDialog._exit_app()` 直接调用 `QApplication.quit()`。
   - `MainWindow.closeEvent()` 才拥有串口、UDP、设备会话、录制、夹具和后台线程的受控收尾。
   - 自动更新绕过该 owner 后，主进程可能继续存活，updater 会停在等待旧 PID 的阶段。
3. **现场诊断路径错误**
   - 实现把日志写入 `%TEMP%/satdbg_update_*/updater.log`。
   - `doc/RELEASING.md` 声明日志位于 `~/.satellite_debug_tool/updates/<tag>/updater.log`。
   - 成功路径还会删除包含日志的整个 workdir，无法追溯升级过程。
4. **长耗时阶段缺少可观察状态**
   - 201 MB v1.4.0 归档包含约 8100 个条目。
   - `py7zr.extractall()` 期间只记录一次 `extract 0%`，直到全部完成才记录 100%，用户无法区分正常解压与进程卡死。

现有 updater 单元测试全部通过（78 passed），但测试归档的顶层名称恰好与安装目录一致，也没有覆盖 MainWindow 受控退出和真实 CI 归档布局，因此没有发现现场故障。

## 2. 修复目标

- 下载完成后由 `MainWindow` 统一执行受控关闭，全部 owner 成功释放后才启动 updater。
- updater 在修改旧安装目录之前，解析并验证唯一有效的 Windows 应用载荷根目录。
- CI 生成的真实层级、单层目录和历史兼容层级均由同一个载荷解析器处理。
- 更新全过程写入固定日志和状态文件；终端持续显示当前阶段与已用时间。
- 任一失败都保留旧版本或完成回滚，并给用户显示明确失败阶段与日志路径。

## 3. 单一所有权与状态流

### 3.1 主程序更新交接

新增不可变 `UpdateLaunchRequest`，包含：

- 当前主进程 PID
- 安装目录
- updater 路径
- 已下载资产路径
- 更新版本
- 固定事务目录、工作目录、日志和状态文件路径

状态流：

1. `UpdateDialog` 完成下载并构造请求，只发出“请求安装”意图。
2. `MainWindow` 保存本次请求并调用自身 `close()`。
3. `MainWindow.closeEvent()` 执行现有电源、网络测试、串口、Customer、Production、夹具、录制、Endpoint 和 UDP 收尾。
4. 任一收尾被用户取消或失败时，清除待安装请求，不启动 updater，应用保持可用。
5. 全部收尾成功后，由 `MainWindow` 启动 updater，再接受关闭事件。

`UpdateDialog` 不再直接退出 QApplication，也不再直接拥有应用关闭行为。

### 3.2 更新器事务状态

固定事务目录：

```text
~/.satellite_debug_tool/updates/<tag>/
├── 下载资产
├── updater.log
├── update-status.json
└── work-<transaction-id>/
```

`update-status.json` 使用单义状态：

- `prepared`
- `waiting_for_app_exit`
- `merging`
- `extracting`
- `validating_payload`
- `swapping`
- `restarting`
- `completed`
- `failed`

失败状态保存错误代码、错误详情、时间、安装路径和备份路径。

## 4. 载荷解析与替换合同

新增唯一 `resolve_payload_root(staging, executable_name)`：

1. 在 staging 中递归查找 `SatelliteDebugTool.exe`。
2. 候选目录必须同时包含 `updater.exe` 和 `_internal/`。
3. 恰好一个候选时返回该目录。
4. 零个候选明确失败为 `apply_payload_missing`。
5. 多个候选明确失败为 `apply_payload_ambiguous`。

替换顺序固定为：

1. 合并并校验 7z。
2. 解压。
3. 解析并完整验证载荷根目录。
4. 记录准备替换状态。
5. 旧安装目录重命名为时间戳备份。
6. 已验证载荷移动到原安装目录。
7. 验证新安装目录根部的主 EXE。
8. 启动新版本。

载荷验证完成前不得移动、删除或重命名当前安装目录。

## 5. 进度与失败呈现

- updater 控制台保留，用明确阶段文本显示等待、合并、解压、验证、替换和重启。
- 阻塞解压期间每 5 秒记录一次已用时间，证明进程仍在运行。
- Windows 失败时使用系统消息框显示错误阶段和固定日志路径。
- 成功后只删除事务工作目录；保留 `updater.log` 和 `update-status.json`。

## 6. 发布包合同

- CI 在上传 Release 资产前运行归档结构校验。
- 校验器必须从最终 `.7z` 中解析出唯一应用载荷，并确认主 EXE、updater 和 `_internal`。
- 修复版必须允许仍运行旧 updater 的已安装版本完成升级，因此新 Windows Release `.7z` 直接以应用内容为归档根，不增加版本目录或 `release/` wrapper。
- 新 updater 继续递归识别历史单层和多层 wrapper，但发布流程不再生成这些 wrapper。

## 7. 实施顺序

1. 增加真实 CI 层级的失败回归，稳定复现 EXE 被放入嵌套目录的问题。
2. 实现载荷解析与替换前验证，删除 staging 根目录兜底模型。
3. 增加 `UpdateLaunchRequest`，把退出与 updater 启动所有权移交给 `MainWindow`。
4. 固化事务目录、日志和状态文件。
5. 增加解压心跳、Windows 失败消息框和重启验证。
6. 增加最终归档结构校验并接入 Windows CI。
7. 更新翻译、发布文档、AGENTS 架构说明并执行专项和全量回归。
8. 发布 Windows 修复版本后，在真实 Windows 安装目录执行一次旧版到新版升级验收。

## 8. 边界

- macOS 当前不发布自动更新资产，本次真机发布验收仅覆盖 Windows。
- 单元测试和 CI 归档校验不能证明 Windows 文件占用、杀毒软件和权限环境，最终仍需真实 Windows 升级证据。
- 当前 v1.4.0 updater 日志若仍存在，应从 `%TEMP%/satdbg_update_*/updater.log` 提取，用于确认本次现场停止的具体阶段。
