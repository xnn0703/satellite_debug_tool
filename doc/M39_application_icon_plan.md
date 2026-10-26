# M39 应用图标接入计划

用户已确认石墨灰底、白色卫星、蓝色通信波纹的图标设计（2026-10-22，“可以”）。本次按确认图稿完成桌面图标接入。

## 范围

1. 保存已确认的透明 PNG 母版；从同一母版生成 Windows 多尺寸 ICO、macOS ICNS 和实际尺寸预览。
2. 主程序 EXE、macOS app、updater EXE 使用同一图标；QApplication 和顶栏品牌标记使用同一来源。
3. 源码启动、PyInstaller 数据目录与 Python 包安装均包含运行时图标。
4. 增加格式、尺寸和 Qt 加载检查；对照[验收标准](M39_application_icon_acceptance.md)记录[开发结果](M39_application_icon_dev_log.md)。

## 实施顺序

先固定母版和验收，再生成资源、连接打包及 Qt 入口，最后完成自动检查与本机可完成的格式验证。

## 边界

本次交付源码和资源。GitHub 已发布的历史安装包保留原有内容；新图标随后续打包生效。Windows 原生 EXE/任务栏和 macOS Finder/Dock 的最终显示由对应平台发行包复核。提交、发版按用户后续指令执行。
