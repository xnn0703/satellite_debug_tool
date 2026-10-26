# M39 应用图标开发记录

配套：[计划](M39_application_icon_plan.md)、[验收标准](M39_application_icon_acceptance.md)。

## 2026-10-22：设计确认与实施准备

- 用户确认首版图标：石墨灰圆角底、白色卫星、蓝色通信波纹。
- 图稿由内置 imagegen 生成，采用透明背景；提示摘要：专业卫星通信调试桌面工具、简洁几何卫星、石墨灰与蓝色、清晰轮廓。
- 当前主程序 EXE/BUNDLE 和 updater EXE 的 spec 均为 `icon=None`；应用启动入口尚未设置窗口图标，顶栏使用通用线条卫星图标。
- 保留既有 M37、M38 工作区改动，本次增量围绕应用品牌资源与打包入口。

## 实施与验证

当前结果：资源、打包配置和 Qt 接入完成；48 项相关测试及 macOS 原生 Qt 显示检查通过。

### 单一来源与接入

- `ui/assets/icons/app_icon_master.png` 保存确认图稿，1254 × 1254，透明外围。
- `scripts/generate_application_icons.py` 直接从母版生成各尺寸 PNG 帧，打包为 ICO / ICNS，输出明暗背景实际尺寸预览；仅依赖项目已有 PySide6。
- `satellite_debug_tool.spec` 的 Windows EXE 引用 ICO，macOS BUNDLE 引用 ICNS；既有 assets 数据收集覆盖图标目录。
- `updater.spec` 的 Windows EXE 引用同一 ICO。
- `main.py` 在创建窗口前设置 QApplication 全局图标；`ui/application_icon.py` 按模块相对路径解析运行时资源，保持源码和冻结包一致。
- 顶栏移除原有通用卫星和蓝色方块绘制，改用同一品牌图稿；按设备像素比生成 pixmap，本机 Retina 为 52 × 52 物理像素 / 26 × 26 逻辑像素。
- Python package-data 显式包含 PNG / ICO / ICNS，`BUILDING.md` 和 `AGENTS.md` 同步入口。

### 对照验收

| 验收项 | 证据与结论 |
|---|---|
| 设计与透明度 | 母版已保存；ICO 各帧、ICNS 各帧均可解码，四角透明、中心有效 |
| Windows 资源 | ICO 9 帧：16/20/24/32/40/48/64/128/256；Qt 解码通过 |
| macOS 资源 | 标准及 Retina 11 帧；系统 `iconutil --convert iconset` 成功解包 |
| Qt 启动回归 | `test_startup_sets_icon_inherited_by_windows` 验证全局图标在窗口创建前设置；旧入口缺少全局设置，新入口验证通过 |
| 打包与安装 | AST 求值检查 win32 EXE 与 darwin BUNDLE 路径存在；package-data 模式覆盖三份资源 |
| 相邻回归 | `test_application_icon.py`、`test_customer_main_window_integration.py`、`test_view_lifecycle.py`、`test_ui_color_contract.py`、`test_styles.py`：**48 passed in 5.68s** |
| 原生 Qt | macOS `cocoa` 实际创建窗口；dark/light/dark_hc 均可加载品牌标记，DPR 2.0；明暗主题截图保存 |
| 静态检查 | compileall、`git diff --check` 通过 |

### 预览证据

- `doc/screenshots/M39_application_icon_sizes.png`：16/24/32/48/64/128 像素在明暗背景下的实际尺寸。
- `doc/screenshots/M39_application_icon_dark.png`、`M39_application_icon_light.png`：本机原生 Qt 窗口及顶栏显示。
- 16 像素下卫星轮廓可见，通信波纹细节随缩小减弱；保持用户确认图稿，后续若需要单独优化小尺寸表示，应另行确认该视觉调整。

### 待平台发行包复核

本次完成资源、配置和运行时接入，未执行完整 Windows/macOS 发布打包。Windows 原生 EXE、任务栏及 macOS 发布 app 的 Finder/Dock 显示留待对应平台发行包验证。GitHub 已发布历史包保持原样。本次按用户指令保留本地改动，尚未提交或发版。
