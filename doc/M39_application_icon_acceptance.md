# M39 应用图标验收标准

配套：[计划](M39_application_icon_plan.md)、[开发记录](M39_application_icon_dev_log.md)。

| 项目 | 验收标准 |
|---|---|
| 设计来源 | 使用用户确认的卫星图稿；保存透明 PNG 母版和生成说明 |
| Windows | ICO 包含 16/20/24/32/40/48/64/128/256 像素图像，各尺寸可解码；主程序及 updater spec 引用该 ICO |
| macOS | ICNS 包含标准与 Retina 尺寸；本机 iconutil 能解包；BUNDLE 引用该 ICNS |
| Qt | 应用启动设置全局图标，顶栏使用同一资源；Qt 可读取各帧，窗口继承应用图标 |
| 分发 | 主程序 spec 收集资源；Python package-data 收集 PNG/ICO/ICNS |
| 预览 | 保存实际 16/24/32/48/64/128 像素在明暗背景下的预览 |
| 回归 | 图标相关测试通过；相邻主题及主窗口测试通过；差异空白检查通过 |
| 平台显示 | Windows 原生 EXE/任务栏和 macOS 发布 app 的 Finder/Dock 在平台包验证时确认 |
