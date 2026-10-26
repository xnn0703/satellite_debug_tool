# 发版 SOP

> 适用：satellite_debug_tool v1.0+
> 关联：[../BUILDING.md](../BUILDING.md) / [M11_plan.md](M11_plan.md)

## 一图流

```
改版本号 → commit → 打 tag → push GitHub tag
                            ↓
                GitHub Actions 触发 build.yml
                            ↓
   prepare-release (解析 tag)
              ↓
   build-windows (user/ops → 两个 7z → 验证 artifact)
              ↓
   publish-windows (两版成功后创建草稿，核对资产后发布)
              ↓
   完成 ✅  https://github.com/xnn0703/satellite_debug_tool/releases
                              ↓
   （可选）本机出 mac 包  ./scripts/build_macos.sh both
                              ↓
   release/SatelliteDebugTool-<user|ops>-macOS-<arch>.zip → 自行分发
```

**注**：mac 不走 CI（PyInstaller .app 与 7z symlink 兼容性问题难修），
本地脚本一键打包，产物 zip 分发给 mac 用户即可。
mac 平台不走自动升级链路（updater 仍嵌入，但 GitHub Release 暂不提供 mac asset）。

> **更新源迁移提示**：`v1.0.1` 及更早版本只查询 Gitee。首次迁移到 `v1.1.0`
> 需要手动下载 GitHub Release；从 `v1.1.0` 起，后续版本可继续通过应用内更新获取。

## 前置检查（每次发版前）

- [ ] 全套 pytest 通过：`PYTHONPATH=. python3 -m pytest satellite_debug_tool/tests -q`
- [ ] 两版本地启动与退出：`python3 -m satellite_debug_tool.main_user`、`python3 -m satellite_debug_tool.main`
- [ ] 本地能打包：`./scripts/build_macos.sh both`（mac）或 `scripts\build_windows.bat both`（win）
- [ ] `doc/v<version>_release_notes.md` 已写新版本变更；发布 job 直接读取该文件作为 Release 正文
- [ ] GitHub Actions 的 `contents: write` 权限可用

## 标准发版流程

### 1. 决定版本号

