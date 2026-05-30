# M11 — GitHub Actions 自动打包 + GUI 自动升级

> 状态：草案，待用户确认
> 关联：[M11_acceptance.md](M11_acceptance.md)
> 前置：M10（chart UX 优化）已合并

## 1. 背景与目标

当前打包靠本地 `scripts/build_macos.sh` / `build_windows.bat`，没有 CI 自动出包，
也没有自动升级机制。M11 在 4-documents_generate 项目已验证可用的方案基础上：

1. **CI 自动打包**：push `v*` tag → GitHub Actions 并行出 mac + win 产物
2. **GUI 自动升级**：内置 updater 从 Gitee 发版仓库拉新版分卷下载 → 解压 → 替换 → 重启
3. **版本统一管理**：`satellite_debug_tool/__init__.py.__version__` 作为单一源头
4. **保留用户数据**：升级时 `~/.satellite_debug_tool/`（settings / profiles / tiles）天然在
   安装目录外，零额外保留逻辑

## 2. 关键决策（用户已确认）

| 项目 | 决策 |
|------|------|
| 代码签名 | mac / win 都不签名；首次启动 BUILDING.md 已说明绕过方法 |
| 发版仓库 | Gitee 主（updater 拉这个）+ GitHub Release 镜像（海外/开源访问） |
| 自动检查 | 默认开启 + `settings.update.auto_check` 复选框可关；同时支持手动按钮 |
| 失败处理 | 中等：替换前把旧 .app / install dir 改名 `.bak`，新版释放失败回滚 .bak |
| 分卷大小 | 30MB（py7zr 兼容、弱网友好），与参考项目一致 |
| 跳过此版本 | 支持。`~/.satellite_debug_tool/.skip_version` 记录已跳过的 tag |
| 平台 | macOS arm64 + Windows x86_64；mac x86_64 / linux 不在范围 |

## 3. 各模块设计

### 3.1 版本号单源 + release.config.json

**`satellite_debug_tool/__init__.py`**：
```python
__version__ = "1.0.0"
```
所有需要版本号的地方一律 `from satellite_debug_tool import __version__`。

**`release.config.json`**（与 main.py 同目录，CI 注入）：
```json
{
  "gitee_owner": "soft-hertz",
  "gitee_repo": "satellite_debug_tool_release",
  "gitee_api": "https://gitee.com/api/v5",
  "github_owner": "xnn0703",
  "github_repo": "satellite_debug_tool"
}
```
- 默认值嵌进源码作为兜底；CI 用 GitHub Secrets 注入正式值（防泄漏 token，但本配置无 token，注入意义不大，主要是统一约定）
- PyInstaller spec `datas` 加入此文件

### 3.2 updater 模块

**`satellite_debug_tool/updater/__init__.py`**：核心 API
- `class ReleaseChecker`：查 Gitee API `/repos/{owner}/{repo}/releases/latest`，
  返回 `LatestRelease(tag, body, assets: List[Asset])`
- `class Downloader`：HTTP GET，断点续传 + 进度回调 + MD5 / Content-Length 校验
- `class Applier`：合并分卷 → py7zr 解压 → 原子替换（旧版改 .bak）→ 失败 rollback
- `compare_versions(a, b) -> int`：语义化版本比较（`1.2.10 > 1.2.9`）
- `should_check_update(settings) -> bool`：从 settings.update 读策略

**`satellite_debug_tool/updater/main.py`**：独立可执行（PyInstaller 单独打成 updater.exe）
- 接收命令行参数：`--pid <main_pid> --target <install_dir> --archive <staging_dir> --restart-cmd ...`
- 等主进程退出 → 替换 → 启动新版 → 退出
- **关键技巧**：自身在执行前先复制到 OS 临时目录，避免被替换流程删掉自己

**`satellite_debug_tool/ui/update_dialog.py`**：检查 / 下载 / 应用进度弹窗
- 三步骤页：检查中 / 发现新版（changelog + 立即更新/跳过/稍后） / 下载进度

