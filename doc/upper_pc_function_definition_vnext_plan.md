# 上位机功能定义 vNext 开发计划

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-30 |
| 状态 | 已按用户确认执行 |
| 目标产物 | `doc/upper_pc_function_definition_vnext.md` |
| 验收文档 | `doc/upper_pc_function_definition_vnext_acceptance.md` |
| 开发记录 | `doc/upper_pc_function_definition_vnext_dev_log.md` |

## 背景

`doc/optimization_plan.md` 是 M1-M6 阶段的历史蓝图，核心方向仍有效：协议 v2、profile 驱动、状态/事件、分组曲线、异步录制和车载可读性。

当前项目已经继续演进到 M7-M12，并新增或落地了四 Tab、Device/OTA、离线地图、自动更新、仿真、归一化性能优化等能力。继续把旧 plan 当作当前功能定义，会带来三类问题：

- 入口文档与当前代码不一致，例如 Tab 数量、CSV 支持、字号调节、已知失败测试等。
- “完全 profile 驱动”的边界不够清晰，现有 GPS 名称、trace mode 控制、3D 自动绑定仍有约定式语义。
- 真机/外场/长跑验收项散在多个日志里，没有聚合成下一阶段的路线图。

## 开发目标

1. 新增一份当前上位机功能定义，明确已交付基线、功能边界、已知限制和 vNext 路线。
2. 新增本次文档开发的计划、验收、开发记录，满足后续继续迭代的可追溯要求。
3. 修正明显过期的入口文档，避免新 agent 或新开发者按旧事实工作。
4. 不修改运行逻辑，不扩大到真实功能实现。

## 范围

### 本次包含

- 新增 `doc/upper_pc_function_definition_vnext.md`：
  - 当前产品定位与用户场景
  - Live / Playback / Log / Device 四 Tab 功能定义
  - 跨 Tab 能力：profile、SDB、地图、自动更新、仿真、主题、设置
  - 已知限制与明确不做
  - M13-M16 后续路线
- 新增本次 plan / acceptance / dev log 三份支撑文档。
- 更新 README、AGENTS、CLAUDE、用户手册等入口文档里的明显漂移。
- 在历史优化计划顶部增加当前状态说明，保留原内容作为 M1-M6 追溯。

### 本次不包含

- 不改协议编码、UI、OTA、仿真、地图等运行代码。
- 不新增 pytest 用例；本次是文档基线收敛。
- 不处理未跟踪截图、`.claude/`、`.mimocode/` 等无关工作树内容。
- 不提交 git commit。

## 实施步骤

1. 对照当前代码和既有文档，整理功能事实与漂移点。
2. 写入 vNext 功能定义和验收文档。
3. 更新入口文档，避免继续传播过期命令和过期能力声明。
4. 运行文档一致性检查：
   - `rg` 检查已知失败测试、错误启动命令、根目录 requirements、旧 Tab 数量表述。
   - `git diff --check` 检查 Markdown 空白问题。
5. 在开发记录中写入实际改动和验证结果。

## 验收标准

- vNext 功能定义能覆盖四个 Tab 与跨 Tab 能力，不只复述 M1-M6。
- 明确列出当前限制：SDB v2 only、CSV import 未支持、字体固定 small、仿真 Chart 注入缺口、profile 语义约定缺口。
- 入口文档不再宣称固定已知 pytest 失败。
- 快速开始命令使用当前有效路径和入口。
- 文档检查通过，且未触碰无关脏文件。
