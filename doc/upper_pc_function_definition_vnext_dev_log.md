# 上位机功能定义 vNext 开发记录

## 2026-06-30

### 输入

用户确认：“按计划开发吧，有问题直接询问我”。

### 实施内容

- 新增 `upper_pc_function_definition_vnext_plan.md`，记录本次文档开发范围和验收标准。
- 新增 `upper_pc_function_definition_vnext_acceptance.md`，聚合文档验收项。
- 新增 `upper_pc_function_definition_vnext.md`，作为当前上位机产品功能定义和长期优化路线。
- 更新入口文档：
  - `README.md`
  - `AGENTS.md`
  - `CLAUDE.md`
  - `doc/user_manual.md`
  - `doc/optimization_plan.md`
  - `doc/UI_DESIGN_REVIEW.md`

### 关键决策

- `optimization_plan.md` 保留为 M1-M6 历史蓝图，不强行重写。
- 当前功能定义以现有代码事实为准：四 Tab、SDB v2、Device/OTA、地图、自动更新、仿真入口。
- 对“完全 profile 驱动”做更精确定义：现有版本仍有 GPS 名称、trace mode、3D 自动绑定等约定，M13 需要用 profile 语义字段或协议 v2.x 收口。
- 本次不改运行代码，不新增测试。

### 验证

- `rg -n "已知 baseline 失败|payload 513B|python -m satellite_debug_tool\\b|pip install -r requirements.txt|三 Tab|三个 Tab|不控制曲线|只控制 label|SimulationEngine 生成|\\.csv.*支持|数据录制与回放（.*csv" README.md AGENTS.md CLAUDE.md doc/user_manual.md doc/upper_pc_function_definition_vnext*.md satellite_debug_tool/ui/live_view.py`
  - 结果：无命中。
- `git diff --check`
  - 结果：通过。
- `PYTHONPATH=. pytest satellite_debug_tool/tests`
  - 结果：488 passed, 4 warnings in 19.17s。

### 遗留

- 历史计划/开发日志中仍保留 M7/M10 当时的“三 Tab”“字号”等表述，作为里程碑追溯不在本次修改范围。
- `simulation_plan.md` / `simulation_design.md` 已有废弃标注，当前事实以 `simulation_delivery.md` 和 vNext 功能定义为准。
