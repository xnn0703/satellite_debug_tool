# 应用品牌图标

用户于 2026-10-22 确认：石墨灰圆角底、白色卫星、蓝色通信波纹。

- `app_icon_master.png`：已确认的透明 PNG 母版，1254 × 1254，由内置 imagegen 生成。
- `app_icon.ico`：Windows EXE、updater EXE 与 Qt 运行时使用，包含 16/20/24/32/40/48/64/128/256 像素。
- `app_icon.icns`：macOS app 使用，包含标准与 Retina 表示，最大 1024 像素。

生成提示摘要：专业卫星通信与相控阵调试桌面工具；石墨灰底、白色几何卫星、克制的蓝色通信波纹；清晰轮廓、透明外围、无文字。

母版原始文件：`exec-fffed85c-7fad-47c0-b4ce-25f68256c932.png`。项目保存母版，后续生成无需访问会话目录。

在仓库根目录运行：

```bash
.venv/bin/python scripts/generate_application_icons.py
```

脚本直接从母版缩放并保留 alpha；生成 ICO、ICNS 和 `doc/screenshots/M39_application_icon_sizes.png` 明暗背景实际尺寸预览。
