# M11 — 开发日志

> 关联：[M11_plan.md](M11_plan.md) / [M11_acceptance.md](M11_acceptance.md)

实施过程中的决策变更、踩坑、阶段证据。每完成一个 P 阶段更新一段。

---

## 顺手修：设置按钮暗色主题对比度（完成 ✅）

`MainWindow._apply_theme` 给 `_settings_btn` 加 stylesheet（input_bg / text /
input_border / hover / pressed），与主题 combo 同源色板，三种主题下都可读。

---

## P1 — 版本号单源 + spec 读取（完成 ✅）

- `satellite_debug_tool/__init__.py` 加 `__version__ = "1.0.0"`（首发）+ 模块 docstring
- `satellite_debug_tool.spec` 用正则 `re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _init_src)`
  从源文件提取版本，注入 `APP_VERSION` 变量
- mac BUNDLE 的 `CFBundleShortVersionString` / `CFBundleVersion` 改用 `APP_VERSION`
- 验证：`from satellite_debug_tool import __version__` 返回 `"1.0.0"`；全套 347 个 pytest 全过

UI 状态栏 / about 显示推迟到 P7。

---

## P2 — release.config.json + settings.update schema（完成 ✅）

- 新建 `release.config.json`（repo root，CI 可覆盖）：5 个 key（gitee_owner / gitee_repo / gitee_api / github_owner / github_repo）
- 新建 `satellite_debug_tool/release_config.py`：
  - `DEFAULTS` 常量兜底
  - `load_release_config(override_path=None)` 按 4 路径查找
    （PyInstaller _MEIPASS → exe 同级 → repo root → DEFAULTS）
  - 文件不存在 / JSON 坏掉 → 静默退回 DEFAULTS（升级机制不应让程序崩溃）
- `core/config.py` DEFAULT_CONFIG 新增 `update` section：auto_check / check_interval_hours / last_check_iso / skip_version

**单测**：`tests/test_release_config.py` 7 个：
- override_path 不存在 / 合法 / 坏 JSON 三种情形
- repo root 文件真实存在 + 开发模式默认加载
- settings.update schema 默认值 + 持久化往返

全套 354 个 pytest 全过。

---

## P3 — updater.checker（Gitee API + 版本比较）（完成 ✅）

**新建模块**：
- `satellite_debug_tool/updater/__init__.py`：公共 API 导出
- `satellite_debug_tool/updater/checker.py` (~190 行)：核心实现

**核心 API**：
- `compare_versions(a, b) -> int`：返回 -1/0/+1。处理：v 前缀、数字段比较（1.2.10 > 1.2.9）、
  pre-release（"1.0.0-beta" < "1.0.0"）、段数不同（"1.0" == "1.0.0"）
- `current_platform() -> 'mac' / 'win' / 'linux'`
- `Asset(name, url, size)` / `LatestRelease(tag_name, body, html_url, assets)` dataclass
- `LatestRelease.assets_for_platform(platform)`：用正则 `-{platform}-.*\.7z\.\d+$` 过滤
  + 按名字排序保证 001/002/... 顺序
- `ReleaseChecker(owner, repo, api_base)`.`fetch_latest(opener=None)`：urllib GET +
  JSON 解析；HTTP / 网络 / JSON 错误统一抛 `UpdateCheckError`
- `opener` 参数允许测试注入 mock，不接触真实网络

**单测**：`tests/test_updater_checker.py` 28 个：
- compare_versions 15 个参数化 case（含 v 前缀、数字段、pre-release、段数差异）
- assets_for_platform 4 个（mac / win / 排序 / 无匹配）
- ReleaseChecker 8 个（URL 格式 / 成功解析 / 缺字段 / 跳过坏 asset / HTTP 404 /
  URLError / 坏 JSON / 缺 tag_name）
- 用自定义 `_FakeOpener` 类（实现 .open(req)）注入 urllib.OpenerDirector 接口

全套 382 个 pytest 全过（之前 354 + 28 = 382）。

---

## P4 — updater.downloader（分卷下载 + 重试 + 断点续传）（完成 ✅）

**新建**：`satellite_debug_tool/updater/downloader.py` (~160 行)

**API**：
- `Downloader(timeout=30, max_retries=3, chunk_size=65536, progress_interval=262144)`
- `.download_all(assets, dest_dir, on_progress=None, cancel_event=None, opener=None) -> List[Path]`
- 异常：`DownloadError`（重试耗尽/校验失败）/ `DownloadCancelled`（用户取消）

