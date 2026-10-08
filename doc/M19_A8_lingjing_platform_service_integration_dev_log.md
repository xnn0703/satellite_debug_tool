# M19-A.8 灵境平台服务集成开发记录

## 2026-10-08

### 资源冻结

- 从用户提供的“平台服务程序2604”中提取 Windows 运行所需的 EXE、托管程序集、x86/x64 Skia 原生库和配置文件，共 27 个运行文件、24,003,018 字节。
- 删除 PDB、XML API 说明、历史日志、macOS/Linux 绘图库和示例动作文件，不把非 Windows 运行资源带入发行包。
- 用用户原始 `plat.xml` 替换服务包示例文件，固定 SHA-256 为 `eaead18830af2ce2799c73f47d7197954eb26b02f8d95c616949203ec190d761`。
- `service-manifest.json` 逐文件记录长度和 SHA-256；`ProConvert.exe` SHA-256 为 `e6420b826fd5e0d2535a3afdd70dae3a2642c104222b14dbefc579f310ac592b`，`StewartPlat.dll` SHA-256 为 `ad9d0d364695cfb09267443bf4b9842ac4e761ab453f346da0c67cf4aa924744`。

### 运行 owner

- 新增 `LingjingPlatformServiceController`，Windows 上检查本机 `.101` 地址、UDP 9800 占用、既有进程身份、资源完整性和部署目录。
- 服务资源按版本部署到 `~/.satellite_debug_tool/vendor_services/lingjing-a6/2604/`，以该目录作为工作目录最小化启动 `ProConvert.exe`。
- 只有受管 PID 或相同哈希的既有 `ProConvert.exe` 监听 UDP 9800 后，状态才进入 `listening`；未知进程占用时拒绝接管。
- 服务异常退出进入 `exited`，摇摆台页面禁止开始新会话。现有厂家配置没有 UDP 应用回执，因此仍不把监听或发送解释为平台已运动。
- macOS 不携带也不启动 Windows 服务，界面明确要求 `192.168.15.101:9800` 的远程 Windows 服务。

### UI 与打包

- 摇摆台测试页新增平台服务状态和手动重试入口；页面首次激活自动请求服务启动。
- Windows PyInstaller 发行包携带 vendor 资源；macOS 构建排除这 23 MB Windows 资源。
- Windows 构建在 PyInstaller 前后分别执行 `verify_platform_service_assets.py`，同时验证源资源和最终发行目录。
- 未实现强制结束厂家进程；主程序退出时保留无法确认正常关闭的服务实例，避免直接杀死运动控制进程。
- 主程序启动后写入 5 MB 轮转 `application.log`，保留当前文件和三个历史文件，并记录未处理异常。
- 摇摆台页新增“导出诊断包”，汇总主程序日志、平台服务事件、厂家 `log*.txt/posdata*.txt`、实际 XML、资源清单、本机 IPv4、UDP 9800 owner，以及 Windows `ipconfig /all`、`route print -4`、`netstat -ano -p udp` 输出；`bundle_manifest.json` 保存内容清单、缺项和 SHA-256。

### 验证

- `test_platform_service.py` 覆盖固定 XML、部署修复、macOS 远程模式、`.101` 地址、单实例监听、未知端口 owner、既有服务复用和异常退出。
- `test_fixture_debug_workspace.py` 覆盖 Windows 服务未监听时禁止开始动作会话，监听成立后解除门禁。
- 诊断包测试确认厂家日志、应用日志、事件、配置和网络快照均可单独复核，缺少可选日志不会阻断导出。
- macOS offscreen 组合运行曾在既有生产布局测试处出现一次 Qt 段错误；拆分复跑结果为平台服务与夹具 `21 passed`、生产工作区 `25 passed`、i18n `16 passed`。该段错误没有稳定复现。
- 加入诊断包后最终全量回归 `1422 passed in 54.00s`；翻译目录检查 `1317 messages`，服务资源清单检查通过，`git diff --check` 通过。
- Windows PyInstaller 和摇摆台真机运动仍待 Windows 工作站执行。
