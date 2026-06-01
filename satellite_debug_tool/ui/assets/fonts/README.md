# 打包字体（可选）

Mission Console 设计偏好 **IBM Plex Sans**（界面）+ **IBM Plex Mono**（数字/值/表格）。

把对应 `.ttf` / `.otf` 文件放进本目录，启动时 `styles.load_bundled_fonts()` 会自动注册，
`sans_family()` / `monospace_family()` 即命中。**留空也没关系** —— 自动回退到系统字体：

- macOS: SF Mono / Menlo / PingFang SC
- Windows: Consolas / Cascadia Code / Microsoft YaHei

## 如何添加 IBM Plex（OFL 协议，可自由分发）

从 https://github.com/IBM/plex/releases 下载，取以下文件放进本目录：

```
IBMPlexSans-Regular.ttf
IBMPlexSans-Medium.ttf
IBMPlexSans-SemiBold.ttf
IBMPlexMono-Regular.ttf
IBMPlexMono-Medium.ttf
IBMPlexMono-SemiBold.ttf
```

中文界面建议同时加 `IBMPlexSansSC`（思源体系）或保留系统 PingFang/YaHei 回退。
