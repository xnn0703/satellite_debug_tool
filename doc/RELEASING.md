# 发版 SOP

> 适用：satellite_debug_tool v1.0+
> 关联：[../BUILDING.md](../BUILDING.md) / [M11_plan.md](M11_plan.md)

## 一图流

```
改版本号 → commit → 打 tag → push tag (gitee+github)
                              ↓
                  GitHub Actions 触发 build.yml
                              ↓
   prepare-release (Gitee 建空 release)
                ↓
   build-windows (PyInstaller → 7z 30MB 分卷 → 上传 Gitee + GH)
                ↓
   finalize-release (拼 markdown body + 清旧 release)
                ↓
   完成 ✅  https://gitee.com/soft-hertz/satellite_debug_tool_release/releases
                              ↓
   （可选）本机出 mac 包  ./scripts/build_macos.sh
                              ↓
   release/SatelliteDebugTool-macOS-<arch>.zip → 自行分发
```

**注**：mac 不走 CI（PyInstaller .app 与 7z symlink 兼容性问题难修），
本地脚本一键打包，产物 zip 分发给 mac 用户即可。
mac 平台不走自动升级链路（updater 仍嵌入但 Gitee 上无 mac asset 可拉）。

## 前置检查（每次发版前）

- [ ] 全套 pytest 通过：`PYTHONPATH=. python3 -m pytest satellite_debug_tool/tests -q`
- [ ] 本地能跑：`python3 -m satellite_debug_tool.main`
- [ ] 本地能打包：`./scripts/build_macos.sh`（mac）或 `scripts\build_windows.bat`（win）
- [ ] CHANGELOG 已写新版本变更（如有）
- [ ] secrets 已配：repo settings → Secrets → Actions → `GITEE_TOKEN` 存在且有发版仓库写权限

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

# tag 名必须 = v + __version__
git tag v1.1.0
git push origin v1.1.0
```

### 4. 监控 CI

打开 https://github.com/xnn0703/satellite_debug_tool/actions 看进度：
- prepare-release（30s）
- build-windows（约 10min）
- finalize-release（30s）

中间任一失败 → 修问题 → 再走一遍（先删本地 + 远端 tag，改完再 push）

### 5. 验收

- [ ] Gitee release 页面有完整 body + 全部分卷（mac + win）
- [ ] GitHub Release 页面有对应分卷镜像
- [ ] 老版本启动后 24h 内或手动点 🔄 检查更新 → 弹"发现新版"对话框
- [ ] 一键更新 → 下载 → 重启 → 显示新版本号

## tag 删除 / 重发流程

CI 失败需要重发：

```bash
# 删本地 tag
git tag -d v1.1.0

# 删远程 tag（会取消已 trigger 的 workflow，但 Gitee release 可能残留）
git push origin :refs/tags/v1.1.0

# Gitee release 由 workflow 的 prepare-release 自动清同名 release，无需手删

# 修问题后重新 tag + push
git tag v1.1.0
git push origin v1.1.0
```

## 预发版本（rc / beta）

```bash
git tag v1.1.0-rc.1
git push origin v1.1.0-rc.1
```

升级机制中 pre-release 视为旧版（compare_versions：`v1.1.0-rc.1 < v1.1.0`），
正式版发布后用户能自动升上来。

如果只想给少数人测，可以**不 push tag**，改用 workflow_dispatch 在 Actions 页手动触发，
填 `v1.1.0-rc.1` 作为 tag。

## 紧急修复（hotfix）

```bash
# 1. 从上次 tag check out 一个 hotfix 分支
git checkout -b hotfix/1.0.1 v1.0.0

# 2. 改 bug + bump 版本号
vim satellite_debug_tool/...
vim satellite_debug_tool/__init__.py  # → "1.0.1"

# 3. commit + tag + push
git commit -am "fix: 紧急修复 XXX"
git push origin hotfix/1.0.1
git tag v1.0.1
git push origin v1.0.1

# 4. CI 自动发版

# 5. master merge hotfix
git checkout master
git merge --no-ff hotfix/1.0.1
git push origin master
```

## 常见问题

**Q: tag 推上去 CI 没触发？**
A: 检查 `.github/workflows/build.yml` `on.push.tags` 是否包含 `v*`。tag 名必须 `v` 开头。

**Q: 上传 Gitee 失败 "401 Unauthorized"？**
A: GITEE_TOKEN 过期或权限不够。到 https://gitee.com/profile/personal_access_tokens 重新签发，
更新到 repo settings → Secrets → Actions → GITEE_TOKEN。

**Q: PyInstaller 打包后启动崩 "ImportError"？**
A: 通常是依赖没在 `satellite_debug_tool.spec` `hiddenimports` 里。
本地 `pyinstaller --noconfirm satellite_debug_tool.spec && ./dist/SatelliteDebugTool/SatelliteDebugTool`
复现，按错误信息加 hidden import。

**Q: Gitee 7z 分卷下载下来后用 The Unarchiver 解压失败？**
A: 检查 spec 里 `-mf=off` 没有被去掉（关闭 BCJ2 滤器，保 py7zr / The Unarchiver 兼容）。

**Q: 升级失败后老版本启动后报错？**
A: 升级器把旧版改成 `SatelliteDebugTool.app.bak.<时间戳>`。手动改回 `SatelliteDebugTool.app` 即可恢复。
失败原因看 `~/.satellite_debug_tool/updates/<tag>/updater.log`（如有）。
