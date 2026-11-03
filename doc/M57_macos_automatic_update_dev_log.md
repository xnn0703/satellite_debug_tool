# M57 macOS 运维版自动更新开发记录

日期：2026-11-03。计划与验收标准已经用户确认，开始实施。

## 开始前事实

- v1.9.1 macOS ops 可信目录为 `installation_mode=manual`，当前界面按设计只提供人工替换。
- 现有事务更新器拥有下载摘要、树清单、持久请求、同盘 staging、journal、健康回执和失败回滚，但载荷身份、CLI、主程序路径及登录恢复入口只接受 Windows。
- 当前 Mac 包为 arm64、ad-hoc 签名、未 Apple notarization。本项不改变该发行身份边界。

## 实施记录

- `payload.py` 的树清单扩展到 macOS，普通文件保存大小/SHA-256，bundle 内相对符号链接保存目标；链接解析逃逸 app 根时拒绝。Windows 继续拒绝链接。
- Mac ops 候选生成 `payload-manifest.json`；发布目录把 `macos-app-v1` 声明为 `automatic`，客户端按实际平台选择 ZIP/7z 和布局，不再把 automatic 等同于 Windows。
- `install_platform.py` 集中拥有安装根、运行目录、身份文件、主程序、bundle id、版本和 codesign 事实。`edition.py` 不再硬编码 Windows 载荷。
- Mac ZIP 先以规范目录检查、重复项/预算/链接边界预检，再用系统 `ditto` 保留权限、AppleDouble 和签名属性；只接受唯一 `.app`。
- 退出旧程序前已完成包/树摘要、父目录写权限、完整解包、树清单、身份和 codesign。持久 updater 副本逐字节复核并单独 codesign 验证。
- Windows/Mac 共用 `UpdateLaunchRequest`、`InstallJournal`、安装锁、staging/backup rename、健康回执和回滚。Mac staging 保持 `.app` 后缀并用 `ditto` 保留签名；切换后再次 codesign。
- Mac 登录恢复入口写入当前用户 `~/Library/LaunchAgents`，只引用事务外置 updater 与 journal，0600 原子落盘；提交或回滚终态删除。
- 健康失败时先终止候选进程、恢复旧 app，再尝试重新启动旧 app。`recovery=rolled_back` 与 `application_restart=started/failed` 分别记录，重新启动失败不再抹掉已经完成的目录回滚事实。

## 当前证据

- M57 新增回归 7 项：安全链接树、ZIP 冻结准备、链接逃逸拒绝、成功交换提交、交换后签名失败回滚与旧版重启、重启失败仍保留回滚事实、LaunchAgent 绑定与清理。
- 更新/发行/edition 相关回归：272 passed；最终 updater 专项：53 passed。
- 全量回归：2256 passed；新增的最后一项回滚/重启分离测试已在最终 updater 专项中独立通过。
- 翻译目录检查：1502 messages；`compileall` 与 `git diff --check` PASS。
- 实际 v1.9.1 Mac ops 完整包：解包后 9775 项、其中 1868 个相对链接，树清单和 `codesign --verify --deep --strict` PASS；build ID 为 `3f89817f6e9b31dbc77c798b2f63e9d58697e244`。
- 实际冻结 updater 复制到 app 外后，独立 codesign 与 `--help` 启动 PASS。

## 未外推边界

- 必须等下一个唯一版本冻结包生成后，执行真实旧版→新版健康回执升级和人为健康失败恢复。不得以同版本不同字节重打包替代该验收。
- v1.9.0/v1.9.1 内没有本次自动安装器，因此升级到首个包含 M57 的版本仍需最后一次人工替换；此后 macOS ops 才进入应用内自动更新链路。
- 当前仍是 ad-hoc 签名、未 Apple notarization；arm64 证据不外推 x86_64。
