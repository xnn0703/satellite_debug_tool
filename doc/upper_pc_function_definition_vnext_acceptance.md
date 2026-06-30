# 上位机功能定义 vNext 验收标准

## 验收范围

本验收只覆盖文档基线收敛，不覆盖运行逻辑、UI 行为、打包发布或真机联调。

## 验收清单

| 编号 | 项目 | 验收条件 | 状态 | 证据 |
|------|------|----------|------|------|
| D-01 | vNext 功能定义 | `doc/upper_pc_function_definition_vnext.md` 存在，并覆盖 Live / Playback / Log / Device 四 Tab | ✅ | 本次新增 |
| D-02 | 当前事实与历史 plan 分离 | `optimization_plan.md` 顶部说明其为 M1-M6 历史蓝图，并指向 vNext 功能定义 | ✅ | 本次更新 |
| D-03 | 功能边界 | 文档明确 SDB v2 only、CSV import 未实现、字体固定 small、仿真 Chart 注入缺口 | ✅ | vNext 功能定义 §4/§5 |
| D-04 | 后续路线 | 文档给出 M13-M16 路线，并按 profile 语义、仿真、真机验收、数据/发布治理分组 | ✅ | vNext 功能定义 §6 |
| D-05 | README 当前化 | README 的功能、安装、运行、测试命令与当前仓库一致 | ✅ | 本次更新 |
| D-06 | Agent 入口当前化 | AGENTS/CLAUDE 不再写固定 baseline 失败，不再使用旧 Tab 数量描述主架构 | ✅ | 本次更新 |
| D-07 | 用户手册当前化 | 快速开始、ChannelPanel、主题/字号、3D 绑定等不再传播旧行为 | ✅ | 本次更新 |
| D-08 | 文档一致性检查 | README / AGENTS / CLAUDE / 用户手册等入口文档不再命中已修正的旧失败声明和错误启动命令 | ✅ | 见 dev log |
| D-09 | 空白检查 | `git diff --check` 通过 | ✅ | 见 dev log |

## 不验收项

- 不验收真机连接、OTA 实刷、CI 发版、地图瓦片下载、仿真闭环精度。
- 不验收 UI 截图视觉效果。
- 不验收未跟踪设计稿、截图或用户本地配置文件。

## 后续需要单独验收

- M13 profile 语义扩展需要协议和上下位机同步验收。
- M14 仿真 Chart 注入需要 UI 自动化/手动截图验收。
- M15 真机/外场 SOP 需要现场记录、抓包、长跑日志和截图。
- M16 数据互操作与发布治理需要真实 release/update 流程验证。
