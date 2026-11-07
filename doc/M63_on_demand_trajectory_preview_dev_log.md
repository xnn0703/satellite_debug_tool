# M63 按需轨迹预览开发记录

## 实施记录

- 删除组合连续摇摆区域中的常驻 `PlotWidget`，参数区域下方直接排列三个操作按钮。
- 新增 `TrajectoryPreviewDialog`，只在用户点击“预览并校验”且预检成功后显示。
- 预览窗口复用 Roll/Pitch/Yaw 主题色，支持当前主题切换，最多绘制 1200 个目标点。
- 将 `_validate_trajectory()` 设为预览和启动共用的唯一预检入口。
- “开始组合摇摆”完成预检后直接提交，不创建或显示预览窗口。
- 新增“轨迹预览 / Trajectory preview”翻译，翻译目录共 1507 项。

## 验收对照

- 定向测试：`PYTHONPATH=. pytest satellite_debug_tool/tests/test_fixture_debug_workspace.py -q`，25 passed。
- 新增回归证明：主界面不创建常驻预览图、预检失败不创建窗口、预检成功按需显示三轴曲线、长轨迹限制 1200 点、直接启动不显示窗口。
- 翻译检查：`python3 scripts/update_translations.py check`，1507 messages，全部完成。
- 语法检查：`python3 -m py_compile satellite_debug_tool/ui/fixture_debug_workspace.py satellite_debug_tool/tests/test_fixture_debug_workspace.py`，通过。
- 全量测试：`PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q`，2280 passed in 326.66s。
- `git diff --check`：通过。