### 3.3 settings schema 扩展

`core/config.py` DEFAULT_CONFIG 新增：
```python
"update": {
    "auto_check": True,                  # 启动后台静默检查
    "check_interval_hours": 24,          # 不要每次启动都查（拒绝刷 Gitee API）
    "last_check_iso": "",                # 上次检查时间
    "skip_version": "",                  # 用户跳过的 tag（再发现同 tag 不弹）
}
```

### 3.4 UI 集成

**MainWindow 工具栏**：在 ⚙ 设置按钮旁加 🔄 "检查更新" 按钮
- 点击 → `UpdateChecker(modal=False)` 后台查 + 显示 UpdateDialog
- 当前版本号显示在状态栏右下角："v1.0.0"

**SettingsDialog**：新增「更新设置」section
- ☑ 启动时自动检查更新
- 检查间隔：[24] 小时
- [立即检查] 按钮（手动触发，复用 UpdateChecker）

**启动后台流程**（main.py / MainWindow init）：
```python
if settings.update.auto_check:
    QTimer.singleShot(2000, lambda: _silent_check(settings))  # 2s 延迟

def _silent_check(settings):
    if not _should_check_now(settings):    # 间隔未到
        return
    checker.check_async(callback=_on_silent_check_done)
    settings.set("update.last_check_iso", datetime.utcnow().isoformat())
```

### 3.5 PyInstaller spec 调整

**`satellite_debug_tool.spec`**：
- 在 `datas` 加 `release.config.json`
- 同步 spec 内的版本号从 `__version__` 读（mac plist CFBundleVersion）
- 新增 second target：updater 可执行（`tools/updater_main.py` → `dist/updater(.exe)`）
- 主程序 + updater **共享 _internal**（PyInstaller MERGE 机制）减重 ~150MB

**新建 `updater.spec`**：
- 入口：`satellite_debug_tool/updater/main.py`
- 极简依赖：`urllib + py7zr + psutil`，无 PySide6
- 输出：`dist/updater(.exe)`

### 3.6 GitHub Actions workflow

**`.github/workflows/build.yml`**：

```yaml
on:
  push:
    tags: ['v*']
  workflow_dispatch:

jobs:
  prepare-release:    # ubuntu-latest, 在 Gitee 建 release 占位
  build-windows:      # windows-latest, PyInstaller → 7z 分卷 → 上传 Gitee + GH
  build-macos:        # macos-14 (arm64), PyInstaller → 7z 分卷 → 上传
  finalize-release:   # ubuntu, 生成最终 release body + 清理旧版（保留最新 3）
```

**关键 secrets**：
- `GITEE_TOKEN`：拥有目标发版仓库写权限的 access token
- `GITHUB_TOKEN`：自动注入，发 GH release 用
- 仓库 vars：`GITEE_OWNER` / `GITEE_REPO` / `GITHUB_OWNER` / `GITHUB_REPO`

### 3.7 文档

- 更新 `BUILDING.md`：增加"CI 自动打包"章节，说明 tag 触发流程
- 新建 `doc/RELEASING.md`：release SOP（怎么打 tag、检查 CI、补 changelog）
- 更新 `README.md` / 用户手册：自动升级使用说明