遵循 [SemVer](https://semver.org/lang/zh-CN/)：

| 改动类型 | 版本号变化 | 例子 |
|---------|----------|------|
| 不兼容 API 修改 / 大功能 | MAJOR | 1.x.x → 2.0.0 |
| 兼容功能新增 | MINOR | 1.0.x → 1.1.0 |
| 兼容问题修复 | PATCH | 1.0.0 → 1.0.1 |
| 预发版本 | -beta.N / -rc.N | 1.1.0-rc.1 |

### 2. 改版本号

**单一来源**：`satellite_debug_tool/__init__.py.__version__`

```bash
vim satellite_debug_tool/__init__.py
# 改 __version__ = "1.1.0"
```

mac .app `Info.plist` 的 CFBundleVersion 会由 spec 自动读这个值，**无需重复修改**。

### 3. commit + tag + push

```bash
git add satellite_debug_tool/__init__.py
git commit -m "chore: bump version to 1.1.0"
git push origin master
git push github master

# tag 名必须 = v + __version__
git tag v1.1.0
git push origin v1.1.0    # 同步 Gitee 源码 tag
git push github v1.1.0    # 触发 GitHub Release
```

### 4. 监控 CI

打开 https://github.com/xnn0703/satellite_debug_tool/actions 看进度：
- prepare-release（30s）
- build-windows（user/ops 两项构建及身份检查）
- publish-windows（两版成功后共同发布）

中间任一失败 → 修问题 → 再走一遍（先删本地 + 远端 tag，改完再 push）

### 5. 验收

- [ ] GitHub Release 同时包含用户版 `satellite_terminal_tool-user-windows-v<version>.7z` 与运维版 `satellite_debug_tool-win-v<version>.7z`
- [ ] 两包构建身份均匹配 tag，用户包包含完整 Shell，旧全量更新器只匹配 ops 资产
- [ ] Windows user/ops 冻结离屏启动烟测及其 smoke artifact 均通过，版本/总览/Shell/退出一致
- [ ] 下载并解压 `.7z` 后目录结构完整
- [ ] 老版本启动后 24h 内或手动点 🔄 检查更新 → 弹"发现新版"对话框
- [ ] 两版分别完成同版更新 → 下载 → 重启 → 回读 edition 与版本；交叉包在替换前被拒绝

## tag 删除 / 重发流程

CI 失败需要重发：

```bash
# 删本地 tag
git tag -d v1.1.0

# 删远程 tag
git push origin :refs/tags/v1.1.0
git push github :refs/tags/v1.1.0

# 修问题后重新 tag + push
git tag v1.1.0
git push origin v1.1.0
git push github v1.1.0
```

## 预发版本（rc / beta）

```bash
git tag v1.1.0-rc.1
git push github v1.1.0-rc.1
```

升级机制中 pre-release 视为旧版（compare_versions：`v1.1.0-rc.1 < v1.1.0`），
正式版发布后用户能自动升上来。

如果只想给少数人测，可以**不 push tag**，改用 workflow_dispatch 在 Actions 页手动触发，
填 `v1.1.0-rc.1` 作为 tag。

## 紧急修复（hotfix）

```bash
# 1. 从上次 tag check out 一个 hotfix 分支
git checkout -b codex/hotfix-1.0.1 v1.0.0

# 2. 改 bug + bump 版本号
vim satellite_debug_tool/...
vim satellite_debug_tool/__init__.py  # → "1.0.1"

# 3. commit + tag + push
git commit -am "fix: 紧急修复 XXX"
git push origin codex/hotfix-1.0.1
git push github codex/hotfix-1.0.1
git tag v1.0.1
git push github v1.0.1

# 4. CI 自动发版

# 5. master merge hotfix
git checkout master
git merge --no-ff codex/hotfix-1.0.1
git push origin master
```

## 常见问题

**Q: tag 推上去 CI 没触发？**
A: 检查 `.github/workflows/build.yml` `on.push.tags` 是否包含 `v*`。tag 名必须 `v` 开头。

**Q: GitHub Release 上传失败 "Resource not accessible by integration"？**
A: 检查 workflow 顶层是否保留 `permissions: contents: write`，并确认仓库 Actions 权限允许写入。

**Q: PyInstaller 打包后启动崩 "ImportError"？**
A: 通常是依赖没在 `satellite_debug_tool.spec` `hiddenimports` 里。
本地 `pyinstaller --noconfirm satellite_debug_tool.spec && ./dist/SatelliteDebugTool/SatelliteDebugTool`
复现，按错误信息加 hidden import。

**Q: `.7z` 下载后无法解压？**
A: 检查 spec 里 `-mf=off` 没有被去掉（关闭 BCJ2 滤器，保 py7zr / The Unarchiver 兼容）。

**Q: 自动更新包应使用什么目录结构？**
A: Windows Release 的 `.7z` 必须直接以本版目录内容作为归档根：用户版 `dist/user/SatelliteTerminalToolUser/`，运维版 `dist/ops/SatelliteDebugTool/`，
即根部同时包含 `SatelliteDebugTool.exe`、`updater.exe` 和 `_internal/`。这保证仍使用旧 updater
的已安装版本也能安装修复版。CI 上传前必须运行 `scripts/verify_update_archive.py <archive> --edition <user|ops> --version <tag>`；新版 updater
也会递归识别历史版本的单层或多层 wrapper，但新的发布包不得再增加 wrapper。

**Q: 升级失败后老版本启动后报错？**
A: Windows 升级器把旧目录改成 `SatelliteDebugTool.bak.<时间戳>`，替换失败会自动尝试回滚；
若需要人工恢复，先退出相关进程，再把备份目录改回原安装目录名。
固定诊断文件为 `~/.satellite_debug_tool/updates/<tag>/updater.log` 和同目录的
`update-status.json`。状态中的 `stage` 是失败阶段，`detail` 是直接原因。

## 双版本首次发布边界（M42）

- 两版共用唯一源码版本号；本地验证包不等于已发布版本。首次正式双版发布应使用新的版本 tag，保留历史 v1.7.0 资产。
- 旧全量版属于 ops，继续读取原设置目录；用户版读取 `~/.satellite_debug_tool/user/`，首次使用空设备目录。Windows 安装目录和 macOS app identity 按 BUILDING.md 区分。
- 旧更新器平台正则只命中保留旧命名的 ops 包。用户版采用 `-user-windows-` 独立名称。新更新器验证 edition/platform/version，历史 ops 目标 `<=1.7.0` 才允许缺少身份 marker。
- 两版下载后都需真实 Windows 冻结程序启动与退出、旧版安装升级以及交叉包拒绝验收。源码测试与 macOS 本地构建不替代该平台门禁。
- 用户版更新状态路径为 `~/.satellite_debug_tool/user/updates/<tag>/`；ops 沿用上文路径。macOS 仍手动分发，不进入当前 Windows updater 安装链路。

## 草稿发布门禁

Windows 两版校验通过后先建立 Release 草稿。核对本地全量/两版 macOS 结果、Windows 离屏启动、资产身份与下载载荷后，通过 `gh release edit v<version> --draft=false --latest` 完成发布。CI 离屏检查验证装配、资源和受控退出；目标机 OpenGL 渲染、旧 Windows 安装原地升级及物理设备动作继续独立验收。