**实现要点**：
- 顺序下载（不并行）：实现简单 + Gitee 友好 + 弱网更稳
- 单分卷失败：指数退避 1s/2s/4s 重试，可配 max_retries
- 断点续传：dest 已存在 → 发 `Range: bytes=N-` header；服务器 206 接续 / 200 重头
- Content-Length 校验：asset.size > 0 时与实际文件大小比对，不一致视为失败重试
- 进度回调：累计达 progress_interval 字节才调一次，避免 GUI 信号过密；收尾必触发
- 取消：threading.Event；download_all 入口 + 每分卷重试前 + read 循环每 chunk 检查

**单测**：`tests/test_updater_downloader.py` 13 个：
- 基础（单卷 / 多卷顺序 / 空列表 / 目录不存在）
- 重试（1 次失败后成功 / 耗尽抛错）
- size 校验（不匹配触发重试 / size=0 不校验）
- 进度（被调用 / total 是全部 assets 之和）
- 取消（事件预设导致 DownloadCancelled）
- 断点续传（Range header 正确 / 服务器 200 全量重写）

mock 用自定义 `_FakeResponse + _FakeOpener`（实现 .read/.close/.status 接口），
不接触真实网络。

全套 395 个 pytest 全过。

---

## P5 — updater.applier（合并/解压/替换/rollback）（完成 ✅）

**依赖**：requirements.txt 新增 `py7zr>=0.20` + `psutil>=5.9`（updater 阶段才用）

**新建**：`satellite_debug_tool/updater/applier.py` (~155 行)

**API**：
- `Applier(on_progress=None)`：构造可注入进度回调 `(stage_text, pct_0_1)`
- `.merge_volumes(volumes, output, chunk_size=1MB) -> Path`：分卷顺序拼成单文件 + 实时进度
- `.verify_7z_magic(path) -> bool`：staticmethod，校验头 6 字节 `377abcaf271c`
- `.extract_to(archive, staging) -> Path`：py7zr 解压；先 verify_magic 防分卷错乱
- `.swap_install_dir(install_dir, ext_root, backup_suffix=".bak") -> ApplyResult`：
  - 自动识别 ext_root/{install_dir.name} 或 ext_root 自身两种解压结构
  - 旧 install_dir → `{name}.bak.{YYYYMMDD_HHMMSS}`（时间戳避免覆盖历史 .bak）
  - 替换失败时 rollback（backup_dir.rename(install_dir)）
- `.apply(volumes, install_dir, workdir) -> ApplyResult`：一站式 merge → verify → extract → swap

**ApplyResult**：dataclass，含 install_dir + backup_dir 路径，供调用方决定保留/清理备份

**单测**：`tests/test_updater_applier.py` 15 个：
- merge_volumes 4 个（顺序拼接 / 缺卷 / 空列表 / 进度回调）
- verify_7z_magic 3 个（合法 / 随机字节 / 缺文件）
- extract_to 3 个（真实 7z / 坏 magic / 缺文件）
- swap 4 个（成功 + 备份 / 首次安装 / ext_root 含 app 子目录两种结构 / 缺源）
- 一站式 apply 1 个（造 7z → 拆分 → merge → extract → swap → 验证内容）

通过 helper `_make_7z_with_app` + `_split_file` 在 tmp 里现造 7z 测试输入，避免依赖外部归档。

全套 410 个 pytest 全过。

---

## P6 — updater/main.py + updater.spec（完成 ✅）

**新建**：`satellite_debug_tool/updater/main.py` (~180 行)

**职责**：作为独立可执行（PyInstaller 单独打 `dist/updater(.exe)`），由主程序触发，
完成"等主进程退出 → self-relocate → apply → restart"全流程。

**关键函数**：
- `wait_for_pid(pid, timeout=30)` — 用 psutil；不可用时盲等 3s
- `self_relocate_and_relaunch(install_dir, argv)` — 若 updater 自身在 install_dir 内（onedir 共享场景），
  把 updater 整个 onedir 复制到 OS 临时目录，加 `--no-relocate` 重启自己，避免被 swap 流程删掉
- `restart_app(install_dir, restart_cmd=None)` — mac `open .app` / win 找同名 .exe / 显式 cmd
- `main(argv)` — CLI 入口，退出码 0/2/3 区分成功/超时/替换失败

**CLI**：
```
updater --pid <PID> --install-dir <PATH> --workdir <PATH> \
        --volumes <P1> <P2> ... [--restart-cmd CMD] [--log PATH] [--wait-timeout 30]
```

