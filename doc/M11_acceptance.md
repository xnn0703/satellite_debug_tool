# M11 — 验收锚点（CI 自动打包 + GUI 自动升级）

> 关联：[M11_plan.md](M11_plan.md)
> 验收方式：每条勾选 + 截图 / 命令证据

## V1 — 版本号与 release.config

- [ ] **A1.1** `satellite_debug_tool/__init__.py` 定义 `__version__ = "x.y.z"`
- [ ] **A1.2** `python3 -m satellite_debug_tool.main` 在 about / 状态栏显示该版本号
- [ ] **A1.3** `release.config.json` 含 gitee_owner / gitee_repo / github_owner / github_repo 四个字段
- [ ] **A1.4** PyInstaller 打包后 `release.config.json` 出现在 `_internal/` 内可被读取
- [ ] **A1.5** mac .app `Info.plist` 的 CFBundleVersion = `__version__`

## V2 — Updater Checker

- [ ] **A2.1** `compare_versions("1.2.3", "1.2.10")` 返回负数（"1.2.10" 更新）
- [ ] **A2.2** `compare_versions("v1.0.0", "1.0.0")` 兼容前缀 v
- [ ] **A2.3** `compare_versions("1.0.0-beta.1", "1.0.0")` beta 视为更旧（按 PEP 440 或 semver pre-release 规则）
- [ ] **A2.4** ReleaseChecker.fetch_latest() 用 mock HTTP 返回 release JSON 时正确解析 tag / body / assets
- [ ] **A2.5** Gitee API 404 / 网络超时 → 抛 UpdateCheckError，不崩溃
- [ ] **A2.6** 实际命中真实 Gitee API 拿到 latest release（人工执行）

## V3 — Downloader

- [ ] **A3.1** 多分卷文件按 url 顺序下载到 staging dir
- [ ] **A3.2** 单卷下载失败重试 3 次（指数退避 1s / 2s / 4s）
- [ ] **A3.3** Content-Length 与实际大小不匹配 → 丢弃重下
- [ ] **A3.4** 进度回调每 256KB 触发一次（不卡 UI）
- [ ] **A3.5** 用户中途取消 → 已下文件被清理 / 状态可恢复
- [ ] **A3.6** 断点续传：第二次启动下载续接已下载的字节数（HTTP Range header）

## V4 — Applier

- [ ] **A4.1** 合并 .7z.001/.002... 为单文件 → 头 6 字节 `377abcaf271c` 校验通过
- [ ] **A4.2** py7zr 解压成功，文件结构与新版一致
- [ ] **A4.3** 替换前把旧 install_dir 改名为 `<dir>.bak`
- [ ] **A4.4** 解压失败 → 把 `.bak` 改回 → 用户感知"升级失败，已回滚"
- [ ] **A4.5** Windows 下成功替换 install_dir（updater self-relocate 验证）
- [ ] **A4.6** macOS 下成功替换 .app bundle
- [ ] **A4.7** 替换后启动新版本 → 主程序起来 → __version__ == 新版

## V5 — updater 独立可执行

- [ ] **A5.1** `dist/updater(.exe)` 单独打包成功，大小 < 30MB
- [ ] **A5.2** 命令行调用 `updater --pid X --target Y --archive Z` 行为符合预期
- [ ] **A5.3** updater 启动后 5s 内能等到 main 进程退出（psutil.wait_for_pid）
- [ ] **A5.4** updater 在替换前已 self-relocate 到 OS 临时目录

## V6 — UI 集成

- [ ] **A6.1** MainWindow 工具栏出现 🔄 "检查更新" 按钮
- [ ] **A6.2** 状态栏右下显示当前版本号 `v1.0.0`
- [ ] **A6.3** 点击"检查更新" → UpdateDialog 弹窗，3 个阶段（检查中 / 发现新版 / 下载中）
- [ ] **A6.4** "发现新版"页显示 tag + 前 500 字 release body + 立即更新 / 跳过 / 稍后 三按钮
- [ ] **A6.5** SettingsDialog 出现「更新设置」一行：自动检查复选框 + 间隔输入 + 立即检查按钮
- [ ] **A6.6** "跳过此版本" → 下次后台检查到同 tag 不弹窗
- [ ] **A6.7** "重置跳过版本"按钮（设置内）→ 清掉 skip_version

## V7 — 启动后台静默检查

- [ ] **A7.1** auto_check=True 时启动 2s 后台查
- [ ] **A7.2** check_interval_hours=24 时，上次检查 < 24h 不查
- [ ] **A7.3** 静默检查发现新版 → 状态栏出现"发现新版本"链接，点击展开 UpdateDialog
- [ ] **A7.4** 静默检查网络失败 → 不弹任何东西，不影响主程序使用
- [ ] **A7.5** auto_check=False 时不做后台查

## V8 — CI 自动打包

- [ ] **A8.1** Push `git tag v0.0.1-rc1 && git push --tags` 触发 build.yml
- [ ] **A8.2** prepare-release job 在 Gitee 建空 release（release_id 输出）
- [ ] **A8.3** windows job 成功生成 `satellite_debug_tool-win-v0.0.1-rc1.7z.001...N` 并上传到 Gitee
- [ ] **A8.4** macos job 成功生成 mac 产物并上传
- [ ] **A8.5** finalize-release job 生成最终 release body（含分卷链接 + MD5 + 大小）
- [ ] **A8.6** GitHub Release 镜像了同样的产物
- [ ] **A8.7** Gitee 发版仓库保留最近 3 个 release，更早的自动清理
- [ ] **A8.8** 单个 job 失败时整个 release 不会留下脏数据（prepare 失败则不进入 build）

## V9 — 端到端升级流程

- [ ] **A9.1** 本机装 v0.0.1（手动从 Gitee 下并解压安装）
- [ ] **A9.2** push v0.0.2 → CI 出包成功
- [ ] **A9.3** 启动 v0.0.1 → 后台检查 → 发现 v0.0.2 → 点更新 → 自动下载分卷
- [ ] **A9.4** 应用更新 → 主程序自动重启 → 显示 v0.0.2
- [ ] **A9.5** `~/.satellite_debug_tool/settings.json` 在升级前后内容一致（用户数据未丢）
- [ ] **A9.6** 升级前的 `~/.satellite_debug_tool/profiles/*.json` 保留
- [ ] **A9.7** 模拟下载中途断网 → 提示"下载失败"且可重试

## V10 — 文档

- [ ] **A10.1** BUILDING.md 新增"CI 自动打包"章节
- [ ] **A10.2** `doc/RELEASING.md` 完整 SOP（tag 命名规范 + push 前检查清单 + 失败排查）
- [ ] **A10.3** README.md 加自动升级使用说明（首次安装 + 自动升级）
- [ ] **A10.4** `doc/M11_dev_log.md` 完整记录每个 P 阶段
- [ ] **A10.5** CLAUDE.md 更新（如有新模块 / 新命令）

## V11 — 兼容 / 回归

- [ ] **A11.1** 现有 347 个 pytest 单测全过
- [ ] **A11.2** 现有 BUILDING.md 中描述的本地脚本 (`scripts/build_*.sh/.bat`) 仍可用
- [ ] **A11.3** 设备协议 / 数据流 / chart UX（M9/M10）全部行为无回归
- [ ] **A11.4** 启动时 settings.json 缺 `update` section（旧版本配置文件）能补默认值不崩
