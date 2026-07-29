# M16 Plan：英文国际化适配

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-07-29 |
| 状态 | 源码软件验证完成，平台验收中 |
| 当前版本 | `v1.0.1` |
| 验收文档 | `doc/M16_i18n_english_acceptance.md` |
| 开发记录 | `doc/M16_i18n_english_dev_log.md` |

## 1. 当前基线

- 项目当前没有应用级翻译器、语言配置或翻译资源。
- 启发式盘点涉及约 22 个界面模块、593 处 UI 文本调用，其中约 290 处含中文。
- `ui.active_tab`、部分图表分组标题和 Device Tab 状态判断与可见文案耦合，必须先改为稳定内部状态。
- PyInstaller 当前没有显式包含应用翻译资源和 `ui/assets`。
- 实施前全量测试：`625 passed, 4 warnings`；4 条均为既有 `datetime.utcnow()` 弃用警告。

## 2. 目标

1. 支持 `跟随系统 / 简体中文 / English`，设置确认后即时生效并持久化。
2. 以英文作为源文案和缺失翻译回退，简体中文通过 Qt TS/QM 目录提供。
3. 所有第一方窗口、状态、提示、错误、地图和升级流程均可完整显示英文。
4. 语言切换不得重建通信连接、清空运行数据或改变设备控制状态。
5. 协议字段、设备上报文本、用户自定义内容和工程数值格式保持原样。
6. 打包后的 Windows/macOS 应用必须包含并能加载翻译与地图资源。

## 3. 国际化底座

- 新增应用级 `TranslationManager`，负责语言偏好解析、`QTranslator` 生命周期和 `language_changed` 信号。
- 语言偏好固定为 `auto / zh_CN / en_US`；`SATELLITE_DEBUG_LOCALE` 可临时覆盖但不写入配置。
- `main.py` 在创建窗口前加载 Settings 和翻译器；所有窗口共享同一个 Settings 实例。
- 英文为代码源文案；`zh_CN.ts` 为翻译源，`zh_CN.qm` 为运行资源。
- 提供提取、编译和检查脚本，检查未完成翻译、空翻译、占位符和 QM 同步。
- 每个复合界面提供 `retranslate_ui()`，只重绘文案，不改变业务对象和运行状态。

## 4. 稳定状态与配置迁移

- 新增 `ui.language`。
- `ui.active_tab` 迁移为 `ui.active_tab_id = live/playback/log/device`，兼容旧中英文值。
- 时间范围、语言、主题等下拉项使用稳定 `itemData`，不能依赖显示文本判断。
- Device 参数/OTA 状态改为内部枚举，删除根据 QLabel 文本判断业务状态的代码。
- 图表默认分组标题使用稳定内部定义；用户重命名内容不翻译。
- 旧自定义分组标题若等于已知默认中英文标题，则按默认标题迁移，否则保留为用户文本。
- Updater 核心异常增加稳定错误码与上下文，UI 负责本地化，独立日志使用英文。

## 5. UI 适配范围

- Main、Live、Playback、Log、Device、Settings、Update。
- 连接、通道、图表、Dashboard、状态、事件、控制、时间范围、GNSS、地图、3D、仿真等组件。
- 按钮、Tab、表头、工具提示、空状态、确认框、文件过滤器、进度、超时、错误和状态栏消息。
- Leaflet 通过 `setLocaleTexts()` 接收 Python 注入的本地化文案。

以下内容不翻译：

- 设备下发的通道名、参数键、状态名、枚举名、事件名、单位和响应文本。
- 用户自定义图表标题、标记和导入文件内容。
- `Serial/UDP/GNSS/OTA/C/N0/RTK/SDB` 等技术标识和工程数值格式。
- 固件、Shell、协议规范和录制文件内容。

## 6. 实施顺序

1. 固化计划、验收和开发日志。
2. 实现 TranslationManager、配置迁移、TS/QM 工具和底座测试。
3. 迁移 Main、Settings 和公共控件，先验证即时切换。
4. 迁移 Live/Playback/Log、Device/OTA、GNSS/地图、仿真和升级流程。
5. 更新 PyInstaller、macOS 元数据、Windows 包内说明和中英文用户手册。
6. 完成自动化、视觉和打包验收，最后对照本计划 review。

## 7. 发布边界

- 实施阶段不自动提交、不打标签。
- 源码 pytest 通过只代表软件验证完成。
- Windows/macOS 打包启动和中英文视觉检查通过后，才可考虑发布 `v1.1.0`。

## 8. 实施结果

- 已完成应用级翻译管理、语言配置、旧配置迁移、运行时即时切换和原始设备文本边界。
- 已完成 Main、Live、Playback、Log、Device、Settings、Update 及公共组件的中英文迁移。
- 已完成 Device 稳定状态、图表默认标题、Leaflet 文案和 Updater 稳定错误码改造。
- 翻译目录包含 402 条源文案，检查结果为 402 finished、0 unfinished。
- 最终全量测试为 `640 passed, 4 warnings`；4 条均为基线已有的 `datetime.utcnow()` 弃用提示。
- macOS arm64 包已生成，并分别以 `en_US`、`zh_CN` 完成离屏启动冒烟。
- 1024×600、1280×800 的源码界面中英文截图未发现文本重叠或截断。

以下平台验收仍未关闭：

- Windows 打包启动及 125%/150% DPI 视觉检查。
- macOS 原生 OpenGL 3D 视图和真机连接状态下的即时语言切换。
- macOS 深度签名检查仍受 PySide6 内嵌 `Assistant.app` 符号链接布局影响；当前包可启动，但不能据此宣称签名或公证通过。
