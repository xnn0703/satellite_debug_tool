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
