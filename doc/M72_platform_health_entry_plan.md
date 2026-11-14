# M72 自动更新健康试运行入口根因修复计划

## 已确认根因

- v1.9.11 macOS 更新事务已完成下载、SHA-256、载荷准备、签名验证和目录切换。
- 新版试运行进程在健康回执前退出，更新器按合同回滚并重新启动 v1.9.10。
- `updater/health.py` 在冻结应用健康检查中写死 Windows 的
  `SatelliteTerminalManager.exe`，而 macOS 正式入口由 app 的
  `Info.plist:CFBundleExecutable` 声明为 `Contents/MacOS/SatelliteTerminalManager`。
- 因此 macOS 候选在创建窗口和应用日志初始化前被稳定拒绝；下载重试、后台项目权限和
  LaunchAgent 设置均不能改变该错误入口事实。

## 实施范围

1. 健康试运行入口统一消费 `install_platform.main_executable()`：Windows 解析固定 `.exe`，
   macOS 从 `Info.plist` 解析唯一声明入口。
2. 删除健康模块对 `WINDOWS_EXECUTABLE` 的直接依赖；一个安装目录只保留一个入口 owner。
3. 回归测试分别覆盖 Windows 正确/错误入口和真实 macOS app bundle 正确/错误入口。
4. macOS 自动更新 fixture 使用正式产品入口 `SatelliteTerminalManager`，不再使用目录名
   `SatelliteTerminalManagerOps` 伪装可执行文件名。
5. 不放宽身份校验、不增加重试、不通过 LaunchAgent 绕过健康事务。

## 验证与发布边界

- 先运行健康门禁、macOS 自动更新、更新器主事务和 edition 相邻测试，再运行全量 pytest、
  翻译检查、`compileall` 与 `git diff --check`。
- 软件回归通过只证明入口选择和事务模型；真实 v1.9.11 → 后续版本的应用内更新仍由用户
  在正式包上独立验收。
- 本次不自动提交或发布；提交、版本号和发版需继续遵循用户明确指令。
