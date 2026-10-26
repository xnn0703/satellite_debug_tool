# M42 用户版与运维版开发记录

状态：2026-10-23 用户版 Shell 修订、总览公共面板修复及双版 macOS 验证完成。Windows 冻结安装、更新及真实设备动作待验。当前版本仍为 1.7.0，本地验证产物已生成；Git 提交、推送和发布由用户后续明确指令执行。

配套：[计划](M42_user_operations_editions_plan.md)、[验收](M42_user_operations_editions_acceptance.md)、[构建说明](../BUILDING.md)、[发行 SOP](RELEASING.md)。

## 2026-10-22 初版范围与验证（Shell 修订前）

用户确认保留独立客户加密固件 OTA。user 固定四页：总览、RF 控制、回放、固件升级；ops 保留全量工作区、外接电源、网络测试、夹具、Shell/YMODEM、工程、试产及报告。客户设备仍按 IP/端口显式添加。先前 M38–M41 的配色、图标、中性名称及下拉框修复保持完整。

## 装配与领域实现

1. `edition.py` 提供固定产物身份；`main_user.py` 装配 `UserWindow`，`main.py` 装配运维 `MainWindow`。`bootstrap.py` 和 `application_window_actions.py` 共用启动、更新、设备目录操作及受控退出。源代码启动默认 ops，冻结产物验证固定入口与包身份一致。
2. 客户 bundle 按 edition 创建必要 owner；user 的电源、iperf、夹具、Shell、安装标定及试产执行 owner 均为零。工作区直接操作入口、CLI、工程呈现入口都检查 user 能力边界。旧 `ui.edition`、页面配置无法改变固定类型。
3. 移除公共装配路径的运维 eager import，包聚合改为显式 lazy export，工程 Live 呈现按需导入。会话、协议、录制/回放、Product Service 和设备事务继续共用原领域 owner。
4. 加密 OTA 使用共享 `customer_ota_access.py`、原 `OtaController` 与绑定操作网关；user 使用小型 `CustomerOtaAdapter`，ops DeviceView 调用同一接口。安装标定控件及 controller 只由 ops 创建。认证、会话绑定、租约、OTA 回读与物理证据语义保持现有合同。
5. 界面检查发现新共享 OTA 状态调用了错误翻译 context，中文页出现 `Idle`；增加中文运行回归后改用词条原有默认 context，最终显示“空闲”。

## 设置与更新合同

- ops 设置路径保持 `~/.satellite_debug_tool/`，user 使用其 `user/` 子目录。设置、日志、更新暂存及事务状态分别保存；user 首次设备目录为空。共享模型覆盖缓存等既有纯资源路径保持原约定。
- Windows user 安装目录 `SatelliteTerminalToolUser`，ops `SatelliteDebugTool`；两者 EXE 均沿用 `SatelliteDebugTool.exe` 与同层 updater 合同。macOS app 名与 bundle identifier 分开。
- Windows 正式资产分别为 `satellite_terminal_tool-user-windows-v<version>.7z` 与 `satellite_debug_tool-win-v<version>.7z`。用户名称避开旧全量更新器的平台正则。新客户端按 edition/platform/version 精确选择唯一资产；updater 在覆盖前验证已安装身份和下载载荷身份。
- 历史无身份 marker 包只在 ops 目标版本 `<=1.7.0` 内接受；更高版本与 user 均要求完整身份。平台必须为 Windows。macOS 仍手动分发 zip。

## 构建与裁剪

`build_editions.py` 统一调用两个 spec、验证模型、身份与 TOC，嵌入 updater，并在 macOS 签名后归档。`edition_policy.py` 为用户 UI 白名单及运维排除项提供单一来源；用户版移除运维执行模块、报告依赖、Leaflet/map 与 QtWebEngine，保留 Qt/OpenGL/numpy/pyqtgraph 及认证 OTA 依赖。历史 SDB 读取所需纯数据模型保留。

Windows CI 已配置 user/ops 矩阵，校验各自 7z 后上传 artifact；唯一 publish job 等待两版成功再共同发布。本次未触发远端 CI。

macOS arm64 最终实测：

| 版本 | zip | app 文件有效载荷 | 首个窗口 | 首窗口 RSS | 首窗口线程 |
|---|---|---|---|---|---|
| user | 83.23 MiB | 196.38 MiB | 1.262 s | 272.86 MiB | 16 |
| ops | 265.94 MiB | 704.25 MiB | 1.582 s | 289.92 MiB | 16 |

