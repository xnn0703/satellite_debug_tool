# M16 英文国际化适配开发记录

## 2026-07-29：实施启动

### 已确认基线

- Git：`master`，HEAD `ef76340`，版本 `v1.0.1`。
- 工作区在实施前无未提交修改。
- 全量测试：`625 passed, 4 warnings in 28.16s`。
- 4 条 warning 均来自既有 `datetime.utcnow()` 弃用提示，本阶段不得新增 warning。
- 当前未发现 `QTranslator`、`QLocale`、`LanguageChange` 或翻译目录实现。

### 静态审查

- 约 22 个 UI/Updater 模块直接承载用户文案。
- 启发式统计约 593 处 UI 文本调用，其中约 290 处含中文。
- `MainWindow` 将中文 Tab 文本写入 `ui.active_tab`。
- `DeviceView` 存在读取 QLabel 文本决定参数/OTA 状态的逻辑。
- 图表默认分组标题与用户自定义标题共用同一持久化字段。
- `satellite_debug_tool.spec` 当前没有显式包含 `ui/assets` 或翻译目录。

### 实施记录

- [x] 创建 M16 plan、acceptance、dev log。
- [x] 国际化底座。
- [x] UI 文案迁移。
- [x] 地图、Updater 和打包资源。
- [x] 自动化与 macOS 软件验收。
- [ ] Windows、高 DPI、原生 OpenGL 和真机验收。

## 2026-07-29：国际化底座与界面迁移

### 底座

- 新增应用级 `TranslationManager`，支持 `auto / zh_CN / en_US`、Qt 标准翻译器和即时语言切换。
- `SATELLITE_DEBUG_LOCALE` 只覆盖当前进程，不写入 Settings。
- `main.py` 在构造窗口前安装翻译器，MainWindow 和各子视图共享同一个 Settings。
- 新增 TS/QM 更新与检查脚本，当前目录为 402 条源文案、402 finished、0 unfinished。
- 为设备字段、用户文本和原始响应增加显式 raw-text 边界，避免设备恰好上报中文界面词时被误翻译。

### 稳定业务状态

- `ui.active_tab` 迁移为 `ui.active_tab_id`，兼容既有中英文设置值。
- 时间范围、语言和其他选项使用稳定 `itemData`。
- Device 参数与 OTA 状态改为内部枚举，不再使用 QLabel 文案判断流程。
- 图表内置标题使用稳定默认键；旧中英文默认标题可迁移，用户标题保持原样。
- Updater 核心错误改为稳定错误码和英文日志，UI 按错误码生成本地化说明。

### 界面与资源

- 完成 Main、Live、Playback、Log、Device、Settings、Update 及通道、图表、状态、事件、GNSS、3D、仿真等组件迁移。
- Leaflet 新增 `setLocaleTexts()`，切换语言时更新占位、起终点和 attribution，不清空轨迹。
- PyInstaller 显式包含 TS/QM、地图资源和 macOS 本地化 plist 文案。
- Windows 构建包加入中英文快速说明和用户手册。
- 更新中文用户手册，并新增英文用户手册与中英术语表。

## 2026-07-29：验证结果

### 自动化

- 国际化专项测试：`14 passed`。
- 最终全量测试：`640 passed, 4 warnings in 33.94s`。
- 4 条 warning 与实施前一致，均为 `datetime.utcnow()` 弃用提示。
- 翻译检查：402 messages、402 finished、0 unfinished，TS/QM 同步。
- `git diff --check` 通过。

### 视觉与打包

- 1024×600、1280×800 的中英文源码截图未发现文本截断或重叠。
- macOS arm64 已生成：
  - `release/SatelliteDebugTool-macOS-arm64.zip`
  - `release/DeviceSimulator-macOS-arm64.zip`
- 冻结应用分别使用 `SATELLITE_DEBUG_LOCALE=en_US` 和 `zh_CN` 完成离屏启动冒烟。
- 离屏环境无法验证 OpenGL 3D 视图，原生视觉仍需人工验收。

### 未关闭边界

- 未在 Windows 执行打包启动及 125%/150% DPI 检查。
- 未在真机连接和 OTA 进行中执行语言切换验收；自动化只证明相关对象和状态保持不变。
- `codesign --verify --deep --strict` 未通过。失败点为 PySide6 内嵌 `Assistant.app` 的符号链接布局；应用可启动，但正式签名与公证未完成。
- M16 以 `v1.1.0` 为目标发布版本；Windows 构建与双站 Release 必须在 tag 推送后独立核验。
