# M16 英文国际化适配验收标准

> 日期：2026-07-29
>
> 状态：源码软件验证完成，平台验收中
>
> 计划：`doc/M16_i18n_english_plan.md`

## A. 国际化底座

- [x] 支持 `auto / zh_CN / en_US` 三种语言偏好。
- [x] 中文系统的 `auto` 解析为 `zh_CN`，其他系统解析为 `en_US`。
- [x] `SATELLITE_DEBUG_LOCALE` 可覆盖本次运行且不污染 settings。
- [x] 翻译器在 MainWindow 创建前安装，不出现启动语言闪烁。
- [x] `zh_CN.ts` 无 unfinished、空翻译或占位符不一致。
- [x] `zh_CN.qm` 可加载并与 TS 同步。
- [x] 缺失翻译时安全回退英文，不显示空白、不崩溃。

## B. 设置与即时切换

- [x] 设置窗口提供“跟随系统 / 简体中文 / English”。
- [x] 按确定后当前所有第一方窗口即时切换，按取消不改变语言。
- [x] 语言选择重启后仍有效。
- [x] 切换语言不改变当前 Tab、连接、Profile、Store、通道选择、图表模式、Splitter 和 OTA 状态。
- [x] `ui.active_tab` 正确迁移到稳定 `ui.active_tab_id`。
- [x] 时间范围和其他选项不再依赖可见文本执行逻辑。
- [x] 用户自定义图表标题保持原样；默认分组标题随语言切换。

## C. 界面覆盖

- [x] Main、Live、Playback、Log、Device、Settings、Update 全部完成中英文适配。
- [x] 通道、图表、Dashboard、状态、事件、控制、GNSS、3D 和仿真组件完成适配。
- [x] 按钮、Tab、表头、提示、空态、确认框、文件过滤器、进度、超时和错误均完成适配。
- [x] Device 参数/OTA 使用内部状态而非 QLabel 文本判断。
- [x] Leaflet placeholder、起点、终点和 attribution 可即时切换。
- [x] 英文模式下第一方文本无未允许的中文残留。
- [x] 中文模式主要既有文案无明显回归。

## D. 数据边界

- [x] 设备通道名、参数键、状态/枚举/事件名和响应文本保持原始内容。
- [x] 用户标记、自定义标题、导入日志和 SDB 内容保持原始内容。
- [x] 协议帧、Profile、SDB、OTA 和设备控制行为没有变化。
- [x] 工程单位、小数点和 24 小时时间格式保持稳定。
- [x] Updater UI 本地化，核心错误码稳定，独立日志为英文。

## E. 自动化与打包

- [x] 新增语言解析、目录检查、配置迁移和即时切换测试。
- [x] 新增双语言主要窗口、Device、GNSS、地图和 Updater 测试。
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q` 全绿且不新增 warning。
- [x] PyInstaller 显式包含 QM、map.html 和 Leaflet 资源。
- [ ] Windows 包在 `en_US`、`zh_CN` 下均可启动并加载翻译。
- [x] macOS 包在 `en_US`、`zh_CN` 下均可启动并加载翻译。
- [x] 1024×600、1280×800 的源码界面中英文截图无文本截断或重叠。
- [ ] Windows 125%/150% DPI 下无文本截断、重叠或控件跳动。
- [ ] macOS 原生 OpenGL 3D 视图完成中英文视觉检查。
- [x] 中文用户手册已更新，英文用户手册可独立完成安装、连接、查看和升级操作。

## 尚待外部验收

- Windows 原生打包、启动和高 DPI 视觉检查。
- macOS 原生 OpenGL 3D 视图，以及真机连接状态下的即时语言切换。
- macOS 深度签名/公证。当前 PySide6 内嵌 `Assistant.app` 的符号链接布局未通过 `codesign --verify --deep --strict`，不得将可启动冒烟等同于正式签名通过。

## 通过规则

- 自动化全绿、目录检查通过：标记“源码软件验证完成”。
- Windows/macOS 打包启动与视觉项全部通过：标记“M16 验收完成”。