zip 缩小约 68.7%，app 有效载荷缩小约 72.1%。文件有效载荷按非符号链接文件大小累计；不等同磁盘占用。启动及内存为本机单次烟测观测值，测试期间还有软件验证任务，不能视作性能基准。烟测使用临时设置与未连接的回环 endpoint；两版初始线程数均为 16，user 的运维模块清单为空。逐页遍历和加载模型后的 RSS 记录在 JSON 中，不能当作空闲启动内存。

## 验证结果

- 最终全量：`PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q` → **1622 passed in 237.79s**。
- 新增 edition 回归覆盖：四页与执行 owner 边界、直接入口拒绝、旧配置、独立设置、唯一资产、旧正则、双向交叉载荷拒绝、已安装身份、错误平台及缺失身份。
- 较早一轮真实 iperf UDP 回环出现 `SERVER ERROR - unable to start stream listener: Address already in use`，原始 JSON 证实服务端端口占用。未改 iperf 生产实现或放宽断言；隔离相关文件后 36 项通过，随后两轮全量均 1622 项通过（195.92 s 与最终 237.79 s）。保留该失败证据。
- 翻译 update/check 完成：1420 messages，全部 finished，QM 同步；最后的默认 context 修正复用已有词条。
- 两份真实冻结 app 均通过模块/模型审计、页面遍历、STL 载入、正常退出；user 遍历四页面后运维执行模块加载数为零，ops 构造工程各 Tab 与试产工作区。未激活试产发现及硬件动作。
- 两份原 app 签名和 zip 解压后签名均通过 `codesign --verify --deep --strict`；两个 zip CRC 校验通过，嵌入 updater 的 `--help` 可运行。
- 源码客户 UI 的两语言 × 三主题 × 四页面检查通过；中文固件升级最终截图再次确认“空闲”。截图见 `doc/screenshots/M42_user_*.png`。
- workflow 经 Ruby Psych 解析成功，macOS shell 脚本通过 `bash -n`，最终差异空白检查通过。

## 验收边界及证据

Windows EXE 构建、冻结界面、旧安装同版更新、真实设备连接/RF/OTA、现场夹具及电源动作继续待验。本地 macOS 构建与软件回归证明装配、资源和软件合同，设备动作成功以既有回读/现场证据为准。

完整测试/构建日志、两版度量与烟测 JSON、SHA-256 清单位于：
`/Users/xmac/.codex/visualizations/2026/10/22/01a1628b-5c77-74b0-80a3-d225f0c8c452/m42-check/`。
分发压缩包及哈希清单位于仓库 `release/`，不纳入源码提交。

## 2026-10-23：用户版保留 Shell 修订

用户明确要求用户版保留 Shell，计划与验收已先行修订。以上 2026-10-22 的裁剪结果与体积为初版历史证据；本轮恢复 TCP、串口、YMODEM 入口及依赖，按需装配，其他运维能力保持原版矩阵。实施及验证结果如下。


### 修订实施

- CustomerWorkspace 的 Shell 入口供两版使用；首次打开懒创建，重复打开复用，关闭及应用退出继续走原收尾路径。UserWindow 传入现有客户会话目录，YMODEM 与原客户事务网关共享设备事实和租约。
- 用户 UI 白名单保留两个 Shell UI 模块；spec 和审计器将 TCP/串口/YMODEM 转为两版共享模块。`USER_SHELL_MODULES` 提供用户包必备模块清单，最终 TOC 同时检查 Shell 完整性及运维模块排除。
- Workspace 按 Settings.config_directory 将 Shell 会话根目录传给原 Controller/Worker/Recorder；user 保存到 `user/tcp_shell_sessions/`，ops 保持原路径。Shell 文本日志的用户自选目录继续由本版设置保存。
- 新增/修订回归覆盖：原 user 按钮缺失的稳定复现、首次懒创建、弹窗复用/重开、TCP/串口控件、应用退出、会话目录绑定、真实回环 TCP 命令及证据落盘、用户包 Shell 缺失与运维模块混入拒绝。

### 修订验证

