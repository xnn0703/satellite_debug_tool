# M57 macOS 运维版自动更新计划

日期：2026-11-03。目标是让 macOS 运维版在可信更新包下载并校验后，由测试人员一次确认完成应用退出、`.app` 替换、新版启动、健康确认和失败恢复，不再要求打开下载目录人工替换。

## 当前根因

- 发布目录把 macOS 声明为 `installation_mode=manual`，因此界面在下载校验后只显示人工替换说明。
- 自动安装载荷身份、更新器 CLI、主程序定位及恢复入口当前只接受 Windows onedir；macOS `.app` 虽包含 updater，但不能进入正式安装事务。
- macOS 候选目前是 ad-hoc 签名且未做 Apple notarization。TUF 与 SHA-256 能证明发布授权和字节一致，不能冒充 Apple Developer ID 或公证结论。

## 单一更新事务

继续由现有 `UpdateLaunchRequest`、`InstallJournal`、`Applier`、启动健康回执和主窗口资源收尾拥有安装事实。UI 只发出“安装并重启”意图，不直接复制、删除或替换 `.app`。

平台适配只负责以下确定差异：

| 事实 | Windows | macOS |
| --- | --- | --- |
| 安装根 | onedir 目录 | `SatelliteTerminalManagerOps.app` |
| 主程序 | 根目录 EXE | `Contents/MacOS/SatelliteTerminalManagerOps` |
| 载荷布局 | `windows-onedir-v1` | `macos-app-v1` |
| 身份文件 | `_internal/.../build_identity.json` | `.app/Contents/MacOS/_internal/.../build_identity.json` |
| 签名检查 | 当前系统签名边界不变 | 解包后、切换前、切换后均执行 `codesign --verify --deep --strict`，并核对 bundle id、版本、edition、架构和源码 build ID |
| 持久恢复 | 当前用户登录恢复项 | `~/Library/LaunchAgents/` 的事务专属恢复项，终态删除 |
| 新版启动 | 直接执行 EXE | `open -n <app> --args ...` 或等价确定 PID 的启动入口，健康回执绑定事务、版本和进程 |

## 实施顺序

1. 把平台、布局、主程序、身份路径和恢复入口收敛为一个肯定式安装平台合同，移除 Windows-only 判断散落点。
2. 扩展 Mac ZIP 解析：只接受唯一 `.app` 根，拒绝绝对路径、`..`、链接逃逸、重复成员和额外应用根；保留文件权限与 bundle 结构。
3. 在旧应用仍运行时完成下载摘要、TUF、树清单、空间、目标目录可写性、bundle 身份和 ad-hoc/正式签名验证。任一失败都不退出旧程序。
4. 把 updater 的可运行副本和依赖冻结到 `~/.satellite_debug_tool/updates/transactions/<id>/runtime`，证明它不依赖即将替换的 `.app`。
5. 主窗口按现有 owner 顺序收尾批次、录制、Shell、设备会话；更新助手等待主进程及 `.app` 内子进程全部退出。
6. 在 `.app` 同级创建已完整校验的 staging，先持久化 journal 和 LaunchAgent 恢复入口，再以 rename 完成旧 app 备份和新 app 切换。
7. 启动新 app，等待现有健康回执；成功后提交事务并删除恢复入口，失败/超时/异常退出则停止候选、恢复旧 app、重新启动旧版并保留日志。
8. 发布器为 macOS ops 生成 `automatic` 目录、树清单和可选一跳增量；user 版继续人工完整包，Windows 合同保持不变。
9. 更新中英文文案、发行 SOP、AGENTS 和开发记录；完成源码、冻结包和真实安装演练。

## 权限与安全边界

- 安装目录及其父目录必须能由当前用户完成同目录 staging/rename。无权限时在关闭应用前明确提示，首版不调用 `sudo`、osascript 管理员授权或宽化目录权限。
- ad-hoc 签名只适用于当前内部运维分发边界；实现必须把 `adhoc-verified` 与未来 `developer-id-notarized` 明确区分。自动安装能力不等于 Apple 公证。
- TUF、完整 SHA-256、树清单、bundle 身份和 codesign 缺一不可；失败不得降级为未经验证的覆盖安装。
- user 版仍不包含应用在线更新入口；设备固件 OTA 不受本项影响。
- 配置、录制、SDB、报告和工位数据继续保存在 `.app` 外，不参与应用目录回滚。

## 交付与发布边界

源码和模拟故障回归通过后，必须用两个真实版本的 macOS ops `.app` 做升级与恢复演练。现有 v1.9.1 元数据保持不可变；自动安装从后续新版本开始，旧 v1.9.0/v1.9.1 客户端可下载该版本，但是否能自动安装取决于旧客户端能力，不能通过新包反向补齐旧安装器。