## 4. 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `satellite_debug_tool/__init__.py` | 改 | 加 `__version__ = "1.0.0"` |
| `release.config.json` | **新建** | gitee/github 仓库配置 |
| `satellite_debug_tool/updater/__init__.py` | **新建** | ReleaseChecker / Downloader / Applier 公共 API |
| `satellite_debug_tool/updater/checker.py` | **新建** | Gitee API 查询 + 版本比较 |
| `satellite_debug_tool/updater/downloader.py` | **新建** | 分卷下载 + 重试 + 校验 |
| `satellite_debug_tool/updater/applier.py` | **新建** | 合并 + 解压 + 替换 + rollback |
| `satellite_debug_tool/updater/main.py` | **新建** | updater 独立可执行入口 |
| `satellite_debug_tool/ui/update_dialog.py` | **新建** | 检查/下载/应用进度弹窗 |
| `satellite_debug_tool/ui/main_window.py` | 改 | 加"检查更新"按钮 + 启动后台静默检查 |
| `satellite_debug_tool/ui/settings_dialog.py` | 改 | 新增更新设置 section |
| `satellite_debug_tool/core/config.py` | 改 | DEFAULT_CONFIG 加 update section |
| `satellite_debug_tool.spec` | 改 | datas 加 release.config.json + plist 版本号 |
| `updater.spec` | **新建** | updater 可执行打包配置 |
| `requirements.txt` | 改 | 加 `py7zr`, `psutil` |
| `.github/workflows/build.yml` | **新建** | CI 自动出包 |
| `tests/test_updater_checker.py` | **新建** | 版本比较 / API 解析单测 |
| `tests/test_updater_downloader.py` | **新建** | mock HTTP 下载 + 校验单测 |
| `BUILDING.md` | 改 | 新增 CI 章节 |
| `doc/RELEASING.md` | **新建** | release SOP |

## 5. 实施顺序

| 阶段 | 内容 | 依赖 |
|------|------|------|
| P1 | 版本号单源 + `__init__.py.__version__` + spec 读取 | 无 |
| P2 | release.config.json + settings.update schema + 单测 | P1 |
| P3 | updater.checker 模块（Gitee API + 版本比较 + 单测） | P2 |
| P4 | updater.downloader 模块（分卷下载 + 重试 + 单测） | P3 |
| P5 | updater.applier 模块（合并/解压/替换/rollback + 单测） | P4 |
| P6 | updater/main.py 独立入口 + updater.spec | P5 |
| P7 | UpdateDialog UI + 启动后台静默检查 + SettingsDialog 更新区 | P3 |
| P8 | 主 spec 整合 updater 共享 _internal + 本地端到端联调 | P6+P7 |
| P9 | `.github/workflows/build.yml` 三阶段 job | P8 |
| P10 | Gitee 发版联调（手动 push v0.0.1-rc1 测试 CI 全流程） | P9 |
| P11 | BUILDING.md / RELEASING.md 文档 + 验收勾选 | 全部 |

## 6. 风险

| 风险 | 缓解 |
|------|------|
| Gitee API 限流 / 服务不稳 | settings.update.check_interval_hours 默认 24h；失败静默不打扰用户 |
| PySide6/PyQtGraph 打包体积大（>200MB）影响下载体验 | 分卷 30MB + 进度条；CI 用 UPX 压缩 PyInstaller 产物 |
| Windows 替换运行中的 .exe 失败 | updater self-relocate 到 %TEMP%，再删原 install_dir |
| macOS 替换 .app 失败 / 权限问题 | 解压到隔壁 `.staging` dir → mv old to .bak → mv staging to original |
| 用户网络断开导致下载中断 | 断点续传（HTTP Range）+ 单卷失败 3 次重试 |
| 跨大版本不兼容（如 settings schema 变） | updater 后向兼容 schema；major 版本变化时弹窗提示用户备份 |
| CI 上 PySide6 装机依赖太多导致 build 慢 | actions/cache 缓存 pip + PyInstaller build dir |
| 用户跳过版本后想恢复检查 | 设置里加"重置跳过版本"按钮 |
| 没有 Apple 签名 mac 首次打开报"已损坏" | quarantine 属性问题；BUILDING.md 教 `xattr -dr com.apple.quarantine` |

## 7. 不在 M11 范围

- 代码签名 / 公证（成本 + 周期问题）
- 增量更新（diff patch）— 全量替换够用
- 自动安装到 /Applications（用户拖进去就行）
- 多语言 release notes（暂只中文）
- 灰度发布 / A/B 分流