- 修改前用户版回归稳定失败：Shell 按钮为 None。恢复公共入口后通过，其他运维入口继续拒绝。
- 最终全量 **1624 passed in 222.48s (0:03:42)**；翻译 **1420 messages**、全部 finished。未增加 UI 源文案，TS/QM 校验同步。
- 用户/运维真实 macOS 冻结包均通过 Shell 弹窗打开、复用和关闭、页面遍历、模型加载、应用正常退出；user 打开 Shell 后运维执行模块清单仍为空。
- Shell 两语言 × 三主题源码窗口检查通过，截图为 `doc/screenshots/M42_user_shell_<locale>_<theme>.png`；四个客户页面截图已刷新，侧栏包含 Shell。
- 两包模型、身份、TOC（user 10996 项、ops 20201 项）审计通过。两个最终 zip CRC、解压 app 的严格签名验证均通过。归档验证曾提前读取仍在写入的 ops zip，构建完成后重新验证最终归档通过，未修改归档内容。
- 最终 user zip **87,336,819 B（83.29 MiB）**，ops zip **278,863,819 B（265.95 MiB）**。user 增加约 62 KiB 的 Shell 内容，仍较全量 zip 小约 68.7%。
- 原生烟测单次首窗口：user 0.995 s / RSS 280.52 MiB，ops 1.322 s / RSS 295.78 MiB；初始线程均 16。逐页与 Shell 后的内存及模块明细见 JSON；以上为本机观测值，测试并行运行，不作为性能基准。
- 工作流 YAML 解析、shell 语法及 `git diff --check` 通过。版本仍 1.7.0；未提交、推送、打 tag 或发布。

本轮完整日志、JSON、归档 SHA-256 与验证记录位于 `/Users/xmac/.codex/visualizations/2026/10/22/01a1628b-5c77-74b0-80a3-d225f0c8c452/m42-shell-check/`；`release/M42-manifest.json` 对应本轮最终两包。Windows CI/EXE 安装和设备侧 Shell/YMODEM/固件生效继续待验；回环测试及本地包验证不替代这些门禁。


## 2026-10-23 总览公共面板缺失修复

用户进一步指出 SNR 曲线和下方模块温度缺失。根因是拆版引入的布局缩进：公共面板 addWidget 和初始密度设置属于外接电源分支。计划及验收已先行补齐，实施按公共布局/可选外设边界修正。


### 公共面板修复与证据

- 根因修复：只将 SNR、模块温度面板的两个 `addWidget` 及初始密度设置移出外接电源条件；条件继续只控制外接电源自身锚点。原 SNR Store/五分钟窗口和模块温度 Store/历史弹窗继续复用。
- 修改前新增四组合回归：外接电源 False/True × 连接 False/True，结果 **2 failed, 2 passed**；两个用户版条件均稳定复现面板 layout index=-1。修复后针对总览及版本装配 **39 passed**。
- 新回归检查两个面板实际加入 root layout、父子关系、可见状态、页面几何范围、三模块锚点和可选电源锚点、SNR 样本绘入曲线。真实 user 子进程也检查两面板与三模块完整性。
- 冻结烟测新增公共面板强制检查；两版原生运行均得到 `overview_panels: PASS`，同时 Shell、页面遍历、模型加载和正常退出 PASS。user 打开公共页面与 Shell 后运维执行模块清单仍为空。
- 最终全量 **1628 passed in 215.43s (0:03:35)**；翻译 **1420 messages**、全部 finished，差异空白检查通过。
- 原生窗口截图检查：两语言 × 三主题 × 三种高度（1440×640、1440×800、1440×900），SNR 和三模块温度栏均处于窗口可见范围。截图 `doc/screenshots/M42_user_panels_<locale>_<theme>.png`，四页面截图同步刷新。额外探索的 1024 宽及部分 1280 英文窗口存在既有横向最小宽度裁切，未将窄窗范围计入此项 PASS；本次修复未改变横向响应布局。
- 双版重新构建、模型/模块/身份审计、两个 zip CRC 与解压 app 严格签名验证 PASS。最终 user zip 87,335,757 B，ops zip 278,862,490 B；最终 SHA-256 已更新到 `release/M42-manifest.json`。
- 证据目录：`/Users/xmac/.codex/visualizations/2026/10/22/01a1628b-5c77-74b0-80a3-d225f0c8c452/m42-panels-check/`。Windows 构建与实际设备遥测/温度采样保持待验；本轮未提交、推送或发布。