**新建**：`updater.spec`（PyInstaller，独立打包）
- 仅 `py7zr` + `psutil` + 标准库；excludes PySide6 / pyqtgraph / numpy / OpenGL
- console=True（命令行工具）
- 预期产物 15-25MB（vs 主程序 250MB+）

**单测**：`tests/test_updater_main.py` 7 个
- CLI --help 返回 0
- wait_for_pid 对不存在的 PID 立即返回 True
- restart_app 三个平台分支（mac open / win 找 .exe / 显式 cmd / 找不到只 warn）
- 端到端 main()：造 7z 分卷 → main() → 验证 install_dir 被替换、backup 存在

monkeypatch 拦截 subprocess.Popen + wait_for_pid + self_relocate 避免真启动进程。

全套 417 个 pytest 全过。

---

## P7 — UpdateDialog + 后台静默检查 + SettingsDialog 更新区（完成 ✅）

**新建**：`satellite_debug_tool/ui/update_dialog.py` (~430 行)

**UpdateDialog**：QStackedWidget 6 页
- CHECKING / UP_TO_DATE / NEW_FOUND（含 release notes + 立即更新/跳过/稍后）/
  DOWNLOADING（进度条 + 取消）/ LAUNCHING / ERROR（含重试）
- `_CheckWorker` 在 QThread 后台跑 `ReleaseChecker.fetch_latest`
- `_DownloadWorker` 后台跑 `Downloader.download_all`，threading.Event 取消
- `_detect_install_dir()` / `_detect_updater_exe()`：基于 PyInstaller 约定找路径
  （mac .app / win onedir）；开发模式返回 None
- 下载完成后 `subprocess.Popen` 启动 updater，DETACHED_PROCESS (win) / start_new_session (posix)，
  接着 `QApplication.quit() + sys.exit(0)` 让主程序退出

**模块级辅助**：
- `should_check_in_background(settings) -> bool`：检查 settings.update.auto_check +
  last_check_iso + interval；`SATELLITE_NO_UPDATE_CHECK=1` 一键禁用（测试/CI 用）
- `silent_background_check(settings, parent, on_new_version)`：非阻塞，发现新版才
  回调 on_new_version；用户已 skip 该 tag 时不打扰；失败静默

**MainWindow 集成**：
- 工具栏新增 `🔄 检查更新` 按钮（位置：⚙ 设置 之前），主题样式同 chrome 按钮
- statusBar permanent widget 显示 `v{__version__}`（M11 A1.2 ✓）
- init 末尾 `QTimer.singleShot(2000, _kick_silent_update_check)` 启动后台静默检查
- 后台发现新版 → statusBar.showMessage(...) 提示"点工具栏检查更新"，用户主动点开 UpdateDialog

**SettingsDialog 新增"自动更新"区**（QFrame 分隔线后）：
- ☑ 启动时后台检查更新（默认勾选）
- 检查间隔 QSpinBox 1~168 小时
- 已跳过版本显示 + 重置按钮
- accept 时把 auto_check / interval 写回 settings

**测试支持**：
- `tests/conftest.py` 设 `SATELLITE_NO_UPDATE_CHECK=1` + `QT_QPA_PLATFORM=offscreen`，
  确保测试永不发起后台升级检查（即便 MainWindow 构造会启动 timer）

**单测**：`tests/test_update_dialog.py` 10 个
- should_check_in_background 6 个（env var / auto_check=False / 首次 / 间隔内 / 间隔外 / 坏 iso）
- UpdateDialog 构造（不 auto_start）1 个
- SettingsDialog 更新区 3 个（初始值加载 / accept 写回 / 重置跳过版本）

全套 427 个 pytest 全过。

---

## P8 — 主 spec 整合 + 本地端到端联调（完成 ✅）

**改动**：
- `satellite_debug_tool.spec`：`datas` 加 `(release.config.json, ".")`，确保
  打包后 release_config 能从 _MEIPASS 读到
- `scripts/build_macos.sh`：依赖自检加 `py7zr, psutil`；
  build 主程序 + build updater + build simulator；
  把 `dist/updater/updater` + `dist/updater/_internal/` 复制进 `.app/Contents/MacOS/`
- `scripts/build_windows.bat`：同上，把 `dist/updater/updater.exe` + `_internal/`
  合并进 `dist/SatelliteDebugTool/`

**本地联调说明**：
- 完整端到端联调（dev → build → install → trigger update）需要打 PyInstaller 包 + 实际 Gitee 发版，
  推到 P10
- 单元 + 集成级别的更新逻辑（applier 一站式 + updater main 退出码）已在 P5/P6 测过

---

## P9 — `.github/workflows/build.yml` CI（完成 ✅）

