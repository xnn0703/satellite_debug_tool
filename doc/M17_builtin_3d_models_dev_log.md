# M17 内置 3D 设备模型开发记录

## 2026-07-31：实施启动

### 基线

- 正式版本：`v1.1.0`，HEAD `ad76abe`。
- `v1.1.0` 正式 Windows 归档 SHA-256：
  `0bee5f8b25bfd5c5bcbbb6ea48a3d0a17b934f3ac401944ffaa11733796757b1`。
- 对正式归档执行内容检查，结果为无 `.stl`、无 `models/`。
- 正式提交测试基线：`643 passed, 4 warnings`。
- 当前工作区包含既有 INS/姿态修改，实施前测试：
  `650 passed, 4 warnings in 37.56s`。

### 模型证据

- 本地路径：
  - `~/.satellite_debug_tool/models/afd01.stl`
  - `~/.satellite_debug_tool/models/esa01.stl`
- 两个文件大小均为 8,066,184 字节。
- 两个文件 SHA-256 均为：
  `f58706d3b6fa1ac67f67fea50b8c9c4aa10b50716c8aedab8ef529290f0d5b37`。
- 当前加载器仅访问用户目录，PyInstaller 不会访问 CI 用户主目录。

### 实施清单

- [x] 创建 M17 plan、acceptance、dev log。
- [x] 加入包内模型资源。
- [x] 实现用户覆盖与包内回退。
- [x] 增加构建产物检查。
- [x] 增加专项测试并完成工作区全量验证。
- [ ] 发布并检查 `v1.1.1` 正式归档。

## 2026-07-31：实现与源码验证

### 实现

- 新增 `ui/device_model_resources.py`，校验设备类型键并固定用户覆盖、包内回退顺序。
- `AttitudeWidget` 对每个候选执行完整的 STL 读取、归一化和 MeshData 构造；损坏的用户覆盖不会阻断包内回退。
- 两个已验收模型复制到 `ui/assets/models/`，继续使用当前 AFD01/ESA01 轴向变换。
- 新增 `scripts/verify_model_assets.py`，按路径、大小和 SHA-256 验证冻结产物。
- Windows/macOS 构建脚本在主程序冻结后立即执行模型检查。
- 版本号升至 `1.1.1`。

### 验证

- 模型资源与 STL 专项测试：`10 passed in 0.29s`。
- 两个模型均由现有加载器成功解析：
  - 顶点数组 `(483966, 3)`
  - 三角面数组 `(161322, 3)`
- 当前工作区全量测试：`655 passed, 4 warnings in 33.30s`。
- 4 条 warning 均为既有 `datetime.utcnow()` 弃用提示。
- `git diff --check` 与新增 Python 文件编译检查通过。

### 待完成

- 推送 `v1.1.1`，下载正式 GitHub 归档并完成产物级检查。

## 2026-07-31：干净暂存快照验证

- 使用 `git checkout-index` 导出仅含 M17 暂存内容的独立快照，未包含工作区既有 INS/姿态修改。
- 快照全量测试：`648 passed, 4 warnings in 37.09s`。
- 翻译目录检查：402 messages、402 finished、0 unfinished。
- macOS PyInstaller 主程序冻结成功，应用版本为 `1.1.1`。
- 冻结产物模型检查通过：
  - `Contents/Resources/satellite_debug_tool/ui/assets/models/afd01.stl`
  - `Contents/Resources/satellite_debug_tool/ui/assets/models/esa01.stl`
- 两个冻结文件大小均为 8,066,184 字节，SHA-256 与源模型一致。
- PyInstaller 输出包含既有可选数据库驱动/OpenGL 平台 warning，本次未新增模型资源相关 warning。
