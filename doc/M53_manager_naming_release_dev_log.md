# M53 开发与发布记录

2026-11-02。用户确认命名并授权发布 v1.9.0；计划与验收先行。当前 HEAD 479e014，工作区 M47–M52 为本次对话已授权实现，随本次功能版本交付；发布仅用 github 和既有 ECS。

## 进行中

- 核对名称、更新目标与完整包合同。
- Windows/Mac user/ops 完整包，首版人工迁入新在线站。
- 后续追加实际测试、commit/tag、CI、归档/上传/晋升与下载证据。

## 发布前修订

- 新建共享 package_names 单一文件名合同，Windows/macOS构建、归档、冻结启动、更新/健康/恢复均使用Manager入口；定向212项通过。
- 名称与翻译、报告更新完成，31项相关回归通过，翻译1497条通过。
- 用户选择服务器直链交付，下载目录只允许GET/HEAD，完整user包不进入在线更新渠道；本机Downloads不增加副本。
- Mac构建建立独立Python3.11环境，NumPy2.4.4实际wheel为cp311-macosx_14_0_arm64；PySide6 6.11为macosx_13_0_universal2，最低Mac声明修正14.0。Qt Windows平台参考：https://doc.qt.io/qt-6.11/windows.html （Windows10 1809+，本轮产品最低准入设10.0.19041，物理旧系统仍独立验收）。

## 源码验收

- 发布前全量：`.venv/bin/python -m pytest satellite_debug_tool/tests -q`，2063 passed / 246.05 秒，日志 `/tmp/m53-full-tests.log`。新增服务器人工交付工具若在此后完成，将单列专项及构建环境全量。
- 翻译检查1497条、compileall和git diff --check通过。
- GitHub master与本地HEAD均479e014，v1.9.0标签/Release尚不存在；本轮源码/tag使用github，应用二进制使用ECS。
- 服务器完整user交付新增13项专项通过：receipt/包内身份、CRC、SHA、公开权限、同名冲突及安全路径；只复制已校验完整包，不进入ops在线渠道。
- 本地AGENTS已按用户要求同步M47–M53实际领域owner、预置、默认录制路径、报告质量、命名、构建与直链交付。

## Mac Python 3.11 线程归属修复

- 发布专用 Python 3.11 / PySide6 6.11.0 的同进程顺序 `test_application_icon.py`、`test_customer_session_binding.py`、`test_i18n.py` 重现 SIGSEGV；Qt 原生栈为 `QIcon → QFactoryLoader::instance → QObject::moveToThread`，前面存在 `Python GC → Shiboken → QThread` 析构。同版本 Python 3.13 全量通过不能代替发布环境验收。单独换用 PySide6 6.11.2 仍重现，未把问题归因于升级已经修复的 PYSIDE-3307。
- 根因证据：发布环境 `QtCore.abi3.so` 的 `Sbk_QObjectFunc_thread` 反汇编在返回 `QThread` wrapper 后执行 `Shiboken::Object::setParent(self, result)`；`QThread.currentThread()` 的绑定没有此操作。`shiboken6.dump` 记录共享主线程 wrapper 的 Python parent 随 `EndpointSessionRuntime._assert_owner_thread` / `CustomerDeviceDirectory._assert_owner_thread` 改变。两对象销毁后原主线程 wrapper 的 `isValid` 稳定变为 False，说明一次线程归属查询污染了共享线程的包装对象生命期。这里的 Python parent 不是 Qt 原生 QObject parent。
- 两个领域 owner 均在创建线程内运行，没有 `moveToThread` 合同；构造时只从 `QThread.currentThread()` 捕获不可变 `_owner_thread`，后续入口比较当前线程与该 owner。删除原有 `QObject.thread()` 查询路径；更新器两项线程测试也改为从当前 GUI 线程捕获对照事实。没有增加进程级 keepalive、GC 开关或插件重试。
- 新增 `test_session_owner_thread.py` 覆盖 Runtime/CustomerDirectory 销毁及 GC 后主线程 wrapper、图标插件继续有效，并用真实另一条 Python 线程确认 owner 拒绝调用。旧实现 `2 failed / 2 passed`（`/tmp/m53-owner-thread-old.log`），修复后与原崩溃三文件合跑 `28 passed / 5.33s`（`/tmp/m53-owner-thread-new.log`）。
- Runtime、客户目录、客户会话绑定、更新器与新回归合跑 `55 passed / 1.04s`（`/tmp/m53-owner-thread-adjacent.log`）；项目全部 Python 源码已没有 `.thread()` 调用。发布环境全量和冻结构建由发布流程继续记录。

## 大文件上传与候选更新

- SFTP 写启用流水线；远端摘要与候选拉取按已打开文件的实际长度、32 个并发请求预取。封口仍由关闭后的完整 size/SHA-256 回读确认，任何关闭、读取异常或内容差异均阻止 rename。Paramiko 4.0.0 的流水线响应实现与文档中的关闭错误承诺有差异，因此以最终字节验证作为权威事实。
- 上传/晋升专项 `29 passed / 0.94s`，覆盖续传、并发上界、关闭/预取异常、空文件/截断/等长篡改拒绝封口；实际速率留待正式包传输记录。
- 初始发布提交 `d9ecd9d52fc6fc017ef980d755d0a01a92a1b5f5` 的 Actions `41333023236` 已取消，包尚未进入服务器候选区或公开目录；完成上述修复后，未公开的 v1.9.0 标签和四包统一指向新提交。