**5 个 job**：

1. **prepare-release**（ubuntu, 仅 tag/dispatch 触发）
   - 解析 tag（push tag 或 workflow_dispatch input）
   - 删除 Gitee 上已有同 tag release
   - 调 Gitee API POST 建空 release → 输出 `gitee_release_id`

2. **build-windows**（windows-latest）
   - `scripts\build_windows.bat`（主 + updater + simulator）
   - layout 到 `release/satellite_debug_tool-win-{tag}/`
   - 系统自带 `C:\Program Files\7-Zip\7z.exe` 分卷 30MB（`-mf=off` 关 BCJ2 保 py7zr 兼容）
   - 上传到 Gitee（3 次重试）+ GitHub Release

3. **build-macos**（macos-14 arm64）
   - `brew install p7zip` + `scripts/build_macos.sh`
   - 同 windows job 流程；upload max-time 3600s + 5 次重试（mac 网络更慢）

4. **finalize-release**（ubuntu，build-* 全过才跑）
   - 拉 Gitee release assets 列表，python 拼 markdown body（mac/win 分组列下载链接 + 大小）
   - PATCH Gitee release 写 body
   - 清理 Gitee 旧 release（保留最近 3 个）

5. **ci-only-build**（master/main 推送时）
   - 仅本地构建验证 + 上传 GH artifact（retention 7d）
   - 不触发 release 流程

**关键 secret**：`GITEE_TOKEN`（用户已配）
**关键 var**：env.GITEE_OWNER / GITEE_REPO / GITHUB_OWNER / GITHUB_REPO（与 release.config.json 一致）

YAML 通过 `yaml.safe_load` 验证语法 OK。

---

## P10 — Gitee 发版联调（待用户执行）

**说明**：实际 push tag → CI 跑 → Gitee 出包 → 老版本升级到新版本，这个端到端
联调需要在用户的真实 git 仓库 + GITEE_TOKEN secret 下执行，沙箱里无法做。

**用户操作步骤**（详见 `doc/RELEASING.md`）：

```bash
# 验证 CI 全流程
git tag v0.0.1-rc1
git push origin v0.0.1-rc1
# 看 GitHub Actions：prepare-release → build-* → finalize-release
# 完成后查 https://gitee.com/soft-hertz/satellite_debug_tool_release/releases

# 然后再发一个正式版（如 v0.0.1）测升级链路
# 安装 v0.0.1-rc1 → 启动 → 工具栏检查更新 → 应弹"发现新版 v0.0.1"
# 点立即更新 → 下载 → 应用 → 自动重启 v0.0.1
```

---

## P11 — 文档（完成 ✅）

**改动**：
- `BUILDING.md` 重写"GitHub Actions"章节：5 job 表 + 标准发版流程 + 失败排查 + 仅 CI 构建 + 手动触发
- `doc/RELEASING.md` 新建（完整 SOP）：一图流 / 前置检查 / 标准流程 / 删 tag 重发 /
  预发版本 / 紧急修复 / 4 个 FAQ

待用户验收：跑 P10 后按 `doc/M11_acceptance.md` 逐条勾选。

---

# M11 汇总

11 个 P 阶段，无返工，全程 427 测试零退化（70 个新测试覆盖 updater 全链路）。

| 新增模块 | 行数 | 用途 |
|---------|------|------|
| `release_config.py` | ~55 | release.config.json 加载兜底 |
| `updater/checker.py` | ~190 | Gitee API + 版本比较 |
| `updater/downloader.py` | ~160 | 分卷下载 + 重试 + 断点续传 |
| `updater/applier.py` | ~155 | 合并 + 解压 + 替换 + rollback |
| `updater/main.py` | ~180 | updater 独立可执行 |
| `ui/update_dialog.py` | ~430 | 检查/下载/应用 GUI |
| `release.config.json` | 5 行 | 发版仓库配置 |
| `updater.spec` | 整文件 | updater PyInstaller spec |
| `.github/workflows/build.yml` | 整文件 | CI 5 job 完整发版流程 |
| `doc/RELEASING.md` | 整文件 | 发版 SOP |

| 测试文件 | 用例数 |
|----------|--------|
| test_release_config.py | 7 |
| test_updater_checker.py | 28 |
| test_updater_downloader.py | 13 |
| test_updater_applier.py | 15 |
| test_updater_main.py | 7 |
| test_update_dialog.py | 10 |

修改：8 个现有文件（main_window / settings_dialog / config / requirements.txt /
satellite_debug_tool.spec / build_macos.sh / build_windows.bat / BUILDING.md）。
